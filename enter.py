#!/usr/bin/env python3
"""Enter one benchmark filesystem, then drop Linux capabilities before execution."""
import ctypes
import os
from pathlib import Path
import resource
import signal
import sys

arguments = sys.argv[1:]
offline_agent = arguments[:1] == ['--offline-agent']
if offline_agent:
    arguments = arguments[1:]
root, cwd, *command = arguments
root = Path(root).resolve()
allowed = (Path(__file__).resolve().parent / 'work').resolve()
if not root.is_relative_to(allowed) or root == allowed:
    raise SystemExit('Refusing an unrecognized benchmark filesystem')
# Solver shells and all descendants are offline. The interactive-shell check
# also protects existing saved launch patches that predate --offline-agent.
# Controller preparation/verifier commands use a separate noninteractive shell.
if offline_agent or (command and Path(command[0]).name == 'bash' and '-i' in command[1:]):
    sys.path.insert(0, str(allowed.parent))
    import network_guard
    network_guard.prepare()()
os.chroot(root)
os.chdir(cwd)
# Background launchers inherit ignored SIGINT; task programs need normal Linux
# signal behavior (notably asyncio cancellation and the official verifier).
for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
    signal.signal(number, signal.SIG_DFL)
libc = ctypes.CDLL(None, use_errno=True)
# Drop the entire capability bounding set while CAP_SETPCAP is still available.
for capability in range(41):
    libc.prctl(24, capability, 0, 0, 0)
if libc.prctl(38, 1, 0, 0, 0) != 0:
    raise OSError(ctypes.get_errno(), 'PR_SET_NO_NEW_PRIVS failed')
class Header(ctypes.Structure):
    _fields_ = [('version', ctypes.c_uint32), ('pid', ctypes.c_int)]
class Data(ctypes.Structure):
    _fields_ = [('effective', ctypes.c_uint32), ('permitted', ctypes.c_uint32), ('inheritable', ctypes.c_uint32)]
header = Header(0x20080522, 0)
data = (Data * 2)()
if libc.capset(ctypes.byref(header), ctypes.byref(data)) != 0:
    raise OSError(ctypes.get_errno(), 'capset failed')
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
resource.setrlimit(resource.RLIMIT_NOFILE, (4096, 4096))
resource.setrlimit(resource.RLIMIT_FSIZE, (4*1024**3, 4*1024**3))
keep = {'PS1', 'PROMPT_COMMAND', 'TERM', 'NO_COLOR'}
env = {k: v for k, v in os.environ.items() if k in keep}
env.update(PATH='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
           HOME='/root', LANG='C.UTF-8', LC_ALL='C.UTF-8',
           DEBIAN_FRONTEND='noninteractive', PIP_BREAK_SYSTEM_PACKAGES='1',
           PIP_INDEX_URL='https://pypi.tuna.tsinghua.edu.cn/simple',
           UV_INDEX_URL='https://pypi.tuna.tsinghua.edu.cn/simple')
if root.name.startswith('sqlite-with-gcov-EVO-'):
    # Native capability dropping prohibits restoring archived owners. Preserve
    # archive contents/modes while extracting as the current benchmark user.
    env['TAR_OPTIONS']='--no-same-owner'
os.execvpe(command[0], command, env)
