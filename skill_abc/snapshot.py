"""Archive changed submission files and exact before/after manifests."""
from common import *
import stat
import subprocess

TOPS = ('app', 'workspace', 'data', 'build')

def inventory(root):
    root = Path(root)
    values = {}
    def add(path):
        info = path.lstat()
        mode = stat.S_IFMT(info.st_mode)
        record = {'mode': info.st_mode & 0o7777, 'kind': mode}
        if stat.S_ISLNK(info.st_mode):
            record['target'] = os.readlink(path)
        elif stat.S_ISREG(info.st_mode):
            # Do not follow a symlink into the host filesystem.
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'rb') as f:
                h = hashlib.sha256()
                for block in iter(lambda: f.read(1024 * 1024), b''):
                    h.update(block)
            record.update(bytes=info.st_size, sha256=h.hexdigest())
        values[path.relative_to(root).as_posix()] = record
    for top in TOPS:
        base = root / top
        if not os.path.lexists(base):
            continue
        add(base)
        if not stat.S_ISDIR(base.lstat().st_mode):
            continue
        for directory, folders, files in os.walk(base, followlinks=False):
            for name in folders + files:
                add(Path(directory) / name)
    return values

def archive(root, baseline, destination):
    destination = Path(destination)
    after = inventory(root)
    changed = [name for name, value in after.items() if baseline.get(name) != value]
    deleted = sorted(set(baseline) - set(after))
    file_list = destination / 'submission-paths.nul'
    file_list.write_bytes(b''.join(name.encode() + b'\0' for name in changed))
    with (destination / 'archive.log').open('ab') as log:
        result = subprocess.run(['tar', '--no-recursion', '-czf', str(destination / 'submission-changes.tar.gz'),
            '-C', str(root), '--null', '-T', str(file_list)], stdout=log, stderr=log, timeout=180)
    if result.returncode:
        raise RuntimeError('Changed submission archive failed')
    save(destination / 'submission-manifest.json', {'before': baseline, 'after': after,
        'changed': changed, 'deleted': deleted, 'scope': list(TOPS),
        'archive': 'submission-changes.tar.gz', 'unchanged_files_omitted': True,
        'baseline_identity': 'frozen task source + recorded native preparation; exact baseline hashes retained'})
