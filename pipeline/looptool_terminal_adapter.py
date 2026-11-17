"""Adapt LoopTool schema checks and comparative label verification to Setup reviews."""
import ast
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Dict, List, Tuple

LABELS = {"accept", "revise", "reject", "unresolved"}


def load_official(directory):
    source = Path(directory) / "judgement_label_verification.py"
    provenance = json.loads((Path(directory) / "source.json").read_text())
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != provenance["sha256"]:
        raise ValueError("LoopTool source fingerprint changed")
    names = {"validate_parameter_type", "validate_function_call", "extract_date_from_instruction",
             "create_llm_judge_prompt"}
    tree = ast.parse(raw.decode())
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in functions} != names:
        raise ValueError("Pinned LoopTool functions unavailable")
    namespace = dict(json=json, re=re, Any=Any, Dict=Dict, List=List, Tuple=Tuple)
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
    return namespace


def validate_setup(candidate, official):
    tools = {"bash": {"name": "bash", "parameters": {"type": "object",
             "properties": {"command": {"type": "string"}}, "required": ["command"]}}}
    if candidate.get("rules_code") or not isinstance(candidate.get("in_env_actions"), list) or not candidate["in_env_actions"]:
        return {"passed": False, "reason": "Require Setup-only nonempty action list"}
    for action in candidate["in_env_actions"]:
        if not isinstance(action, dict):
            return {"passed": False, "reason": "Invalid action object"}
        valid, reason = official["validate_function_call"](
            {"name": action.get("name"), "arguments": action.get("kwargs")}, tools)
        if not valid or not str(action.get("kwargs", {}).get("command", "")).strip():
            return {"passed": False, "reason": reason or "Empty command"}
    return {"passed": True, "reason": "LoopTool function name, argument coverage and type checks passed"}


def validate_evidence(result, context):
    if result.get("verdict") not in LABELS:
        raise ValueError("Unknown review label")
    evidence = result.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("Review lacks checkable evidence")
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
    sources = {"public_task": context["seed"]["public_task"],
               "candidate_commands": [a["kwargs"]["command"] for a in context["candidate"]["in_env_actions"]],
               "actual_diff": context.get("actual_diff", {}),
               "controls": context.get("controls", {}),
               "finding": context["seed"]["finding"],
               "value_evidence": context.get("value_evidence")}
    verified, rejected = [], []
    for reference in evidence:
        if not isinstance(reference, dict):
            raise ValueError("Invalid evidence object")
        value = sources.get(reference.get("source"))
        quote = reference.get("quote")
        texts = list(strings(value))
        texts.append(json.dumps(value, ensure_ascii=False, sort_keys=True))
        if (not isinstance(quote, str) or not quote.strip()
                or not any(quote in text or quote in json.dumps(text, ensure_ascii=False)[1:-1] for text in texts)):
            rejected.append(reference)
        else:
            verified.append(reference)
    if not verified:
        raise ValueError("Review quote absent from supplied evidence")
    if result["verdict"] in ("accept", "reject") and not {"candidate_commands", "public_task"}.issubset(
            {ref["source"] for ref in verified}):
        raise ValueError("Review needs verified command and public-contract evidence")
    result["evidence"] = verified
    result["unverified_citations"] = rejected
    return result


def resolve_label(original, proposed, comparison):
    if comparison == "RESPONSE1_INCORRECT":
        return proposed
    if comparison == "RESPONSE2_INCORRECT":
        return original
    if comparison == "BOTH_CORRECT" and original == proposed:
        return proposed
    if comparison == "BOTH_INCORRECT":
        return "revise"
    return "unresolved"


