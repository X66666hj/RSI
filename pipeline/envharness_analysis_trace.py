"""Project archived Solver events into analysis input without reasoning text."""
from diagnosis import public_trace


def _is_reasoning(value):
    if not isinstance(value, dict):
        return False
    kind = value.get("type")
    if kind == "chunk":
        return _is_reasoning(value.get("chunk"))
    if kind == "block-end":
        return _is_reasoning(value.get("block"))
    return kind in ("reasoning", "reasoning-delta", "reasoning-chunks") or (
        kind == "block-start" and value.get("blockType") == "reasoning")


def _without_reasoning(value):
    if isinstance(value, list):
        return [_without_reasoning(item) for item in value if not _is_reasoning(item)]
    if isinstance(value, dict):
        return {key: _without_reasoning(item) for key, item in value.items()
                if key != "reasoning_content"}
    return value


def without_reasoning_event(event):
    """Preserve event identity and tool evidence while dropping assistant reasoning."""
    if event["type"] in ("assistant/message", "assistant/attempt") and "data" in event:
        return {**event, "data": _without_reasoning(event["data"])}
    return event


def contains_reasoning(value):
    if isinstance(value, dict):
        return _is_reasoning(value) or "reasoning_content" in value or any(
            contains_reasoning(child) for child in value.values())
    if isinstance(value, list):
        return any(contains_reasoning(child) for child in value)
    return False


def analysis_trace(directory):
    trace = public_trace(directory)
    trace["events"] = [without_reasoning_event(event) for event in trace["events"]]
    return trace
