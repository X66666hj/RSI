"""One baseline rollout per original EVO version, routed through frozen admissions."""
from collections import Counter, defaultdict
from hashlib import file_digest
from itertools import count
from pathlib import Path
import json
import re
import shutil
import subprocess
import sys
import time

JOB = Path(__file__).resolve().parent
ROOT = JOB.parent
SWEEP = JOB / "full-train-coverage-thinking-v1"
SCREEN = ROOT / "runs-expanded-screen-20261019"
FROZEN = JOB / "frozen-runtime"
SOURCE = SCREEN / "screen-source"
PROFILES = ROOT / "work"
LEGACY = ROOT / "shared-runtime"
sys.path[:0] = [str(FROZEN), str(ROOT / "skill_abc")]
import run_unique as runtime
from common import lock, stamp
from diagnosis import digest, read, save
import envharness_train_diagnostic_v2 as recorder
from envharness_recovery import guard_clone_cleanup, is_http_429


def file_hash(path):
    with path.open("rb") as f:
        return file_digest(f, "sha256").hexdigest()


def base_route(job, plan, task):
    mapped = plan.get("task_environment_profiles") or {}
    profiles = mapped.get("profiles") or plan.get("profiles") or {}
    tasks = plan.get("tasks", {})
    name = mapped.get("routes", {}).get(task) or (tasks.get(task, {}).get("profile") if isinstance(tasks, dict) else None)
    if name in profiles:
        profile = profiles[name]
        return Path(profile["root"]), job / profile["fingerprint_file"]
    fingerprint = job / "fixed-base-expected.json"
    if not fingerprint.is_file():
        return None
    metadata = read(fingerprint)
    explicit = plan.get("base_profile") or metadata.get("root")
    if explicit:
        return Path(explicit), fingerprint
    version = re.search(r"native base v(\d+)\b", plan.get("fixed_base_policy", ""))
    if version:
        return PROFILES / f"category-dependencies-v{version[1]}-20261019/base", fingerprint
    return None


def admission(route_job, task, plan, screen_only=False):
    folder = route_job / ("tasks" if screen_only else "admitted-baselines") / task
    meta = plan.get("tasks", {}).get(task, {}) if isinstance(plan.get("tasks"), dict) else {}
    case = folder if screen_only else Path(meta.get("screen_job", route_job)) / "tasks" / task
    result = case / "result.json"
    if not result.is_file():
        result = folder / "result.json"
    source = route_job / ("screen-source" if screen_only else "source")
    manifest = route_job / ("screen-source-manifest.json" if screen_only else "source-manifest.json")
    profile = base_route(route_job, plan, task)
    archive = folder / "public-baseline.tar.gz"
    if not (archive.is_file() and result.is_file() and manifest.is_file() and profile
            and profile[0].is_dir() and profile[1].is_file() and (folder / "baseline-manifest.json").is_file()
            and (source / task / "instruction.md").is_file() and (route_job / "screen_worker.py").is_file()):
        return None
    proof = read(result)
    if not proof.get("admitted") or not proof.get("source_unchanged"):
        return None
    archive_hash = meta.get("public_baseline_sha256") or proof.get("baseline_archive_sha256")
    preparation = folder / "preparation.json"
    cwd = meta.get("cwd") or (read(preparation).get("cwd") if preparation.is_file() else None)
    if not archive_hash or not cwd:
        return None
    return {"task": task, "origin": str(route_job), "archive": str(folder), "archive_sha256": archive_hash,
            "admission_receipt": str(result), "admission_receipt_sha256": meta.get("screen_result_sha256") or file_hash(result),
            "source": str(source), "source_manifest": str(manifest), "source_manifest_sha256": file_hash(manifest),
            "profile": str(profile[0]), "fingerprint": str(profile[1]), "fingerprint_sha256": file_hash(profile[1]),
            "inventory_code": str(route_job / "screen_worker.py"), "cwd": cwd}


