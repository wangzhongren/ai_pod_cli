"""Persistent, sanitized execution traces for AI agents and human review."""

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


RUNS_DIR = Path(".aipod") / "runs"
SENSITIVE_MARKERS = ("key", "secret", "password", "token", "passwd", "api_key", "apikey", "authorization")


def redact(value, field_name: str = ""):
    """Return a JSON-safe value with common secret fields redacted."""
    lowered = field_name.lower().replace("_", "")
    if any(marker.replace("_", "") in lowered for marker in SENSITIVE_MARKERS):
        return "***"
    if isinstance(value, dict):
        return {str(key): redact(item, str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    try:
        json.dumps(value)
        return value
    except TypeError:
        return repr(value)


def write_run_trace(
    route: str,
    params: dict,
    result: dict | None,
    error: Exception | None,
    duration_ms: float,
    started_at: str | None = None,
) -> dict:
    """Persist one execution trace and return its machine-readable record."""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    run_id = f"run_{now.strftime('%Y%m%dT%H%M%SZ')}_{uuid4().hex[:8]}"
    structured_failure = (
        isinstance(result, dict)
        and result.get("status") == "failure"
        and isinstance(result.get("error"), dict)
    )
    failed = error is not None or structured_failure
    if structured_failure:
        trace_error = redact(result["error"])
        trace_error.setdefault("type", "Failure")
    elif error is not None:
        trace_error = {"type": type(error).__name__, "message": str(error)}
    else:
        trace_error = None
    if isinstance(result, dict) and isinstance(result.get("context"), dict):
        steps = result["context"].get("steps", [])
    else:
        steps = result.get("steps", []) if isinstance(result, dict) else []
    trace = {
        "schema_version": "1.0",
        "run_id": run_id,
        "route": route,
        "status": "failed" if failed else "success",
        "started_at": started_at or now.isoformat(),
        "duration_ms": round(duration_ms, 3),
        "params": redact(params),
        "result": redact(result),
        "steps": redact(steps),
        "error": trace_error,
    }
    path = RUNS_DIR / f"{run_id}.json"
    path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**trace, "trace_path": str(path)}


def list_run_traces() -> list[dict]:
    """Return trace summaries, newest first, without loading user code."""
    if not RUNS_DIR.exists():
        return []
    traces = []
    for path in sorted(RUNS_DIR.glob("run_*.json"), reverse=True):
        try:
            trace = json.loads(path.read_text(encoding="utf-8"))
            traces.append({
                "run_id": trace.get("run_id"), "route": trace.get("route"), "status": trace.get("status"),
                "started_at": trace.get("started_at"), "duration_ms": trace.get("duration_ms"), "trace_path": str(path),
            })
        except (OSError, json.JSONDecodeError):
            continue
    return traces


def get_run_trace(run_id: str) -> dict | None:
    """Load a trace by id, or return None when it does not exist."""
    path = RUNS_DIR / f"{run_id}.json"
    try:
        trace = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return {**trace, "trace_path": str(path)}
