"""Agent-friendly deterministic pipeline execution command."""

import json
from datetime import datetime, timezone
from time import perf_counter

from ai_pod_cli.runner import PipelineRunner
from ai_pod_cli.run_store import write_run_trace


def handle_run(args) -> None:
    """Run one registered route and always persist a structured trace."""
    try:
        params = json.loads(args.params)
        if not isinstance(params, dict):
            raise ValueError("--params 必须是 JSON 对象")
    except (json.JSONDecodeError, ValueError) as error:
        _print(args.json, {"error": {"code": "invalid_params", "message": str(error)}})
        return

    started = perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        result, _ctx = PipelineRunner().run_with_context(args.route, params)
        trace = write_run_trace(args.route, params, result, None, (perf_counter() - started) * 1000, started_at)
    except Exception as error:
        context = getattr(error, "aipod_context", None)
        partial_result = context.summary() if context is not None else None
        trace = write_run_trace(args.route, params, partial_result, error, (perf_counter() - started) * 1000, started_at)

    if args.json:
        _print(True, trace)
    elif trace["status"] == "success":
        print(f"✅ 运行成功: {args.route} ({trace['duration_ms']} ms)")
        print(f"   Trace: {trace['trace_path']}")
    else:
        print(f"❌ 运行失败: {trace['error']['type']} — {trace['error']['message']}")
        print(f"   Trace: {trace['trace_path']}")
        raise SystemExit(1)


def _print(_json_mode: bool, value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))
