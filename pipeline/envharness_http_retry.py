"""Retry only definitively rejected HTTP 429 requests, preserving their input."""
from datetime import datetime, timezone
import json
import time
import urllib.error
import urllib.request

RETRY_SECONDS = 60


def request_json(request, call, model, read, save, *, opener=None, sleep=None, clock=None):
    opener = opener or urllib.request.urlopen
    sleep = sleep or time.sleep
    clock = clock or time.time
    state_path = call / "retry-state.json"
    state = read(state_path) if state_path.exists() else {}
    if state.get("phase") == "completed" and (call / "response.json").exists():
        return read(call / "response.json"), int(state.get("retry_count", 0))
    if state.get("phase") not in {None, "cooldown"}:
        raise RuntimeError("Prior model request is not a confirmed retryable 429; inspect its outcome")
    attempt = int(state.get("attempt", 0))
    retries = int(state.get("retry_count", 0))
    authorization = request.get_header("Authorization", "")
    secret = authorization.partition(" ")[2]
    while True:
        if state.get("phase") == "cooldown":
            remaining = max(0, float(state["next_retry_epoch"]) - clock())
            if remaining:
                sleep(remaining)
        attempt += 1
        save(state_path, {"phase": "requesting", "attempt": attempt, "retry_count": retries,
                          "model": model, "at": datetime.now(timezone.utc).isoformat()})
        try:
            with opener(request, timeout=600) as response:
                result = json.loads(response.read())
            # Save the response before marking the operation complete.
            save(call / "response.json", result)
            save(state_path, {"phase": "completed", "attempt": attempt, "retry_count": retries, "model": model})
            return result, retries
        except urllib.error.HTTPError as error:
            body = error.read().decode(errors="replace")
            if secret:
                body = body.replace(secret, "[REDACTED]")
            record = {"type": "HTTPError", "http_status": error.code, "attempt": attempt,
                      "model": model, "at": datetime.now(timezone.utc).isoformat(), "body": body[:4096]}
            save(call / "attempts" / f"attempt-{attempt:04}.json", record)
            if error.code != 429:
                save(call / "error.json", record)
                save(state_path, dict(record, phase="failed_non_retryable"))
                raise RuntimeError("Non-429 model request failed; inspect durable record") from None
            retries += 1
            state = dict(record, phase="cooldown", retry_count=retries,
                         next_retry_epoch=clock() + RETRY_SECONDS)
            save(state_path, state)
        except Exception as error:
            record = {"phase": "unknown", "type": type(error).__name__, "attempt": attempt,
                      "retry_count": retries, "model": model, "at": datetime.now(timezone.utc).isoformat()}
            save(call / "error.json", record)
            save(state_path, record)
            raise RuntimeError("Model request outcome unknown; inspect before retry") from None
