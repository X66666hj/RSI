"""Fixed-base admission controls only. No solver/optimizer and no old-run mutation."""
from pathlib import Path,PurePosixPath
from contextlib import contextmanager,ExitStack
import os,sys,json,hashlib,shutil,subprocess,shlex,fcntl,time,traceback,argparse,signal
R=Path(__file__).resolve().parent.parent.parent;MAIN=R/'shared-runtime'
sys.path.insert(0,str(R));import native
def read(p):return json.loads(Path(p).read_text())
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def save(p,x):native.save(p,x)
def now():return native.stamp()
def identity(pid):
 try:
  p=Path('/proc')/str(pid);s=(p/'stat').read_text().split(') ',1)[1].split();return {'pid':int(pid),'starttime':s[19],'state':s[0]}
 except (FileNotFoundError,ProcessLookupError):return None
@contextmanager
def lock(p,nonblock=False):
 with Path(p).open('a+') as h:
  fcntl.flock(h,fcntl.LOCK_EX|(fcntl.LOCK_NB if nonblock else 0));yield
def base_check(job):
 x=read(job/'fixed-base-expected.json');b=R/'work/category-dependencies-v2-20261019/base'
 for n,v in x['configuration_identity_sample'].items():
  p=b/n;a={'symlink':os.readlink(p)} if p.is_symlink() else {'sha256':sha(p),'bytes':p.stat().st_size}
  if a!=v:raise RuntimeError('Base fingerprint changed: '+n)
 for n,h in x['installed_python_package_metadata'].items():
  if sha(b/n)!=h:raise RuntimeError('Base package fingerprint changed: '+n)
def validate(job):
 p=read(job/'screen-plan.json')
 for n,h in p['dependencies_sha256'].items():
  if sha(job/n)!=h:raise RuntimeError('Screen dependency changed '+n)
 for n,h in p['shared_dependencies_sha256'].items():
  if sha(R/n)!=h:raise RuntimeError('Shared dependency changed '+n)
 base_check(job);return p
def cow(src,dst):
 if dst.exists():raise RuntimeError('Existing workspace; unknown attempt not repeated')
 dst.mkdir(parents=True);subprocess.run(['cp','-a','--reflink=always',str(src)+'/.',str(dst)],check=True)

