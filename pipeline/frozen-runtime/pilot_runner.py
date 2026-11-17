"""Full ABC sweep: isolated family processes, four global solver slots."""
from common import *
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
import importlib.util
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback

spec = importlib.util.spec_from_file_location('abc_native', ROOT / 'matts_official/native_adapter.py')
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)
native.WORK = ROOT / 'work' / RUN.name
native.BUDGET = BUDGET
LOCAL = threading.RLock()

def publish(path, **data):
    with LOCAL:
        old = read(path) if Path(path).exists() else {}
        save(path, {**old, **data, 'updated_at': stamp(), 'pid': os.getpid()})

def verify_frozen():
    protocol = read(HERE / 'protocol.json')
    for relative, value in protocol['implementation_sha256'].items():
        if digest((HERE / relative).read_bytes()) != value:
            raise RuntimeError('Frozen implementation changed: ' + relative)
    for relative, value in protocol['external_sha256'].items():
        if digest((ROOT / relative).read_bytes()) != value:
            raise RuntimeError('Frozen shared dependency changed: ' + relative)
    return protocol

def verify_source(task, protocol):
    expected = protocol['source_fingerprints'][task]
    folder = ROOT / 'source' / task
    actual = {p.relative_to(folder).as_posix(): digest(p.read_bytes()) for p in folder.rglob('*') if p.is_file()}
    if expected != actual:
        raise RuntimeError('Frozen task source changed: ' + task)

def resource_snapshot():
    data = {'free_disk_bytes': shutil.disk_usage(ROOT).free}
    for name in ('memory.current', 'memory.max', 'memory.events'):
        p = Path('/sys/fs/cgroup') / name
        if p.exists():
            data[name] = p.read_text().strip()
    p = Path('/sys/fs/cgroup/memory.stat')
    if p.exists():
        data['anon'] = int(dict(line.split() for line in p.read_text().splitlines()).get('anon', 0))
    return data

def live_solver_processes():
    values = []
    for folder in Path('/proc').glob('[0-9]*'):
        try:
            fields = (folder / 'stat').read_text().split(') ', 1)[1].split()
            args = [a.decode(errors='replace') for a in (folder / 'cmdline').read_bytes().split(b'\0') if a]
            if fields[0] != 'Z' and str(HERE / 'agent.py') in args:
                directory = Path(args[-1])
                if directory.is_relative_to(RUN) and not directory.is_relative_to(RUN / 'engineering'):
                    values.append({'pid': int(folder.name), 'ppid': int(fields[1]), 'start': fields[19],
                                   'directory': str(directory)})
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    return values

def enforce_solver_ownership(active_workers):
    """The coordinator also enforces budgets if a family worker is lost."""
    uptime = float(Path('/proc/uptime').read_text().split()[0])
    for record in live_solver_processes():
        elapsed = uptime - int(record['start']) / os.sysconf('SC_CLK_TCK')
        if record['ppid'] not in active_workers or elapsed > BUDGET['agent_wall_timeout_seconds'] + 10:
            pid = record['pid']
            # Match identity again; never terminate a reused PID or unrelated group.
            same = next((p for p in live_solver_processes() if p['pid'] == pid and p['start'] == record['start']), None)
            try:
                if same and os.getpgid(pid) == pid:
                    followup(f'orphan/{pid}/{record["start"]}', kind='orphan_or_overdue_solver',
                             **record, elapsed_seconds=elapsed)
                    os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

def solver_slot():
    from contextlib import contextmanager
    @contextmanager
    def acquire():
        while True:
            snapshot = resource_snapshot()
            limit = snapshot.get('memory.max', 'max')
            if limit != 'max' and snapshot.get('anon', 0) > int(limit) * .78:
                time.sleep(3)
                continue
            for index in range(4):
                context = lock(RUN / 'locks' / f'solver-{index}.lock', nonblocking=True)
                try:
                    context.__enter__()
                except BlockingIOError:
                    continue
                try:
                    yield index
                finally:
                    context.__exit__(None, None, None)
                return
            time.sleep(1)
    return acquire()