def inventory():
    families = sorted({m[1] for path in SOURCE.iterdir() if path.is_dir()
                       if (m := re.fullmatch(r"(.+)-EVO-[1-4]", path.name))})
    assert len(families) == 85 and all((SOURCE / f"{f}-EVO-{v}").is_dir() for f in families for v in range(1, 5))
    current = read(FROZEN / "expanded-plan.json")
    held = set(current["test_chains"])
    formal = defaultdict(list)
    for split in sorted(ROOT.glob("runs-evo-*/split-manifest.json")):
        p = read(split)
        for category in p.get("categories", {}).values():
            held.update(category.get("test", []))
            for family in category.get("train", []) + category.get("test", []):
                formal[family].append(split.parent)
    archived = defaultdict(list)
    for pattern in ("runs-expanded-*/tasks/*/result.json", "runs-expanded-*/*/tasks/*/result.json",
                    "runs-expanded-*/*/*/tasks/*/result.json"):
        for result in ROOT.glob(pattern):
            proof = read(result)
            if proof.get("admitted") and proof.get("source_unchanged"):
                archived[result.parent.name].append(result.parent.parent.parent)
    previous = read(ROOT / "runs-expanded-offline-admission-20261021/remaining-85-chain-registry.json")["rows"]
    rows = {}
    for family in families:
        role = "sealed-test" if family in held else "train"
        tasks = [f"{family}-EVO-{v}" for v in range(1, 5)]
        if family in current["train_chains"]:
            rows[family] = {"role": role, "phase": "already_in_current_train_coverage", "tasks": tasks}
            continue
        routes = []
        for task in tasks:
            choices = []
            for job in formal[family]:
                if (job / "plan.json").is_file():
                    route = admission(job, task, read(job / "plan.json"))
                    if route:
                        choices.append(route)
            if not choices:
                for job in archived[task]:
                    if (job / "screen-plan.json").is_file():
                        route = admission(job, task, read(job / "screen-plan.json"), screen_only=True)
                        if route:
                            choices.append(route)
            if choices:
                routes.append(choices[0])
        rows[family] = {"role": role, "phase": "queued" if len(routes) == 4 else "needs_environment_admission",
                        "tasks": tasks, "routes": routes, "previous_environment_state": previous.get(family, {}).get("state")}
    return {"at": stamp(), "families": rows, "original_versions": 340,
            "counts": dict(Counter(row["phase"] for row in rows.values())), "formal_training_positions_added": 0}


def verify_route(route):
    for path_key, hash_key in (("admission_receipt", "admission_receipt_sha256"),
                               ("source_manifest", "source_manifest_sha256"), ("fingerprint", "fingerprint_sha256")):
        if file_hash(Path(route[path_key])) != route[hash_key]:
            raise RuntimeError("Frozen route changed: " + path_key)
    root = Path(route["profile"])
    fingerprint = read(Path(route["fingerprint"]))
    for relative, expected in fingerprint["configuration_identity_sample"].items():
        p = root / relative
        actual = {"symlink": str(p.readlink())} if p.is_symlink() else ({"sha256": file_hash(p), "bytes": p.stat().st_size}
                 if p.is_file() else {"absent": True})
        if actual != expected:
            raise RuntimeError("Dependency profile mismatch: " + relative)
    for relative, expected in fingerprint["installed_python_package_metadata"].items():
        if file_hash(root / relative) != expected:
            raise RuntimeError("Dependency package mismatch: " + relative)
    manifest = read(Path(route["source_manifest"]))["files"]
    task_files = {name: h for name, h in manifest.items() if name.startswith(route["task"] + "/")}
    if not task_files or any(file_hash(Path(route["source"]) / name) != h for name, h in task_files.items()):
        raise RuntimeError("Task source identity mismatch")


def validate_control_reports(engine, task):
    for label in ("negative", "positive"):
        reports = list((engine / "controls" / task / label).rglob("*ctrf*.json"))
        if len(reports) != 1:
            raise RuntimeError("Missing individual Oracle/NOP test statuses")
        statuses = {t["status"] for t in read(reports[0])["results"]["tests"]}
        if (label == "positive" and statuses != {"passed"}) or (label == "negative" and "failed" not in statuses):
            raise RuntimeError("Invalid Oracle/NOP test results")


