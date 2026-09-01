"""Canonical, machine-readable representation of an AIPod project."""

import ast
import json
import os
from pathlib import Path

import tomlkit

from ai_pod_cli.config import CONFIG_FILE, ROUTES_TOML
from ai_pod_cli.contracts import analyze_pipeline_contracts
from ai_pod_cli.run_store import get_run_trace, list_run_traces
from ai_pod_cli.pod.state import load_current_plan


SCHEMA_VERSION = "1.0"
POD_PLAN_FILE = Path("aipod_plan.json")


class ProjectModelError(ValueError):
    """Raised when the requested project-model view cannot be produced."""


def extract_pipeline_services(pipeline_path: str, class_to_id: dict[str, str]) -> list[str]:
    """Read ordered ``S(Service)`` calls without importing or executing the pipeline."""
    path = Path(pipeline_path)
    if not path.exists():
        return []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []

    services = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "S":
            continue
        if node.args and isinstance(node.args[0], ast.Name):
            component_id = class_to_id.get(node.args[0].id)
            if component_id and component_id not in services:
                services.append(component_id)
    return services


def load_project_graph() -> tuple[list[dict], list[dict]]:
    """Return registry components and statically parsed route pipelines."""
    if not os.path.exists(CONFIG_FILE):
        raise ProjectModelError(f"未找到 {CONFIG_FILE}。请先运行 aipod init。")
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            beans = json.load(f).get("beans", [])
    except (OSError, json.JSONDecodeError) as error:
        raise ProjectModelError(f"无法读取 {CONFIG_FILE}: {error}") from error

    class_to_id = {
        bean.get("class_path", "").rsplit(".", 1)[-1]: bean["id"]
        for bean in beans
        if bean.get("id") and bean.get("class_path")
    }
    routes = []
    if os.path.exists(ROUTES_TOML):
        try:
            with open(ROUTES_TOML, "r", encoding="utf-8") as f:
                route_doc = tomlkit.load(f)
        except (OSError, tomlkit.exceptions.TOMLKitError) as error:
            raise ProjectModelError(f"无法读取 {ROUTES_TOML}: {error}") from error
        for name, route in route_doc.items():
            if isinstance(route, dict) and route.get("pipeline"):
                pipeline_path = str(route["pipeline"])
                routes.append({
                    "name": str(name),
                    "pipeline": pipeline_path,
                    "description": str(route.get("description", "")),
                    "services": extract_pipeline_services(pipeline_path, class_to_id),
                    "exists": Path(pipeline_path).exists(),
                })
    return beans, routes


def load_pod_agent_state() -> dict | None:
    """Return compact, public Pod Agent state without frozen plan prompt content."""
    state = load_current_plan()
    if state is None:
        return None
    agent = state.get("agent", {})
    return {
        "objective": state.get("objective", ""),
        "status": agent.get("status", "idle"),
        "step": agent.get("step", 0),
        "current_stage": state.get("current_stage"),
        "stages": {
            name: record.get("status", "pending")
            for name, record in state.get("stages", {}).items()
            if isinstance(record, dict)
        },
        "stage_evidence": {
            name: list(record.get("runtime_checks", []))
            for name, record in state.get("stages", {}).items()
            if isinstance(record, dict) and record.get("runtime_checks")
        },
        "last_action": agent.get("last_action"),
        "last_observation": agent.get("last_observation", {}),
        "verification": {
            key: value
            for key, value in agent.get("verification", {}).items()
            if key in {"status", "attempts", "repairs", "command", "repaired_file"}
        },
        "recent_actions": [
            {
                key: item.get(key)
                for key in ("step", "action", "stage", "status", "summary")
                if key in item
            }
            for item in agent.get("history", [])[-6:]
            if isinstance(item, dict)
        ],
    }


def build_project_model() -> dict:
    """Build the stable Agent Project Model used by inspect and visualize."""
    beans, pipelines = load_project_graph()
    component_fields = ("id", "category", "type", "class_path", "file", "dependencies", "inputs", "outputs", "methods", "description")
    components = [{field: bean[field] for field in component_fields if field in bean} for bean in beans]
    component_ids = {component.get("id") for component in components}
    issues = []
    warnings = []
    for component in components:
        for dependency in component.get("dependencies", []):
            if dependency not in component_ids:
                issues.append({
                    "code": "missing_dependency",
                    "component": component["id"],
                    "dependency": dependency,
                })
    for pipeline in pipelines:
        if not pipeline["exists"]:
            issues.append({
                "code": "missing_pipeline_file",
                "pipeline": pipeline["name"],
                "path": pipeline["pipeline"],
            })
        contract = analyze_pipeline_contracts(pipeline.get("services", []), components)
        pipeline["contract"] = contract
        for issue in contract["issues"]:
            issues.append({**issue, "pipeline": pipeline["name"]})
        for warning in contract.get("warnings", []):
            warnings.append({**warning, "pipeline": pipeline["name"]})

    provider_count = sum(component.get("category") == "provider" for component in components)
    model_count = sum(component.get("category") == "model" for component in components)
    service_count = sum(component.get("category") == "service" for component in components)
    summary = {
        "component_count": len(components),
        "provider_count": provider_count,
        "model_count": model_count,
        "service_count": service_count,
        "pipeline_count": len(pipelines),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "project_root": str(Path.cwd()),
        "summary": summary,
        "pod_agent": load_pod_agent_state(),
        "components": components,
        "pipelines": pipelines,
        "validation": {"valid": not issues, "issues": issues, "warnings": warnings},
    }


def inspect_project(target: str = "project", name: str = "", summary_only: bool = False) -> dict:
    """Select a compact, agent-friendly view from the full project model."""
    model = build_project_model()
    base = {"schema_version": model["schema_version"], "project_root": model["project_root"], "validation": model["validation"]}
    if summary_only:
        return {**base, "summary": model["summary"]}
    if target == "project":
        return model
    if target == "components":
        return {**base, "components": model["components"]}
    if target == "pipelines":
        return {**base, "pipelines": model["pipelines"]}
    if target == "component":
        component = next((item for item in model["components"] if item.get("id") == name), None)
        if component is None:
            raise ProjectModelError(f"未找到组件: {name}")
        return {**base, "component": component}
    if target == "pipeline":
        pipeline = next((item for item in model["pipelines"] if item.get("name") == name), None)
        if pipeline is None:
            raise ProjectModelError(f"未找到 Pipeline: {name}")
        return {**base, "pipeline": pipeline}
    if target == "runs":
        return {**base, "runs": list_run_traces()}
    if target == "run":
        trace = get_run_trace(name)
        if trace is None:
            raise ProjectModelError(f"未找到运行 Trace: {name}")
        return {**base, "run": trace}
    raise ProjectModelError(f"不支持的查看范围: {target}")
