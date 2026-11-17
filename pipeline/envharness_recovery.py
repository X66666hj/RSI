"""Preserve failed quota attempts before restarting their exact experimental slots."""
from contextlib import contextmanager
from pathlib import Path
import json
import os
import shutil
import time
import re
from datetime import datetime, timezone

JOB = Path(__file__).resolve().parent


def quota_marker():
    # Isolated admission workers link this marker to their coordinator. Resolve
    # the link before atomic writes so a quota error stops every lane.
    return (JOB / "quota-exhausted.json").resolve()


def is_http_429(error):
    return "insufficient_quota" in str(error) or bool(re.search(r"\b429\b", str(error)))


def solver_roots():
    roots = [JOB / name for name in ("shared-train", "expansion", "sealed-A", "engineering", "abc")]
    roots.extend((JOB / "admission-workers").glob("*/expansion"))
    return roots


def controller_lock_for_slot(path):
    relative = Path(path).resolve().relative_to(JOB.resolve())
    if relative.parts[0] == "admission-workers":
        return JOB / "admission-workers" / relative.parts[1] / "engineering-worker.lock"
    if relative.parts[0] in {"shared-train", "sealed-A"}:
        return JOB / "worker.lock"
    if relative.parts[0] == "abc":
        return JOB / "abc-worker.lock"
    if relative.parts[0] == "reviews":
        return JOB / "supervisor.lock"
    return JOB / "engineering-worker.lock"


def guard_clone_cleanup(runner, workspace):
    """Remove only newly created partial clones if filesystem copying fails."""
    workspace = Path(workspace).resolve()
    previous = runner.native.clone

    def clone(source, target):
        target = Path(target)
        existed = target.exists() or target.is_symlink()
        try:
            return previous(source, target)
        except Exception:
            resolved = target.resolve()
            if (not existed and not target.is_symlink() and target.exists()
                    and resolved != workspace and resolved.is_relative_to(workspace)):
                shutil.rmtree(target)
            raise

    runner.native.clone = clone


def record_solver_quota(path):
    record = json.loads((Path(path) / "agent.json").read_text())
    if is_http_429(record.get("error", "")):
        marker = quota_marker()
        marker.write_text(json.dumps({"model": "qwen3.8-flash", "reason": "insufficient_quota",
                          "source": str(path), "http_status": 429,
                          "at": datetime.now(timezone.utc).isoformat()}))


def serialize_solver(runner, lane="engineering"):
    """Serialize within one workload; different lanes may run concurrently.

    The runner's original resource slot still limits host-wide concurrency.
    Baseline and expansion admission must never share this workload lock.
    """
    from common import lock
    if lane not in {"baseline", "engineering", "abc", "breadth-a", "breadth-b"}:
        raise ValueError("Unknown Solver lane")
    if getattr(runner, "solver_lane", None) is not None:
        if runner.solver_lane != lane:
            raise ValueError("Runner already assigned to another Solver lane")
        return
    previous = runner.solver_slot

    @contextmanager
    def slot():
        with lock(JOB / ("qwen-solver-" + lane + ".lock")):
            if quota_marker().exists():
                raise RuntimeError("Qwen quota exhausted")
            while True:
                if quota_marker().exists():
                    raise RuntimeError("Qwen quota exhausted")
                context = previous()
                try:
                    number = context.__enter__()
                except RuntimeError as error:
                    if "Insufficient disk reserve" not in str(error):
                        raise
                    time.sleep(1)
                    continue
                break
            try:
                if quota_marker().exists():
                    raise RuntimeError("Qwen quota exhausted")
                yield number
            finally:
                context.__exit__(None, None, None)
    runner.solver_slot = slot
    runner.solver_lane = lane


def archive_failed_slots(include_known_terminal=False, explicit_quota_retry=False, only_idle=False):
    if JOB != (JOB.parent / "unconfigured-legacy-recovery"):
        raise RuntimeError("Unexpected experiment directory")
    mappings = []
    roots = solver_roots()
    for parent in roots:
        for path in sorted(parent.rglob("agent.json")):
            record = json.loads(path.read_text())
            error = str(record.get("error", ""))
            reason = next((value for value in (
                "insufficient_quota",
                *(["Agent exceeded fixed step budget", "Repetitive tool calls detected"]
                  if include_known_terminal else [])) if value in error), None)
            if reason is None and is_http_429(error):
                reason = "http_429"
            quota_failure = reason in {"http_429", "insufficient_quota"}
            terminal_budget = (reason == "Agent exceeded fixed step budget"
                               and (path.parent / "solver-end.json").exists()
                               and (path.parent / "operator-evaluation.json").exists())
            if reason is None or (record.get("finish_reason") != "error" and not terminal_budget):
                continue
            if only_idle:
                if not (path.parent / "solver-end.json").exists() or not (path.parent / "operator-evaluation.json").exists():
                    continue
                from common import lock
                try:
                    with lock(controller_lock_for_slot(path.parent), nonblocking=True):
                        pass
                except BlockingIOError:
                    continue
            source = path.parent.resolve()
            if not source.is_relative_to(JOB) or source == JOB:
                raise ValueError("Refusing unexpected slot")
            launch = json.loads((source / "solver-start.json").read_text())
            process = Path("/proc") / str(launch["pid"])
            if process.exists():
                fields = (process / "stat").read_text().split(") ", 1)[1].split()
                if fields[19] == str(launch["starttime"]) and fields[0] != "Z":
                    raise RuntimeError("Failed slot still has a live process")
            # At most one automatic retry per known terminal failed slot.
            if ((source / "retry-provenance.json").exists()
                    and not (explicit_quota_retry and quota_failure)):
                continue
            destination = JOB / "interrupted-archive" / source.relative_to(JOB)
            if destination.exists():
                if not (explicit_quota_retry and quota_failure):
                    raise RuntimeError("Previous recovery archive already exists")
                destination = (JOB / "interrupted-archive" /
                    ("quota-retry-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")) /
                    source.relative_to(JOB))
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            source.mkdir()
            provenance = {"archived_attempt": str(destination), "reason": "confirmed terminal " + reason,
                          "same_task_model_skill_and_budget": True,
                          "explicit_user_quota_retry": explicit_quota_retry and quota_failure}
            (source / "retry-provenance.json").write_text(json.dumps(provenance, indent=2))
            mappings.append({"source": str(source), "archive": str(destination)})
    (JOB / "recovery-slots-20261105.json").write_text(json.dumps(mappings, indent=2))
    return mappings


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-known-terminal", action="store_true")
    parser.add_argument("--explicit-quota-retry", action="store_true",
                        help="Use only for a user-authorized retry of confirmed terminal quota failures")
    args = parser.parse_args()
    print(json.dumps(archive_failed_slots(args.include_known_terminal, args.explicit_quota_retry)))