def materializer(route, engine):
    verify_route(route)
    task = route["task"]
    engine.mkdir(parents=True, exist_ok=True)
    inputs = {"source": Path(route["source"]), "screen_worker.py": Path(route["inventory_code"])}
    inputs.update({name: FROZEN / name for name in ("enter-expanded.py", "control_entry.py",
                                                    "plan.json", "initial-skills.json")})
    for name, source in inputs.items():
        path = engine / name
        if not path.exists():
            path.symlink_to(source)
        if path.resolve() != source.resolve():
            raise RuntimeError("Unexpected materializer input: " + name)
    shutil.copy2(FROZEN / "pilot_runner.py", engine / "pilot_runner.py")
    archives = engine / "admitted-baselines"
    archives.mkdir(exist_ok=True)
    if not (archives / task).exists():
        (archives / task).symlink_to(Path(route["archive"]))
    if (archives / task).resolve() != Path(route["archive"]).resolve():
        raise RuntimeError("Unexpected admission archive")
    runtime.JOB = runtime.runtime.JOB = engine
    runner = runtime.runtime.configure_runner()
    runner.HERE = SWEEP
    guard_clone_cleanup(runner, ROOT / "work" / engine.name)
    runtime.select_template_workspace(runner)
    plan = {"profile": {"root": route["profile"]}, "tasks": {task: {
        "public_baseline_sha256": route["archive_sha256"], "cwd": route["cwd"]}}}
    return runner, plan


def execute(route, role, *, archive_role=None, controls_only=False, reuse_controls=False, skills=None,
            max_429_retries=1):
    task = route["task"]
    work = SWEEP / (archive_role or role) / task
    work.mkdir(parents=True, exist_ok=True)
    state = work / "status.json"
    with lock(work / "worker.lock", nonblocking=not reuse_controls):
        if (work / "result.json").is_file():
            return
        if any(work.glob("attempt-*")):
            raise RuntimeError("Prior dispatch needs inspection")
        readiness = work / "environment-ready.json"
        route_identity = digest(json.dumps(route, sort_keys=True))
        if reuse_controls and (not readiness.is_file() or read(readiness)["route_identity"] != route_identity):
            raise RuntimeError("The Solver route does not match the environment handoff")
        save(state, {"phase": "materializing" if reuse_controls else "original_environment_controls",
                     "task": task, "role": role, "at": stamp()})
        engine = SWEEP / "materializers" / (digest(str(SWEEP.resolve()))[:16] + "-" + task)
        runner, plan = materializer(route, engine)
        while shutil.disk_usage(ROOT).free < 24 * 2**30:
            time.sleep(15)
        templates = {}
        try:
            if not reuse_controls:
                runtime.prepare_tasks(runner, plan, [task], run_controls=True)
                validate_control_reports(engine, task)
            if controls_only:
                save(readiness, {"at": stamp(), "route_identity": route_identity, "control_engine": str(engine)})
                save(state, {"phase": "environment_ready", "task": task, "role": role, "at": stamp()})
                return
            templates = runtime.prepare_tasks(runner, plan, [task], run_controls=False)
            for attempt in count(1):
                if (JOB / "quota-exhausted.json").exists():
                    raise RuntimeError("Shared quota marker set")
                save(state, {"phase": "running", "task": task, "role": role, "attempt": attempt, "at": stamp()})
                trial = runtime.attempt_one(
                    runner, plan, templates[task], work / f"attempt-{attempt:03}", "S0", task,
                    read(FROZEN / "initial-skills.json") if skills is None else skills, work / "runtime-status.json")
                evaluation = read(trial / "operator-evaluation.json")
                if not (trial / "solver-end.json").exists():
                    raise RuntimeError("Unknown Solver outcome")
                agent = read(trial / "agent.json") if (trial / "agent.json").is_file() else read(trial / "result.json")
                receipt = trial / "trace-recording.json"
                audit = recorder.verify_recording(trial) if receipt.is_file() else None
                if audit:
                    save(trial / "trace-audit.json", audit)
                if (audit and (agent.get("model") != "qwen3.8-flash" or agent.get("enable_thinking") is not True)
                        or evaluation["execution_complete"] and not audit):
                    raise RuntimeError("Missing filtered thinking receipt")
                if (is_http_429(agent.get("error")) and (max_429_retries is None or attempt <= max_429_retries)
                        and (trial / "submission-changes.tar.gz").is_file()):
                    save(state, {"phase": "cooling_429", "task": task, "role": role, "failed_trial": str(trial),
                                 "retry_delay_seconds": 60, "at": stamp()})
                    time.sleep(60)
                    continue
                category = "http_429" if is_http_429(agent.get("error")) else evaluation["termination_reason"]
                result = {"task": task, "role": role, "category": category, "trial": str(trial), "trace_audit": audit,
                          "score": evaluation["evaluation"].get("reward"), "execution_complete": evaluation["execution_complete"],
                          "eligible_for_attribution": role == "train" and category == "answer_failed_valid_tests",
                          "formal_training_positions_added": 0, "at": stamp()}
                save(work / "result.json", result)
                save(state, {**result, "phase": "finished"})
                break
        finally:
            runtime.delete_templates(runner, templates or {task: (ROOT / "work" / engine.name / "templates" / (task + "-template"), None, None)})


