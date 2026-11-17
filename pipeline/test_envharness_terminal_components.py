"""Check component selection, public tool behavior, and fail-closed grading."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent / "vendor/envharness-main"))
from envharness.core.types import EnvResetResponse, EnvResponse, EvaluationResult, Observation
from envharness_terminal_components import TerminalCandidate, TerminalEpisode, validate_candidate


class Stage:
    def __init__(self, task, success=True, *, broken=False):
        self.task = task
        self.success = success
        self.broken = broken
        self.submitted = False
        self.started = False
        self.commands = []

    def reset(self, seed=None, options=None):
        self.started = True
        return EnvResetResponse(observation=self.observe())

    def observe(self):
        return Observation(text=self.task, data={"task": self.task})

    def get_env_state(self):
        return {}

    def notify_replay_complete(self):
        pass

    def step(self, action):
        if action.name == "submit":
            self.submitted = True
            return EnvResponse(observation=Observation(text=action.kwargs.get("handoff", "")),
                               reward=0, terminated=True, truncated=False, info={"submitted": True})
        self.commands.append((action.name, action.kwargs["command"]))
        return EnvResponse(observation=Observation(text=action.kwargs["command"], data={"exit_code": 0}),
                           reward=0, terminated=False, truncated=False)

    def evaluate(self):
        if self.broken:
            raise RuntimeError("grader unavailable")
        return EvaluationResult(success=self.success, score=int(self.success))


class TerminalComponentsTests(unittest.TestCase):
    def test_setup_and_selection(self):
        candidate = TerminalCandidate(setup=["prepare"], rationale="change initial state")
        stage = Stage("parent")
        TerminalEpisode(candidate, ["setup"], stage)
        self.assertEqual(stage.commands, [("setup_bash", "prepare")])
        with self.assertRaises(ValueError):
            validate_candidate(candidate, ["rules"])

    def test_rules_page_and_reset_cursor(self):
        candidate = TerminalCandidate(rules={"lines_per_page": 1}, rationale="page tool output")
        episode = TerminalEpisode(candidate, ["rules"], Stage("parent"))
        first = episode.action("bash", command="first\nsecond\nthird")
        self.assertEqual(first.text, "first\n")
        self.assertTrue(first.data["more"])
        self.assertEqual(episode.action("next_page").text, "second\n")
        episode.action("bash", command="new output")
        self.assertFalse(episode.action("next_page").data["more"])
        self.assertEqual(episode.task_observation.data["task"], "parent")

    def test_combined_components_run_in_order(self):
        candidate = TerminalCandidate(setup=["prepare"], rules={"lines_per_page": 1},
                                      link="followup", rationale="combine reproduced conditions")
        first, second = Stage("parent"), Stage("followup")
        episode = TerminalEpisode(candidate, ["setup", "rules", "link"], first, second)
        self.assertEqual(first.commands, [("setup_bash", "prepare")])
        self.assertTrue(episode.action("bash", command="one\ntwo").data["more"])
        episode.action("submit")
        self.assertEqual(second.commands, [])
        self.assertEqual(episode.action("next_page").text, "")
        self.assertTrue(episode.action("bash", command="one\ntwo").data["more"])

    def test_link_is_binary_and_requires_both_stages(self):
        candidate = TerminalCandidate(link="followup", rationale="exercise handoff")
        for first, second, expected in ((True, True, 1), (False, True, 0), (True, False, 0)):
            with self.subTest(first=first, second=second):
                followup = Stage("followup", second)
                episode = TerminalEpisode(candidate, ["link"],
                                          Stage("parent", first), followup)
                self.assertFalse(followup.started)
                handoff = episode.action("submit", handoff="public note")
                self.assertIn("public note", handoff.text)
                self.assertNotIn("a_success", handoff.data)
                self.assertEqual(episode.grade(incomplete_control=True)["reward"], 0)
                with self.assertRaises(RuntimeError):
                    episode.grade()
                episode.action("submit")
                self.assertEqual(episode.grade()["reward"], expected)

    def test_infrastructure_failure_is_unscored(self):
        episode = TerminalEpisode(TerminalCandidate(link="followup", rationale="exercise handoff"),
                                  ["link"], Stage("parent", broken=True),
                                  Stage("followup"))
        with self.assertRaises(RuntimeError):
            episode.action("submit")
        with self.assertRaises(RuntimeError):
            episode.grade(incomplete_control=True)


if __name__ == "__main__":
    unittest.main()
