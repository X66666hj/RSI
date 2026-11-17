"""Independent, append-only ABC study support; no old results are modified."""
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RUN = ROOT / 'shared-runtime'
PYTHON = ROOT / 'venv/bin/python'
GROUPS = ('A', 'B', 'C')
BUDGET = {'label': 'skill-abc-64k-90min-240steps', 'max_steps': 240,
          'max_tokens_per_request': 65536, 'agent_wall_timeout_seconds': 5400,
          'agent_sdk_request_timeout_seconds': 5340}

def stamp():
    return datetime.now(timezone.utc).isoformat()

def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)

def immutable(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != text.encode():
            raise ValueError('Immutable artifact conflict: ' + str(path))
    else:
        with path.open('xb') as f:
            f.write(text.encode())

@contextmanager
def lock(path, nonblocking=False):
    import fcntl
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        yield

def followup(key, **record):
    path = RUN / 'followups' / (digest(key)[:24] + '.json')
    if not path.exists():
        save(path, {'id': key, 'at': stamp(), **record})

def strict_pass(r):
    return (r.get('evaluation', {}).get('status') == 'passed' and
            r.get('process_exit_code') == 0 and r.get('finish_reason') == 'completed' and not r.get('error'))

def source_directory(directory):
    directory = Path(directory)
    reference = directory / 'shared-evo1.json'
    return Path(read(reference)['source_directory']) if reference.exists() else directory

def trajectory(directory):
    """Read only agent-visible evidence; never the hidden verifier or result."""
    directory = source_directory(directory)
    events = []
    count = 0
    clipped = 0
    path = directory / 'events.jsonl'
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            e = json.loads(line)
            if e.get('type') not in ('tool/call', 'tool/result'):
                continue
            count += 1
            value = json.dumps(e, ensure_ascii=False)
            if len(value) > 6000:
                value = value[:3000] + '\n[EVENT MIDDLE OMITTED]\n' + value[-3000:]
                clipped += 1
            events.append(value)
    agent = read(directory / 'agent.json') if (directory / 'agent.json').exists() else {}
    final = str(agent.get('final_response') or '')
    events.append('Agent final response (unverified): ' + final)
    text = '\n\n'.join(events)
    shortened = len(text) > 80000
    if shortened:
        text = text[:40000] + '\n[TRAJECTORY MIDDLE OMITTED]\n' + text[-40000:]
    return text, {'events': count, 'clipped_events': clipped, 'trajectory_clipped': shortened,
                  'has_final': bool(final.strip()), 'usable': count > 0,
                  'source_directory': str(directory)}

def validate_skill(text, initial):
    if not isinstance(text, str) or len(text.encode()) > 12000:
        raise ValueError('Skill exceeds 12000 UTF-8 bytes')
    header = initial.split('---', 2)[:2]
    if text.split('---', 2)[:2] != header or '# Terminal task strategy' not in text:
        raise ValueError('Skill identity or main heading changed')
    import re
    if re.search(r'/tests(?:/|\b)|/solution(?:/|\b)|sk-[A-Za-z0-9]{12,}|EVO-[1-4]', text):
        raise ValueError('Skill contains hidden-test paths, credentials, or version IDs')
    return text

def validate_edits(patch, skill):
    if not isinstance(patch, dict) or not isinstance(patch.get('edits'), list):
        raise ValueError('Missing SkillOpt edits')
    if len(patch['edits']) > 2:
        raise ValueError('More than two edits')
    front = skill[:skill.find('---', 3) + 3]
    for e in patch['edits']:
        if not isinstance(e, dict) or e.get('op') not in ('append', 'insert_after', 'replace', 'delete'):
            raise ValueError('Invalid edit operation')
        if not isinstance(e.get('content', ''), str) or len(e.get('content', '').encode()) > 2000:
            raise ValueError('Edit exceeds 2000 UTF-8 bytes')
        if e['op'] != 'append':
            target = e.get('target')
            if not isinstance(target, str) or not target or skill.count(target) != 1 or target in front:
                raise ValueError('Missing, ambiguous, or protected edit target')
    return patch
