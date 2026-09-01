"""Typed, versioned state for resumable Pod construction plans."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Literal, TypedDict, cast


PLAN_VERSION = 6
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
    runtime_checks: list[dict[str, Any]]


class InterfaceArtifact(TypedDict, total=False):
    path: str
    role: str
    format: str
    instruction: str


class InterfaceVerification(TypedDict, total=False):
    name: str
    kind: str
    required: bool
    command: list[str]
    timeout: int


class InterfaceManifest(TypedDict, total=False):
    name: str
    kind: str
    platform: str
    instruction: str
    adapter: dict[str, str]
    artifacts: list[InterfaceArtifact]
    lifecycle: dict[str, list[str]]
    permissions: list[str]
    support: dict[str, Any]
    verify: list[InterfaceVerification]


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


def stage_index(stage: str | int) -> int:
    """Resolve a public stage name or index to the governed stage index."""
    if isinstance(stage, int):
        if 0 <= stage < len(STAGE_NAMES):
            return stage
        raise ValueError(f"Invalid Pod stage index: {stage}")
    normalized = str(stage).strip().lower()
    if normalized not in STAGE_NAMES:
        raise ValueError(
            "Pod stage must be one of: " + ", ".join(STAGE_NAMES)
        )
    return STAGE_NAMES.index(normalized)


def default_interface_verification(interface: dict[str, Any]) -> dict[str, Any]:
    """Return a portable runtime proof for one Interface delivery unit."""
    if isinstance(interface.get("adapter"), dict):
        return {
            "name": "adapter_smoke", "kind": "runtime", "required": True,
            "command": [
                "{python}", "-m", "ai_pod_cli", "interface",
                "--project-root", "{project_root}", "smoke",
                str(interface.get("name", "interface")),
            ],
            "timeout": 30,
        }
    artifacts = interface.get("artifacts", [])
    runtime = next(
        (
            str(item.get("path")) for item in artifacts
            if isinstance(item, dict) and item.get("role") == "runtime" and item.get("path")
        ),
        "",
    )
    if not runtime:
        name = str(interface.get("name", "application.py")).strip() or "application.py"
        runtime = name if name.endswith(".py") else name + ".py"
    return {
        "name": "runtime_smoke", "kind": "runtime", "required": True,
        "command": ["python", runtime, "--smoke"], "timeout": 30,
    }


def normalize_interface_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Ensure every Interface explicitly declares how it proves it can run."""
    interfaces = plan.get("interfaces", [])
    if not isinstance(interfaces, list):
        plan["interfaces"] = []
        return plan
    for raw in interfaces:
        if not isinstance(raw, dict):
            continue
        artifacts = raw.get("artifacts")
        original_name = str(raw.get("name", "application")).strip() or "application"
        name = re.sub(
            r"[^A-Za-z0-9_-]+", "-", Path(original_name).stem,
        ).strip("-") or "interface"
        raw["name"] = name
        if not isinstance(artifacts, list) or not artifacts:
            legacy_path = original_name if original_name.endswith(".py") else original_name + ".py"
            runtime_path = (
                legacy_path if Path(legacy_path).is_file()
                else f"interfaces/{name}/main.py"
            )
            artifacts = [{
                "path": runtime_path, "role": "runtime", "format": "python",
                "instruction": str(raw.get("instruction", "")),
            }]
            raw["artifacts"] = artifacts
        normalized_artifacts = []
        for artifact in artifacts:
            if not isinstance(artifact, dict) or not artifact.get("path"):
                continue
            item = dict(artifact)
            item["path"] = str(item["path"])
            item["role"] = str(item.get("role", "resource"))
            item["format"] = str(item.get("format", "text"))
            item["instruction"] = str(item.get("instruction", ""))
            normalized_artifacts.append(item)
        raw["artifacts"] = normalized_artifacts
        adapter = raw.get("adapter")
        if isinstance(adapter, dict):
            adapter.setdefault(
                "entry_path", adapter.get("path", f"interfaces/{name}/adapter.py"),
            )
            adapter.setdefault("path", adapter["entry_path"])
            adapter.setdefault("class_name", "GeneratedInterfaceAdapter")
            raw["adapter"] = adapter
        runtime_path = next(
            (
                item["path"] for item in normalized_artifacts
                if item.get("role") == "runtime"
            ),
            normalized_artifacts[0]["path"] if normalized_artifacts else "",
        )
        lifecycle = raw.get("lifecycle")
        if not isinstance(lifecycle, dict):
            lifecycle = {}
        lifecycle.setdefault("run", ["python", runtime_path] if runtime_path else [])
        lifecycle.setdefault("install", [])
        lifecycle.setdefault("uninstall", [])
        raw["lifecycle"] = lifecycle
        permissions = raw.get("permissions")
        raw["permissions"] = [str(item) for item in permissions] if isinstance(permissions, list) else []
        support = raw.get("support")
        if not isinstance(support, dict):
            support = {}
        support.setdefault("level", "supported")
        support.setdefault("manual_steps", [])
        raw["support"] = support
        fallback = default_interface_verification(raw)
        checks = raw.get("verify")
        if isinstance(checks, dict):
            checks = [checks]
        if not isinstance(checks, list) or not checks:
            checks = [fallback]
        normalized_checks = []
        for index, check in enumerate(checks):
            if not isinstance(check, dict):
                continue
            item = dict(check)
            command = item.get("command")
            if not isinstance(command, list) or not command or not all(
                isinstance(part, str) and part for part in command
            ):
                if index == 0:
                    item = dict(fallback)
                else:
                    continue
            item.setdefault("name", f"check_{index + 1}")
            item.setdefault("kind", "runtime")
            item.setdefault("required", True)
            try:
                timeout = int(item.get("timeout", 30))
            except (TypeError, ValueError):
                timeout = 30
            item["timeout"] = max(1, timeout)
            normalized_checks.append(item)
        raw["verify"] = normalized_checks or [fallback]
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


def prepare_stage_rebuild(stage: str | int, instruction: str) -> PodPlanState:
    """Invalidate one selected layer and its downstream while freezing upstream."""
    state = load_current_plan()
    if state is None:
        raise ValueError("No existing Pod plan is available to modify")
    index = stage_index(stage)
    instruction = str(instruction).strip()
    if not instruction:
        raise ValueError("Describe the change required for the selected Pod layer")

    for name in STAGE_NAMES[index:]:
        record = state["stages"][name]
        record["status"] = "pending"
        record["plan"] = None
        record.pop("reduction", None)
        record.pop("last_evidence", None)
        record.pop("runtime_checks", None)
        if name == "interfaces":
            record.pop("artifacts", None)

    state["current_stage"] = STAGE_NAMES[index]
    state["revision"] = {
        "from_stage": STAGE_NAMES[index],
        "instruction": instruction,
    }
    state["agent"]["status"] = "idle"
    state["agent"]["verification"] = {
        "status": "pending", "attempts": 0, "repairs": 0,
    }
    save_decision_plan(state)
    return state


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
