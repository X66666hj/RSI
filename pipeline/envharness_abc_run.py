"""Run independent EVO training chains with matched B/C practice and a shared holdout."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import re
import subprocess
import sys
import time

import envharness_full_coverage as sweep
from envharness_expand import read, save, ask, QwenClient
from envharness.infra.llm import Message
from common import lock, stamp
from diagnosis import digest, validate_package, validate_package_fail_closed, DIAGNOSIS_PROMPT
from envharness_analysis_trace import analysis_trace
from envharness_abc_preflight import paired_training_plan
from envharness_jev import MODEL as JEV_MODEL, check_transport, decide_skill_edit
import optimizer

def qwen_chat(directory, name, system, payload, max_tokens=16384):
    # B/C share the same updater, prompt and edit limits.
    system = system.replace("distinct-task", "independent-practice")
    return QwenClient(Path(directory) / "calls" / name).chat([
        Message(role="system", content=system),
        Message(role="user", content=json.dumps(payload, ensure_ascii=False))]).content


def practice_checks(trial):
    reports = list(trial.rglob("ctrf.json"))
    if len(reports) != 1:
        return []
    checks = []
    for test in read(reports[0])["results"]["tests"]:
        name = test["name"].rsplit("::", 1)[-1].split("[", 1)[0]
        if re.fullmatch(r"test_[a-zA-Z0-9_]{1,100}", name):
            checks.append({"check": name, "status": test["status"]})
    return checks


def packet(path, route, destination, *, previous=None, excluded_findings=None, verified_finding=None,
           replay_evidence=None):
    task = route["task"]
    trace = analysis_trace(path)
    public = (Path(route["source"]) / task / "instruction.md").read_text(encoding="utf-8")
    result = read(path / "operator-evaluation.json")
    if not result["execution_complete"] or result["evaluation"].get("reward") not in ("0", "1"):
        raise ValueError("Practice execution or scoring incomplete")
    reward = int(result["evaluation"]["reward"])
    outcomes = practice_checks(path)
    file = destination / "diagnosis.json"
    if verified_finding is not None:
        if reward != 0 or verified_finding.get("status") != "located":
            raise ValueError("A verified failure finding requires a completed failed practice")
        package = {"trajectory_id": trace["trajectory_id"], "findings": [verified_finding]}
    elif file.exists():
        package = read(file)
        if package["trajectory_id"] != trace["trajectory_id"]:
            raise ValueError("Practice trace changed")
    elif previous is not None and (previous / "diagnosis.json").is_file():
        package = read(previous / "diagnosis.json")
        if package["trajectory_id"] != trace["trajectory_id"] or package["hidden_grader_used"] is not False:
            raise ValueError("Archived diagnosis identity does not match reused practice")
    else:
        evidence_scope = (
            "Read the public task, public trajectory, and controller-provided training check statuses.\n"
            "No test implementation, expected answer, oracle or external tools are available."
        )
        diagnosis_prompt = DIAGNOSIS_PROMPT.replace(
            "Read only the supplied public task and this one full agent-visible trajectory.\n"
            "No hidden tests, oracle, grader result, prior trajectory or outside tools exist.",
            evidence_scope,
        )
        if reward:
            diagnosis_prompt = diagnosis_prompt.split("Describe public requirements", 1)[0] + (
                "For this scored success, return up to three distinct observations of useful "
                "decisions whose effects were observed. Use claim_type observation and exact "
                "citations for the decision/action and its result. Check that the action survived "
                "through submission. The score does not establish that every action was helpful "
                "or necessary. An empty findings list is valid."
            )
        else:
            diagnosis_prompt += (
                "\nRelate the proposed causes to the failed training checks. Treat their names as "
                "outcome labels only. A recovered tool error unrelated to a scored failure is "
                "insufficient evidence for a repair."
            )
        evidence = {"public_task": public, "trajectory": trace,
                    "training_reward": reward, "check_outcomes": outcomes,
                    "confirmed_replays": replay_evidence or []}
        parsed = ask(destination / "diagnose", diagnosis_prompt, evidence)
        package, audit = validate_package_fail_closed(parsed, trace)
        save(destination / "reference-audit.json", audit)
        review = ask(destination / "review",
            "Review these findings against the training outcomes and public trace. Return JSON "
            "with accepted_finding_ids and reasons. For a success, accept only an executed "
            "decision with an observed effect, and avoid inferring necessity. For a failure, "
            "accept only findings tied to its scored failure, accounting for subsequent "
            "corrections. Require a visible final-state link for causal claims. "
            "Check labels alone do not confirm causes. Do not infer hidden test contents.",
            {**evidence, "findings": package["findings"]})
        ids = review.get("accepted_finding_ids")
        valid_ids = {f["finding_id"] for f in package["findings"]}
        if not isinstance(ids, list) or not set(ids).issubset(valid_ids):
            raise ValueError("Invalid practice review")
        for finding in package["findings"]:
            if finding["finding_id"] not in ids or (reward and finding.get("claim_type") != "observation"):
                finding.update(status="unresolved", evidence=[])
        package = validate_package(package, trace)
    if not file.exists() or excluded_findings or verified_finding is not None:
        if excluded_findings:
            for finding in package["findings"]:
                if finding["finding_id"] in excluded_findings:
                    finding.update(status="unresolved", evidence=[], uncertainty=excluded_findings[finding["finding_id"]])
            save(destination / "operator-evidence-review.json", excluded_findings)
        package.pop("package_sha256", None)
        package = validate_package(package, trace)
        if previous is not None and (previous / "diagnosis.json").is_file():
            save(destination / "reused-from.json", {"diagnosis": str(previous / "diagnosis.json")})
        save(file, package)
    return {"task_id": task + "/" + trace["trajectory_id"], "public_task": public,
            "trajectory": trace, "diagnosis": package, "training_reward": reward, "check_outcomes": outcomes,
            "confirmed_replays": replay_evidence or [],
            **({"verified_failure_mechanism": verified_finding["problem_or_observation"]}
               if verified_finding is not None else {})}


def freeze(path, payload):
    if path.is_file():
        if read(path) != payload:
            raise ValueError(f"Frozen input changed: {path.name}")
    else:
        save(path, payload)


def skill_hash(skills):
    return digest(skills["format"] + "\n\n" + skills["task"])


def eligible_trial(trial, task, skills):
    trial = Path(trial)
    agent = read(trial / "agent.json")
    info = read(trial / "input.json")
    evaluation = read(trial / "operator-evaluation.json")
    if (info["task_id"] != task or info["skill_sha256"] != skill_hash(skills)
            or agent["model"] != "qwen3.8-flash" or agent["enable_thinking"] is not True
            or not evaluation["execution_complete"] or evaluation["evaluation"]["reward"] not in ("0", "1")):
        raise ValueError(f"Incompatible or incomplete Solver position: {trial}")
    return trial


def score_summary(trial, expanded):
    evaluation = read(trial / "operator-evaluation.json")["evaluation"]
    agent = read(trial / "agent.json")
    summary = {"trial": str(trial), "reward": int(evaluation["reward"]),
               "usage": agent.get("usage"), "elapsed_seconds": agent.get("elapsed_seconds")}
    if expanded:
        reports = list(trial.rglob("ctrf.json"))
        if len(reports) != 1:
            raise ValueError("Expanded C practice requires its original and variant checks")
        tests = read(reports[0])["results"]["tests"]
        groups = {False: [], True: []}
        for check in tests:
            groups[check["name"].split("::")[-1].startswith("test_variant_")].append(check["status"])
        if not all(groups.values()):
            raise ValueError("Expanded C practice omitted a scoring layer")
        original, variant = (int(all(status == "passed" for status in groups[k])) for k in (False, True))
        if summary["reward"] != (original & variant):
            raise ValueError("Variant combined reward disagrees with test results")
        summary.update(original_reward=original, variant_reward=variant)
    return summary


def run_position(directory):
    request = read(directory / "request.json")
    route, skills = request["route"], request["skills"]
    sweep.recorder.PILOT = directory
    sweep.recorder.prepare_agent()
    sweep.SWEEP = directory
    configure = sweep.runtime.runtime.configure_runner
    slot = request["slot"]

    @contextmanager
    def take_slot():
        with lock(sweep.LEGACY / "locks" / f"solver-{slot}.lock"):
            yield slot

    def configure_in_slot():
        runner = configure()
        runner.solver_slot = take_slot
        return runner

    sweep.runtime.runtime.configure_runner = configure_in_slot
    sweep.execute(route, request["role"], skills=skills, max_429_retries=None)
    row = read(directory / request["role"] / route["task"] / "result.json")
    trial = eligible_trial(row["trial"], route["task"], skills)
    save(directory / "position-result.json", score_summary(trial, request["expanded"]))


def run_wave(directory, items, skills, slots, *, role="train"):
    processes = []
    for index, item in enumerate(items):
        position = directory / item["id"]
        request = {"route": item["route"], "skills": skills, "role": role,
                   "slot": slots[index % len(slots)], "expanded": item.get("expanded", False)}
        freeze(position / "request.json", request)
        if (position / "position-result.json").is_file():
            continue
        position_lock = position / "position.lock"
        if position_lock.is_file():
            try:
                with lock(position_lock, nonblocking=True):
                    if (position / "position-result.json").is_file():
                        continue
            except BlockingIOError:
                with lock(position_lock):
                    if not (position / "position-result.json").is_file():
                        raise RuntimeError(f"Active Solver position requires inspection: {position}")
                continue
        cached = item.get("previous_position")
        if cached:
            previous = Path(cached)
            old_request = read(previous / "request.json")
            if all(old_request[k] == request[k] for k in ("route", "skills", "role", "expanded")):
                trial = eligible_trial(read(previous / "position-result.json")["trial"], item["route"]["task"], skills)
                budget = read(sweep.FROZEN / "plan.json")["solver_budget"]
                evidence = read(trial / "trace-audit.json")
                if (read(trial / "input.json")["budget"] == budget
                        and evidence["tool_events_preserved"] and evidence["no_tagged_reasoning_in_archive"]):
                    sweep.verify_route(request["route"])
                    save(position / "position-result.json", {**score_summary(trial, request["expanded"]),
                                                             "reused_from": str(previous)})
                    continue
        with (position / "worker.log").open("ab", buffering=0) as log:
            child = subprocess.Popen([sys.executable, "-Bu", __file__, "--position", str(position)],
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        processes.append((position, child))
    failures = [str(position) for position, child in processes if child.wait()]
    if failures:
        raise RuntimeError("Solver positions require inspection: " + ", ".join(failures))
    return [read(directory / item["id"] / "position-result.json") for item in items]


def stage_practice(plan, group, family, stage):
    task = f"{family}-EVO-{stage}"
    route = plan["train_routes"][task]
    original = {"id": task, "route": route}
    variants = plan["variants"].get(task, {})
    extra = [{"id": f"{task}-{variant_id}", "route": variant if group == "C" else route,
              "expanded": group == "C"}
             for variant_id, variant in sorted(variants.items())]
    return original, extra


def train_family(directory, plan, group, family, slots):
    skills = dict(plan["initial_skills"])
    status = directory / "status.json"
    try:
        for stage in range(1, 5):
            step = directory / f"evo-{stage}"
            completed = step / "stage-result.json"
            freeze(step / "skill-before.json", skills)
            if completed.is_file():
                skills = read(completed)["skill_after"]
                continue
            original, extra = stage_practice(plan, group, family, stage)
            task = original["id"]
            baseline = plan.get("shared_stage_one", {}).get(task) if stage == 1 else None
            if baseline is not None:
                trial = eligible_trial(baseline, task, skills)
                shared = [(original, {**score_summary(trial, False), "shared_stage_one": True})]
                items = extra
            else:
                shared = []
                items = [original, *extra]
            for item in items:
                previous = plan.get("previous_positions", {}).get(group, {}).get(item["id"])
                completed_run = plan.get("completed_positions_run")
                if completed_run:
                    recent = Path(completed_run) / group / family / f"evo-{stage}" / "practice" / item["id"]
                    request = recent / "request.json"
                    if request.is_file() and read(request)["skills"] == skills:
                        receipt = recent / "position-result.json"
                        old_request = read(request)
                        active_lock = recent / old_request["role"] / old_request["route"]["task"] / "worker.lock"
                        while not receipt.is_file():
                            try:
                                with lock(active_lock, nonblocking=True):
                                    break
                            except BlockingIOError:
                                time.sleep(10)
                        if receipt.is_file():
                            previous = str(recent)
                item["previous_position"] = previous
            save(status, {"phase": "running_stage", "stage": stage, "shared": len(shared),
                          "new_rollouts": len(items), "at": stamp()})
            results = run_wave(step / "practice", items, skills, slots)
            all_results = shared + list(zip(items, results, strict=True))
            save(status, {"phase": "updating_skill", "stage": stage, "at": stamp()})
            update = step / "update"
            packets = []
            for item, row in all_results:
                archive = row.get("reused_from")
                previous_step = plan.get("previous_run")
                archived_diagnosis = None
                if archive and not plan.get("refresh_diagnoses"):
                    archived_diagnosis = Path(archive).parents[1] / "diagnoses" / item["id"]
                elif row.get("shared_stage_one") and previous_step and not plan.get("refresh_diagnoses"):
                    archived_diagnosis = Path(previous_step) / group / "evo-1" / "diagnoses" / item["id"]
                packets.append(packet(Path(row["trial"]), item["route"], step / "diagnoses" / item["id"],
                                      previous=archived_diagnosis,
                                      excluded_findings=plan.get("reviewed_diagnoses", {}).get(row["trial"]),
                                      verified_finding=plan.get("verified_findings", {}).get(row["trial"]),
                                      replay_evidence=plan.get("confirmed_replays", {}).get(task)))
            if (update / "skill-after.json").is_file():
                skills = read(update / "skill-after.json")
            elif len(json.dumps(packets).encode()) > 3000000:
                save(update / "skill-before.json", skills)
                save(update / "skill-after.json", skills)
                save(update / "decision.json", {"status": "full_trace_input_budget_exceeded_carry_forward"})
            else:
                skills, _ = optimizer.update(skills, packets, "joint", update, decide=decide_skill_edit)
            save(completed, {"family": family, "stage": stage, "skill_after": skills,
                             "practice": [{**row, "position": item["id"], "task": item["route"]["task"]}
                                          for item, row in all_results]})
        save(directory / "S4.json", skills)
        save(status, {"phase": "train_finished", "at": stamp()})
    except Exception as error:
        save(status, {"phase": "needs_inspection", "error": str(error), "at": stamp()})
        raise


def train_group(directory, group):
    plan = read(directory / "plan.json")
    if plan.get("training_unit") != "family" or plan.get("skill_decision_model") != JEV_MODEL:
        raise ValueError("Use a run directory with a frozen family-level Jev training plan")
    if not hasattr(optimizer, "MEMORY_PATCH_PROMPT"):
        raise RuntimeError("The loaded Skill optimizer does not support the frozen memory format")
    check_transport()
    families = plan["train_families"]
    slots = (0, 1, 2, 3) if group == "B" else (4, 5, 6, 7)
    optimizer.chat = qwen_chat
    status = directory / group / "status.json"
    save(status, {"phase": "training", "families": families, "at": stamp()})
    try:
        with ThreadPoolExecutor(max_workers=min(len(families), len(slots))) as pool:
            jobs = []
            for index, family in enumerate(families):
                offset = index % len(slots)
                jobs.append(pool.submit(train_family, directory / group / family, plan, group, family,
                                        slots[offset:] + slots[:offset]))
            for job in jobs:
                job.result()
    except Exception as error:
        save(status, {"phase": "needs_inspection", "error": str(error), "at": stamp()})
        raise
    save(status, {"phase": "train_finished", "families": families, "at": stamp()})


def test_groups(directory, test_plan):
    plan = read(directory / "plan.json")
    if plan.get("training_unit") != "family":
        raise ValueError("Use a run directory with a frozen family-level training plan")
    routes = read(test_plan)
    families = {task.rsplit("-EVO-", 1)[0] for task in routes}
    expected = {f"{family}-EVO-{v}" for family in families for v in range(1, 5)}
    if not families or families.intersection(plan["train_families"]) or set(routes) != expected:
        raise ValueError("Test requires disjoint complete EVO chains")
    train_families = plan["train_families"]
    skills = {(None, "A"): plan["initial_skills"]}
    for family in train_families:
        for group in ("B", "C"):
            skills[family, group] = read(directory / group / family / "S4.json")
    for task, route in routes.items():
        if route["task"] != task:
            raise ValueError(f"Test route does not match {task}")
        sweep.verify_route(route)
    freeze(directory / "frozen-test.json", routes)
    summary = {key: {} for key in skills}
    slots = {"A": (0, 1, 2, 3), "B": (4, 5, 6, 7), "C": (8, 9, 10, 11)}
    family_offsets = {family: index for index, family in enumerate(train_families)}
    with ThreadPoolExecutor(max_workers=min(len(skills), 12)) as pool:
        for stage in range(1, 5):
            tasks = sorted(f"{family}-EVO-{stage}" for family in families)
            items = [{"id": task, "route": routes[task]} for task in tasks]
            jobs = {}
            for (family, group), current in skills.items():
                output = directory / "test" / group
                if family is not None:
                    output /= family
                group_slots = slots[group]
                offset = family_offsets.get(family, 0) % len(group_slots)
                jobs[family, group] = pool.submit(run_wave, output, items, current,
                                                  group_slots[offset:] + group_slots[:offset], role="test")
            for key, job in jobs.items():
                summary[key].update(zip(tasks, job.result(), strict=True))
            trials = {family: {group: summary[family, group] for group in ("B", "C")}
                      for family in train_families}
            report = {"stages_completed": stage, "baseline": summary[None, "A"], "by_train_family": trials}
            if stage == 4:
                report["baseline_chain_rewards"] = chain_rewards(report["baseline"], families)
                for comparison in trials.values():
                    comparison["chain_rewards"] = {group: chain_rewards(comparison[group], families)
                                                   for group in ("B", "C")}
                    comparison["paired_C_minus_B"] = {task: comparison["C"][task]["reward"]
                                                             - comparison["B"][task]["reward"]
                                                       for task in sorted(routes)}
            save(directory / "test-results.json", report)


def chain_rewards(rows, families):
    return {family: int(all(rows[f"{family}-EVO-{stage}"]["reward"] for stage in range(1, 5)))
            for family in sorted(families)}


def run_baseline(directory, source):
    spec = read(source)
    plan = {key: spec[key] for key in ("train_families", "train_routes", "initial_skills")}
    routes = plan["train_routes"]
    expected = {f"{family}-EVO-{stage}" for family in plan["train_families"] for stage in range(1, 5)}
    if not expected or set(routes) != expected:
        raise ValueError("Baseline requires complete EVO chains")
    freeze(directory / "baseline-plan.json", plan)
    baseline = directory / "baseline"
    status = directory / "baseline-status.json"
    items = [{"id": task, "route": route} for task, route in routes.items()]
    save(status, {"phase": "running", "tasks": len(items)})
    try:
        slots = {}
        for task in routes:
            path = baseline / task / "request.json"
            if path.is_file():
                slots[task] = read(path)["slot"]
        available = [slot for slot in range(8) if slot not in slots.values()]
        for index, task in enumerate(task for task in routes if task not in slots):
            candidates = available or list(range(8))
            slots[task] = candidates[index % len(candidates)]
        # All baseline positions use the same S0. Per-stage Skill dependencies apply to B/C.
        with ThreadPoolExecutor(max_workers=min(8, len(items))) as pool:
            jobs = {item["id"]: pool.submit(run_wave, baseline, [item], plan["initial_skills"],
                                         (slots[item["id"]],)) for item in items}
            results = {task: job.result()[0] for task, job in jobs.items()}
        save(directory / "baseline-results.json", results)
        save(status, {"phase": "completed", "tasks": len(results)})
    except Exception as error:
        save(status, {"phase": "needs_inspection", "error": str(error)})
        raise


def diagnose_baseline(directory):
    plan = read(directory / "baseline-plan.json")
    routes = plan["train_routes"]
    positions = directory / "baseline"
    report = {}
    with ThreadPoolExecutor(max_workers=min(len(routes), 4)) as pool:
        pending = {}
        while len(report) < len(routes):
            for task, route in routes.items():
                receipt = positions / task / "position-result.json"
                if task in report or task in pending or not receipt.is_file():
                    continue
                row = read(receipt)
                trial = eligible_trial(row["trial"], task, plan["initial_skills"])
                report[task] = {"trial": str(trial), "reward": row["reward"],
                                "status": "passed" if row["reward"] else "diagnosing"}
                if not row["reward"]:
                    destination = directory / "baseline-evl" / task
                    pending[task] = pool.submit(packet, trial, route, destination)
            for task in [key for key, job in pending.items() if job.done()]:
                finish_baseline_diagnosis(report[task], pending.pop(task).result())
            save(directory / "baseline-evl-results.json", report)
            if len(report) < len(routes):
                if read(directory / "baseline-status.json")["phase"] == "needs_inspection":
                    raise RuntimeError("Fresh baseline needs inspection")
                time.sleep(10)
        for task, job in pending.items():
            finish_baseline_diagnosis(report[task], job.result())
    save(directory / "baseline-evl-results.json", report)


def finish_baseline_diagnosis(row, result):
    findings = result["diagnosis"]["findings"]
    row.update(status="diagnosed", hypotheses=len(findings),
               located=sum(finding["status"] == "located" for finding in findings),
               check_outcomes=result["check_outcomes"])


def retest_train_chains(directory):
    plan = read(directory / "plan.json")
    if plan.get("training_unit") != "family" or plan.get("skill_decision_model") != JEV_MODEL:
        raise ValueError("Use a run directory with a frozen family-level Jev training plan")
    families = plan["train_families"]
    slots = {"A": (8, 9, 10, 11), "B": (0, 1, 2, 3), "C": (4, 5, 6, 7)}
    skills = {(family, group): (plan["initial_skills"] if group == "A"
                               else read(directory / group / family / "S4.json"))
              for family in families for group in slots}
    previous_run = plan.get("previous_retest_run")
    report = {family: {group: {} for group in slots} for family in families}
    with ThreadPoolExecutor(max_workers=min(12, len(skills))) as pool:
        for stage in range(1, 5):
            jobs = {}
            for index, family in enumerate(families):
                task = f"{family}-EVO-{stage}"
                for group in slots:
                    output = directory / "train-retest" / group / family
                    item = {"id": task, "route": plan["train_routes"][task]}
                    if previous_run:
                        previous = Path(previous_run) / "train-retest" / group / family / task
                        if (previous / "position-result.json").is_file():
                            item["previous_position"] = str(previous)
                    slot = slots[group][index % len(slots[group])]
                    jobs[family, group] = pool.submit(run_wave, output, [item], skills[family, group],
                                                       (slot,), role="test")
            for (family, group), job in jobs.items():
                report[family][group][f"{family}-EVO-{stage}"] = job.result()[0]
            summary = {"stages_completed": stage, "scope": "seen_train_originals", "trials": report}
            if stage == 4:
                summary["scores"] = {
                    family: {group: {"passed": sum(row["reward"] for row in rows.values()),
                                     "complete_chain": chain_rewards(rows, [family])[family]}
                             for group, rows in groups.items()}
                    for family, groups in report.items()}
                summary["paired_C_minus_B"] = {
                    family: {task: groups["C"][task]["reward"] - groups["B"][task]["reward"]
                             for task in groups["B"]} for family, groups in report.items()}
            save(directory / "train-retest-results.json", summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--group", choices=("B", "C"))
    parser.add_argument("--position", action="store_true")
    parser.add_argument("--test-plan", type=Path)
    parser.add_argument("--retest-train", action="store_true")
    parser.add_argument("--diagnose-baseline", action="store_true")
    parser.add_argument("--baseline-plan", type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if args.position:
        with lock(directory / "position.lock", nonblocking=True):
            run_position(directory)
        return
    if args.group:
        with lock(directory / args.group / "group.lock", nonblocking=True):
            train_group(directory, args.group)
        return
    if args.test_plan:
        with lock(directory / "test.lock", nonblocking=True):
            test_groups(directory, args.test_plan)
        return
    if args.retest_train:
        with lock(directory / "train-retest.lock"):
            retest_train_chains(directory)
        return
    if args.diagnose_baseline:
        with lock(directory / "baseline-evl.lock", nonblocking=True):
            diagnose_baseline(directory)
        return
    if args.baseline_plan:
        with lock(directory / "baseline.lock", nonblocking=True):
            run_baseline(directory, args.baseline_plan)
        return
    plan = {**read(args.plan or directory / "plan.json"), "training_unit": "family",
            "skill_decision_model": JEV_MODEL}
    versions = {family: (1, 2, 3, 4) for family in plan["train_families"]}
    if len(versions) != len(plan["train_families"]):
        raise ValueError("Train families must be distinct")
    chains, pairs = paired_training_plan({task: list(variants) for task, variants in plan["variants"].items()}, versions)
    if set(plan["train_routes"]) != {row["parent"] for chain in chains for row in chain["stages"]}:
        raise ValueError("Train routes must cover all original EVO stages")
    check_transport()
    with lock(directory / "train.lock", nonblocking=True):
        freeze(directory / "plan.json", plan)
        freeze(directory / "train-chains.json", chains)
        freeze(directory / "pair-plan.json", pairs)
        save(directory / "status.json", {"phase": "training", "paired_extra_practices": len(pairs), "at": stamp()})
        children = []
        for group in ("B", "C"):
            with (directory / f"{group}.log").open("ab", buffering=0) as log:
                child = subprocess.Popen([sys.executable, "-Bu", __file__, str(directory), "--group", group],
                                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            children.append(child)
        failures = sum(child.wait() != 0 for child in children)
        save(directory / "status.json", {"phase": "needs_inspection" if failures else "train_finished",
             "paired_extra_practices": len(pairs), "at": stamp()})
        if failures:
            raise SystemExit(1)


if __name__ == "__main__":
    main()