def prompt_patch(root, cwd, skill):
    persona = ('You are a software engineering agent working in a Linux terminal. Complete the current task '
               'by modifying the actual filesystem using bash. Inspect files and run your own checks. '
               'Never seek reference solutions or hidden grading tests. The following is the active Skill '
               'document, also mounted at /opt/agent-skills/terminal-evolution/SKILL.md. '
               'Apply its general procedures subject to the current user task.\n\n' + skill)
    return '\n'.join([
        '- id: terminal-bash', '  config:', '    shellPath: ' + json.dumps(str(PYTHON)),
        '    shellArgs: ' + json.dumps([str(ROOT / 'enter.py'), str(root), cwd, '/bin/bash', '--noprofile', '--norc', '-i']),
        '    timeoutMs: 300000', '- id: system-prompt', '  config:', '    includeHarnessIdentity: false',
        '    includeRuntimeContext: false', '    personaPrefix: ' + json.dumps(persona),
        '- id: llm-deepseek', '  config:', '    streamIdleTimeoutMs: 180000', ''])

def run_attempt(group, family, version, rep, template, cwd, skill, state, baseline=None):
    task = f'{family}-EVO-{version}'
    directory = RUN / group / task / f'rep-{rep}'
    result_path = directory / 'result.json'
    if result_path.exists():
        result = read(result_path)
        if result.get('skill_sha256') != digest(skill):
            raise RuntimeError('Saved result used a different Skill')
        return result
    if (directory / 'launch.json').exists():
        followup(f'{group}/{task}/{rep}/partial', kind='interrupted_attempt', directory=str(directory))
        return {'interrupted': True, 'error': 'Partial prior attempt retained; no rerun during sweep'}
    directory.mkdir(parents=True, exist_ok=True)
    root = native.WORK / f'{group}-{task}-rep-{rep}'
    phase = 'setup'
    with solver_slot() as slot:
        save(directory / 'resource-before.json', resource_snapshot())
        publish(state, phase='four_candidates', active_task=task, active_group=group)
        with lock(RUN / 'locks/filesystem.lock'):
            native.clone(template, root)
        target = root / 'opt/agent-skills/terminal-evolution/SKILL.md'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(skill, encoding='utf-8')
        target.chmod(0o444)
        immutable(directory / 'skill-before.md', skill)
        immutable(directory / 'prompt.txt', (Path(__file__).resolve().parent / 'source' / task / 'instruction.md').read_text())
        immutable(directory / 'native.patch.yml', prompt_patch(root, cwd, skill))
        save(directory / 'input.json', {'group': group, 'task_id': task, 'repetition': rep, 'cwd': cwd,
            'budget': BUDGET, 'skill_sha256': digest(skill), 'memory_injection': False,
            'fresh_environment': True, 'slot': slot})
        save(directory / 'launch.json', {'at': stamp(), 'slot': slot, 'state': 'launching'})
        try:
            phase = 'solver_runtime'
            with (directory / 'process.log').open('ab', buffering=0) as log:
                proc = subprocess.Popen([PYTHON, HERE / 'agent.py', directory], stdout=log, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, start_new_session=True)
                fields = (Path('/proc') / str(proc.pid) / 'stat').read_text().split(') ', 1)[1].split()
                started_monotonic = time.monotonic()
                launch = {'at': stamp(), 'slot': slot, 'pid': proc.pid, 'starttime': fields[19], 'state': 'running', 'budget_seconds': BUDGET['agent_wall_timeout_seconds']}
                save(directory / 'launch.json', launch)
                save(directory / 'solver-start.json', launch)
                timed_out = False
                try:
                    code = proc.wait(timeout=BUDGET['agent_wall_timeout_seconds'])
                except subprocess.TimeoutExpired:
                    timed_out = True
                    save(directory / 'timeout-action.json', {'at': stamp(), 'pid': proc.pid, 'starttime': launch['starttime'], 'elapsed_seconds': time.monotonic()-started_monotonic, 'reason': 'wall_budget_expired', 'signal': 'SIGKILL'})
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                    code = 124
            save(directory / 'solver-end.json', {'at': stamp(), 'pid': proc.pid, 'starttime': launch['starttime'], 'process_exit_code': code, 'wall_timeout_confirmed': timed_out, 'elapsed_seconds': time.monotonic()-started_monotonic, 'resources': resource_snapshot()})
            phase = 'archive'
            outcome = read(directory / 'agent.json') if (directory / 'agent.json').exists() else {'error': f'agent process exited {code}'}
            # Retain exact submitted workspace before hidden evaluation. Global I/O
            # serialization limits concurrent tar/preparation memory and disk bursts.
            with lock(RUN / 'locks/filesystem.lock'):
                from snapshot import archive, inventory
                try:
                    archive(root, baseline if baseline is not None else inventory(template), directory)
                except Exception as exc:
                    save(directory / 'archive-error.json', {'at': stamp(), 'phase': 'archive', 'error': str(exc), 'errno': getattr(exc, 'errno', None)})
                    followup(f'{group}/{task}/{rep}/archive', kind='archive_failure', error=str(exc))
                phase = 'grading'
                save(directory / 'grading-start.json', {'at': stamp()})
                try:
                    evaluation = native.verify(task, root, cwd, f'rep-{rep}')
                except Exception as exc:
                    save(directory / 'grading-error.json', {'at': stamp(), 'phase': phase, 'error': str(exc), 'errno': getattr(exc, 'errno', None)})
                    evaluation = {'status': 'error', 'error': str(exc)}
                save(directory / 'grading-end.json', {'at': stamp(), 'evaluation': evaluation})
            outcome.update(task_id=task, repetition=rep, group=group, process_exit_code=code,
                           evaluation=evaluation, skill_sha256=digest(skill), finished_at=stamp(), budget=BUDGET)
            if not strict_pass(outcome):
                followup(f'{group}/{task}/{rep}/solver', kind='solver_result', group=group, task=task, rep=rep,
                         error=outcome.get('error'), finish_reason=outcome.get('finish_reason'),
                         process_exit_code=code, evaluation=evaluation)
            save(result_path, outcome)
            return outcome
        except Exception as exc:
            save(directory / 'infrastructure-error.json', {'at': stamp(), 'phase': phase, 'error': str(exc), 'errno': getattr(exc, 'errno', None), 'resources': resource_snapshot()})
            followup(f'{group}/{task}/{rep}/infrastructure', kind='attempt_infrastructure',
                     group=group, task=task, rep=rep, error=str(exc), directory=str(directory))
            raise
        finally:
            native.delete_work(root)

