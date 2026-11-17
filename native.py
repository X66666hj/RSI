"""Native adaptation of pinned Terminal-Bench-Evo tasks; never invokes Docker.

Original instructions, environment assets, oracle and verifier are kept intact.
Container base OS/Python are replaced with the recorded Ubuntu native rootfs.
All runtime filesystem paths remain unchanged using chroot; the agent cannot see
the source tree, oracle, held-out tests, other trials, or controller credentials.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import signal
import subprocess
import time
import traceback

ROOT = Path(__file__).resolve().parent
WORK = ROOT / 'work'
BASE = ROOT / 'base'
SOURCE = ROOT / 'source'
RUNS = ROOT / 'runs'
PYTHON = ROOT / 'venv/bin/python'
ENTER = ROOT / 'enter.py'
BUDGET = {'label':'raised-64k-30min-120steps','max_steps':120,'max_tokens_per_request':65536,
          'agent_wall_timeout_seconds':1800,'agent_sdk_request_timeout_seconds':1740}

def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False))
    temp.replace(path)

def stamp():
    return datetime.now(timezone.utc).isoformat()

def delete_work(path):
    path = Path(path).resolve()
    if not path.is_relative_to(WORK.resolve()) or path == WORK.resolve():
        raise ValueError('Work cleanup path outside task workspace')
    for process in Path('/proc').glob('[0-9]*'):
        try:
            if Path(os.readlink(process/'root').removesuffix(' (deleted)')) == path:
                os.kill(int(process.name),signal.SIGKILL)
        except (FileNotFoundError,ProcessLookupError,PermissionError):
            continue
    if path.exists():
        shutil.rmtree(path)

def command(argv, log, timeout=900, env=None):
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('ab') as output:
        output.write(('\n$ ' + shlex.join(map(str,argv)) + '\n').encode())
        output.flush()
        proc = subprocess.Popen(list(map(str,argv)), stdout=output, stderr=subprocess.STDOUT,
                                start_new_session=True, env=env)
        try:
            return proc.wait(timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            output.write(b'\nCONTROLLER_TIMEOUT\n')
            return 124

def inside(root, cwd, script, log, timeout=900):
    return command([PYTHON, ENTER, root, cwd, '/bin/bash', '-c', script], log, timeout)

def clone(source, target):
    delete_work(target)
    target.mkdir(parents=True)
    subprocess.run(['cp','-a',str(source)+'/.',str(target)],check=True)

def instructions(path):
    current = ''
    lines = iter(path.read_text().splitlines())
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        current += line[:-1] + ' ' if line.endswith('\\') else line
        if line.endswith('\\'):
            continue
        op, _, value = current.partition(' ')
        heredoc=re.search(r"<<(-?)[\"']?([A-Za-z_][A-Za-z_0-9]*)[\"']?",value)
        if heredoc:
            if op.upper()!='RUN':
                raise ValueError('Only RUN heredocs are supported')
            marker=heredoc.group(2)
            body=[]
            for body_line in lines:
                body.append(body_line)
                if (body_line.lstrip('\t') if heredoc.group(1) else body_line)==marker:
                    break
            else:
                raise ValueError('Unterminated shell heredoc')
            value+='\n'+'\n'.join(body)
        yield op.upper(), value
        current = ''
    if current:
        raise ValueError('Unterminated environment command')

def prepare(task_id, root):
    task = SOURCE / task_id
    info = {'task_id': task_id, 'started_at': stamp(), 'environment_changes': [
        'Base image replaced by Ubuntu 24.04 native filesystem with Python 3.12.',
        'Common compiler, shell, network-client and verifier packages are preinstalled.',
        'No Docker, PID namespace, process filesystem, or GPU is exposed.',
        'Package mirror changed; original task assets, instructions and tests are unmodified.'
    ], 'steps': []}
    clone(BASE, root)
    if task_id.startswith('fix-git-EVO-'):
        fixture=ROOT/'fixtures/personal-site-restored.git'
        revision=subprocess.check_output(['git','--git-dir='+str(fixture),'rev-parse','d7d3e4b'],text=True).strip()
        if revision!='d7d3e4ba9350f634d92d39ace2b471433ee57d50':
            raise RuntimeError('Unexpected Git fixture commit')
        shutil.copytree(fixture,root/'opt/fixtures/personal-site.git')
        code=command(['chroot',root,'git','config','--global',
                      'url.file:///opt/fixtures/personal-site.git.insteadOf',
                      'https://github.com/TheMikeMerrill/personal-site.git'],RUNS/task_id/'prepare.log')
        if code:
            raise RuntimeError('Cannot install verified Git fixture URL mapping')
        info['environment_changes'].append('Unavailable GitHub origin restored from wangsikuan/personal-site public mirror; verified full d7d3e4b commit and its original parent. Original setup.sh unchanged.')
        info['git_fixture_revision']=revision
    if task_id.startswith('sqlite-with-gcov-EVO-'):
        info['environment_changes'].append('TAR_OPTIONS=--no-same-owner avoids archive-owner restoration forbidden by native capability dropping; archive content and original scripts unchanged.')
    cwd = '/'
    env = {}
    log = RUNS / task_id / 'prepare.log'
    for op, value in instructions(task / 'environment/Dockerfile'):
        info['steps'].append({'operation':op,'value':value})
        if op == 'FROM':
            info['upstream_base'] = value
        elif op == 'WORKDIR':
            cwd = str(PurePosixPath(cwd) / value)
            (root / cwd.lstrip('/')).mkdir(parents=True, exist_ok=True)
        elif op == 'ENV':
            for entry in shlex.split(value):
                name, sep, val = entry.partition('=')
                if not sep:
                    raise ValueError('Unsupported legacy ENV syntax')
                env[name] = val
        elif op == 'COPY':
            items = json.loads(value) if value.startswith('[') else shlex.split(value)
            if any(x.startswith('--') for x in items):
                raise ValueError('COPY flags require manual native review')
            *sources, dest = items
            dest = str(PurePosixPath(cwd) / dest)
            destination = root / dest.lstrip('/')
            for item in sources:
                src = task / 'environment' / item
                if not src.resolve().is_relative_to((task / 'environment').resolve()):
                    raise ValueError('Source COPY escaped task environment')
                if src.is_dir():
                    shutil.copytree(src, destination, dirs_exist_ok=True)
                elif src.is_file():
                    target = destination / src.name if dest.endswith('/') or destination.is_dir() or len(sources)>1 else destination
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, target)
                else:
                    raise FileNotFoundError(src)
        elif op == 'RUN':
            prefix = 'export ' + ' '.join(f'{k}={shlex.quote(v)}' for k,v in env.items()) + '; ' if env else ''
            setup = 'cd '+shlex.quote(cwd)+'; export DEBIAN_FRONTEND=noninteractive PIP_BREAK_SYSTEM_PACKAGES=1 PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple; '+prefix+value
            code = command(['chroot',root,'/bin/bash','-c',setup],log,timeout=1200)
            if code:
                raise RuntimeError(f'Environment setup returned {code}: {value}')
        else:
            raise ValueError(f'Environment operation needs review: {op}')
    # Fixed rootfs identity and clean empty held-out evaluation paths.
    if task_id.startswith('fix-git-EVO-'):
        command(['chroot',root,'git','config','--global','--remove-section',
                 'url.file:///opt/fixtures/personal-site.git'],log)
        shutil.rmtree(root/'opt/fixtures')
    (root/'root').mkdir(exist_ok=True)
    (root/'logs/verifier').mkdir(parents=True,exist_ok=True)
    info['cwd'] = cwd
    info['finished_at'] = stamp()
    save(RUNS/task_id/'environment.json',info)
    return info

def verify(task_id, root, cwd, label, oracle=False):
    directory = RUNS / task_id / label
    directory.mkdir(parents=True,exist_ok=True)
    if oracle:
        shutil.copytree(SOURCE/task_id/'solution',root/'solution',dirs_exist_ok=True)
        rc = inside(root,cwd,'bash /solution/solve.sh',directory/'oracle.log',1800)
        if rc:
            return {'status':'oracle_execution_error','exit_code':rc,'reward':None}
    shutil.copytree(SOURCE/task_id/'tests',root/'tests',dirs_exist_ok=True)
    (root/'logs/verifier').mkdir(parents=True,exist_ok=True)
    reward_path = root/'logs/verifier/reward.txt'
    reward_path.unlink(missing_ok=True)
    rc = inside(root,cwd,'export TEST_DIR=/tests; bash /tests/test.sh',directory/'verifier.log',1200)
    reward = reward_path.read_text().strip() if reward_path.exists() else None
    shutil.copytree(root/'logs/verifier',directory/'verifier',dirs_exist_ok=True)
    return {'status':'passed' if reward=='1' else 'failed' if reward=='0' else 'verifier_error',
            'exit_code':rc,'reward':reward}

def run_agent(task_id, repetition, template, cwd):
    trial = f'rep-{repetition}'
    directory = RUNS/task_id/trial
    result_path = directory/'result.json'
    if result_path.exists():
        return json.loads(result_path.read_text())
    directory.mkdir(parents=True,exist_ok=True)
    root = WORK/f'{task_id}-{trial}'
    clone(template,root)
    save(directory/'input.json',{'task_id':task_id,'repetition':repetition,'cwd':cwd,'budget':BUDGET})
    patch = [
        '- id: terminal-bash', '  config:', f'    shellPath: {json.dumps(str(PYTHON))}',
        '    shellArgs: '+json.dumps([str(ENTER),'--offline-agent',str(root),cwd,'/bin/bash','--noprofile','--norc','-i']),
        '    timeoutMs: 300000',
        '- id: system-prompt', '  config:', '    includeHarnessIdentity: false',
        '    includeRuntimeContext: false',
        '    personaPrefix: >-',
        '      You are a software engineering agent working in a Linux terminal.',
        '      Complete the task by modifying the actual filesystem using the bash tool.',
        '      Inspect files and run your own checks. Do not seek benchmark reference solutions',
        '      or hidden grading tests. State briefly when your work is complete.',
        '- id: llm-deepseek', '  config:', '    streamIdleTimeoutMs: 180000',
    ]
    (directory/'native.patch.yml').write_text('\n'.join(patch)+'\n')
    code = command([PYTHON,ROOT/'agent.py',task_id,str(repetition),directory],directory/'process.log',timeout=BUDGET['agent_wall_timeout_seconds'])
    outcome = json.loads((directory/'agent.json').read_text()) if (directory/'agent.json').exists() else {'error':f'agent process exited {code}'}
    outcome.update(task_id=task_id,repetition=repetition,process_exit_code=code,budget=BUDGET)
    if task_id=='largest-eigenval-EVO-1':
        outcome['evaluation_note']='Upstream starter can already pass its own 110%-of-reference threshold. Official tests unchanged; initial baseline score recorded separately.'
    # Capture the submitted filesystem before fixture setup can modify it.
    for folder in ('app','workspace','data','build'):
        if (root/folder).exists():
            command(['tar','-czf',directory/f'{folder}.tar.gz','-C',root,folder],directory/'archive.log',120)
    # The held-out verifier is made available only after the Harness has exited.
    outcome['evaluation'] = verify(task_id,root,cwd,trial)
    outcome['finished_at'] = stamp()
    save(result_path,outcome)
    delete_work(root)
    return outcome

def retry_budget_exhausted(manifest_name='budget-retries.json',runs_name='runs-raised-budget',status_name='retry-status.json'):
    global RUNS
    manifest=ROOT/manifest_name
    if not manifest.exists():
        return 0
    original_runs=RUNS
    RUNS=ROOT/runs_name
    grouped={}
    for item in json.loads(manifest.read_text()):
        grouped.setdefault(item['task_id'],[]).append(item['repetition'])
    try:
        for task_id,repetitions in grouped.items():
            if all((RUNS/task_id/f'rep-{r}/result.json').exists() for r in repetitions):
                continue
            state={'phase':'raised_budget_retries','active_task':task_id,'repetitions':repetitions,'budget':BUDGET,'blocked':[]}
            save(ROOT/status_name,state)
            template=WORK/f'{task_id}-retry-template'
            oracle_root=WORK/f'{task_id}-retry-oracle'
            try:
                info=prepare(task_id,template)
                clone(template,oracle_root)
                check=verify(task_id,oracle_root,info['cwd'],'preflight-oracle',oracle=True)
                save(RUNS/task_id/'preflight.json',check)
                delete_work(oracle_root)
                if check['status']!='passed':
                    raise RuntimeError('Retry reference solution did not pass')
                clone(template,oracle_root)
                negative=verify(task_id,oracle_root,info['cwd'],'preflight-empty')
                save(RUNS/task_id/'negative-control.json',negative)
                delete_work(oracle_root)
                if negative['status']!='failed':
                    raise RuntimeError('Retry negative control did not fail')
                print(stamp(),'RAISED_BUDGET_RETRY',task_id,repetitions,flush=True)
                with ThreadPoolExecutor(max_workers=4) as pool:
                    list(pool.map(lambda r:run_agent(task_id,r,template,info['cwd']),repetitions))
                delete_work(template)
                subprocess.run([str(PYTHON),str(ROOT/'summarize.py')],stdout=subprocess.DEVNULL,check=True)
            except Exception as exc:
                state['blocked']=[{'task_id':task_id,'error':str(exc)}]
                save(ROOT/status_name,state)
                traceback.print_exc()
                return 2
        save(ROOT/status_name,{'phase':'completed','finished_at':stamp(),'budget':BUDGET})
        return 0
    finally:
        RUNS=original_runs

def batch(limit=None, retry_deferred=False):
    tasks = json.loads((ROOT/'candidates.json').read_text())
    ids = [r['task_id'] for r in tasks]
    # Exercise the already packaged verifier before legacy download-based runners.
    ids.remove('cancel-async-tasks-EVO-1')
    ids.insert(0,'cancel-async-tasks-EVO-1')
    if limit:
        ids = ids[:limit]
    deferred_path=ROOT/'deferred-tasks.json'
    deferred=json.loads(deferred_path.read_text()) if deferred_path.exists() else {}
    state = {'started_at':stamp(),'planned_tasks':len(ids),'repetitions':4,'completed':[], 'blocked':[],
             'policy':'continue remaining tasks; preserve environment failures for later repair'}
    for task_id in ids:
        existing = [RUNS/task_id/f'rep-{r}/result.json' for r in range(1,5)]
        if all(p.exists() for p in existing):
            results=[json.loads(p.read_text()) for p in existing]
            state['completed'].append({'task_id':task_id,'results':[r['evaluation'] for r in results]})
            save(ROOT/'status.json',state)
            continue
        if task_id in deferred and not retry_deferred:
            state['blocked'].append(deferred[task_id])
            save(ROOT/'status.json',state)
            print(stamp(),'DEFERRED_PREVIOUS',task_id,flush=True)
            continue
        state['active_task']=task_id
        state['phase']='preparing'
        save(ROOT/'status.json',state)
        print(stamp(),'PREPARE',task_id,flush=True)
        template = WORK/f'{task_id}-template'
        oracle_root = WORK/f'{task_id}-oracle'
        try:
            info = prepare(task_id,template)
            clone(template,oracle_root)
            check = verify(task_id,oracle_root,info['cwd'],'preflight-oracle',oracle=True)
            save(RUNS/task_id/'preflight.json',check)
            delete_work(oracle_root)
            if check['status'] != 'passed':
                raise RuntimeError('Reference solution did not pass unmodified verifier')
            clone(template,oracle_root)
            negative = verify(task_id,oracle_root,info['cwd'],'preflight-empty')
            save(RUNS/task_id/'negative-control.json',negative)
            delete_work(oracle_root)
            if task_id=='largest-eigenval-EVO-1' and negative['status']=='passed':
                # This task ships a working NumPy implementation and allows up
                # to 110% of the reference runtime. A passing starter is valid.
                save(RUNS/task_id/'initial-baseline.json',negative)
                clone(template,oracle_root)
                (oracle_root/'app/repo/linalg/src/eigen.py').write_text(
                    'def find_dominant_eigenvalue_and_eigenvector(A):\n    raise NotImplementedError("deliberately invalid negative control")\n')
                negative=verify(task_id,oracle_root,info['cwd'],'preflight-invalid')
                negative['control_type']='deliberately invalid implementation; only this isolated control root was modified'
                save(RUNS/task_id/'negative-control.json',negative)
                delete_work(oracle_root)
            if negative['status'] not in ('failed',):
                raise RuntimeError('Unsolved negative control did not fail cleanly')
            print(stamp(),'RUN_FOUR',task_id,flush=True)
            state['phase']='four_independent_attempts'
            save(ROOT/'status.json',state)
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda rep:run_agent(task_id,rep,template,info['cwd']), range(1,5)))
            state['completed'].append({'task_id':task_id,'results':[r['evaluation'] for r in results]})
            delete_work(template)
        except Exception as exc:
            save(RUNS/task_id/'blocked.json',{'error':str(exc),'traceback':traceback.format_exc()})
            entry={'task_id':task_id,'error':str(exc),'phase':state['phase'],'deferred_at':stamp(),
                   'missing_repetitions':[r for r in range(1,5) if not (RUNS/task_id/f'rep-{r}/result.json').exists()],
                   'logs_directory':str(RUNS/task_id)}
            deferred[task_id]=entry
            save(deferred_path,deferred)
            state['blocked'].append(entry)
            print(stamp(),'DEFERRED',task_id,str(exc),flush=True)
            # User requested a full sweep; retain evidence without inventing model failures.
            for leftover in [template,oracle_root]+[WORK/f'{task_id}-rep-{r}' for r in range(1,5)]:
                try:
                    delete_work(leftover)
                except Exception as cleanup_error:
                    print(stamp(),'CLEANUP_WARNING',str(leftover),str(cleanup_error),flush=True)
            save(ROOT/'status.json',state)
            subprocess.run([str(PYTHON),str(ROOT/'summarize.py')],stdout=subprocess.DEVNULL,check=True)
            continue
        if task_id in deferred:
            del deferred[task_id]
            save(deferred_path,deferred)
        save(ROOT/'status.json',state)
        subprocess.run([str(PYTHON),str(ROOT/'summarize.py')],stdout=subprocess.DEVNULL,check=True)
    state['finished_at'] = stamp()
    state['active_task']=None
    state['phase']='completed_with_deferred' if state['blocked'] else 'completed'
    save(ROOT/'status.json',state)
    subprocess.run([str(PYTHON),str(ROOT/'summarize.py')],stdout=subprocess.DEVNULL,check=True)
    return 0

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--limit',type=int)
    parser.add_argument('--retry-deferred',action='store_true')
    args=parser.parse_args()
    if not (BASE/'.ready').exists():
        raise SystemExit('Native base environment is not ready')
    WORK.mkdir(exist_ok=True)
    retry_code=retry_budget_exhausted('token-retries.json','runs-raised-tokens','token-retry-status.json')
    if retry_code:
        raise SystemExit(retry_code)
    raise SystemExit(batch(args.limit,args.retry_deferred))
