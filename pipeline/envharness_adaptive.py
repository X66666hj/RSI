"""EnvHarness environment and MCP tool bridge used by the DeepSeek SDK launchers."""
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import envharness_full_coverage as coverage

sys.path.insert(0, str(coverage.JOB / "vendor/envharness-main"))
from envharness.core.actionable_env import ActionableEnv
from envharness.core.types import Action, EnvResetResponse, EnvResponse, EvaluationResult, Observation
from envharness_terminal_components import (
    Reproduction, TerminalCandidate, TerminalEpisode, validate_components, validate_candidate,
)


class SetupActionError(RuntimeError):
    """A candidate's public Setup command failed during isolated replay."""


class NativeStage(ActionableEnv):
    def __init__(self, runner, root, route, output, *, oracle=False):
        self.native = runner.native
        self.root = root
        self.task = route["task"]
        self.cwd = route["cwd"]
        self.instruction = (Path(route["source"]) / self.task / "instruction.md").read_text()
        self.output = output
        self.oracle = oracle
        self.submitted = False
        self.started = False
        self.result = None
        self.calls = 0

    def reset(self, seed=None, options=None):
        if self.started:
            raise RuntimeError("A fresh materialized stage is required")
        self.started = True
        return EnvResetResponse(observation=self.observe())

    def observe(self):
        return Observation(text=self.instruction, data={"task": self.task, "cwd": self.cwd})

    def step(self, action):
        if not self.started or self.submitted:
            raise RuntimeError("Stage is not accepting actions")
        if action.name == "submit":
            handoff = action.kwargs.get("handoff", "")
            if set(action.kwargs) - {"handoff"} or not isinstance(handoff, str) or len(handoff) > 1200:
                raise ValueError("Invalid submit handoff")
            self.submitted = True
            return EnvResponse(observation=Observation(text=handoff or "Submission recorded."), reward=0,
                               terminated=True, truncated=False, info={"submitted": True})
        command = action.kwargs.get("command")
        if (action.name not in {"bash", "setup_bash"} or set(action.kwargs) != {"command"}
                or not isinstance(command, str) or not command.strip()):
            raise ValueError("Invalid tool call for this stage")
        self.calls += 1
        log = self.output / f"bash-{self.calls:04}.log"
        argv = [sys.executable, str(coverage.FROZEN / "enter-expanded.py"), "--offline-agent",
                str(self.root), self.cwd, "/bin/bash", "--noprofile", "--norc", "-c", command]
        with log.open("wb") as stream:
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=stream,
                                    stderr=subprocess.STDOUT, timeout=60)
        if action.name == "setup_bash" and result.returncode != 0:
            with log.open("rb") as stream:
                detail = stream.read(2048).decode(errors="replace").strip()
            raise SetupActionError(f"Setup Bash exited {result.returncode}: {detail}")
        # Keep very large responses operator-side. The output rule operates below this cap.
        with log.open("rb") as stream:
            raw = stream.read(256 * 1024 + 1)
        capped = len(raw) > 256 * 1024
        output = raw[:256 * 1024].decode(errors="replace")
        return EnvResponse(observation=Observation(text=output, data={
            "exit_code": result.returncode, "output_capped": capped}), reward=0,
            terminated=False, truncated=False)

    def evaluate(self):
        if not self.submitted:
            raise RuntimeError("Stage has not been submitted")
        if self.result is None:
            value = self.native.verify(self.task, self.root, self.cwd, "submit", oracle=self.oracle)
            if value.get("reward") not in {"0", "1"} or value.get("exit_code") is None:
                raise RuntimeError("Original verifier did not produce a binary score")
            reports = list(self.output.rglob("*ctrf*.json"))
            if len(reports) != 1:
                raise RuntimeError("Missing original verifier report")
            statuses = {row["status"] for row in coverage.read(reports[0])["results"]["tests"]}
            success = value["reward"] == "1"
            if ((success and (value["exit_code"] != 0 or statuses != {"passed"}))
                    or (not success and ("failed" not in statuses or statuses - {"passed", "failed"}))):
                raise RuntimeError("Original verifier score lacks matching test evidence")
            self.result = EvaluationResult(success=success, score=float(success), metrics=value)
        return self.result

    def get_env_state(self):
        return {"task": self.task, "cwd": self.cwd}

    def save_state(self):
        raise NotImplementedError("Replay starts from a fresh admitted baseline")

    @classmethod
    def from_state(cls, state):
        raise NotImplementedError("Replay starts from a fresh admitted baseline")