def commit_stage(group, family, version, before, after, decision, inherited_taints):
    stage = RUN / 'stages' / group / f'{family}-EVO-{version}'
    immutable(stage / 'skill-before.md', before)
    immutable(stage / 'skill-after.md', after)
    taints = list(inherited_taints)
    if decision.get('error') or decision.get('partial_reflection') or (group != 'A' and decision.get('empty_trajectories')):
        taints.append(f'{group}/{family}/EVO-{version}:update_or_empty_evidence')
    save(stage / 'committed.json', {'at': stamp(), 'group': group, 'family': family, 'version': version,
        'skill_before_sha256': digest(before), 'skill_after_sha256': digest(after),
        'taints_for_next_stage': taints, 'update': decision})
    return taints

def run_family(family):
    protocol = verify_frozen()
    state = RUN / 'families' / family / 'status.json'
    with lock(RUN / 'locks' / f'family-{family}.lock', nonblocking=True):
        import optimizer
        initial = (HERE / 'S0.md').read_text()
        skills = {g: initial for g in GROUPS}
        taints = {g: [] for g in GROUPS}
        family_index = protocol['families'].index(family)
        for version in range(1, 5):
            task = f'{family}-EVO-{version}'
            # Recover complete commits first. Never derive a Skill from future stages.
            todo = []
            for group in GROUPS:
                stage = RUN / 'stages' / group / task
                if (stage / 'committed.json').exists():
                    record = read(stage / 'committed.json')
                    if record['skill_before_sha256'] != digest(skills[group]):
                        raise RuntimeError('Predecessor Skill mismatch')
                    skills[group] = (stage / 'skill-after.md').read_text()
                    taints[group] = record['taints_for_next_stage']
                else:
                    todo.append(group)
            if not todo:
                continue
            template = native.WORK / f'{task}-template'
            native.RUNS = RUN / 'environment'
            try:
                publish(state, phase='preparing', active_task=task, active_version=version, active_group=None)
                with lock(RUN / 'locks/filesystem.lock'):
                    if shutil.disk_usage(ROOT).free < 8 * 1024**3:
                        raise RuntimeError('Less than 8 GiB free; retain pending attempts for later')
                    verify_source(task, protocol)
                    info = native.prepare(task, template)
                    from snapshot import inventory
                    baseline = inventory(template)
                    save(RUN / 'environment' / task / 'baseline-manifest.json', baseline)
                order = list(GROUPS)
                rotate = (family_index + version - 1) % 3
                order = order[rotate:] + order[:rotate]
                if version == 1:
                    order = list(GROUPS)  # A executes; B/C share exact same S0 evidence.
                for group in order:
                    if group not in todo:
                        continue
                    native.RUNS = RUN / group
                    stage = RUN / 'stages' / group / task
                    before = skills[group]
                    immutable(stage / 'skill-before.md', before)
                    save(stage / 'stage-input.json', {'group': group, 'task': task, 'version': version,
                        'skill_sha256': digest(before), 'inherited_taints': taints[group], 'group_order': order,
                        'input_quality_valid': not taints[group]})
                    if version == 1 and group != 'A':
                        for rep in range(1, 5):
                            source = RUN / 'A' / task / f'rep-{rep}'
                            directory = RUN / group / task / f'rep-{rep}'
                            if (directory / 'result.json').exists():
                                continue
                            result = read(source / 'result.json')
                            loaded = read(source / 'skill-loaded.json')
                            configured = read(source / 'input.json')
                            if (result.get('skill_sha256') != digest(before) or loaded['skill_sha256'] != digest(before)
                                or configured['budget'] != BUDGET or configured['task_id'] != task
                                or configured['repetition'] != rep
                                or digest((source / 'prompt.txt').read_text()) != digest((ROOT / 'source' / task / 'instruction.md').read_text())):
                                raise RuntimeError('Shared EVO1 Skill was not actually loaded')
                            save(directory / 'shared-evo1.json', {'source_directory': str(source),
                                'source_result_sha256': digest((source / 'result.json').read_bytes()),
                                'skill_sha256': digest(before), 'reason': 'identical S0, task, model and budget; paired shared start'})
                            save(directory / 'result.json', {**result, 'group': group, 'shared_evo1': True})
                    else:
                        errors = []
                        with ThreadPoolExecutor(max_workers=4) as pool:
                            fs = [pool.submit(run_attempt, group, family, version, rep, template, info['cwd'], before, state, baseline)
                                  for rep in range(1, 5)]
                            for f in as_completed(fs):
                                try:
                                    f.result()
                                except Exception as exc:
                                    errors.append(str(exc))
                        if errors:
                            followup(f'{group}/{task}/stage-attempt-errors', kind='stage_attempt_errors', errors=errors)
                    directories = [RUN / group / task / f'rep-{rep}' for rep in range(1, 5)]
                    publish(state, phase='skill_update', active_group=group, active_task=task)
                    after, decision = optimizer.update(group, family, version, before, directories, stage)
                    skills[group] = after
                    taints[group] = commit_stage(group, family, version, before, after, decision, taints[group])
            except Exception as exc:
                followup(f'{family}/{version}/environment-or-stage', kind='stage_infrastructure', family=family,
                         version=version, error=str(exc), traceback=traceback.format_exc())
                # Complete the traversal while retaining explicit missing runs.
                for group in todo:
                    stage = RUN / 'stages' / group / task
                    if (stage / 'committed.json').exists():
                        continue
                    decision = {'status': 'stage_failed_previous_skill_carried_forward', 'error': str(exc), 'accepted': False}
                    taints[group] = commit_stage(group, family, version, skills[group], skills[group], decision, taints[group])
            finally:
                native.delete_work(template)
        publish(state, phase='family_sweep_finished', active_group=None, taints=taints)