SYSTEM = """Review a Terminal-Evo initial-state variant using only supplied evidence.
Return JSON {"verdict":"accept|revise|reject|unresolved","reason":"...",
"evidence":[{"source":"public_task|candidate_commands|actual_diff|controls|finding|value_evidence","quote":"exact contiguous text"}],
"correct_approach":"public, actionable feedback"}.
For pre-execution review accept means eligible for isolated execution checks only.
For post-execution review accept means semantic checks pass; final admission still
requires controls, completed development and frozen behavior validation.
Setup may create unfinished code or defective artifacts. These do not violate the
public goal merely because they need repair. However, reject low-value expansions
whose only change is syntax-delimiter repair, renaming, output reminders, or
placeholder/decoy files. Existence of an output write alone is not evidence of solving
the underlying image interpretation or computation challenge. Check semantic input
validity, including referenced records and required state transitions; fixed-width
length alone is insufficient. Oracle/NOP controls are not a reproduction of the
historical faulty solver. Require a separate discriminating public reproduction before
claiming the target mechanism was exercised. Judge whether
the variation makes the confirmed target decision observable, leaves legitimate
solutions possible and public requirements sufficient. Do not require Setup to fix
the historical error. Reject concrete answer leakage, hidden-test/credential access,
disabled tools, changed OS/toolchain, or broken immutable public-input contracts.
An input-content change is not automatically forbidden. Identify an explicit fixed
input requirement or verifier-owned fixture before calling it a contract violation.
Ground every verdict in exact evidence. Execution needed to resolve an uncertainty
should lead to pre-execution accept with the needed check, or unresolved, never an
invented test outcome. A solver timeout, quota error or step-budget interruption is
an execution-status issue, not evidence that the candidate task is invalid."""