def train_routes():
    path = Path(os.environ.get("RSI_TRAIN_PLAN", str(coverage.JOB / "baseline-plan.json")))
    plan = coverage.read(path)
    return plan["train_routes"]


def solver_spec(session, task):
    spec = coverage.read(session / "component-session.json")
    if spec["role"] != "solver" or spec["task"] != task:
        raise ValueError("Solver session task does not match its component candidate")
    designer = Path(spec["designer"]).resolve(strict=True)
    candidate = designer / "component/candidate.json"
    checked = designer / "component/design-self-check.json"
    result = coverage.read(designer / "designer-agent.json")
    if (result["finish_reason"] != "completed" or result["error"] or not checked.is_file()
            or coverage.file_hash(candidate) != spec["candidate_sha256"]
            or coverage.read(candidate) != spec["candidate"]):
        raise ValueError("Solver requires a completed, unchanged designer candidate")
    designed = coverage.read(designer / "component-session.json")
    if any(designed[key] != spec[key] for key in ("task", "reproductions", "components")):
        raise ValueError("Solver session differs from the designer's evidence or component selection")
    if set(coverage.read(checked)["components"]) != validate_components(spec["components"]):
        raise ValueError("Designer self-check differs from the selected components")
    return spec


@contextmanager
def open_stage(route, output, *, oracle=False):
    output.mkdir(parents=True, exist_ok=False)
    engine = output / ("adaptive-" + uuid.uuid4().hex)
    runtime = coverage.runtime
    previous = runtime.JOB, runtime.runtime.JOB
    task = route["task"]
    runner = None
    templates = {}
    try:
        runner, plan = coverage.materializer(route, engine)
        templates = runtime.prepare_tasks(runner, plan, [task], run_controls=False)
        runner.native.RUNS = output / "verifier-runs"
        root, _, _ = templates[task]
        stage = NativeStage(runner, root, route, output, oracle=oracle)
        probe = stage.root
        if any((probe / name).exists() for name in ("tests", "solution", "proc/self")):
            raise RuntimeError("Protected files in public chroot")
        yield stage
    finally:
        try:
            if runner is not None:
                runtime.JOB = runtime.runtime.JOB = engine
                runtime.delete_templates(runner, templates or {task: (
                    coverage.ROOT / "work" / engine.name / "templates" / (task + "-template"), None, None)})
        finally:
            runtime.JOB, runtime.runtime.JOB = previous


@contextmanager
def open_episode(task, candidate, components, directory, *, oracle=False, skills=None):
    validate_candidate(candidate, components)
    routes = train_routes()
    if task not in routes or (candidate.link and (candidate.link not in routes or candidate.link == task
            or candidate.link.rsplit("-EVO-", 1)[0] != task.rsplit("-EVO-", 1)[0])):
        raise ValueError("The parent and a distinct Link follow-up must use admitted Train routes")
    with ExitStack() as stack:
        primary = stack.enter_context(open_stage(routes[task], directory / "stage-a", oracle=oracle))
        followup = (stack.enter_context(open_stage(routes[candidate.link], directory / "stage-b", oracle=oracle))
                    if candidate.link else None)
        episode = TerminalEpisode(candidate, components, primary, followup)
        if skills:
            for stage in (item for item in (primary, followup) if item is not None):
                for name, text in skills.items():
                    folder = stage.root / "opt/agent-skills" / name
                    if not folder.resolve().is_relative_to(stage.root.resolve()):
                        raise RuntimeError("Skill path escapes the public workspace")
                    folder.mkdir(parents=True, exist_ok=True)
                    path = folder / "SKILL.md"
                    path.write_text(text, encoding="utf-8")
                    path.chmod(0o444)
                    folder.chmod(0o555)
        yield episode


