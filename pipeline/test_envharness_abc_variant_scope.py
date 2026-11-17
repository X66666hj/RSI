"""Exercise the actual ABC routing functions without importing paid SDK modules."""
import ast
import json
from pathlib import Path
import re
import tempfile
import unittest

from diagnosis import read
from envharness_abc_preflight import paired_training_plan


class PairedTrainingPlanTests(unittest.TestCase):
    def test_sparse_variants_keep_complete_original_chains_and_match_counts(self):
        versions = {"raman-fitting": (1, 2, 3, 4), "sparql-university": (1, 2, 3, 4)}
        chains, pairs = paired_training_plan({
            "raman-fitting-EVO-1": ["variant-01"],
            "raman-fitting-EVO-4": ["variant-02", "variant-01"],
            "sparql-university-EVO-2": [],
        }, versions)
        self.assertEqual([[stage["parent"] for stage in chain["stages"]] for chain in chains],
                         [[f"{family}-EVO-{stage}" for stage in range(1, 5)] for family in versions])
        self.assertEqual([[len(stage["variants"]) for stage in chain["stages"]] for chain in chains],
                         [[1, 0, 0, 2], [0, 0, 0, 0]])
        self.assertEqual([pair["parent"] for pair in pairs],
                         ["raman-fitting-EVO-1", "raman-fitting-EVO-4", "raman-fitting-EVO-4"])
        self.assertEqual(8 + sum(pair["B_repeats"] for pair in pairs), 11)
        self.assertEqual(8 + sum(pair["C_trials"] for pair in pairs), 11)

    def test_no_variants_still_builds_all_original_stages(self):
        chains, pairs = paired_training_plan({}, {"example": (1, 2, 3, 4)})
        self.assertEqual([stage["variants"] for stage in chains[0]["stages"]], [[], [], [], []])
        self.assertEqual(pairs, [])

    def test_rejects_unknown_parent_duplicate_variant_or_incomplete_original_chain(self):
        versions = {"example": (1, 2, 3, 4)}
        for accepted in ({"outside-EVO-1": ["variant-01"]},
                         {"example-EVO-1": ["variant-01", "variant-01"]}):
            with self.subTest(accepted=accepted), self.assertRaises(ValueError):
                paired_training_plan(accepted, versions)
        with self.assertRaises(ValueError):
            paired_training_plan({}, {"example": (1, 3, 4)})


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.slot = Path(self.temp.name)
        self.write("agent.json", {})
        self.write("operator-evaluation.json", {"evaluation": {"reward": "1"}})
        source = Path(__file__).parent / "envharness_abc_run.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in {"packet", "practice_checks", "score_summary", "stage_practice", "train_family",
                                       "test_groups", "retest_train_chains", "chain_rewards", "freeze"}]
        self.namespace = {"read": read, "Path": Path, "re": re}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), self.namespace)

    def write(self, name, value):
        (self.slot / name).write_text(json.dumps(value), encoding="utf-8")

    def test_b_repeats_the_parent_and_only_c_uses_the_variant_route(self):
        task = "raman-fitting-EVO-1"
        original, variant = {"task": task}, {"task": "raman-variant-EVO-1"}
        plan = {"train_families": ["raman-fitting"], "train_routes": {task: original},
                "variants": {task: {"variant-01": variant}}}
        b_original, b_extra = self.namespace["stage_practice"](plan, "B", "raman-fitting", 1)
        c_original, c_extra = self.namespace["stage_practice"](plan, "C", "raman-fitting", 1)
        self.assertEqual(b_original, c_original)
        self.assertEqual(b_extra[0]["id"], c_extra[0]["id"])
        self.assertEqual(b_extra[0]["route"], original)
        self.assertFalse(b_extra[0]["expanded"])
        self.assertEqual(c_extra[0]["route"], variant)
        self.assertTrue(c_extra[0]["expanded"])

    def test_each_family_updates_and_resumes_only_its_own_skill_chain(self):
        from types import SimpleNamespace

        families = ["first", "second"]
        routes = {f"{family}-EVO-{stage}": {"task": f"{family}-EVO-{stage}"}
                  for family in families for stage in range(1, 5)}
        variant = "second-variant-EVO-1"
        initial = {"task": "S0"}
        plan = {"train_families": families, "train_routes": routes, "initial_skills": initial,
                "variants": {"second-EVO-1": {"variant-01": {"task": variant}}}}
        waves, updates = [], []

        def save(path, value):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value), encoding="utf-8")

        def run_wave(directory, items, skills, slots):
            waves.append((directory.parent.parent.name, [item["route"]["task"] for item in items], skills))
            return [{"trial": item["id"], "reward": 1} for item in items]

        def update(skills, packets, mode, destination, *, decide):
            updates.append(packets)
            return {"task": packets[0]}, {}

        self.namespace.update(save=save, run_wave=run_wave, optimizer=SimpleNamespace(update=update),
                              stamp=lambda: "time", packet=lambda trial, route, output, **kwargs: route["task"], json=json,
                              decide_skill_edit=object())
        train = self.namespace["train_family"]
        for family in families:
            train(self.slot / family, plan, "C", family, (4, 5))
            train(self.slot / family, plan, "C", family, (4, 5))
        self.assertEqual(len(waves), 8)
        self.assertEqual(updates, [tasks for _, tasks, _ in waves])
        self.assertEqual(waves[4][1], ["second-EVO-1", variant])
        for family, tasks, skills in waves:
            stage = int(tasks[0][-1])
            self.assertEqual(tasks[0], f"{family}-EVO-{stage}")
            self.assertEqual(skills, initial if stage == 1 else {"task": f"{family}-EVO-{stage - 1}"})
        for family in families:
            self.assertEqual(read(self.slot / family / "S4.json"), {"task": f"{family}-EVO-4"})

    def test_test_runs_one_baseline_and_evaluates_each_family_skill_independently(self):
        from concurrent.futures import ThreadPoolExecutor
        from types import SimpleNamespace

        def save(path, value):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value), encoding="utf-8")

        save(self.slot / "plan.json", {"training_unit": "family", "train_families": ["first", "second"],
                                       "initial_skills": {"task": "S0"}})
        plan = self.slot / "holdout.json"
        save(plan, {f"holdout-EVO-{stage}": {"task": f"holdout-EVO-{stage}"} for stage in range(1, 5)})
        for family in ("first", "second"):
            for group in ("B", "C"):
                save(self.slot / group / family / "S4.json", {"task": f"{family}-{group}"})

        def run_wave(directory, items, skills, slots, *, role):
            label = skills["task"]
            expected = self.slot / "test" / "A"
            if label != "S0":
                family, group = label.split("-")
                expected = self.slot / "test" / group / family
            self.assertEqual(directory, expected)
            self.assertEqual(role, "test")
            return [{"reward": int(label != "second-B")} for _ in items]

        self.namespace.update(save=save, run_wave=run_wave, sweep=SimpleNamespace(verify_route=lambda _: None),
                              ThreadPoolExecutor=ThreadPoolExecutor)
        self.namespace["test_groups"](self.slot, plan)
        report = read(self.slot / "test-results.json")
        self.assertEqual(len(report["baseline"]), 4)
        self.assertEqual(report["baseline_chain_rewards"], {"holdout": 1})
        self.assertEqual(set(report["by_train_family"]["first"]["paired_C_minus_B"].values()), {0})
        self.assertEqual(set(report["by_train_family"]["second"]["paired_C_minus_B"].values()), {1})

    def test_original_reward_ignores_any_auxiliary_variant_score(self):
        self.write("task-evaluation.json", {"variant": False})
        result = self.namespace["score_summary"](self.slot, False)
        self.assertEqual(result["reward"], 1)
        self.assertNotIn("variant_reward", result)

    def test_retest_applies_each_frozen_skill_only_to_its_own_original_chain(self):
        from concurrent.futures import ThreadPoolExecutor

        families = ["first", "second"]

        def save(path, value):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value), encoding="utf-8")

        routes = {f"{family}-EVO-{stage}": {"task": f"{family}-EVO-{stage}"}
                  for family in families for stage in range(1, 5)}
        save(self.slot / "plan.json", {"training_unit": "family", "skill_decision_model": "jev",
                                       "train_families": families, "initial_skills": {"task": "S0"},
                                       "train_routes": routes})
        for family in families:
            for group in ("B", "C"):
                save(self.slot / group / family / "S4.json", {"task": f"{family}-{group}"})

        def run_wave(directory, items, skills, slots, *, role):
            family, group = directory.name, directory.parent.name
            task = items[0]["id"]
            self.assertTrue(task.startswith(family + "-EVO-"))
            self.assertEqual(items[0]["route"], routes[task])
            self.assertEqual(skills["task"], "S0" if group == "A" else f"{family}-{group}")
            self.assertEqual(role, "test")
            return [{"reward": int(group != "B")}]

        self.namespace.update(save=save, run_wave=run_wave, ThreadPoolExecutor=ThreadPoolExecutor,
                              JEV_MODEL="jev")
        self.namespace["retest_train_chains"](self.slot)
        result = read(self.slot / "train-retest-results.json")
        self.assertEqual(result["scope"], "seen_train_originals")
        self.assertEqual(result["stages_completed"], 4)
        for family in families:
            self.assertEqual(result["scores"][family]["C"], {"passed": 4, "complete_chain": 1})
            self.assertEqual(set(result["paired_C_minus_B"][family].values()), {1})

    def test_expanded_c_requires_both_scoring_layers(self):
        with self.assertRaisesRegex(ValueError, "original and variant checks"):
            self.namespace["score_summary"](self.slot, True)
        self.write("ctrf.json", {"results": {"tests": [
            {"name": "test_original", "status": "passed"}]}})
        with self.assertRaisesRegex(ValueError, "omitted a scoring layer"):
            self.namespace["score_summary"](self.slot, True)

    def test_expanded_c_reports_combined_and_individual_rewards(self):
        self.write("operator-evaluation.json", {"evaluation": {"reward": "0"}})
        self.write("ctrf.json", {"results": {"tests": [
            {"name": "test_original", "status": "passed"},
            {"name": "test_variant_background", "status": "failed"}]}})
        result = self.namespace["score_summary"](self.slot, True)
        self.assertEqual((result["reward"], result["original_reward"], result["variant_reward"]), (0, 1, 0))

    def test_rejected_diagnosis_clears_evidence_before_validation(self):
        task = "raman-fitting-EVO-1"
        public = self.slot / task / "instruction.md"
        public.parent.mkdir()
        public.write_text("public task", encoding="utf-8")
        self.write("operator-evaluation.json", {"execution_complete": True, "evaluation": {"reward": "0"}})
        self.write("ctrf.json", {"results": {"tests": [{
            "name": "tests/test_outputs.py::test_peak_offset[private-case]", "status": "failed"}]}})
        trace = {"trajectory_id": "trajectory"}
        finding = {"finding_id": "f1", "status": "located", "evidence": ["event 1"]}
        package = {"trajectory_id": "trajectory", "findings": [finding]}

        def validate(candidate, source):
            self.assertEqual(source, trace)
            self.assertEqual(candidate["findings"], [
                {"finding_id": "f1", "status": "unresolved", "evidence": []}])
            return candidate

        self.namespace.update(analysis_trace=lambda _: trace, DIAGNOSIS_PROMPT="diagnose",
                              ask=lambda path, *_: package if path.name == "diagnose" else {"accepted_finding_ids": []},
                              save=lambda *_: None, validate_package=validate,
                              validate_package_fail_closed=lambda *_: (package, {}))
        packet = self.namespace["packet"](self.slot, {"task": task, "source": self.slot}, self.slot / "diagnosis")
        self.assertEqual(packet["training_reward"], 0)
        self.assertEqual(packet["check_outcomes"], [{"check": "test_peak_offset", "status": "failed"}])

    def test_success_can_furnish_observed_strategy_evidence(self):
        task = "query-EVO-1"
        public = self.slot / task / "instruction.md"
        public.parent.mkdir()
        public.write_text("public task", encoding="utf-8")
        self.write("operator-evaluation.json", {"execution_complete": True, "evaluation": {"reward": "1"}})
        finding = {"finding_id": "f1", "claim_type": "observation", "status": "located", "evidence": ["event 1"]}
        package = {"trajectory_id": "trajectory", "findings": [finding]}

        def ask(path, prompt, payload):
            self.assertEqual(payload["training_reward"], 1)
            if path.name == "diagnose":
                self.assertIn("scored success", prompt)
                return package
            return {"accepted_finding_ids": ["f1"]}

        self.namespace.update(analysis_trace=lambda _: {"trajectory_id": "trajectory"}, DIAGNOSIS_PROMPT="diagnose",
                              ask=ask, save=lambda *_: None, validate_package=lambda candidate, _: candidate,
                              validate_package_fail_closed=lambda *_: (package, {}))
        packet = self.namespace["packet"](self.slot, {"task": task, "source": self.slot}, self.slot / "diagnosis")
        self.assertEqual(packet["diagnosis"]["findings"], [finding])


if __name__ == "__main__":
    unittest.main()