def summary():
    groups = {g: {'planned': 384, 'completed': 0, 'strict_passed': 0, 'shared_evo1': 0,
                  'timeouts': 0, 'update_accepted': 0, 'update_failed': 0, 'tainted_inputs': 0} for g in GROUPS}
    for group, data in groups.items():
        for p in (RUN / group).glob('*/rep-*/result.json'):
            r = read(p)
            data['completed'] += 1
            data['strict_passed'] += int(strict_pass(r))
            data['shared_evo1'] += int(r.get('shared_evo1', False))
            data['timeouts'] += int(r.get('process_exit_code') == 124)
        for p in (RUN / 'stages' / group).glob('*/committed.json'):
            d = read(p)['update']
            data['update_accepted'] += int(d.get('accepted', False))
            data['update_failed'] += int(bool(d.get('error')))
        for p in (RUN / 'stages' / group).glob('*/stage-input.json'):
            data['tainted_inputs'] += int(not read(p)['input_quality_valid'])
    value = {'at': stamp(), 'planned_logical_results': 1152, 'planned_actual_solver_runs': 960,
        'logical_completed': sum(g['completed'] for g in groups.values()),
        'actual_completed': sum(g['completed'] - g['shared_evo1'] for g in groups.values()),
        'groups': groups, 'followup_records': len(list((RUN / 'followups').glob('*.json'))),
        'resources': resource_snapshot(), 'performance_gate': False}
    save(RUN / 'summary.json', value)
    return value

