"""Interactive Max designer using the DeepSeek Harness component tools."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sys
import time
from deepseek_harness import DeepSeekHarness
from diagnosis import read, save
from qwen_provider import harness_options
from envharness_analysis_trace import without_reasoning_event

def run(directory, *, options_factory=harness_options, api_key=None):
    directory = Path(directory).resolve()
    job = Path(__file__).resolve().parent
    info = read(directory / "input.json")
    budget = info["budget"]
    key = (job / ".qwen.key").read_text().strip() if api_key is None else api_key
    from envharness_adaptive import harness_patch
    component_patch = directory / "component.patch.yml"
    save(component_patch, harness_patch(directory))
    options = options_factory(directory, budget, key)
    options["patches"] = (*options["patches"], str(component_patch))
    patch = directory / "qwen-provider.patch.yml"
    configuration = read(patch)
    provider = configuration[1]["insert"][0]["config"]["providers"]["qwen-token-plan"]
    provider["models"][0]["id"] = "qwen3.8-max"
    provider["reasoning"] = "off"
    options["reasoning_effort"] = "off"
    save(patch, configuration)
    options["model"] = "qwen3.8-max"
    events, outcome, error = [], None, None
    started = time.monotonic()
    with (directory / "events.jsonl").open("x", encoding="utf-8") as file:
        def record(notification):
            if notification.method != "session.event":
                return
            event = notification.payload.get("event")
            if not isinstance(event, dict):
                return
            event = without_reasoning_event(event)
            file.write(json.dumps(event, ensure_ascii=False).replace(key, "[REDACTED]") + "\n")
            file.flush()
            events.append(event)
            if sum(e.get("type") == "step/start" for e in events) > budget["max_steps"]:
                raise RuntimeError("Designer exceeded its fixed step budget")
        try:
            with TemporaryDirectory(dir="/dev/shm", prefix="terminal-evo-designer-") as session_home:
                options["dsh_home"] = session_home
                with DeepSeekHarness(**options) as harness:
                    outcome = harness.run((directory / "prompt.txt").read_text(), on_notification=record)
        except Exception as exception:
            error = str(exception).replace(key, "[REDACTED]")
    failures = [e.get("data", {}).get("reason", {}).get("error") for e in events
                if e.get("type") == "turn/end" and e.get("data", {}).get("reason", {}).get("kind") == "error"]
    if failures and not error:
        error = json.dumps(failures, ensure_ascii=False)
    if outcome and outcome.finish_reason == "completed" and not error and not (directory / "component/candidate.json").is_file():
        error = "Designer ended without a validated candidate"
    save(directory / "designer-agent.json", {
        "model": "qwen3.8-max", "enable_thinking": False, "budget": budget,
        "finish_reason": outcome.finish_reason if outcome else None,
        "error": error, "elapsed_seconds": time.monotonic() - started,
        "steps": sum(e.get("type") == "step/start" for e in events),
        "tool_calls": sum(e.get("type") == "tool/call" for e in events),
        "final_response": outcome.final_response if outcome else "",
        "effective_training_positions_added": 0})


if __name__ == "__main__":
    run(sys.argv[1])