def guarded_inside(root,cwd,script,log,timeout):
 """Stop only this control's own process group on timeout or disk reserve loss."""
 with Path(log).open('ab',buffering=0) as output:
  proc=subprocess.Popen([str(R/'venv/bin/python'),str(Path(__file__).parent/'enter-expanded.py'),str(root),cwd,'/bin/bash','-c',script],stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
  deadline=time.monotonic()+timeout
  while proc.poll() is None:
   reason='control_timeout' if time.monotonic()>deadline else 'data_disk_reserve_below_8GiB' if shutil.disk_usage(R).free<8*1024**3 else None
   if reason:
    try:os.killpg(proc.pid,signal.SIGKILL)
    except ProcessLookupError:pass
    proc.wait();save(Path(log).with_suffix('.resource-stop.json'),{'at':now(),'reason':reason,'owned_process_group':proc.pid,'no_model_calls':True})
    return 124 if reason=='control_timeout' else 125
   time.sleep(1)
  return proc.returncode
PUBLIC_TOPS=['app','workspace','data','build','opt/task','root/.gitconfig','usr/local/bin/promote-regex-rules'] + ['fonts', 'templates', 'opt/artifact', 'usr/local/bin/promote-edits', 'usr/local/bin/promote-portfolio']

def assert_public_coverage(root,log):
 # A public change outside the restorable archive must not silently disappear in A/B/C.
 base=R/'work/category-dependencies-v2-20261019/base'
 def signatures(tree):
  out={}
  for d,ds,fs in os.walk(tree,followlinks=False):
   for n in ds+fs:
    p=Path(d)/n;rel=p.relative_to(tree).as_posix();st=p.lstat()
    out[rel]=('link',os.readlink(p)) if p.is_symlink() else ('dir',st.st_mode&0o777) if p.is_dir() else ('file',st.st_size,st.st_mtime_ns,st.st_mode&0o777)
  return out
 a=signatures(base);b=signatures(root)
 allowed=PUBLIC_TOPS+['logs','tests','solution']
 missing=[n for n in a.keys()|b.keys() if a.get(n)!=b.get(n) and not any(n==v or n.startswith(v+'/') for v in allowed)]
 save(Path(log).with_name('public-coverage.json'),{'unrestorable_changed_paths':sorted(missing),'complete':not missing})
 if missing:raise RuntimeError('Public preparation changes paths outside reviewed restoration scope: '+', '.join(sorted(missing)[:12]))

def inventory(root):
 out={}
 for top in PUBLIC_TOPS:
  start=root/top
  for p in ([start] if start.is_file() or start.is_symlink() else sorted(start.rglob('*'))):
   if p.is_symlink():out[str(p.relative_to(root))]={'symlink':os.readlink(p)}
   elif p.is_file():out[str(p.relative_to(root))]={'sha256':sha(p),'mode':p.stat().st_mode&0o777}
 return out
def prepare(job,task,root,log):
 recipe=read(job/'public-preparation-recipes.json')[task];src=job/'screen-source'/task/'environment'
 if sha(src/'Dockerfile')!=recipe['dockerfile_sha256']:raise RuntimeError('Dockerfile changed')
 cow(R/'work/category-dependencies-v2-20261019/base',root);cwd='/';skips=[];public_env={}
 for hidden in ['tests','solution']:
  hp=root/hidden
  if hp.exists():
   if any(hp.iterdir()):raise RuntimeError('Hidden payload in dependency base')
   hp.rmdir()
 if any((root/'app').iterdir()):raise RuntimeError('Unclean base')
 for op,v in recipe['operations']:
  if op=='FROM':continue
  if op=='ENV':
   for assignment in shlex.split(v):
    k,sep,value=assignment.partition('=')
    if not sep or k not in ['DEBIAN_FRONTEND','PIP_BREAK_SYSTEM_PACKAGES','HF_HOME','HF_DATASETS_CACHE','USER','KVSTORE_PORT','PYTHONPATH','CC']:raise ValueError('Unreviewed runtime ENV '+v)
    public_env[k]=value
   save(root/'opt/task/runtime-environment.json',public_env);continue
  if op=='BASE_DEPENDENCIES_ALREADY_FIXED':skips.append(v);continue
  if op=='WORKDIR':cwd=str(PurePosixPath(cwd)/v);(root/cwd.lstrip('/')).mkdir(parents=True,exist_ok=True)
  elif op=='COPY':
   parts=json.loads(v) if v.startswith('[') else shlex.split(v);*ss,d=parts;dest=str(PurePosixPath(cwd)/d);dp=root/dest.lstrip('/')
   for s in ss:
    sp=(src/s).resolve()
    if not sp.is_relative_to(src.resolve()):raise ValueError('COPY escape')
    if sp.is_dir():shutil.copytree(sp,dp,dirs_exist_ok=True)
    else:
     target=dp/sp.name if d.endswith('/') or dp.is_dir() or len(ss)>1 else dp;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(sp,target)
  elif op=='RUN':
   if any(s in v for s in ('apt-get','pip install','pip3 install','flit install')):raise ValueError('Dependency/network mutation refused')
   if guarded_inside(root,cwd,v,log,180):raise RuntimeError('Public setup failed; see prepare.log')
  else:raise ValueError('Unreviewed operation '+op)
 (root/'logs/verifier').mkdir(parents=True,exist_ok=True)
 return {'cwd':cwd,'public_environment':public_env,'base_dependency_setup_omitted':skips,'adaptation':'expanded frozen dependency base; upstream pinned dependency versions replaced by recorded common versions; exact tests unchanged'}
def verify(job,task,root,cwd,label,oracle):
 src=job/'screen-source'/task;out=job/'controls'/task/label;out.mkdir(parents=True)
 if oracle:
  shutil.copytree(src/'solution',root/'solution')
  adapted=job/'reference-adaptations'/task/'solve.sh'
  if adapted.exists():
   manifest=read(job/'reference-adaptations.json')[task]
   assert sha(adapted)==manifest['adapted_sha256'] and sha(src/'solution/solve.sh')==manifest['original_sha256']
   shutil.copy2(adapted,root/'solution/operator-control.sh')
  rc=guarded_inside(root,cwd,'bash /solution/operator-control.sh' if adapted.exists() else 'bash /solution/solve.sh',out/'oracle.log',600)
  if rc:return {'status':'reference_execution_failure','exit_code':rc,'cause':'control_timeout' if rc==124 else 'data_disk_reserve_below_8GiB' if rc==125 else 'reference_script_error'}
 shutil.copytree(src/'tests',root/'tests');script=(root/'tests/test.sh').read_text()
 if any(s in script for s in ('apt-get','pip install','uvx','curl ','wget ')):raise ValueError('Verifier requires changed dependencies/network; defer')
 rc=guarded_inside(root,cwd,'export TEST_DIR=/tests; bash /tests/test.sh',out/'verifier.log',600)
 shutil.copytree(root/'logs/verifier',out/'verifier');rp=root/'logs/verifier/reward.txt';cp=root/'logs/verifier/ctrf.json'
 reward=rp.read_text().strip() if rp.exists() else None;summary=read(cp).get('results',{}).get('summary',{}) if cp.exists() else {}
 return {'exit_code':rc,'reward':reward,'summary':summary,'status':'passed' if rc==0 and reward=='1' else 'failed' if rc in (0,1) and reward=='0' else 'control_execution_or_scoring_error'}
def case(job,task):
 p=validate(job);c=job/'tasks'/task;c.mkdir(parents=True,exist_ok=True)
 with lock(c/'worker.lock',True):
  if (c/'started.json').exists():raise RuntimeError('Case already started; never replay')
  save(c/'started.json',{'at':now(),**identity(os.getpid()),'model_calls':0});native.WORK=R/'work'/job.name
  template=native.WORK/(task+'-template');work=native.WORK/(task+'-control');result={'task':task,'admitted':False,'model_calls':0,'at':now()}
  with ExitStack() as st:
   acquired=False
   while not acquired:
    if shutil.disk_usage(R).free>=30*1024**3:
     for i in range(8):
      h=(MAIN/'locks'/f'solver-{i}.lock').open('a+')
      try:fcntl.flock(h,fcntl.LOCK_EX|fcntl.LOCK_NB)
      except BlockingIOError:h.close();continue
      st.enter_context(h);acquired=True;result['slot']=i;break
    if not acquired:
     save(c/'waiting-for-resource.json',{'at':now(),'phase':'waiting_for_free_slot_or_disk','no_control_started':True});time.sleep(5)
   try:
    if shutil.disk_usage(R).free<8*1024**3:raise RuntimeError('Data disk free below8GiB')
    sources=read(job/'screen-source-manifest.json')['files'];expected={n:h for n,h in sources.items() if n.startswith(task+'/')}
    if any(sha(job/'screen-source'/n)!=h for n,h in expected.items()):raise RuntimeError('Source changed')
    with lock(MAIN/'locks/filesystem.lock'):info=prepare(job,task,template,c/'prepare.log');save(c/'preparation.json',info);assert_public_coverage(template,c/'prepare.log');save(c/'baseline-manifest.json',inventory(template));cow(template,work)
    config=template/'root/.gitconfig'
    if config.exists():shutil.copy2(config,c/'public-gitconfig');result['public_gitconfig_sha256']=sha(config)
    result['negative']=verify(job,task,work,info['cwd'],'negative',False)
    with lock(MAIN/'locks/filesystem.lock'):native.delete_work(work);cow(template,work)
    result['reference']=verify(job,task,work,info['cwd'],'reference',True)
    n=result['negative'];r=result['reference'];ns=n.get('summary',{});rs=r.get('summary',{})
    result['source_unchanged']=all(sha(job/'screen-source'/z)==h for z,h in expected.items())
    result['admitted']=(n['status']=='failed' and ns.get('tests',0)>0 and ns.get('failed',0)>0 and r['status']=='passed' and rs.get('tests',0)>0 and rs.get('passed')==rs.get('tests') and result['source_unchanged'])
    if result['admitted']:
     tops=[x for x in PUBLIC_TOPS if (template/x).exists()]
     subprocess.run(['tar','-czf',str(c/'public-baseline.tar.gz'),'-C',str(template),*tops],check=True);result['baseline_archive_sha256']=sha(c/'public-baseline.tar.gz')
   except Exception as e:result.update(error=type(e).__name__+': '+str(e));save(c/'error.json',{'traceback':traceback.format_exc()})
   finally:
    with lock(MAIN/'locks/filesystem.lock'):
     native.delete_work(work);native.delete_work(template)
   base_check(job);result['finished_at']=now();save(c/'result.json',result)
def run(job):
 p=validate(job)
 with lock(job/'worker.lock',True):
  if (job/'started.json').exists():raise RuntimeError('Screen already started; no resend')
  save(job/'started.json',{'at':now(),**identity(os.getpid()),'model_calls':0})
  queue=list(p['tasks']);live={}
  while queue or live:
   while queue and len(live)<2:
    task=queue.pop(0);folder=job/'tasks'/task;folder.mkdir(parents=True,exist_ok=True)
    if (folder/'dispatch-intent.json').exists():raise RuntimeError('Prior dispatch intent')
    save(folder/'dispatch-intent.json',{'at':now(),'task':task})
    with (folder/'process.log').open('ab',buffering=0) as log:proc=subprocess.Popen([str(R/'venv/bin/python'),'-u',str(job/'screen_worker.py'),'--job',str(job),'--task',task],stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    save(folder/'launch.json',{'at':now(),**identity(proc.pid)});live[task]=proc
   for task,proc in list(live.items()):
    if proc.poll() is not None:
     save(job/'tasks'/task/'process-exit.json',{'at':now(),'exit_code':proc.returncode});del live[task]
   results=[read(z) for z in (job/'tasks').glob('*/result.json')]
   save(job/'status.json',{'at':now(),'phase':'screening' if queue or live else 'complete','completed':len(results),'pending':len(queue),'live':{t:identity(q.pid) for t,q in live.items()},'admitted_tasks':sum(r['admitted'] for r in results),'model_calls':0})
   if queue or live:time.sleep(2)
  chains={f:all((job/'tasks'/f'{f}-EVO-{v}'/'result.json').exists() and read(job/'tasks'/f'{f}-EVO-{v}'/'result.json')['admitted'] for v in range(1,5)) for f in p['families']}
  base_check(job);save(job/'completed.json',{'at':now(),'tasks_expected':len(p['tasks']),'results':len(results),'chains':chains,'model_calls':0,'base_fingerprint_unchanged':True,'main_resumed':False})
if __name__=='__main__':
 a=argparse.ArgumentParser();a.add_argument('--job',type=Path,required=True);a.add_argument('--task');x=a.parse_args()
 try:case(x.job,x.task) if x.task else run(x.job)
 except Exception as e:save(x.job/('tasks/'+x.task if x.task else '')/'operator-error.json',{'at':now(),'error':str(e),'traceback':traceback.format_exc(),'unknown_calls_reissued':False});raise

# Exact public challenge-setup output; no general /tmp allowance.
PUBLIC_TOPS = PUBLIC_TOPS + ['tmp/dangling-secret-blob']
