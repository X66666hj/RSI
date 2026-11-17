"""One Qwen Solver session with separate Skills and a filtered event archive."""
import argparse
from hashlib import sha256
from pathlib import Path
import json
import sys
from tempfile import TemporaryDirectory
import time
from diagnosis import read, save, digest

JOB = Path(__file__).resolve().parent


def prepare(directory, designer, baseline):
    """Start a candidate session with the archived original's Skill and budget."""
    from envharness_adaptive import coverage, solver_spec

    designer = designer.resolve(strict=True)
    baseline = baseline.resolve(strict=True)
    spec = read(designer / "component-session.json")
    original = read(baseline / "input.json")
    task = spec["task"]
    if original["task_id"] != task or not read(baseline / "operator-evaluation.json")["execution_complete"]:
        raise ValueError("Candidate requires a completed original baseline for the same task")
    candidate = designer / "component/candidate.json"
    skills = {name: (baseline / f"{name.upper()}_SKILL.md").read_text(encoding="utf-8")
              for name in ("format", "task")}
    combined = skills["format"] + "\n\n" + skills["task"]
    if digest(combined) != original["skill_sha256"]:
        raise ValueError("Baseline Skill identity changed")
    directory.mkdir(parents=True, exist_ok=False)
    save(directory / "input.json", {key: original[key] for key in ("task_id", "budget", "skill_sha256")})
    save(directory / "component-session.json", {
        "role": "solver", "task": task, "candidate": read(candidate),
        "reproductions": spec["reproductions"], "designer": str(designer),
        "candidate_sha256": coverage.file_hash(candidate), "original_baseline": str(baseline),
    })
    solver_spec(directory, task)
    for name, text in skills.items():
        (directory / f"{name.upper()}_SKILL.md").write_text(text, encoding="utf-8")
    save(directory / "native.patch.yml", [
        {"id": "system-prompt", "config": {"includeHarnessIdentity": False,
         "includeRuntimeContext": False, "personaPrefix": (
             "Complete the current public Linux task through the terminal_evo tools. "
             "Use observe for the task, bash for the workspace and submit when finished. "
             "Follow tool instructions if a continuation or another task is available. "
             "The active Skill documents are also mounted under /opt/agent-skills.\n\n" + combined)}},
    ])
    (directory / "prompt.txt").write_text(
        "Read the current task with observe, solve it in its workspace and submit each required stage.",
        encoding="utf-8")


def run(directory, *, options_factory=None, api_key=None):
    directory = Path(directory).resolve()
    if not directory.is_relative_to(JOB):
        raise ValueError("Solver session is outside the experiment")
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
    from deepseek_harness import DeepSeekHarness
    from envharness_analysis_trace import without_reasoning_event
    from qwen_provider import harness_options, MODEL, ENABLE_THINKING, REASONING_EFFORT

    options_factory = options_factory or harness_options
    key = (JOB / '.qwen.key').read_text().strip() if api_key is None else api_key
    component_session = directory / 'component-session.json'
    thinking = ENABLE_THINKING
    reasoning = REASONING_EFFORT
    if component_session.is_file():
        from envharness_adaptive import harness_patch, solver_spec
        solver_spec(directory, info['task_id'])
        component_patch = directory / 'component.patch.yml'
        save(component_patch, harness_patch(directory))
        thinking, reasoning = True, 'high'
    else:
        component_patch = None
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
            nonlocal steps, reasoning_blocks_received
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
                options = options_factory(directory, budget, key)
                if component_patch is not None:
                    options['patches'] = (*options['patches'], str(component_patch))
                    options['reasoning_effort'] = reasoning
                    provider_patch = directory / 'qwen-provider.patch.yml'
                    configuration = read(provider_patch)
                    route = configuration[1]['insert'][0]['config']['providers']['qwen-token-plan']
                    route['reasoning'] = reasoning
                    save(provider_patch, configuration)
                options['dsh_home'] = session_home
                with DeepSeekHarness(**options) as harness:
                    save(directory / 'skill-loaded.json', {'at': time.time(), 'skill_sha256': digest(combined),
                         'documents': {name: {'sha256': digest(text), 'skill_load_count': 1,
                             'mounted_path': f'/opt/agent-skills/{name}/SKILL.md'} for name,text in skills.items()},
                         'method': 'Both full documents in system prompt and matching isolated read-only files',
                         'enable_thinking': thinking})
                    outcome = harness.run((directory / 'prompt.txt').read_text(), on_notification=record)
        except Exception as exc:
            error = str(exc).replace(key, '[REDACTED]')
    failures = [e.get('data', {}).get('reason', {}).get('error') for e in events
                if e['type'] == 'turn/end' and e.get('data', {}).get('reason', {}).get('kind') == 'error']
    if failures and not error:
        error = json.dumps(failures, ensure_ascii=False)
    if component_patch is not None:
        grade_path = directory / 'component/grade.json'
        completed = outcome is not None and outcome.finish_reason == 'completed' and not error
        if completed and not grade_path.is_file():
            error = 'Component episode ended without all submissions'
        grade = read(grade_path) if completed and grade_path.is_file() else None
        reward = str(grade['reward']) if grade else None
        save(directory / 'operator-evaluation.json', {
            'execution_complete': completed and grade is not None,
            'environment_valid': grade is not None,
            'evaluation': {'reward': reward, 'stages': grade['stages'] if grade else []},
            'termination_reason': ('none' if reward == '1' else 'answer_failed_valid_tests'
                                   if reward == '0' else 'execution_or_scoring_failure'),
            'operator_only': True, 'reflection_input': False})
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
    save(directory / 'agent.json', {'model': MODEL, 'enable_thinking': thinking,
         'reasoning_effort': reasoning,
         'elapsed_seconds': time.monotonic()-started, 'steps': steps, 'usage': usage, 'error': error,
         'tool_calls': sum(e['type']=='tool/call' for e in events),
         'finish_reason': outcome.finish_reason if outcome else None,
         'final_response': outcome.final_response if outcome else '',
         'skill_sha256': digest(combined), 'budget': budget})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--designer', type=Path)
    parser.add_argument('--baseline', type=Path)
    args = parser.parse_args()
    if bool(args.designer) != bool(args.baseline):
        parser.error('--designer and --baseline must be provided together')
    if args.designer:
        prepare(args.directory, args.designer, args.baseline)
    run(args.directory)
