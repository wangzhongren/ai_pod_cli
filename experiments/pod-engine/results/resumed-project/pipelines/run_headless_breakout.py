"""Pipeline: 运行一场完整的无头 Breakout 游戏。使用 Runtime repeat 节点反复调用 BreakoutSimulationService.execute(commands) 推进固定时间步，直到 game_state 为 'won' 或 'lost'，或迭代次数达到 max_frames（取自 params.max_frames 或配置 run.max_frames，默认 120）。每次迭代将返回的 entities、game_state、score、lives、ball_attached 写入 Context，并维护递增的 Context 字段 frame_count。commands 若未由 params/Context 提供，则默认初始一帧发送 fire=true 让球离开挡板，之后按键均为 false。循环结束后，先调用 PngSnapshotService.execute(entities=context.entities, game_state=context.game_state, score=context.score, lives=context.lives, frame_count=context.frame_count, screenshot_path=params.screenshot_path 或配置 run.screenshot_path)，将写入的 screenshot_path 保存到 Context；再调用 RunSummaryService.execute(screenshot_path=context.screenshot_path, game_state=context.game_state, summary_path=params.summary_path 或配置 run.summary_path, outcome=('cleared' if game_state=='won' else 'lost' if game_state=='lost' else 'incomplete'), final_state=('terminal' if game_state in ('won','lost') else 'timeout'))。最终返回包含 frame_count、entities、game_state、score、lives、ball_attached、screenshot_path、summary_path 的摘要 dict。
Generated: 2026-09-08T13:18:04.678092
"""

# -*- coding: utf-8 -*-
from ai_pod_cli.context import PipelineContext
from ai_pod_cli.config import load_beans
from ai_pod_cli.container import build_container, Pod, repeat

from modules.services.breakoutsimulationservice import BreakoutSimulationService
from modules.services.pngsnapshotservice import PngSnapshotService
from modules.services.runsummaryservice import RunSummaryService


class _AdvanceFrame:
    """每一轮模拟前，计算并写入本帧 commands。"""

    def execute(self, ctx: PipelineContext):
        frame_count = int(ctx.get("frame_count", 0))
        user_commands = ctx.get("user_commands")

        if user_commands is not None:
            # 用户/上下文显式提供了 commands，则按其意图执行
            commands = dict(user_commands)
        else:
            # 默认控制：第一帧让球离开挡板，之后保持松开
            commands = {"fire": frame_count == 0}

        ctx.params["commands"] = commands


class _FinalizeFrame:
    """每一轮模拟后，递增 frame_count 并判断是否需要结束循环。"""

    def execute(self, ctx: PipelineContext):
        frame_count = int(ctx.get("frame_count", 0)) + 1
        ctx.set("frame_count", frame_count)

        max_frames = int(ctx.get("max_frames", 120))
        game_state = ctx.get("game_state", "running")

        if game_state in ("won", "lost") or frame_count >= max_frames:
            ctx.set("terminated", True)
        else:
            ctx.set("terminated", False)


def run(ctx: PipelineContext):
    # 构建容器；该 load_beans 仅用于 build_container
    beans = load_beans()
    container = build_container(beans)
    S = Pod(container)

    # 收集 commands 来源：params 或 Context；若均未提供则使用默认控制
    commands = ctx.params.get("commands")
    if commands is None:
        commands = ctx.get("commands")
    if commands is not None:
        ctx.set("user_commands", dict(commands))

    # 最多运行帧数：优先 params.max_frames / Context.max_frames，否则默认 120
    max_frames = ctx.params.get("max_frames")
    if max_frames is None:
        max_frames = ctx.get("max_frames")
    if max_frames is None:
        max_frames = 120
    ctx.set("max_frames", int(max_frames))

    # 初始化循环状态
    ctx.set("frame_count", int(ctx.get("frame_count", 0)))
    ctx.set("terminated", False)

    # 每轮：更新 commands -> 推进一帧模拟 -> 更新结束标志
    game_loop = (
        S(_AdvanceFrame)
        | S(BreakoutSimulationService)
        | S(_FinalizeFrame)
    )

    repeat(
        game_loop,
        until_field="terminated",
        max_iterations_field="max_frames",
    ).execute_all(ctx)

    # ============ 游戏结束后：渲染 PNG 快照 ============
    snapshot_inputs = {
        "entities": ctx.get("entities", []),
        "game_state": ctx.get("game_state", "running"),
        "score": ctx.get("score", 0),
        "lives": ctx.get("lives", 0),
        "frame_count": ctx.get("frame_count", 0),
    }

    # 若 params 提供了显式 screenshot_path，则传递给服务；
    # 未提供时由 PngSnapshotService 内部通过 ConfigStore 读取 run.screenshot_path
    screenshot_path = ctx.params.get("screenshot_path")
    if screenshot_path is None:
        screenshot_path = ctx.get("screenshot_path")
    if screenshot_path is not None:
        snapshot_inputs["screenshot_path"] = screenshot_path

    ctx.params.update(snapshot_inputs)
    (S(PngSnapshotService)).execute_all(ctx)

    resolved_screenshot_path = ctx.get("screenshot_path")
    if not resolved_screenshot_path:
        resolved_screenshot_path = ctx.params.get("screenshot_path")
    ctx.set("screenshot_path", resolved_screenshot_path or "")

    # ============ 生成运行摘要 JSON ============
    game_state = ctx.get("game_state", "running")
    if game_state == "won":
        outcome = "cleared"
    elif game_state == "lost":
        outcome = "lost"
    else:
        outcome = "incomplete"

    final_state = "terminal" if game_state in ("won", "lost") else "timeout"

    summary_inputs = {
        "game_state": game_state,
        "outcome": outcome,
        "final_state": final_state,
    }

    if resolved_screenshot_path:
        summary_inputs["screenshot_path"] = resolved_screenshot_path

    summary_path = ctx.params.get("summary_path")
    if summary_path is None:
        summary_path = ctx.get("summary_path")
    if summary_path is not None:
        summary_inputs["summary_path"] = summary_path

    ctx.params.update(summary_inputs)
    (S(RunSummaryService)).execute_all(ctx)

    resolved_summary_path = ctx.get("summary_path")
    if not resolved_summary_path:
        resolved_summary_path = ctx.params.get("summary_path")
    ctx.set("summary_path", resolved_summary_path or "")

    # 确保最终结果中显式保留所需字段
    ctx.set("frame_count", ctx.get("frame_count", 0))
    ctx.set("entities", ctx.get("entities", []))
    ctx.set("game_state", ctx.get("game_state", "running"))
    ctx.set("score", ctx.get("score", 0))
    ctx.set("lives", ctx.get("lives", 0))
    ctx.set("ball_attached", ctx.get("ball_attached", False))
    ctx.set("screenshot_path", resolved_screenshot_path or "")
    ctx.set("summary_path", resolved_summary_path or "")

    return ctx.summary()