def tool_specs(candidate):
    def tool(name, description, properties=None, required=()):
        return {"name": name, "description": description, "inputSchema": {
            "type": "object", "properties": properties or {}, "required": list(required),
            "additionalProperties": False}}

    specs = [tool("observe", "Read the current public task and working directory."),
             tool("bash", "Run an offline shell command in the current task.",
                  {"command": {"type": "string"}}, ("command",))]
    if candidate.rules:
        specs.append(tool("next_page", "Read the next page of the last Bash result."))
    if candidate.link:
        specs.append(tool("submit", "Submit this task. The first submission opens the next task.",
                          {"handoff": {"type": "string", "maxLength": 1200}}))
    else:
        specs.append(tool("submit", "Submit the current task."))
    return specs


def design_specs():
    specs = tool_specs(TerminalCandidate(rationale="design inspection"))[:2]
    specs.append({"name": "propose", "description": "Validate and save a candidate after inspecting the public task.",
                  "inputSchema": TerminalCandidate.model_json_schema()})
    return specs


class Designer:
    def __init__(self, task, components, followups, directory, stage):
        self.task = task
        self.components = components
        self.followups = followups
        self.directory = directory
        self.stage = stage
        self.inspected = False
        self.task_observation = stage.reset().observation

    def action(self, name, **kwargs):
        if name == "bash":
            response = self.stage.step(Action(name=name, kwargs=kwargs))
            self.inspected |= response.observation.data["exit_code"] == 0
            return response.observation
        if not self.inspected:
            raise ValueError("Inspect the real public environment with Bash first")
        candidate = validate_candidate(TerminalCandidate.model_validate(kwargs), self.components)
        if candidate.link and candidate.link not in self.followups:
            raise ValueError("Link follow-up must be an admitted task from the frozen Train family")
        target = self.directory / "candidate.json"
        if target.exists():
            raise ValueError("Candidate already saved")
        # Materialize fresh stages and exercise the public component hooks. No
        # stage is submitted during design, so hidden scoring never runs here.
        output = self.directory / ("self-check-" + uuid.uuid4().hex)
        try:
            with open_episode(self.task, candidate, self.components, output) as probe:
                if candidate.rules:
                    command = "seq " + str(candidate.rules.lines_per_page + 1)
                    head = probe.action("bash", command=command)
                    tail = probe.action("next_page")
                    if (head.data.get("more") is not True or tail.data.get("more") is not False
                            or len(head.text.splitlines()) != candidate.rules.lines_per_page
                            or tail.text.strip() != str(candidate.rules.lines_per_page + 1)):
                        raise ValueError("Rules public self-check failed")
                stages = [stage.task for stage in probe.stages]
        except SetupActionError as error:
            raise ValueError(f"Revise the candidate and propose again. {error}") from error
        coverage.save(target, candidate.model_dump())
        coverage.save(self.directory / "design-self-check.json", {
            "components": sorted(self.components), "prepared_tasks": stages,
            "solver_tools": tool_specs(candidate)})
        return Observation(text="Candidate saved after isolated public self-check.")


