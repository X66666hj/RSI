"""Fail-closed seccomp isolation for solver tools, installed before chroot.

The controller's model API connection stays outside this process. Solver
children inherit the filter; no environment variable can turn it off.
"""
import ctypes
import ctypes.util
import errno
import os


class ArgCmp(ctypes.Structure):
    _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_int),
                ('datum_a', ctypes.c_uint64), ('datum_b', ctypes.c_uint64)]


def prepare():
    lib = ctypes.CDLL(ctypes.util.find_library('seccomp') or 'libseccomp.so.2', use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
        ctypes.c_int, ctypes.c_uint, ctypes.POINTER(ArgCmp)]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.restype = None
    ctx = lib.seccomp_init(0x7fff0000)
    if not ctx:
        raise RuntimeError('seccomp initialization failed')
    deny = 0x00050000 | errno.EPERM

    def rule(name, comparison=None, required=True):
        number = lib.seccomp_syscall_resolve_name(name.encode())
        if number < 0:
            if required:
                raise RuntimeError('missing required syscall: ' + name)
            return
        count = 1 if comparison is not None else 0
        pointer = ctypes.byref(comparison) if comparison is not None else None
        if lib.seccomp_rule_add_array(ctx, deny, number, count, pointer):
            raise RuntimeError('seccomp rule failed: ' + name)

    try:
        # Retain anonymous Unix socketpair IPC; reject every other family.
        for name in ['socket', 'socketpair']:
            rule(name, ArgCmp(0, 1, 1, 0))  # SCMP_CMP_NE, AF_UNIX=1
        # Named Unix sockets, including the shared abstract namespace, cannot
        # relay requests to a host proxy. Existing socketpairs still read/write.
        for name in ['connect', 'bind', 'sendmsg', 'sendmmsg']:
            rule(name)
        rule('sendto', ArgCmp(4, 1, 0, 0))
        for name in ['io_uring_setup', 'io_uring_enter', 'io_uring_register',
                     'pidfd_getfd', 'ptrace', 'process_vm_writev', 'bpf',
                     'setns', 'unshare']:
            rule(name, required=False)
    except BaseException:
        lib.seccomp_release(ctx)
        raise

    def install():
        # Load while host /proc is visible. No inherited network FD may survive.
        # Refuse socket-backed standard streams rather than retaining a relay.
        for number in range(3):
            try:
                if os.readlink('/proc/self/fd/' + str(number)).startswith('socket:'):
                    raise RuntimeError('socket-backed standard stream is forbidden')
            except FileNotFoundError:
                pass
        for fd in os.listdir('/proc/self/fd'):
            if int(fd) > 2:
                try:
                    os.close(int(fd))
                except OSError:
                    pass
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(38, 1, 0, 0, 0):
            lib.seccomp_release(ctx)
            raise OSError(ctypes.get_errno(), 'no_new_privs')
        rc = lib.seccomp_load(ctx)
        lib.seccomp_release(ctx)
        if rc:
            raise RuntimeError('seccomp load failed: ' + str(rc))

    return install
