"""Agent-facing project inspection command."""

import json

from ai_pod_cli.project_model import ProjectModelError, inspect_project


def handle_inspect(args) -> None:
    """Print a machine-readable view of the current AIPod project."""
    if args.target in {"component", "pipeline", "run"} and not args.name:
        _print_error(args.json, "missing_name", f"inspect {args.target} 需要提供名称")
        return
    try:
        result = inspect_project(args.target, args.name, args.summary)
    except ProjectModelError as error:
        _print_error(args.json, "project_model_error", str(error))
        return

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    summary = result.get("summary", {})
    if summary:
        print("📋 AIPod Project Model")
        print(f"   元素: {summary['component_count']} | model: {summary.get('model_count', 0)} | provider: {summary['provider_count']} | service: {summary['service_count']}")
        print(f"   Pipeline: {summary['pipeline_count']} | 校验: {'通过' if result['validation']['valid'] else '发现问题'}")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


def _print_error(json_mode: bool, code: str, message: str) -> None:
    if json_mode:
        print(json.dumps({"error": {"code": code, "message": message}}, ensure_ascii=False))
    else:
        print(f"❌ {message}")
