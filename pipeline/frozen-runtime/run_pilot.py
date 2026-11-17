"""First debugging category: fresh six-trace fixed-input B/C 2/4/6 pilot.

This launch is deliberately the first fixed-input stage, not a full EVO or Test
result. All six conditions start from identical two-document Skills.
"""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import argparse
import importlib.util
import json
import os
import random
import shutil
import subprocess
import sys
import tarfile
import time
import traceback

ROOT = Path(__file__).resolve().parent.parent.parent
JOB = Path(__file__).resolve().parent
REFERENCE = ROOT / 'runs-evo-expanded-debugging-20261019'
sys.path.insert(1, str(ROOT / 'skill_abc'))
sys.path.insert(2, str(ROOT))
from diagnosis import read, save, digest
from optimizer import diagnose, update
from common import lock, stamp

TASK = 'sqlite-db-truncate-EVO-1'
GROUPS = ['B2', 'B4', 'B6', 'C2', 'C4', 'C6']


def identity(pid):
    stat = (Path('/proc') / str(pid) / 'stat').read_text().split(') ',1)[1].split()
    return {'pid': pid, 'starttime': stat[19]}


def configure_runner():
    spec = importlib.util.spec_from_file_location('pilot_runner', JOB / 'pilot_runner.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    runner.HERE = JOB
    runner.RUN = JOB / 'pool'
    runner.BUDGET = read(JOB / 'plan.json')['solver_budget']
    runner.native.BUDGET = runner.BUDGET
    runner.native.SOURCE = JOB / 'source'
    runner.native.ENTER = JOB / 'enter-expanded.py'
    runner.native.WORK = ROOT / 'work' / JOB.name
    runner.native.WORK.mkdir(exist_ok=True)
    import snapshot
    snapshot.TOPS = ('app','workspace','data','build','opt/task')
    import common
    common.RUN = JOB
    runner.active_skills = read(JOB/'initial-skills.json')
    @contextmanager
    def routed_lock(path, nonblocking=False):
        target = ROOT/'shared-runtime/locks/filesystem.lock' if Path(path).name == 'filesystem.lock' else path
        with lock(target, nonblocking):
            yield
    runner.lock = routed_lock

    def cow(source, target):
        target = Path(target)
        if target.exists():
            raise RuntimeError('Existing work path; inspect before reuse')
        target.mkdir(parents=True)
        subprocess.run(['cp','-a','--reflink=always',str(source)+'/.',str(target)],check=True)
    runner.native.clone = cow

    @contextmanager
    def solver_slot():
        while True:
            if shutil.disk_usage(ROOT).free < 8 * 1024**3:
                raise RuntimeError('Insufficient disk reserve')
            obtained = None
            for i in range(8):
                ctx = lock(ROOT / 'shared-runtime/locks' / f'solver-{i}.lock', nonblocking=True)
                try:
                    ctx.__enter__()
                except BlockingIOError:
                    continue
                obtained = ctx
                break
            if obtained:
                break
            time.sleep(.5)
        try:
            yield i
        finally:
            obtained.__exit__(None,None,None)
    runner.solver_slot = solver_slot

    def prompt_patch(root, cwd, combined):
        # Two distinct mounted files; the prompt contains their exact full text.
        (root/'opt/agent-skills/terminal-evolution/SKILL.md').unlink(missing_ok=True)
        for name,text in runner.active_skills.items():
            path = root / f'opt/agent-skills/{name}/SKILL.md'
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(text)
            path.chmod(0o444)
        persona = ('Complete the public terminal task using bash. Never seek benchmark reference solutions '
                   'or hidden tests. Follow the current task over generic guidance. The following TWO '
                   'Skill documents are loaded: FORMAT_SKILL at /opt/agent-skills/format/SKILL.md and '
                   'TASK_SKILL at /opt/agent-skills/task/SKILL.md. Use format rules for submission '
                   'contracts and task rules for domain strategy.\n\n' + combined)
        return json.dumps([
            {'id':'terminal-bash','config':{'shellPath':str(ROOT/'venv/bin/python'),
             'shellArgs':[str(JOB/'enter-expanded.py'),'--offline-agent',str(root),cwd,
                          '/bin/bash','--noprofile','--norc','-i'],'timeoutMs':300000}},
            {'id':'system-prompt','config':{'includeHarnessIdentity':False,
             'includeRuntimeContext':False,'personaPrefix':persona}},
        ])
    runner.prompt_patch = prompt_patch
    return runner


def validate_frozen():
    plan = read(JOB / 'plan.json')
    for relative,expected in plan['code_sha256'].items():
        if digest((JOB / relative).read_bytes()) != expected:
            raise RuntimeError('Frozen code changed: ' + relative)
    for relative,expected in plan['shared_sha256'].items():
        if digest((ROOT / relative).read_bytes()) != expected:
            raise RuntimeError('Frozen shared code changed: ' + relative)
    for relative,expected in plan['source_sha256'].items():
        if digest((JOB / 'source' / relative).read_bytes()) != expected:
            raise RuntimeError('Frozen source changed: ' + relative)
    base = ROOT / 'work/category-dependencies-v2-20261019/base'
    expected = read(JOB / 'fixed-base-expected.json')
    actual = {}
    for relative in expected['configuration_identity_sample']:
        p = base / relative
        if p.is_symlink():
            actual[relative] = {'symlink':os.readlink(p)}
        elif p.is_file():
            actual[relative] = {'sha256':digest(p.read_bytes()),'bytes':p.stat().st_size}
    if actual != expected['configuration_identity_sample']:
        raise RuntimeError('Frozen base identity changed')
    packages = {}
    for pattern in ('usr/local/lib/python*/dist-packages/*.dist-info/METADATA',
                    'usr/lib/python*/dist-packages/*.dist-info/METADATA',
                    'usr/local/lib/python*/site-packages/*.dist-info/METADATA'):
        packages.update({str(p.relative_to(base)):digest(p.read_bytes()) for p in base.glob(pattern)})
    if packages != expected['installed_python_package_metadata']:
        raise RuntimeError('Frozen base package metadata changed')
    return plan


def prepare(runner):
    import snapshot
    folder = JOB / 'admitted-baselines' / TASK
    template = runner.native.WORK / (TASK + '-template')
    archive = folder / 'public-baseline.tar.gz'
    meta = read(JOB / 'plan.json')['task_metadata']
    if digest(archive.read_bytes()) != meta['public_baseline_sha256']:
        raise RuntimeError('Public baseline changed')
    runner.native.clone(ROOT / 'work/category-dependencies-v2-20261019/base', template)
    for hidden in ('tests','solution'):
        p = template / hidden
        if p.exists():
            if any(p.iterdir()):
                raise RuntimeError('Hidden payload in base')
            p.rmdir()
    with tarfile.open(archive) as file:
        for member in file:
            if member.name.startswith('/') or '..' in Path(member.name).parts:
                raise RuntimeError('Unsafe baseline member')
    subprocess.run(['tar','-xzf',str(archive),'-C',str(template)],check=True)
    # Compare the exact admission file inventory, using its original schema.
    spec=importlib.util.spec_from_file_location('screen_inventory',JOB/'screen_worker.py')
    screen=importlib.util.module_from_spec(spec);spec.loader.exec_module(screen)
    if screen.inventory(template) != read(folder / 'baseline-manifest.json'):
        raise RuntimeError('Public initialization differs from admission')
    cwd = meta['cwd']
    probe = 'import socket,errno\ntry: socket.socket();raise AssertionError("network enabled")\nexcept OSError as e: assert e.errno==errno.EPERM;print("OFFLINE_OK")'
    result = subprocess.run([str(ROOT/'venv/bin/python'),str(JOB/'enter-expanded.py'),
             '--offline-agent',str(template),cwd,'/usr/bin/python3','-c',probe],
             capture_output=True,text=True,timeout=30)
    save(JOB/'controls/network.json',{'returncode':result.returncode,'stdout':result.stdout,'stderr':result.stderr})
    if result.returncode or 'OFFLINE_OK' not in result.stdout:
        raise RuntimeError('Offline boundary failed')
    runner.native.RUNS = JOB / 'controls'
    for oracle in (False,True):
        control = runner.native.WORK / ('positive' if oracle else 'negative')
        runner.native.clone(template,control)
        try:
            outcome=runner.native.verify(TASK,control,cwd,'positive' if oracle else 'negative',oracle=oracle)
            save(JOB/'controls'/('positive.json' if oracle else 'negative.json'),outcome)
            if outcome['reward'] != ('1' if oracle else '0'):
                raise RuntimeError('Environment control failed')
        finally:
            runner.native.delete_work(control)
    save(JOB/'controls/environment.json',{'status':'valid','task':TASK,
         'positive_and_negative_controls':'passed','public_initialization':'matched'})
    return template,cwd,snapshot.inventory(template)


def count_skill_usage(directory):
    # Literal, explicit Skill reads only. Indirect/dynamic references stay unknown.
    import re
    from diagnosis import call_id
    events = [json.loads(line) for line in (directory/'events.jsonl').read_text().splitlines()]
    loaded = read(directory/'skill-loaded.json') if (directory/'skill-loaded.json').exists() else {}
    complete = bool(events and events[-1]['type']=='turn/end')
    report = {}
    for document in ('format','task'):
        path=f'/opt/agent-skills/{document}/SKILL.md'
        refs=[];uncertain=[]
        for event in events:
            if event['type']!='tool/call':continue
            data=event.get('data',{})
            arguments=data.get('arguments',{})
            if isinstance(arguments,str):
                try:arguments=json.loads(arguments)
                except ValueError:arguments={}
            command=arguments.get('command','')
            if path not in command:
                if 'agent-skills' in command or 'SKILL.md' in command:uncertain.append(event['seq'])
                continue
            # Require an explicit reader with the literal path, rather than mere mention.
            reader=re.search(r'(?:^|[;|&\n])\s*(?:cat|head|tail|sed|awk|grep|less|more)\b[^\n;|&]*'+re.escape(path),command)
            if not reader:
                uncertain.append(event['seq']);continue
            cid=call_id(event)
            results=[e for e in events if e['type']=='tool/result' and call_id(e)==cid]
            refs.append({'turn':data.get('turn'),'step':data.get('step'),'seq':event['seq'],
                         'callId':cid,'result_seqs':[e['seq'] for e in results],
                         'outcome':'returned_unclassified' if results else 'no_return'})
        report[document]={'skill_sha256':digest((directory/f'{document.upper()}_SKILL.md').read_bytes()),
             'skill_load_count':loaded.get('documents',{}).get(document,{}).get('skill_load_count'),
             'skill_call_count':len(refs) if complete and not uncertain else None,
             'confirmed_explicit_attempts':len(refs),'calls':refs,'uncertain_event_seqs':uncertain,
             'zero_supported':complete and not uncertain and not refs}
    save(directory/'skill-usage.json',report)


def main():
    args=argparse.ArgumentParser();args.add_argument('--validate',action='store_true');args=args.parse_args()
    plan=validate_frozen()
    if args.validate:
        print(json.dumps({'valid':True,'model_calls':0,'category':'debugging','phase':'fixed_input_EVO1'}));return
    with lock(JOB/'worker.lock',nonblocking=True):
        if (JOB/'started.json').exists():raise RuntimeError('Prior launch exists; no automatic restart')
        save(JOB/'started.json',{'at':stamp(),**identity(os.getpid())})
        runner=configure_runner()
        template=None
        try:
            save(JOB/'status.json',{'phase':'environment_controls','at':stamp()})
            template,cwd,baseline=prepare(runner)
            skills=read(JOB/'initial-skills.json')
            combined=skills['format']+'\n\n'+skills['task']
            runner.native.RUNS=JOB/'pool'/'POOL'
            def solve(rep):
                directory=JOB/'pool'/'POOL'/TASK/f'rep-{rep}'
                directory.mkdir(parents=True,exist_ok=True)
                for name,text in skills.items():(directory/f'{name.upper()}_SKILL.md').write_text(text)
                result=runner.run_attempt('POOL','sqlite-db-truncate',1,rep,template,cwd,combined,
                                         directory/'status.json',baseline)
                count_skill_usage(directory)
                save(directory/'operator-evaluation.json',{'execution_complete':result.get('finish_reason')=='completed'
                     and not result.get('error') and result.get('process_exit_code')==0,
                     'environment_valid':True,'evaluation':result.get('evaluation'),
                     'operator_only':True,'reflection_input':False})
                return directory
            save(JOB/'status.json',{'phase':'sampling_six_fresh_trajectories','at':stamp(),'groups':GROUPS})
            with ThreadPoolExecutor(max_workers=2) as pool:directories=list(pool.map(solve,range(1,7)))
            prompt=(JOB/'source'/TASK/'instruction.md').read_text()
            def one(pair):
                i,directory=pair
                return diagnose(directory,prompt,JOB/'diagnoses'/f'rep-{i}')
            save(JOB/'status.json',{'phase':'independent_diagnosis','at':stamp()})
            with ThreadPoolExecutor(max_workers=2) as pool:inputs=list(pool.map(one,enumerate(directories,1)))
            random.Random(plan['subset_seed']).shuffle(inputs)
            save(JOB/'paired-input-manifest.json',{'ordered_trajectory_ids':[x['trajectory']['trajectory_id'] for x in inputs],
                 'same_packets_across_BC':True,'nested_subsets':[2,4,6]})
            results={}
            for group in GROUPS:
                save(JOB/'status.json',{'phase':'reflection_comparison','group':group,'at':stamp()})
                after,decision=update(skills,inputs[:int(group[1:])],group,prompt,JOB/'comparisons'/group)
                results[group]={'decision':decision,'skill_bytes':{n:len(t.encode()) for n,t in after.items()}}
                save(JOB/'summary.json',{'category':'debugging','task':TASK,'phase':'fixed_input_pilot',
                     'completed_groups':results,'test_results_available':False,'causal_review':'pending'})
            save(JOB/'completion.json',{'at':stamp(),'phase':'fixed_input_pilot_complete_pending_evidence_review',
                 'groups':results,'full_EVO_training_started':False,'test_started':False})
            save(JOB/'status.json',{'phase':'fixed_input_pilot_complete_pending_evidence_review','at':stamp()})
        except Exception as exc:
            save(JOB/'error.json',{'at':stamp(),'error':str(exc),'traceback':traceback.format_exc(),
                 'automatic_retry':False})
            save(JOB/'status.json',{'phase':'blocked_or_failed','at':stamp(),'error':str(exc)})
            raise
        finally:
            if template is not None and template.exists():runner.native.delete_work(template)


if __name__=='__main__':main()
