"""Unique-question 2:1 Train/Test B/C 2/4/6 Skill experiment."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import inspect
import json
import os
import random
import shutil
import subprocess
import sys
import tarfile
import traceback

ROOT = Path(__file__).resolve().parent.parent.parent
JOB = Path(__file__).resolve().parent
sys.path.insert(1, str(ROOT / 'skill_abc'))
sys.path.insert(2, str(ROOT))
from diagnosis import read, save, digest
from optimizer import diagnose_if_eligible, update
from common import lock, stamp
import run_pilot as runtime


def validate():
    p = read(JOB / 'expanded-plan.json')
    assert p['groups'] == ['B2', 'B4', 'B6', 'C2', 'C4', 'C6']
    assert len(p['train_chains']) == 6 and len(p['test_chains']) == 3
    assert set(p['train_chains']).isdisjoint(p['test_chains'])
    assert p['independent_train_chains'] == 6 and p['independent_test_chains'] == 3
    assert p['independent_train_instances'] == 24 and p['independent_test_instances'] == 12
    assert p['planned_train_solver_runs'] == 144
    assert p['planned_test_solver_runs'] == 72
    assert p['planned_baseline_solver_runs'] == 12
    assert p['planned_total_solver_runs'] == 228
    if digest((JOB / 'reuse-audit.json').read_bytes()) != p['reuse_audit_sha256']:
        raise RuntimeError('Reuse audit changed')
    if read(JOB / 'reuse-audit.json').get('all_36_exact_and_admitted') is not True:
        raise RuntimeError('Reuse audit did not pass')
    assert len(p['blocks']) == 4
    for block in p['blocks']:
        assert len(block['tasks']) == 6 and len(set(block['tasks'])) == 6
        assert set(block['selected']['2']).issubset(block['selected']['4'])
        assert set(block['selected']['4']).issubset(block['selected']['6'])
        assert [len(block['selected'][k]) for k in ('2', '4', '6')] == [2, 4, 6]
    for relative, expected in p['code_sha256'].items():
        if digest((JOB / relative).read_bytes()) != expected:
            raise RuntimeError('Frozen code changed: ' + relative)
    for relative, expected in p['source_sha256'].items():
        if digest((JOB / 'source' / relative).read_bytes()) != expected:
            raise RuntimeError('Frozen source changed: ' + relative)
    for relative, expected in p['shared_sha256'].items():
        if digest((ROOT / relative).read_bytes()) != expected:
            raise RuntimeError('Shared runtime changed: ' + relative)
    profile = p['profile']
    base = Path(profile['root'])
    fingerprint = read(JOB / profile['fingerprint_file'])
    for relative, value in fingerprint['configuration_identity_sample'].items():
        path = base / relative
        actual = ({'symlink': os.readlink(path)} if path.is_symlink() else
                  {'sha256': digest(path.read_bytes()), 'bytes': path.stat().st_size}
                  if path.exists() else {'absent': True})
        if actual != value:
            raise RuntimeError('Profile changed: ' + str(path))
    for relative, expected in fingerprint['installed_python_package_metadata'].items():
        if digest((base / relative).read_bytes()) != expected:
            raise RuntimeError('Profile package changed: ' + relative)
    return p


def prepare_tasks(runner, p, tasks, run_controls):
    import snapshot
    spec = importlib.util.spec_from_file_location('public_inventory', JOB / 'screen_worker.py')
    screen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(screen)
    templates = {}
    runner.native.RUNS = JOB / 'controls'
    runner.native.ENTER = JOB / 'control_entry.py'
    for task in tasks:
        if shutil.disk_usage(ROOT).free < 10 * 1024**3:
            raise RuntimeError('Disk reserve insufficient during admission')
        meta = p['tasks'][task]
        folder = JOB / 'admitted-baselines' / task
        archive = folder / 'public-baseline.tar.gz'
        if digest(archive.read_bytes()) != meta['public_baseline_sha256']:
            raise RuntimeError('Archive changed: ' + task)
        template = runner.native.WORK / (task + '-template')
        runner.native.clone(Path(p['profile']['root']), template)
        for hidden in ('tests', 'solution'):
            path = template / hidden
            if path.exists():
                if any(path.iterdir()):
                    raise RuntimeError('Hidden payload in profile')
                path.rmdir()
        with tarfile.open(archive) as file:
            for member in file:
                if member.name.startswith('/') or '..' in Path(member.name).parts:
                    raise RuntimeError('Unsafe public baseline member')
        subprocess.run(['tar', '-xzf', str(archive), '-C', str(template)], check=True)
        if screen.inventory(template) != read(folder / 'baseline-manifest.json'):
            raise RuntimeError('Original public initialization mismatch: ' + task)
        baseline = snapshot.inventory(template)
        save(JOB / 'environment' / task / 'baseline-manifest.json', baseline)
        cwd = meta['cwd']
        probe = ('import socket,errno\ntry:socket.socket();raise AssertionError("network enabled")\n'
                 'except OSError as e:assert e.errno==errno.EPERM;print("OFFLINE_OK")')
        result = subprocess.run([str(ROOT / 'venv/bin/python'), str(JOB / 'enter-expanded.py'),
            '--offline-agent', str(template), cwd, '/usr/bin/python3', '-c', probe],
            capture_output=True, text=True, timeout=30)
        save(JOB / 'environment' / task / 'network.json', {
            'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
        if result.returncode or 'OFFLINE_OK' not in result.stdout:
            raise RuntimeError('Offline probe failed: ' + task)
        if run_controls:
            controls = {}
            for oracle in (False, True):
                label = 'positive' if oracle else 'negative'
                control = runner.native.WORK / (task + '-' + label)
                runner.native.clone(template, control)
                try:
                    controls[label] = runner.native.verify(task, control, cwd, label, oracle=oracle)
                    save(JOB / 'environment' / task / (label + '.json'), controls[label])
                    expected = '1' if oracle else '0'
                    if controls[label].get('reward') != expected:
                        raise RuntimeError('Environment control failed: ' + task + '/' + label)
                finally:
                    runner.native.delete_work(control)
            save(JOB / 'environment' / task / 'environment.json', {
                'status': 'valid', 'task': task, 'controls': controls,
                'same_initial_state_all_conditions': True})
            runner.native.delete_work(template)
        else:
            templates[task] = (template, cwd, baseline)
    return templates


def delete_templates(runner, templates):
    """Delete only exact materialized templates under their verified parent."""
    previous = runner.native.WORK
    try:
        for template, _, _ in templates.values():
            template = Path(template).resolve()
            parent = template.parent.resolve()
            expected_root = (ROOT / 'work' / JOB.name).resolve()
            if not parent.is_relative_to(expected_root) or template == parent:
                raise RuntimeError('Refusing unexpected template cleanup path: ' + str(template))
            runner.native.WORK = parent
            if template.exists():
                runner.native.delete_work(template)
    finally:
        runner.native.WORK = previous


def select_template_workspace(runner):
    """Set scheduling state without changing the admitted materializer."""
    runner.native.WORK = ROOT / 'work' / JOB.name / 'templates'
    runner.native.WORK.mkdir(parents=True, exist_ok=True)


def admitted_controls(task):
    folder = JOB / 'environment' / task
    try:
        environment = read(folder / 'environment.json')
        negative = read(folder / 'negative.json')
        positive = read(folder / 'positive.json')
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    return (environment.get('status') == 'valid' and negative.get('reward') == '0' and
            positive.get('reward') == '1')


def environment_identity(p):
    value = {
        'materialization_function': inspect.getsource(prepare_tasks),
        'tasks': p['tasks'], 'profile': p['profile'],
        'source_sha256': p['source_sha256'],
        'shared_sha256': p['shared_sha256'],
        'code': {name: p['code_sha256'][name] for name in
                 ('control_entry.py', 'enter-expanded.py', 'run_pilot.py', 'screen_worker.py')},
    }
    return digest(json.dumps(value, sort_keys=True, separators=(',', ':')))


def attempt_one(runner, p, template_info, phase_root, group, task, skills, state):
    template, cwd, baseline = template_info
    runner.RUN = phase_root
    runner.native.RUNS = phase_root / group
    runner.native.WORK = ROOT / 'work' / JOB.name / phase_root.name
    runner.native.WORK.mkdir(parents=True, exist_ok=True)
    runner.active_skills = dict(skills)
    combined = skills['format'] + '\n\n' + skills['task']
    directory = phase_root / group / task / 'rep-1'
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in skills.items():
        (directory / f'{name.upper()}_SKILL.md').write_text(text, encoding='utf-8')
    family, version = task.rsplit('-EVO-', 1)
    result = runner.run_attempt(group, family, int(version), 1, template, cwd,
                                combined, state, baseline)
    runtime.count_skill_usage(directory)
    complete = (result.get('finish_reason') == 'completed' and not result.get('error') and
                result.get('process_exit_code') == 0)
    evaluation = result.get('evaluation', {})
    cause = ('none' if complete and evaluation.get('reward') == '1' else
             'answer_failed_valid_tests' if complete and evaluation.get('reward') == '0' else
             'execution_or_scoring_failure')
    save(directory / 'operator-evaluation.json', {
        'execution_complete': complete, 'environment_valid': True,
        'evaluation': evaluation, 'termination_reason': cause,
        'operator_only': True, 'reflection_input': False})
    return directory


def diagnose_directory(directory, task, destination):
    prompt = (JOB / 'source' / task / 'instruction.md').read_text(encoding='utf-8')
    destination = Path(destination)
    if (destination / 'diagnosis.json').exists():
        item = {
            'public_task': prompt,
            'trajectory': read(destination / 'input-trace.json'),
            'diagnosis': read(destination / 'diagnosis.json'),
            'task_id': task,
        }
        return item
    item = diagnose_if_eligible(directory, prompt, destination)
    item['task_id'] = task
    return item


def summarize(p):
    rows = []
    for phase in ('train', 'test'):
        for path in sorted((JOB / phase).glob('block-*/*/*/rep-1/operator-evaluation.json')):
            value = read(path)
            parts = path.relative_to(JOB).parts
            rows.append({'phase': parts[0], 'block': parts[1], 'group': parts[2],
                'task': parts[3], 'reward': value.get('evaluation', {}).get('reward'),
                'qualified': value.get('execution_complete') and value.get('environment_valid'),
                'cause': value.get('termination_reason'), 'directory': str(path.parent)})
    save(JOB / 'summary.json', {
        'at': stamp(), 'planned_solver_runs': p['planned_total_solver_runs'],
        'finished_attempts': len(rows), 'rows': rows,
        'independent_train_chains': 6, 'independent_test_chains': 3,
        'independent_train_instances': 24, 'independent_test_instances': 12,
        'within_condition_repetitions': 1})


def run_train(runner, p):
    skills = {group: dict(read(JOB / 'initial-skills.json')) for group in p['groups']}
    taints = {group: [] for group in p['groups']}
    for block in p['blocks']:
        block_name = f"block-{block['index']}"
        phase_root = JOB / 'train' / block_name
        todo = []
        for group in p['groups']:
            committed_path = phase_root / group / 'committed.json'
            if not committed_path.exists():
                todo.append(group)
                continue
            committed = read(committed_path)
            before_hashes = {name: digest(text) for name, text in skills[group].items()}
            if committed.get('skill_before_hashes') != before_hashes:
                raise RuntimeError(f'Resume Skill predecessor mismatch: {block_name}/{group}')
            after = read(phase_root / group / 'optimizer' / 'skill-after.json')
            if committed.get('skill_after_hashes') != {name: digest(text) for name, text in after.items()}:
                raise RuntimeError(f'Resume Skill commit mismatch: {block_name}/{group}')
            skills[group] = after
            taints[group] = committed.get('taints', [])
        if not todo:
            continue
        select_template_workspace(runner)
        templates = prepare_tasks(runner, p, block['tasks'], run_controls=False)
        try:
            for group in todo:
                frozen = dict(skills[group])
                save(JOB / 'status.json', {'phase': 'train', 'block': block['index'],
                    'group': group, 'at': stamp()})
                state = JOB / 'train' / block_name / group / 'status.json'
                def solve(task):
                    return task, attempt_one(runner, p, templates[task], phase_root,
                                             group, task, frozen, state)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    solved = dict(pool.map(solve, block['tasks']))
                def diagnose_task(task):
                    return task, diagnose_directory(solved[task], task,
                        JOB / 'train' / block_name / group / 'diagnoses' / task)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    diagnosed = dict(pool.map(diagnose_task, block['tasks']))
                selected_tasks = block['selected'][group[1:]]
                inputs = [diagnosed[task] for task in selected_tasks]
                before_hashes = {name: digest(text) for name, text in frozen.items()}
                skills[group], decision = update(frozen, inputs, group,
                    JOB / 'train' / block_name / group / 'optimizer')
                if decision.get('error'):
                    taints[group].append({'block': block['index'], 'error': decision['error']})
                save(JOB / 'train' / block_name / group / 'committed.json', {
                    'at': stamp(), 'group': group, 'block': block['index'],
                    'all_six_solver_tasks': block['tasks'], 'selected_tasks': selected_tasks,
                    'skill_before_hashes': before_hashes,
                    'skill_after_hashes': {name: digest(text) for name, text in skills[group].items()},
                    'decision': decision, 'taints': taints[group]})
                summarize(p)
        finally:
            delete_templates(runner, templates)
    for group, value in skills.items():
        out = JOB / 'skills' / group
        out.mkdir(parents=True, exist_ok=True)
        for name, text in value.items():
            (out / f'{name.upper()}_SKILL.md').write_text(text, encoding='utf-8')
        save(out / 'frozen.json', {'at': stamp(), 'taints': taints[group],
            'skill_hashes': {name: digest(text) for name, text in value.items()}})


def run_test(runner, p):
    frozen = {group: {name: (JOB / 'skills' / group / f'{name.upper()}_SKILL.md').read_text(encoding='utf-8')
                      for name in ('format', 'task')} for group in p['groups']}
    frozen['S0'] = read(JOB / 'initial-skills.json')
    for version in range(1, 5):
        tasks = [f'{family}-EVO-{version}' for family in p['test_chains']]
        block_name = f'block-{version}'
        phase_root = JOB / 'test' / block_name
        select_template_workspace(runner)
        templates = prepare_tasks(runner, p, tasks, run_controls=False)
        try:
            groups = list(frozen)
            random.Random(p['seed'] + version).shuffle(groups)
            for task in tasks:
                for group in groups:
                    committed_path = phase_root / group / task / 'committed.json'
                    if committed_path.exists():
                        committed = read(committed_path)
                        expected = {name: digest(text) for name, text in frozen[group].items()}
                        if committed.get('skill_hashes') != expected:
                            raise RuntimeError('Frozen Test Skill mismatch: ' + str(committed_path))
                        continue
                    save(JOB / 'status.json', {'phase': 'test', 'block': version,
                        'group': group, 'task': task, 'at': stamp()})
                    directory = attempt_one(runner, p, templates[task], phase_root,
                                            group, task, frozen[group],
                                            phase_root / group / 'status.json')
                    diagnose_directory(directory, task,
                        phase_root / group / 'diagnoses' / task)
                    save(phase_root / group / task / 'committed.json', {
                        'at': stamp(), 'status': 'frozen_test_no_update',
                        'skill_hashes': {name: digest(text) for name, text in frozen[group].items()}})
                    summarize(p)
        finally:
            delete_templates(runner, templates)


def main():
    p = validate()
    if '--validate' in sys.argv:
        print(json.dumps({'valid': True, 'planned_solver_runs': 228, 'model_calls': 0}))
        return
    if '--controls-only' in sys.argv:
        with lock(JOB / 'controls-worker.lock', nonblocking=True):
            runner = runtime.configure_runner()
            reused = [task for task in p['tasks'] if admitted_controls(task)] if '--resume-controls' in sys.argv else []
            selected = [task for task in p['tasks'] if task not in reused]
            prepare_tasks(runner, p, selected, run_controls=True)
            missing = [task for task in p['tasks'] if not admitted_controls(task)]
            if missing:
                raise RuntimeError('Incomplete controls: ' + ','.join(missing))
            save(JOB / 'environment-preflight.json', {
                'passed': True, 'tasks': len(p['tasks']), 'at': stamp(),
                'environment_identity': environment_identity(p),
                'reused_control_tasks': reused, 'new_control_tasks': selected,
                'solver_calls_started': 0})
            save(JOB / 'status.json', {'phase': 'environment_preflight_complete',
                'tasks': len(p['tasks']), 'at': stamp()})
            print(json.dumps({'environment_preflight': True, 'tasks': len(p['tasks']),
                              'reused': len(reused), 'new': len(selected), 'model_calls': 0}))
        return
    preflight = read(JOB / 'environment-preflight.json')
    if (preflight.get('passed') is not True or preflight.get('tasks') != len(p['tasks']) or
            preflight.get('environment_identity') != environment_identity(p)):
        raise RuntimeError('A current successful controls-only preflight is required')
    with lock(JOB / 'worker.lock', nonblocking=True):
        resume = '--resume' in sys.argv
        if resume:
            if not (JOB / 'started.json').exists() or (JOB / 'completion.json').exists():
                raise RuntimeError('Resume requires an incomplete prior formal run')
            prior_pid = read(JOB / 'started.json').get('pid')
            if prior_pid and Path('/proc') .joinpath(str(prior_pid)).exists():
                raise RuntimeError('Prior formal process is still alive')
            save(JOB / 'resume.json', {'at': stamp(), 'pid': os.getpid(),
                'policy': 'reuse only committed blocks and exact saved attempts'})
        else:
            if (JOB / 'started.json').exists():
                raise RuntimeError('Prior run exists; explicit --resume required')
            save(JOB / 'started.json', {'at': stamp(), 'pid': os.getpid()})
        runner = runtime.configure_runner()
        try:
            run_train(runner, p)
            run_test(runner, p)
            save(JOB / 'completion.json', {
                'at': stamp(), 'status': 'train_test_complete_pending_posthoc_EVL_and_human_review',
                'posthoc_step_evl_complete': False, 'human_evidence_review_complete': False})
            save(JOB / 'status.json', {'phase': 'train_test_complete_pending_posthoc_EVL_and_human_review',
                                       'at': stamp()})
        except Exception as exc:
            save(JOB / 'error.json', {'at': stamp(), 'error': str(exc),
                'traceback': traceback.format_exc(), 'automatic_retry': False})
            save(JOB / 'status.json', {'phase': 'blocked_or_failed', 'at': stamp(), 'error': str(exc)})
            raise


if __name__ == '__main__':
    main()