def coordinator():
    protocol = verify_frozen()
    with lock(RUN / 'controller.lock', nonblocking=True):
        pid_path = RUN / 'controller.pid'
        pid_path.write_text(str(os.getpid()))
        active = {}
        failures = []
        pending = [f for f in protocol['families'] if not (RUN / 'families' / f / 'status.json').exists()
                   or read(RUN / 'families' / f / 'status.json').get('phase') != 'family_sweep_finished']
        while pending or active:
            for family, proc in list(active.items()):
                rc = proc.poll()
                if rc is None:
                    continue
                active.pop(family)
                if rc:
                    record = {'family': family, 'exit_code': rc}
                    failures.append(record)
                    followup(f'{family}/worker', kind='worker_exit', **record)
            enforce_solver_ownership({p.pid for p in active.values()})
            resources = resource_snapshot()
            allow = resources.get('anon', 0) < .68 * int(resources.get('memory.max', 2**63))
            if pending and len(active) < 2 and allow:
                family = pending.pop(0)
                folder = RUN / 'families' / family
                folder.mkdir(parents=True, exist_ok=True)
                with (folder / 'worker.log').open('ab', buffering=0) as log:
                    active[family] = subprocess.Popen([PYTHON, '-u', HERE / 'runner.py', 'worker', '--family', family],
                        cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            save(RUN / 'status.json', {'phase': 'full_sweep', 'at': stamp(), 'pid': os.getpid(),
                'active_families': {f: p.pid for f, p in active.items()}, 'pending_families': pending,
                'family_concurrency': 2, 'global_solver_concurrency': 4, 'worker_failures': failures,
                'resources': resources})
            summary()
            time.sleep(5)
        value = summary()
        save(RUN / 'status.json', {'phase': 'sweep_finished_with_followups' if value['followup_records'] else 'completed',
            'at': stamp(), 'pid': os.getpid(), 'active_families': {}, 'pending_families': [], 'summary': value})

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['run', 'worker', 'status'])
    parser.add_argument('--family')
    args = parser.parse_args()
    RUN.mkdir(parents=True, exist_ok=True)
    native.WORK.mkdir(parents=True, exist_ok=True)
    if args.mode == 'run':
        coordinator()
    elif args.mode == 'worker':
        run_family(args.family)
    else:
        print(json.dumps(summary()))

if __name__ == '__main__':
    main()