def assess(directory, context, prior=None):
    from envharness_expand import JOB, ask, QwenClient, Message, read, save
    directory = Path(directory)
    previous = directory / "previous-label.json"
    if previous.exists():
        prior = read(previous)
    elif prior:
        save(previous, prior)
    official = load_official(JOB / "vendor/looptool")
    rule = validate_setup(context["candidate"], official)
    save(directory / "rule-check.json", rule)
    if not rule["passed"]:
        result = {"verdict": "revise", "reason": rule["reason"], "stage": "rule_check"}
        save(directory / "decision.json", result)
        return result
    primary = ask(directory / "semantic-review", SYSTEM, context)
    try:
        validate_evidence(primary, context)
    except ValueError as error:
        # Repair citation coverage once without changing the semantic judgement.
        repair = ask(directory / "evidence-repair-v1",
            "Return JSON evidence list only, with source and quote for each item. "
            "Supply exact contiguous quotes from candidate_commands and public_task, "
            "and any other evidence needed for the supplied judgement. Do not invent "
            "quotes or change the judgement. If it is unsupported return an empty list.",
            {"context": context, "judgement": primary, "validation_error": str(error)})
        # Both JSON [{source, quote}] and {"evidence": [...]} are valid
        # responses to the citation-only repair prompt.
        references = repair if isinstance(repair, list) else repair.get("evidence", []) if isinstance(repair, dict) else []
        repaired = dict(primary, evidence=references)
        save(directory / "evidence-repair-audit.json", {"original": primary, "repair": repair})
        try:
            primary = validate_evidence(repaired, context)
        except ValueError as repair_error:
            primary = {"verdict": "unresolved", "reason": str(repair_error), "evidence": []}
    save(directory / "proposed-label.json", primary)
    result = dict(primary)
    if prior and prior.get("verdict") in LABELS and prior["verdict"] != primary["verdict"]:
        # Represent the competing judgements as two structured tool-call labels.
        tool = {"name": "review_setup", "description": "Classify the Setup against public constraints and execution evidence",
                "parameters": {"type": "object", "properties": {"verdict": {"type": "string"},
                "reason": {"type": "string"}}, "required": ["verdict", "reason"]}}
        row = {"instruction": "<tools>\n" + json.dumps(tool) + "\n</tools>",
               "input": "Audit these review labels against this task context. Both labels may be wrong. "
                        "Defective initial artifacts are allowed when goals remain solvable. "
                        "During pre-execution, accept means permit real checks. Solver interruptions do not invalidate a task.\n"
                        + json.dumps(context, ensure_ascii=False, sort_keys=True)}
        def label_call(item):
            return "<tool_call>\n" + json.dumps({"name": "review_setup",
                   "arguments": {"verdict": item["verdict"], "reason": item.get("reason", "")}}) + "\n</tool_call>"
        prompts = official["create_llm_judge_prompt"](row, label_call(prior), label_call(primary), {"review_setup": tool})
        messages = [Message(role=m["role"], content=m["content"]) for m in prompts]
        comparison_signature = hashlib.sha256(json.dumps(
            [prior, primary], ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
        response = QwenClient(directory / ("jglv-comparison-" + comparison_signature)).chat(messages)
        labels = re.findall(r"(?<![A-Z_])(RESPONSE1_INCORRECT|RESPONSE2_INCORRECT|BOTH_CORRECT|BOTH_INCORRECT)(?![A-Z_])", response.content)
        unique = set(labels)
        comparison = next(iter(unique)) if len(unique) == 1 else "UNKNOWN"
        save(directory / "comparison.json", {"original": prior, "proposed": primary,
             "label": comparison, "raw": response.content,
             "mapping": {"RESPONSE1": "previous_review_label", "RESPONSE2": "new_evidence_based_label"}})
        result["verdict"] = resolve_label(prior["verdict"], primary["verdict"], comparison)
        result["comparison"] = comparison
        if result["verdict"] == prior["verdict"] and comparison == "RESPONSE2_INCORRECT":
            result["reason"] = prior.get("reason", "")
        elif comparison in ("BOTH_INCORRECT", "UNKNOWN") or result["verdict"] == "unresolved":
            result["reason"] = response.content
        # The old judgement cannot acquire execution evidence merely by winning.
        if not primary.get("evidence"):
            result["verdict"] = "unresolved"
    save(directory / "decision.json", result)
    return result


def expansion_brief(seed, index):
    return {"method": "LoopTool EDDE adapted to EnvHarness Setup",
            "variant_index": index, "parent_task": seed["task"],
            "core_challenge": seed["review"]["target_decision"],
            "confirmed_evidence": seed["finding"],
            "instruction": "Preserve the confirmed semantic or behavioral failure mechanism and current EVO public contracts. Reject syntax-only repairs, reminder-only changes and placeholder-only distractors. Explain the reusable skill, meaningful variation beyond repeating the parent, and a public differential reproduction. Check referenced users/objects, valid state transitions and identical fresh starting data. Do not invent a deeper root cause without evidence.",
            "admission": "Executable Setup; independent comparative review; original Oracle passes; NOP fails; completed development; target behavior observed in fresh frozen validation."}


if __name__ == "__main__":
    import argparse
    from envharness_expand import JOB, read, save
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", nargs="+", required=True, help="Parent/variant/version paths under expansion")
    args = parser.parse_args()
    rows = []
    for relative in args.audit:
        directory = (JOB / "expansion" / relative).resolve()
        if not directory.is_relative_to((JOB / "expansion").resolve()):
            raise ValueError("Audit path outside expansion")
        task = directory.parents[1].name
        seeds = sorted((JOB / "reviews" / task).glob("*/seed.json"))
        context = {"stage": "pre-execution", "seed": read(seeds[0]),
                   "candidate": read(directory / "candidate.json")}
        prior = read(directory / "pre-execution-review.json") if (directory / "pre-execution-review.json").exists() else None
        try:
            result = assess(directory / "looptool/pre-execution", context, prior)
            rows.append({"candidate": relative, "previous": prior, "result": result})
        except Exception as error:
            rows.append({"candidate": relative, "error": str(error)})
    save(JOB / "looptool-adaptation-audit.json", rows)
    print(json.dumps(rows, ensure_ascii=False))
