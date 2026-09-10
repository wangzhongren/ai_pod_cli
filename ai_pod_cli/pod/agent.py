"""Pod entry point and public progress state for autonomous workspace Agents."""

import os

from ai_pod_cli.config import load_beans
from ai_pod_cli.pod.routes import load_routes_map as _load_routes_map
from ai_pod_cli.pod.state import (
    STAGE_NAMES,
    load_current_plan, load_decision_plan as _load_decision_plan,
    prepare_stage_rebuild,
    save_decision_plan as _save_decision_plan,
)
from ai_pod_cli.pod.revision import select_revision_stage
from ai_pod_cli.workspace_agent import DEFAULT_AGENT_MAX_STEPS, DEFAULT_POD_MAX_STEPS


def _agent_project_observation(state: dict) -> dict:
    """Return compact public state that lets the Pod Agent choose its next tool."""
    beans = load_beans().get("beans", [])
    counts = {"model": 0, "provider": 0, "service": 0}
    for bean in beans:
        category = bean.get("category")
        if category in counts and bean.get("status") != "invalid":
            counts[category] += 1
    return {
        "current_stage": state.get("current_stage"),
        "stages": {
            name: state.get("stages", {}).get(name, {}).get("status", "pending")
            for name in STAGE_NAMES
        },
        "component_counts": counts,
        "routes": list(_load_routes_map()),
        "verification": {
            key: value
            for key, value in state.get("agent", {}).get("verification", {}).items()
            if key in {"status", "attempts", "repairs", "command", "repaired_file"}
        },
        "recent_actions": [
            {
                key: item.get(key)
                for key in ("step", "action", "stage", "status", "summary")
                if key in item
            }
            for item in state.get("agent", {}).get("history", [])[-6:]
        ],
    }


def _append_agent_event(desc: str, event: dict) -> dict:
    """Persist one public Agent action/observation without hidden reasoning."""
    state = _load_decision_plan(desc)
    agent = state["agent"]
    agent["step"] = int(agent.get("step", 0)) + 1
    normalized = {"step": agent["step"], **event}
    history = list(agent.get("history", []))
    history.append(normalized)
    agent["history"] = history[-50:]
    agent["status"] = normalized.get("status", "running")
    agent["last_action"] = normalized.get("action")
    agent["last_observation"] = normalized.get("observation", {})
    _save_decision_plan(state)
    return state


def _set_agent_status(desc: str, status: str) -> None:
    state = _load_decision_plan(desc)
    state["agent"]["status"] = status
    if status == "complete":
        state.pop("revision", None)
    _save_decision_plan(state)


def _read_pod_requirement(args) -> str:
    if getattr(args, "file", ""):
        if not os.path.exists(args.file):
            print(f"❌ 文件不存在: {args.file}")
            return ""
        with open(args.file, "r", encoding="utf-8") as file:
            return file.read().strip()
    return str(getattr(args, "desc", "") or "").strip()


def handle_pod(args):
    """Run autonomous layer Agents in one workspace, with Pod-owned change approval."""
    from ai_pod_cli.client import call_llm
    from ai_pod_cli.pod.coordinator import PodCoordinator
    from ai_pod_cli.config import init_config_if_not_exists
    from pathlib import Path

    desc = _read_pod_requirement(args)
    if not desc:
        raise ValueError("Provide a Pod objective or requirement file")
    if not os.environ.get("OPENAI_API_KEY"):
        from ai_pod_cli.commands.env import print_missing_model_config
        print_missing_model_config()
        raise SystemExit(1)
    init_config_if_not_exists()
    requested = str(getattr(args, "stage", "") or "").strip().lower()
    current = load_current_plan()
    if requested == "auto":
        if current and current.get("objective") != desc:
            requested = select_revision_stage(desc, current, _agent_project_observation(current), getattr(args, "progress_callback", None))["stage"]
        else:
            requested = ""
    if requested:
        state = prepare_stage_rebuild(requested, desc)
    else:
        state = _load_decision_plan(desc, getattr(args, "_pod_stage", None))
    state["agent"].get("verification", {}).pop("required_action", None)
    state["agent"]["current_request"] = desc
    coordinator = PodCoordinator(Path.cwd(), state, call_llm, save=_save_decision_plan,
                                 progress_callback=getattr(args, "progress_callback", None),
                                 max_steps=int(getattr(args, "_pod_agent_max_steps", DEFAULT_AGENT_MAX_STEPS)),
                                 pod_max_steps=int(getattr(args, "_pod_max_steps", DEFAULT_POD_MAX_STEPS)))
    try:
        coordinator.build()
    except Exception:
        state["agent"]["status"] = "blocked"
        coordinator.persist()
        raise
    print("✅ Pod 各层 Agent 已完成，开发检查结果已记录。")
