"""Prepare and run an interactive DeepSeek Harness expansion design session."""
import argparse
from itertools import count
import json
from pathlib import Path
import subprocess
import sys
import time

from envharness_adaptive import (
    Reproduction, coverage, design_specs, train_routes,
)
from envharness_jev import decide_components
from envharness_recovery import is_http_429

BUDGET = {"max_steps": 60, "max_tokens_per_request": 12000,
          "agent_wall_timeout_seconds": 3600, "agent_sdk_request_timeout_seconds": 3540,
          "label": "interactive-max-designer-60steps-12k-60min"}


def design_context(request, decision_directory):
    task = request["task"]
    routes = train_routes()
    if task not in routes:
        raise ValueError("Designer requires an admitted Train parent")
    reproductions = [Reproduction.model_validate(row).model_dump(exclude_none=True) for row in request["reproductions"]]
    if not reproductions:
        raise ValueError("A reproduced historical error is required")
    family = task.rsplit("-EVO-", 1)[0]
    linked_tasks = [name for name in sorted(routes)
                    if name != task and name.rsplit("-EVO-", 1)[0] == family]
    decision = decide_components(decision_directory, task, reproductions, linked_tasks)
    components = decision["components"]
    return {
        "task": task, "components": components, "reproductions": reproductions,
        "allowed_link_followups": linked_tasks if "link" in components else [],
    }


def prepare(directory, context):
    task = context["task"]
    directory.mkdir(parents=True, exist_ok=False)
    coverage.save(directory / "component-session.json", {"role": "designer", **context})
    coverage.save(directory / "input.json", {"task_id": task, "budget": BUDGET})
    coverage.save(directory / "native.patch.yml", [
        {"id": "system-prompt", "config": {"includeHarnessIdentity": False,
         "includeRuntimeContext": False, "personaPrefix": (
             "Design a Terminal-Evo variant for the reproduced failure. Use the offline tools to inspect "
             "the actual task before proposing. Preserve the original public goals and allowed tools. "
             "Use exactly the selected components and a meaningful change. Use Rules for Bash output "
             "pagination, Link for a separate same-family follow-up with public context, and Setup only "
             "for changes to public initial data. Do not include solutions, expected answers, grader "
             "contents, outside URLs, or a Git history in a candidate. Call propose with the candidate "
             "object after successful Bash inspection, then finish.")}},
    ])
    (directory / "prompt.txt").write_text(
        "Inspect the public task, then design and self-check one candidate.\n\n"
        + json.dumps({**context, "designer_tools": design_specs()}, ensure_ascii=False), encoding="utf-8")


def run_designer(directory, request):
    context = design_context(request, directory / "component-selection")
    for attempt in count(1):
        session = directory / f"attempt-{attempt:03}"
        result = session / "designer-agent.json"
        if not session.exists():
            prepare(session, context)
            with (session / "process.log").open("xb") as log:
                subprocess.run([sys.executable, str(Path(__file__).with_name(
                    "envharness_deepseek_designer_agent.py")), str(session)], check=True,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    timeout=BUDGET["agent_wall_timeout_seconds"])
        frozen = coverage.read(session / "component-session.json")
        if frozen != {"role": "designer", **context}:
            raise ValueError("Designer request changed on resume")
        if not result.is_file():
            raise RuntimeError("Designer attempt has no execution receipt")
        row = coverage.read(result)
        if is_http_429(row.get("error")):
            retry = session / "retry.json"
            if not retry.exists():
                coverage.save(retry, {"retry_at": time.time() + 60, "reason": "http_429"})
            time.sleep(max(0, coverage.read(retry)["retry_at"] - time.time()))
            continue
        if row.get("finish_reason") != "completed" or row.get("error"):
            raise RuntimeError("Designer execution failed; see execution receipt")
        return session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    with coverage.lock(args.output.with_suffix(".lock"), nonblocking=True):
        session = run_designer(args.output, coverage.read(args.request))
        print(session)


if __name__ == "__main__":
    main()
