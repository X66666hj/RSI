"""Opt-in, hash-bound expansion-value gate, separate from verifier validity."""
from pathlib import Path
import hashlib
import json


def candidate_identity(candidate):
    if hasattr(candidate, "model_dump"):
        candidate = candidate.model_dump()
    return hashlib.sha256(json.dumps(candidate, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def coordinator(job):
    job = Path(job).resolve()
    if job.parent.name == "admission-workers":
        return job.parent.parent
    return job


def policy_for(job):
    path = coordinator(job) / "expansion-value-policy.json"
    return json.loads(path.read_text()) if path.exists() else None


def candidate_block(job, task, candidate):
    policy = policy_for(job)
    if not policy or not policy.get("enabled"):
        return None
    row = policy.get("tasks", {}).get(task, {})
    if row.get("status") == "eligible":
        if candidate_identity(candidate) in row.get("eligible_candidate_sha256", []):
            return None
        return "This candidate hash is not approved; only the selected, independently checked revision is eligible"
    return row.get("reason", "New candidate requires an evidence-backed expansion-value review")


def require_candidate_value(job, task, candidate):
    reason = candidate_block(job, task, candidate)
    if reason:
        # RuntimeError avoids treating a value hold as a request to auto-refine
        # the same shallow candidate with another paid generation call.
        raise RuntimeError("Expansion value gate: " + reason)


def require_seed_value(job, task):
    policy = policy_for(job)
    if not policy or not policy.get("enabled"):
        return
    row = policy.get("tasks", {}).get(task, {})
    if not row.get("generation_allowed", False):
        raise RuntimeError("Expansion value gate: " + row.get(
            "reason", "A meaningful, confirmed error mechanism is required before generation"))


def solver_position_block(job, directory):
    """Check expansion rollouts only; shared Train and sealed Test stay intact."""
    job = Path(job).resolve()
    directory = Path(directory).resolve()
    try:
        parts = directory.relative_to(job).parts
    except ValueError:
        return None
    if len(parts) >= 3 and parts[0] == "admission-workers":
        return solver_position_block(job / parts[0] / parts[1], directory)
    if len(parts) >= 5 and parts[0] == "expansion":
        source = job.joinpath(*parts[:4]) / "candidate.json"
        if not source.exists():
            return "Expansion candidate is missing"
        return candidate_block(job, parts[1], json.loads(source.read_text()))
    if len(parts) >= 3 and parts[:2] == ("engineering", "shared-baseline"):
        policy = policy_for(job)
        if policy and policy.get("enabled"):
            row = policy.get("tasks", {}).get(parts[2], {})
            if row.get("status") != "eligible":
                return row.get("reason", "No eligible expansion needs this original control")
    return None