def run_family(family, row):
    for route in row["routes"]:
        task = route["task"]
        try:
            execute(route, row["role"])
        except BlockingIOError:
            # Another lane holds this task's worker lock.
            continue
        except Exception as exc:
            work = SWEEP / row["role"] / task
            save(work / "status.json", {"phase": "needs_inspection" if any(work.glob("attempt-*")) else "environment_blocked",
                 "task": task, "family": family, "role": row["role"], "error": str(exc), "at": stamp()})


def worker(index):
    for family, row in read(SWEEP / "queue.json")[str(index)]:
        run_family(family, row)


def extend_worker(index):
    original = read(SWEEP / "queue.json")[index]
    preferred = {"openssl-selfsigned-cert", "portfolio-optimization"}
    train = [(i, family, row) for i, (family, row) in enumerate(original) if row["role"] == "train"]
    state = SWEEP / f"overflow-{index}-status.json"
    for i, family, row in sorted(train, key=lambda item: (item[1] not in preferred, -item[0])):
        # Keep a buffer ahead of the original worker. Both routes also use
        # the same task directory and exclusive execution lock.
        # The earliest unfinished family locates the original lane even
        # when an overflow lane has already completed a later family.
        head = next((j for j, _, item in train if any(
            not (SWEEP / "train" / task / "status.json").exists() for task in item["tasks"])), len(original))
        if i <= head + 2 or any((SWEEP / "train" / task).exists() for task in row["tasks"]):
            continue
        save(state, {"at": stamp(), "phase": "running", "family": family})
        run_family(family, row)
    save(state, {"at": stamp(), "phase": "finished"})


def extend():
    with lock(SWEEP / "launch.lock", nonblocking=True):
        receipt = SWEEP / "parallel-extension.json"
        if receipt.exists():
            print(json.dumps(read(receipt)))
            return
        free = shutil.disk_usage(ROOT).free
        if free < 34 * 2**30:
            raise RuntimeError("Parallel extension needs at least 34 GiB free")
        lanes = {}
        for index in read(SWEEP / "queue.json"):
            with (SWEEP / f"overflow-{index}.log").open("ab") as log:
                process = subprocess.Popen([sys.executable, "-B", "-u", __file__, "--overflow-worker", index],
                                           stdout=log, stderr=log, start_new_session=True)
            lanes[index] = process.pid
        result = {"at": stamp(), "pids": lanes, "free_gib": round(free / 2**30, 1), "role": "train",
                  "formal_training_positions_added": 0}
        save(receipt, result)
        print(json.dumps(result))


def launch():
    SWEEP.mkdir(exist_ok=True)
    with lock(SWEEP / "launch.lock", nonblocking=True):
        if (SWEEP / "launch.json").exists():
            print(json.dumps(read(SWEEP / "launch.json")))
            return
        recorder.PILOT = SWEEP
        recorder.prepare_agent()
        registry = inventory()
        save(SWEEP / "inventory.json", registry)
        order = {"regex-chess": 0, "sqlite-db-truncate": 1}
        ready = sorted(((f, row) for f, row in registry["families"].items() if row["phase"] == "queued"),
                       key=lambda entry: (entry[1]["role"] == "sealed-test", order.get(entry[0], 2), entry[0]))
        queue = {str(i): ready[i::2] for i in range(2)}
        save(SWEEP / "queue.json", queue)
        lanes = {}
        for index, tasks in queue.items():
            if not tasks:
                continue
            with (SWEEP / f"lane-{index}.log").open("ab") as log:
                process = subprocess.Popen([sys.executable, "-B", "-u", __file__, "--worker", index],
                                           stdout=log, stderr=log, start_new_session=True)
            lanes[index] = {"pid": process.pid, "families": [family for family, _ in tasks]}
        result = {"at": stamp(), "counts": registry["counts"], "roles_queued": dict(Counter(row["role"] for _, row in ready)),
                  "lanes": lanes, "original_versions": registry["original_versions"], "formal_training_positions_added": 0}
        save(SWEEP / "launch.json", result)
        print(json.dumps(result))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        worker(sys.argv[2])
    elif len(sys.argv) == 3 and sys.argv[1] == "--overflow-worker":
        extend_worker(sys.argv[2])
    elif sys.argv[1:] == ["--extend"]:
        extend()
    else:
        launch()