def serve_mcp(session, requests):
    """One tool server for the existing designer and Solver SDK sessions."""
    spec = coverage.read(session / "component-session.json")
    task = spec["task"]
    reproductions = [Reproduction.model_validate(row) for row in spec["reproductions"]]
    if not reproductions:
        raise ValueError("A reproduced historical error is required")
    components = validate_components(spec["components"])
    design = spec["role"] == "designer"
    if spec["role"] not in ("designer", "solver"):
        raise ValueError("Invalid component session role")
    if not design:
        spec = solver_spec(session, task)
    candidate = None if design else validate_candidate(TerminalCandidate.model_validate(spec["candidate"]), components)
    specs = design_specs() if design else tool_specs(candidate)
    tools = {tool["name"] for tool in specs}
    directory = session / "component"
    skills = None if design else {name: (session / f"{name.upper()}_SKILL.md").read_text(encoding="utf-8")
                                  for name in ("format", "task")}
    episode = None
    with ExitStack() as stack:
        for line in requests:
            request = json.loads(line)
            if "id" not in request:
                continue
            method = request["method"]
            reply = {"jsonrpc": "2.0", "id": request["id"]}
            if method == "initialize":
                reply["result"] = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                                   "serverInfo": {"name": "terminal-evo", "version": "1.0"}}
            elif method == "tools/list":
                reply["result"] = {"tools": specs}
            elif method == "ping":
                reply["result"] = {}
            elif method == "tools/call":
                name = request["params"]["name"]
                kwargs = request["params"].get("arguments", {})
                try:
                    if name not in tools or not isinstance(kwargs, dict) or (name == "observe" and kwargs):
                        raise ValueError("Invalid tool request")
                    if episode is None:
                        directory.mkdir(parents=True, exist_ok=False)
                        if design:
                            stage = stack.enter_context(open_stage(train_routes()[task], directory / "inspection"))
                            episode = Designer(task, components, spec["allowed_link_followups"], directory, stage)
                        else:
                            episode = stack.enter_context(open_episode(task, candidate, components, directory,
                                                                       skills=skills))
                    obs = episode.task_observation if name == "observe" else episode.action(name, **kwargs)
                    with (directory / "tools.jsonl").open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps({"name": name, "arguments": kwargs,
                                                 "observation": obs.model_dump()}, ensure_ascii=False) + "\n")
                    if not design and episode.done and name == "submit":
                        coverage.save(directory / "grade.json", episode.grade())
                    reply["result"] = {"content": [{"type": "text", "text": obs.model_dump_json()}]}
                except ValueError as error:
                    reply["result"] = {"isError": True, "content": [
                        {"type": "text", "text": str(error) if design else "Invalid tool call or episode already closed."}]}
                except Exception as error:
                    if directory.is_dir():
                        coverage.save(directory / "execution-error.json",
                                      {"type": type(error).__name__, "detail": str(error)})
                    reply["result"] = {"isError": True, "content": [
                        {"type": "text", "text": "Episode execution failed."}]}
                    print(json.dumps(reply), flush=True)
                    return
            else:
                reply["error"] = {"code": -32601, "message": "Method not found"}
            print(json.dumps(reply), flush=True)


def harness_patch(session):
    """Register the component bridge in the existing SDK minimal profile."""
    return [
        {"id": "persistent-bash", "disabled": True},
        {"id": "terminal-bash", "disabled": True},
        {"insert": [{"id": "terminal-evo-mcp", "name": "@deepseek-ai/dsh-mcp-client",
                     "config": {"serverName": "terminal_evo", "transport": "stdio",
                                "command": sys.executable,
                                "args": [str(Path(__file__).resolve()), str(session.resolve())],
                                "toolCallTimeoutMs": 2_500_000,
                                "failOnStartupError": True,
                                "reconnect": {"enabled": False}}}]},
    ]


def main():
    session = Path(sys.argv[1]).resolve(strict=True)
    # Keep the MCP transport private. Native offline subprocesses must never
    # inherit its socket as stdin, including probes in the frozen materializer.
    with os.fdopen(os.dup(sys.stdin.fileno()), encoding="utf-8") as requests:
        with open(os.devnull, "rb") as stdin:
            os.dup2(stdin.fileno(), sys.stdin.fileno())
        serve_mcp(session, requests)


if __name__ == "__main__":
    main()
