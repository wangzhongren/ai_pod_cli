"""Deterministic Pod Agent scheduler over governed build tools."""

import os

from ai_pod_cli.config import load_beans
from ai_pod_cli.pod.build import _execute_pod_build_tool, _load_routes_map
from ai_pod_cli.pod.state import (
    STAGE_BUILD_TOOLS, STAGE_NAMES,
    load_current_plan, load_decision_plan as _load_decision_plan,
    prepare_stage_rebuild, stage_index,
    resume_stage as _resume_stage,
    save_decision_plan as _save_decision_plan,
)
from ai_pod_cli.pod.verification import (
    _project_verification_fingerprint, _repair_current_artifact, _verify_application,
)
from ai_pod_cli.pod.revision import select_revision_stage


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
    """Run the resumable Pod Agent over governed five-stage Build Tools."""
    desc = _read_pod_requirement(args)
    if not desc:
        print("❌ 请提供需求描述或 --file 文件路径。")
        return
    if not os.environ.get("OPENAI_API_KEY"):
        from ai_pod_cli.commands.env import print_missing_model_config
        print_missing_model_config()
        raise SystemExit(1)
    requested_stage = str(getattr(args, "stage", "") or "").strip().lower()
    if requested_stage:
        if requested_stage == "auto":
            current = load_current_plan()
            if current is None:
                print("❌ 当前项目没有可修改的 Pod 计划。")
                raise SystemExit(1)
            if current.get("objective") == desc:
                # A same-objective invocation is a resume, not a modification.
                requested_stage = ""
            else:
                decision = select_revision_stage(
                    desc, current, _agent_project_observation(current),
                    getattr(args, "progress_callback", None),
                )
                requested_stage = decision["stage"]
                print(
                    f"🧠 [Pod 影响分析] 最早受影响层: {requested_stage}"
                    + (f" — {decision['summary']}" if decision["summary"] else "")
                )
        if not requested_stage:
            pass
        else:
            try:
                state = prepare_stage_rebuild(requested_stage, desc)
            except ValueError as error:
                print(f"❌ {error}")
                raise SystemExit(1) from error
            rebuild_from = stage_index(requested_stage)
            desc = state["objective"]
            args._pod_stage = rebuild_from
            args._pod_rebuild_from = rebuild_from
            print(
                f"🔧 [Pod 局部重建] 从 {requested_stage} 层开始；"
                "上游已冻结，下游将重新生成并验证。"
            )
    original_file = getattr(args, "file", "")
    args.file = ""
    args.desc = desc
    explicit_stage = int(args._pod_stage) if hasattr(args, "_pod_stage") else None
    state = _load_decision_plan(desc, explicit_stage)
    verification = state["agent"]["verification"]
    if (
        verification.get("status") == "passed"
        and verification.get("fingerprint") != _project_verification_fingerprint()
    ):
        verification["status"] = "pending"
    _save_decision_plan(state)
    _set_agent_status(desc, "running")
    max_steps = int(getattr(args, "_pod_agent_max_steps", 15))
    stage_failures: dict[int, int] = {}
    repair_tool_failures = 0

    print("🧠 [Pod Agent] 启动构建循环：Observe → Policy Select → Execute → Observe")
    if original_file:
        print(f"🧩 [Pod Agent] 需求来源: {original_file}")

    for _ in range(max_steps):
        state = _load_decision_plan(desc)
        stage = _resume_stage(state)
        if stage is None:
            verification = state["agent"]["verification"]
            verification_status = verification.get("status", "pending")
            if verification_status == "passed":
                _set_agent_status(desc, "complete")
                print("✅ [Pod Agent] 五阶段构建与应用运行验证均已完成。")
                return

            action = (
                "repair_current_artifact"
                if verification_status == "failed"
                else "verify_application"
            )
            step = state["agent"].get("step", 0) + 1
            print(f"\n🧠 [Pod Agent · Step {step}] {action} (application)")
            if action == "verify_application":
                result = _verify_application(
                    desc,
                    state,
                    getattr(args, "_pod_verify_timeout", None),
                )
                execution = result.get("checks", {}).get("execution")
                observation = {
                    "verification_status": result.get("status"),
                    "structure": result.get("checks", {}).get("structure", {}).get("status"),
                    "execution": execution.get("status") if execution else "skipped",
                    "command": execution.get("command", []) if execution else [],
                    "suggested_files": result.get("repair", {}).get("suggested_files", []),
                }
                event_status = "succeeded" if result.get("status") == "passed" else "failed"
                _append_agent_event(desc, {
                    "action": action,
                    "stage": "application",
                    "status": event_status,
                    "decision_status": "policy_selected",
                    "summary": "Run the frozen application's deterministic smoke or test command.",
                    "observation": observation,
                })
                if result.get("status") == "passed":
                    _set_agent_status(desc, "complete")
                    print("✅ [verify_application] 应用结构与实际运行命令均已通过。")
                    return
                print("🔁 [verify_application] 运行失败；下一步只允许修复证据指向的当前文件。")
                continue

            if int(verification.get("repairs", 0)) >= 3:
                _set_agent_status(desc, "blocked")
                print("⛔ [Pod Agent] 已达到 3 次受限修复上限，冻结上游保持不变。")
                raise SystemExit(1)
            try:
                repaired = _repair_current_artifact(
                    desc, state, getattr(args, "progress_callback", None),
                )
            except Exception as error:
                repair_tool_failures += 1
                _append_agent_event(desc, {
                    "action": action,
                    "stage": "application",
                    "status": "failed",
                    "decision_status": "policy_selected",
                    "summary": "Repair only the evidence-selected current artifact.",
                    "observation": {"error": f"{type(error).__name__}: {error}"},
                })
                if repair_tool_failures >= 2:
                    _set_agent_status(desc, "blocked")
                    print("⛔ [repair_current_artifact] 连续失败，未修改冻结上游。")
                    raise SystemExit(1)
                print("🔁 [repair_current_artifact] 补丁未通过约束，将重试当前修复工具。")
                continue
            repair_tool_failures = 0
            _append_agent_event(desc, {
                "action": action,
                "stage": "application",
                "status": "succeeded",
                "decision_status": "policy_selected",
                "summary": "Applied a bounded patch to the traceback-selected artifact.",
                "observation": repaired,
            })
            print(
                f"🩹 [repair_current_artifact] 已修复 {repaired['file']}；"
                "下一步重新执行同一验证命令。"
            )
            continue

        action = STAGE_BUILD_TOOLS[stage]
        history = state.get("agent", {}).get("history", [])
        last_action = history[-1] if history else {}
        is_retry = stage_failures.get(stage, 0) > 0 or (
            last_action.get("stage") == STAGE_NAMES[stage]
            and last_action.get("status") == "failed"
        )
        decision_status = "policy_retry" if is_retry else "policy_selected"
        summary = (
            f"Retry the current {STAGE_NAMES[stage]} stage after its governed tool failed."
            if is_retry
            else f"The {STAGE_NAMES[stage]} stage is the earliest incomplete stage."
        )

        print(
            f"\n🧠 [Pod Agent · Step {state['agent'].get('step', 0) + 1}] "
            f"{action} ({STAGE_NAMES[stage]})"
        )
        print(f"   决策摘要: {summary}")

        args._pod_stage = stage
        args.auto_repair = bool(getattr(args, "auto_repair", False) or args.yes)
        try:
            _execute_pod_build_tool(args)
        except SystemExit as error:
            stage_failures[stage] = stage_failures.get(stage, 0) + 1
            _append_agent_event(desc, {
                "action": action,
                "stage": STAGE_NAMES[stage],
                "status": "failed",
                "decision_status": decision_status,
                "summary": summary,
                "observation": {
                    "exit_code": error.code if isinstance(error.code, int) else 1,
                    "stage_status": _load_decision_plan(desc)["stages"][STAGE_NAMES[stage]]["status"],
                },
            })
            if stage_failures[stage] >= 2:
                _set_agent_status(desc, "blocked")
                print("⛔ [Pod Agent] 当前 Build Tool 连续失败，已保留冻结上游与 Agent 状态。")
                raise
            print("🔁 [Pod Agent] 已观察到失败；下一步只允许重试当前 Build Tool。")
            continue
        except Exception as error:
            _append_agent_event(desc, {
                "action": action,
                "stage": STAGE_NAMES[stage],
                "status": "failed",
                "decision_status": decision_status,
                "summary": summary,
                "observation": {"error": f"{type(error).__name__}: {error}"},
            })
            _set_agent_status(desc, "blocked")
            raise

        updated = _load_decision_plan(desc)
        _append_agent_event(desc, {
            "action": action,
            "stage": STAGE_NAMES[stage],
            "status": "succeeded",
            "decision_status": decision_status,
            "summary": summary,
            "success_criteria": [f"The {STAGE_NAMES[stage]} stage is complete and frozen."],
            "observation": _agent_project_observation(updated),
        })

    _set_agent_status(desc, "blocked")
    print(f"⛔ [Pod Agent] 达到最大步骤数 {max_steps}，已保存状态，可再次运行续接。")
    raise SystemExit(1)
