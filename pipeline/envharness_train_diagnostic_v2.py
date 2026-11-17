"""Collect fresh Train-only Solver evidence in a separate diagnostic batch."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

JOB = Path(__file__).resolve().parent
ROOT = JOB.parent
FROZEN = JOB / "frozen-runtime"
PILOT = JOB / "train-error-collection-thinking-v2"
BREADTH = JOB / "train-coverage-thinking-v1"
THINKING = JOB / "solver-thinking-pilot-v1"
sys.path.insert(0, str(FROZEN))
import run_unique as runtime
from diagnosis import read, save, digest, call_id
from common import lock, stamp
from envharness_analysis_trace import contains_reasoning
from envharness_recovery import guard_clone_cleanup, serialize_solver, is_http_429


def prepare_agent():
    """Copy the pinned recorder and provider into the isolated rollout workspace."""
    manifest = read(JOB / "recorder-lock.json")
    PILOT.mkdir(parents=True, exist_ok=True)
    for source, target in manifest["files"].items():
        original, destination = JOB / source, PILOT / target
        expected = manifest["sha256"][source]
        if digest(original.read_bytes()) != expected:
            raise RuntimeError("Recorder source identity changed: " + source)
        if not destination.exists():
            shutil.copy2(original, destination)
        if digest(destination.read_bytes()) != expected:
            raise RuntimeError("Existing recorder workspace differs: " + target)
    key = PILOT / ".qwen.key"
    if not key.is_symlink():
        key.symlink_to(JOB / ".qwen.key")
    if key.resolve() != (JOB / ".qwen.key").resolve() or not key.is_file():
        raise RuntimeError("Configure pipeline/.qwen.key before running paid sessions")


def verify_recording(trial):
    receipt = read(trial / "trace-recording.json")
    sequence_hash, tool_hash = sha256(), sha256()
    seen, calls, results = set(), set(), set()
    request_efforts = set()
    with (trial / "events.jsonl").open("rb") as archive:
        for line in archive:
            event = json.loads(line)
            seq = event["seq"]
            if seq in seen or contains_reasoning(event):
                raise RuntimeError("Duplicate event number or tagged reasoning in the archive")
            seen.add(seq)
            sequence_hash.update(f"{seq}\n".encode())
            kind = event["type"]
            if kind in ("tool/call", "tool/result"):
                tool_hash.update(line)
                (calls if kind == "tool/call" else results).add(call_id(event))
            if kind == "request/header":
                config = event["data"]["header"]["config"]
                request_efforts.add(config.get("reasoningEffort"))
    checks = {"event_sequence_preserved": len(seen) == receipt["events_received"] and
              sequence_hash.hexdigest() == receipt["source_event_seq_sha256"],
              "tool_events_preserved": tool_hash.hexdigest() == receipt["source_tool_events_sha256"],
              "no_tagged_reasoning_in_archive": True,
              "native_session_outside_archive": receipt["sdk_session_storage"] == "temporary-tmpfs" and
              not (trial / "home").exists()}
    if not all(checks.values()):
        raise RuntimeError("Archived trace does not match the recorder receipt")
    return {**checks, "events": len(seen), "tool_calls_with_results": len((calls & results) - {None}),
            "reasoning_blocks_received": receipt["reasoning_blocks_received"],
            "request_reasoning_efforts": sorted(str(v) for v in request_efforts)}


def run_task(task, lane, breadth=False):
    if not breadth and (PILOT / "superseded-by-breadth.json").exists():
        return
    protocol = read(JOB / "envharness_evo_chain_protocol.json")
    if digest((FROZEN / "expanded-plan.json").read_bytes()) != protocol["source_manifest_sha256"]:
        raise RuntimeError("Frozen source manifest mismatch")
    runtime.JOB = FROZEN
    plan = runtime.validate()
    selected = {f"{family}-EVO-{version}" for family, versions in
                protocol["train_versions"].items() for version in versions}
    if breadth:
        holdout = set(plan["test_chains"]) | set(protocol["test_versions"])
        extra_families = set(plan["train_chains"]) - set(protocol["train_versions"]) - holdout
        eligible = {f"{family}-EVO-{v}" for family in extra_families for v in (1, 2, 3, 4)}
        if task not in eligible or not eligible.issubset(plan["tasks"]):
            raise ValueError("Coverage sampling requires distinct, complete frozen Train chains")
    else:
        if task not in selected:
            raise ValueError("Diagnostic task must be in the selected Train set")
        for label, expected in (("positive", "1"), ("negative", "0")):
            control = read(JOB / "environment" / task / f"{label}.json")
            if control.get("reward") != expected:
                raise RuntimeError("Existing original environment control invalid: " + label)
    PILOT.mkdir(exist_ok=True)
    with lock(PILOT / "setup.lock"):
        prepare_agent()
    work = PILOT / task
    work.mkdir(exist_ok=True)
    # The live-check lane and the later controller may offer the same task.
    # Queue behind the lane first, then recheck the task receipt before starting.
    with lock(PILOT / f"{lane}.lock"), lock(work / "worker.lock", nonblocking=True):
        if (work / "result.json").exists():
            return
        if any(work.glob("attempt-*")):
            raise RuntimeError("Inspect archived attempt before restarting this task")
        materializer = PILOT / f"{PILOT.name}-{task}"
        materializer.mkdir(exist_ok=True)
        for name in ("source", "admitted-baselines", "screen_worker.py", "enter-expanded.py",
                     "control_entry.py", "pilot_runner.py", "plan.json", "initial-skills.json"):
            source, target = FROZEN / name, materializer / name
            if not target.is_symlink() and not target.exists():
                target.symlink_to(source)
            if target.resolve() != source.resolve():
                raise RuntimeError("Unexpected materializer input: " + name)
        runtime.JOB = runtime.runtime.JOB = materializer
        runner = runtime.runtime.configure_runner()
        runner.HERE = PILOT
        guard_clone_cleanup(runner, ROOT / "work" / materializer.name)
        serialize_solver(runner, lane=lane)
        runtime.select_template_workspace(runner)
        state = work / "status.json"
        if breadth:
            save(state, {"phase": "original_environment_controls", "task": task, "at": stamp()})
            try:
                runtime.prepare_tasks(runner, plan, [task], run_controls=True)
                if not runtime.admitted_controls(task):
                    raise RuntimeError("Original Oracle/NOP controls did not pass")
            except Exception as exc:
                template = runner.native.WORK / (task + "-template")
                runtime.delete_templates(runner, {task: (template, None, None)})
                report = {"task": task, "category": "environment_control_failed", "error": str(exc),
                          "paid_model_rollouts": 0, "training_positions_added": 0, "at": stamp()}
                save(work / "result.json", report)
                save(state, {**report, "phase": "finished"})
                return
        save(state, {"phase": "materializing", "task": task, "at": stamp()})
        templates = runtime.prepare_tasks(runner, plan, [task], run_controls=False)
        skills = read(FROZEN / "initial-skills.json")
        try:
            for attempt in (1, 2):
                phase = work / f"attempt-{attempt:03d}"
                save(state, {"phase": "running", "task": task, "attempt": attempt, "at": stamp()})
                trial = runtime.attempt_one(runner, plan, templates[task], phase, "S0", task,
                                            skills, work / "runtime-status.json")
                evaluation = read(trial / "operator-evaluation.json")
                if not (trial / "solver-end.json").exists():
                    raise RuntimeError("Unknown Solver exit. Preserve attempt for inspection.")
                agent = read(trial / "agent.json") if (trial / "agent.json").exists() else read(trial / "result.json")
                trace_audit = None
                if (trial / "trace-recording.json").exists():
                    if agent.get("model") != "qwen3.8-flash" or agent.get("enable_thinking") is not True:
                        raise RuntimeError("Actual diagnostic Solver receipt did not enable thinking")
                    trace_audit = verify_recording(trial)
                    save(trial / "trace-audit.json", trace_audit)
                elif evaluation["execution_complete"]:
                    raise RuntimeError("Completed Solver is missing the recorder receipt")
                if is_http_429(agent.get("error")) and attempt == 1:
                    save(state, {"phase": "cooling_429", "task": task, "attempt": attempt,
                                 "failed_trial": str(trial), "retry_delay_seconds": 60, "at": stamp()})
                    time.sleep(60)
                    continue
                category = evaluation["termination_reason"]
                if is_http_429(agent.get("error")):
                    category = "http_429"
                save(work / "result.json", {"task": task, "trial": str(trial), "attempt": attempt,
                     "category": category, "operator_evaluation": evaluation,
                     "trace_audit": trace_audit,
                     "steps": agent.get("steps"), "tool_calls": agent.get("tool_calls"),
                     "elapsed_seconds": agent.get("elapsed_seconds"), "usage": agent.get("usage"),
                     "error": agent.get("error"), "at": stamp(), "training_positions_added": 0,
                     "skill_updates_added": 0, "candidate_admissions_added": 0})
                save(state, {"phase": "finished", "task": task, "category": category, "at": stamp()})
                break
        finally:
            runtime.delete_templates(runner, templates)


def collect_after_check(task):
    PILOT.mkdir(exist_ok=True)
    with lock(PILOT / "controller.lock", nonblocking=True):
        state = PILOT / "batch-status.json"
        if (PILOT / "collection-launch.json").exists():
            raise RuntimeError("Collection has already been dispatched")
        protocol = read(JOB / "envharness_evo_chain_protocol.json")
        tasks = [f"{family}-EVO-{version}" for family, versions in
                 protocol["train_versions"].items() for version in versions]
        save(state, {"phase": "checking_one_train_trace", "pilot_task": task,
                     "planned_total": len(tasks), "at": stamp()})
        run_task(task, "baseline")
        result = read(PILOT / task / "result.json")
        audit = result.get("trace_audit") or {}
        if (not audit.get("reasoning_blocks_received") or not audit.get("tool_calls_with_results")
                or audit.get("request_reasoning_efforts") != ["high"]):
            raise RuntimeError("Pilot did not demonstrate live thinking and paired tool evidence")
        save(PILOT / "pilot-verified.json", result)
        remaining = [item for item in tasks if item != task]
        workers = {}
        for index, lane in enumerate(("baseline", "engineering")):
            queue = remaining[index::2]
            if queue:
                with (PILOT / f"{lane}.log").open("ab") as log:
                    process = subprocess.Popen([sys.executable, "-B", "-u", str(Path(__file__).resolve()),
                                                lane, *queue], stdout=log, stderr=log, start_new_session=True)
                workers[lane] = (process, queue)
        receipt = {"phase": "collecting", "pilot_task": task, "planned_total": len(tasks),
                   "training_positions_added": 0, "at": stamp(), "lanes": {
                       lane: {"pid": proc.pid, "tasks": queue} for lane, (proc, queue) in workers.items()}}
        save(PILOT / "collection-launch.json", receipt)
        save(state, receipt)
        exits = {lane: proc.wait() for lane, (proc, _) in workers.items()}
        results = [read(PILOT / item / "result.json") for item in tasks
                   if (PILOT / item / "result.json").exists()]
        save(state, {"phase": "finished" if len(results) == len(tasks) and not any(exits.values()) else "needs_inspection",
                     "results": results, "planned_total": len(tasks), "lane_exit_codes": exits,
                     "training_positions_added": 0, "at": stamp()})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collect-after-check", action="store_true")
    parser.add_argument("--breadth", action="store_true")
    parser.add_argument("lane", choices=("baseline", "engineering", "breadth-a", "breadth-b"))
    parser.add_argument("tasks", nargs="+")
    args = parser.parse_args()
    if args.breadth:
        if args.collect_after_check or args.lane not in ("breadth-a", "breadth-b"):
            parser.error("Coverage sampling needs its own lane")
        prior = PILOT / "pilot-prefix-verified.json"
        audit = read(prior) if prior.is_file() else {}
        if (not audit.get("event_numbers_exact_match") or not audit.get("tool_events_exact_match")
                or not audit.get("reasoning_bodies_checked") or audit.get("tagged_reasoning_in_archive") != 0):
            parser.error("The verified Train trace recorder is required")
        PILOT = BREADTH
    elif args.lane not in ("baseline", "engineering"):
        parser.error("Repeated diagnostics need a diagnostic lane")
    elif (PILOT / "superseded-by-breadth.json").exists():
        sys.exit(0)
    if args.collect_after_check:
        if len(args.tasks) != 1 or args.lane != "baseline":
            parser.error("The collection check requires exactly one pilot task in the baseline lane")
        try:
            collect_after_check(args.tasks[0])
        except Exception as exc:
            save(PILOT / "controller-error.json", {"type": type(exc).__name__, "error": str(exc), "at": stamp()})
            raise
        sys.exit(0)
    if not args.breadth and not (PILOT / "pilot-verified.json").is_file():
        prefix = PILOT / "pilot-prefix-verified.json"
        audit = read(prefix) if prefix.is_file() else {}
        if (args.lane != "engineering" or not audit.get("event_numbers_exact_match")
                or not audit.get("tool_events_exact_match") or not audit.get("reasoning_bodies_checked")
                or audit.get("tagged_reasoning_in_archive") != 0
                or not audit.get("paired_tool_calls") or audit.get("request_reasoning_effort") != "high"):
            parser.error("The live Train recorder check must pass before collection")
    for task in args.tasks:
        try:
            run_task(task, args.lane, breadth=args.breadth)
        except Exception as exc:
            print(json.dumps({"task": task, "error": str(exc)}, ensure_ascii=False), flush=True)
            raise
