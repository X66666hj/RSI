"""One EnvHarness component contract for Terminal-Evo design and execution."""
from typing import Annotated, Literal

from envharness.core.types import Action, EnvResponse, Observation
from envharness.harnesses.link import Link
from envharness.harnesses.rules import Rules
from envharness.harnesses.setup import Setup
from pydantic import BaseModel, ConfigDict, Field, StringConstraints


Nonempty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ShellCommand = Annotated[str, StringConstraints(pattern=r"\S")]
COMPONENTS = frozenset(("setup", "rules", "link"))


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Reproduction(Contract):
    evidence: Nonempty
    historical_score: Literal[0]
    repaired_score: Literal[1]
    replayed_component: Literal["setup", "rules", "link"] | None = None


class OutputPaging(Contract):
    lines_per_page: int = Field(ge=1, le=200)


class TerminalCandidate(Contract):
    setup: list[ShellCommand] = Field(default_factory=list, max_length=10)
    rules: OutputPaging | None = None
    link: Nonempty | None = None
    rationale: Nonempty


def validate_components(components):
    selected = set(components)
    if not selected or not selected <= COMPONENTS or len(selected) != len(components):
        raise ValueError("Expected a nonempty set of supported components")
    return selected


def validate_candidate(candidate, components):
    selected = validate_components(components)
    actual = {name for name in COMPONENTS if getattr(candidate, name)}
    if actual != selected:
        raise ValueError(f"Expected components {sorted(selected)}; got {sorted(actual)}")
    return candidate


class PaginatedBash(Rules):
    """Expose bounded tool observations and an explicit continuation action."""

    def __init__(self, inner, rule):
        super().__init__(inner)
        self.limit = rule.lines_per_page
        self.remaining = []

    def reset(self, seed=None, options=None):
        self.remaining.clear()
        return super().reset(seed, options)

    def step(self, action):
        if action.name == "next_page":
            if action.kwargs:
                raise ValueError("next_page takes no arguments")
            lines = self.remaining[:self.limit]
            del self.remaining[:self.limit]
            return EnvResponse(
                observation=Observation(text="".join(lines), data={"more": bool(self.remaining)}),
                reward=0, terminated=False, truncated=False)
        self.remaining.clear()
        return super().step(action)

    def filter_observation(self, obs, env_state):
        if "exit_code" not in obs.data:
            if "task" in obs.data:
                return Observation(text=obs.text + "\n\nBash returns up to "
                                   f"{self.limit} lines per page. Call next_page while more is true. "
                                   "A new Bash call replaces any pending pages.", data=obs.data)
            return obs
        lines = obs.text.splitlines(keepends=True)
        self.remaining = lines[self.limit:]
        return Observation(text="".join(lines[:self.limit]),
                           data={**obs.data, "more": bool(self.remaining)})

    def observe(self):
        # Asking for the public task should not consume or reset the page cursor.
        return self.filter_observation(self.inner.observe(), self.inner.get_env_state())


def compose(candidate, primary, followup=None):
    """Apply Setup to the parent, Rules to each stage, then Link the stages."""
    if bool(candidate.link) != (followup is not None):
        raise ValueError("Link requires exactly one prepared follow-up task")
    if candidate.setup:
        primary = Setup(inner=primary, actions=[Action(name="setup_bash", kwargs={"command": command})
                                               for command in candidate.setup])
    if candidate.rules:
        primary = PaginatedBash(primary, candidate.rules)
        if followup is not None:
            followup = PaginatedBash(followup, candidate.rules)
    if followup is not None:
        return Link(primary, followup, carry_context=True, a_done_via="submitted")
    return primary


class TerminalEpisode:
    """Expose public observations at the tool boundary and keep scores operator-only."""

    def __init__(self, candidate, components, primary, followup=None):
        validate_candidate(candidate, components)
        self.stages = [primary] if followup is None else [primary, followup]
        self.environment = compose(candidate, primary, followup)
        self.observation = self.environment.reset().observation
        self.done = False
        self.failed = False
        self.linked = followup is not None
        self.has_rules = candidate.rules is not None
        if self.linked:
            self.observation.text += ("\n\nThis episode has two tasks. Submit to switch to the second task. "
                                      "You may include a short public handoff note. Files stay with "
                                      "their respective tasks. Submit again to finish.")
        self.task_observation = self.observation

    def action(self, name, **kwargs):
        if self.done or self.failed:
            raise ValueError("Episode is closed")
        if name not in {"bash", "next_page", "submit"} or (name == "next_page" and not self.has_rules):
            raise ValueError("Unknown public tool")
        if name == "submit" and kwargs and not self.linked:
            raise ValueError("Handoff notes require Link")
        result = self.environment.step(Action(name=name, kwargs=kwargs))
        if result.truncated or any(result.info.get(key) for key in
                                   ("a_eval_error", "b_eval_error", "handoff_error")):
            self.failed = True
            raise RuntimeError("Component execution or scoring failed")
        self.observation = result.observation
        if result.info.get("link_stage") == "switched_to_B":
            self.task_observation = result.observation
        self.done = result.terminated
        return self.observation

    def grade(self, *, incomplete_control=False):
        if self.failed or (not self.done and not (
                incomplete_control and self.linked and self.stages[0].submitted)):
            raise RuntimeError("Unfinished episode cannot be scored")
        scores = [int(stage.evaluate().success) if stage.submitted else None
                  for stage in self.stages]
        return {"reward": int(all(score == 1 for score in scores)),
                "stages": [{"task": stage.task, "reward": score}
                           for stage, score in zip(self.stages, scores, strict=True)]}
