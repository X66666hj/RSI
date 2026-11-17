"""One Qwen Solver session with separate Skills and a filtered event archive."""
from hashlib import sha256
from pathlib import Path
import json
import sys
from tempfile import TemporaryDirectory
import time
from diagnosis import read, save, digest
from envharness_analysis_trace import without_reasoning_event
from qwen_provider import harness_options, MODEL, ENABLE_THINKING, REASONING_EFFORT
from deepseek_harness import DeepSeekHarness

ROOT = Path(__file__).resolve().parent.parent
JOB = Path(__file__).resolve().parent
directory = Path(sys.argv[1]).resolve()
assert directory.is_relative_to(JOB)
info = read(directory / 'input.json')
skills = {name: (directory / f'{name.upper()}_SKILL.md').read_text() for name in ('format', 'task')}
combined = skills['format'] + '\n\n' + skills['task']
assert digest(combined) == info['skill_sha256']
budget = info['budget']
from envharness_value_gate import solver_position_block
value_block = solver_position_block(JOB, directory)
if value_block:
    save(directory / 'value-gate-blocked.json', {
        'reason': value_block, 'phase': 'blocked_before_api_request',
        'effective_training_positions_added': 0})
    raise SystemExit(78)
key = (JOB / '.qwen.key').read_text().strip()
events = []
steps = 0
reasoning_blocks_received = 0
source_event_sequences = sha256()
source_tool_events = sha256()
outcome = None
error = None
started = time.monotonic()
with (directory / 'events.jsonl').open('x', encoding='utf-8') as file:
    def record(notification):
        global steps, reasoning_blocks_received
        if notification.method != 'session.event':
            return
        event = notification.payload.get('event')
        if not isinstance(event, dict):
            return
        source_event_sequences.update(f"{event['seq']}\n".encode())
        if event['type'] in ('tool/call', 'tool/result'):
            raw_tool = json.dumps(event, ensure_ascii=False).replace(key, '[REDACTED]') + '\n'
            source_tool_events.update(raw_tool.encode())
        if event['type'] in ('assistant/message', 'assistant/attempt'):
            content = event.get('data', {}).get('message', {}).get('content', [])
            reasoning_blocks_received += sum(block.get('type') == 'reasoning' and bool(block.get('text'))
                                             for block in content)
        event = without_reasoning_event(event)
        file.write(json.dumps(event, ensure_ascii=False).replace(key, '[REDACTED]') + '\n')
        file.flush()
        events.append(event)
        if event.get('type') == 'step/start':
            steps += 1
            if steps > budget['max_steps']:
                raise RuntimeError('Agent exceeded fixed step budget')
    try:
        with TemporaryDirectory(dir='/dev/shm', prefix='terminal-evo-sdk-') as session_home:
            options = harness_options(directory, budget, key)
            options['dsh_home'] = session_home
            with DeepSeekHarness(**options) as harness:
                save(directory / 'skill-loaded.json', {'at': time.time(), 'skill_sha256': digest(combined),
                     'documents': {name: {'sha256': digest(text), 'skill_load_count': 1,
                         'mounted_path': f'/opt/agent-skills/{name}/SKILL.md'} for name,text in skills.items()},
                     'method': 'Both full documents in system prompt and matching isolated read-only files',
                     'enable_thinking': ENABLE_THINKING})
                outcome = harness.run((directory / 'prompt.txt').read_text(), on_notification=record)
    except Exception as exc:
        error = str(exc).replace(key, '[REDACTED]')
failures = [e.get('data', {}).get('reason', {}).get('error') for e in events
            if e['type'] == 'turn/end' and e.get('data', {}).get('reason', {}).get('kind') == 'error']
if failures and not error:
    error = json.dumps(failures, ensure_ascii=False)
usage = {}
for event in events:
    if event['type'] == 'assistant/message':
        for name,value in (event.get('data', {}).get('usage') or {}).items():
            if isinstance(value, (float,int)):
                usage[name] = usage.get(name, 0) + value
save(directory / 'trace-recording.json', {'policy': 'omit-tagged-reasoning-v1',
     'reasoning_blocks_received': reasoning_blocks_received, 'events_received': len(events),
     'source_event_seq_sha256': source_event_sequences.hexdigest(),
     'source_tool_events_sha256': source_tool_events.hexdigest(), 'sdk_session_storage': 'temporary-tmpfs'})
save(directory / 'agent.json', {'model': MODEL, 'enable_thinking': ENABLE_THINKING,
     'reasoning_effort': REASONING_EFFORT,
     'elapsed_seconds': time.monotonic()-started, 'steps': steps, 'usage': usage, 'error': error,
     'tool_calls': sum(e['type']=='tool/call' for e in events),
     'finish_reason': outcome.finish_reason if outcome else None,
     'final_response': outcome.final_response if outcome else '',
     'skill_sha256': digest(combined), 'budget': budget})
