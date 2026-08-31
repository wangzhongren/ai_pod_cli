"""Typed, versioned state for resumable Pod construction plans."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal, TypedDict, cast


PLAN_VERSION = 4
DECISION_PLAN_FILE = Path("aipod_plan.json")
STAGE_NAMES = ("models", "providers", "services", "pipelines", "interfaces")
STAGE_BUILD_TOOLS = (
    "generate_models", "generate_providers", "generate_services",
    "compose_pipelines", "generate_interfaces",
)

StageStatus = Literal["pending", "in_progress", "complete", "conflict"]


class VerificationState(TypedDict, total=False):
    status: str
    attempts: int
    repairs: int
    command: list[str]
    commands: list[list[str]]
    timeout: int
    fingerprint: str
    repaired_file: str
    last_result: dict[str, Any]


class StageState(TypedDict, total=False):
    status: StageStatus | str
    plan: dict[str, Any] | None
    reduction: dict[str, Any]
    artifacts: list[str]


class AgentState(TypedDict, total=False):
    status: str
    step: int
    history: list[dict[str, Any]]
    verification: VerificationState
    last_action: str
    last_observation: dict[str, Any]


class PodPlanState(TypedDict):
    version: int
    objective: str
    current_stage: str
    stages: dict[str, StageState]
    agent: AgentState


def default_interface_verification(interface: dict[str, Any]) -> dict[str, Any]:
    """Return a portable, non-interactive proof command for one Interface."""
    name = str(interface.get("name", "application.py")).strip() or "application.py"
    if not name.endswith(".py"):
        name += ".py"
    return {"command": ["python", name, "--smoke"], "timeout": 30}


def normalize_interface_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Ensure every Interface explicitly declares how it proves it can run."""
    interfaces = plan.get("interfaces", [])
    if not isinstance(interfaces, list):
        plan["interfaces"] = []
        return plan
    for raw in interfaces:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", "application.py")).strip() or "application.py"
        if not name.endswith(".py"):
            name += ".py"
        raw["name"] = name
        fallback = default_interface_verification(raw)
        verify = raw.get("verify")
        if not isinstance(verify, dict):
            verify = {}
            raw["verify"] = verify
        command = verify.get("command")
        if not isinstance(command, list) or not command or not all(
            isinstance(item, str) and item for item in command
        ) or name not in command or "--smoke" not in command:
            verify["command"] = fallback["command"]
        try:
            timeout = int(verify.get("timeout", fallback["timeout"]))
        except (TypeError, ValueError):
            timeout = fallback["timeout"]
        verify["timeout"] = max(1, timeout)
    return plan


def _new_plan(desc: str, explicit_stage: int | None = None) -> PodPlanState:
    stage = max(0, min(int(explicit_stage or 0), len(STAGE_NAMES) - 1))
    state: PodPlanState = {
        "version": PLAN_VERSION,
        "objective": desc,
        "current_stage": STAGE_NAMES[stage],
        "stages": {
            name: {"status": "pending", "plan": None}
            for name in STAGE_NAMES
        },
        "agent": {
            "status": "idle", "step": 0, "history": [],
            "verification": {"status": "pending", "attempts": 0, "repairs": 0},
        },
    }
    if explicit_stage is not None:
        for name in STAGE_NAMES[:stage]:
            state["stages"][name]["status"] = "complete"
    return state


def load_and_upgrade_plan(
    candidate: dict[str, Any] | None,
    desc: str,
    explicit_stage: int | None = None,
) -> PodPlanState:
    """Upgrade a compatible plan in memory and preserve unknown public fields."""
    if not isinstance(candidate, dict) or candidate.get("objective") != desc:
        return _new_plan(desc, explicit_stage)

    state = cast(PodPlanState, candidate)
    stages = state.setdefault("stages", {})
    for name in STAGE_NAMES:
        record = stages.setdefault(name, {"status": "pending", "plan": None})
        record.setdefault("status", "pending")
        record.setdefault("plan", None)
        plan = record.get("plan")
        if name == "interfaces" and isinstance(plan, dict):
            normalize_interface_plan(plan)

    state.setdefault("current_stage", STAGE_NAMES[0])
    agent = state.setdefault("agent", {})
    agent.setdefault("status", "idle")
    agent.setdefault("step", 0)
    agent.setdefault("history", [])
    verification = agent.setdefault("verification", {})
    verification.setdefault("status", "pending")
    verification.setdefault("attempts", 0)
    verification.setdefault("repairs", 0)
    state["version"] = PLAN_VERSION
    return state


def load_decision_plan(desc: str, explicit_stage: int | None = None) -> PodPlanState:
    candidate = None
    if DECISION_PLAN_FILE.exists():
        try:
            candidate = json.loads(DECISION_PLAN_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            candidate = None
    return load_and_upgrade_plan(candidate, desc, explicit_stage)


def load_current_plan() -> PodPlanState | None:
    """Read and upgrade the current plan when its objective is available."""
    try:
        candidate = json.loads(DECISION_PLAN_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    objective = candidate.get("objective") if isinstance(candidate, dict) else None
    if not isinstance(objective, str) or not objective:
        return None
    return load_and_upgrade_plan(candidate, objective)


def save_decision_plan(state: PodPlanState | dict[str, Any]) -> None:
    """Atomically persist compact decisions; never persist hidden reasoning text."""
    temporary = DECISION_PLAN_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, DECISION_PLAN_FILE)


def resume_stage(state: PodPlanState | dict[str, Any]) -> int | None:
    for index, name in enumerate(STAGE_NAMES):
        if state["stages"][name].get("status") != "complete":
            return index
    return None


def set_stage_status(state: PodPlanState, stage: int, status: StageStatus) -> None:
    name = STAGE_NAMES[stage]
    state["current_stage"] = name
    state["stages"][name]["status"] = status
    save_decision_plan(state)
