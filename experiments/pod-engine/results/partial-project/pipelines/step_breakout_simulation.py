"""Pipeline: 推进一次固定时间步的 Breakout 物理模拟。从 params 或 Context 读取 commands（字典，键为 left/right/fire/pause/reset，值为 boolean）。调用 BreakoutSimulationService.execute(commands)，将返回的 entities、game_state、score、lives、ball_attached 写入 Context，并作为本 Pipeline 摘要返回。此 Pipeline 不循环，适合交互式逐帧或测试步进。
Generated: 2026-09-08T11:27:03.667756
"""

from ai_pod_cli.context import PipelineContext
from ai_pod_cli.config import load_beans
from ai_pod_cli.container import build_container, Pod
from modules.services.breakoutsimulationservice import BreakoutSimulationService


def run(ctx: PipelineContext):
    """
    推进一次固定时间步的 Breakout 物理模拟。

    从 ctx.params 或 Context 数据池读取 commands 字典，
    交由 BreakoutSimulationService 执行单步模拟。
    服务执行后会将 entities/game_state/score/lives/ball_attached
    写入 Context，并最终通过 ctx.summary() 汇总返回。
    此 Pipeline 不循环，适合交互式逐帧或测试步进。
    """
    # load_beans 仅用于加载 bean 注册表并构建容器，不读取用户配置
    beans = load_beans()
    container = build_container(beans)
    S = Pod(container)

    # 1. 读取 commands：优先取入口参数，其次取 Context 数据池中的值
    commands = ctx.params.get("commands")
    if commands is None:
        commands = ctx.get("commands", {})

    # 防御性校验，确保 commands 是字典
    if not isinstance(commands, dict):
        commands = {}

    # 2. BreakoutSimulationService 默认从 ctx.params 读取输入，因此放入参数池
    ctx.params["commands"] = commands

    # 3. 执行一次固定时间步模拟
    (S(BreakoutSimulationService)).execute_all(ctx)

    # 4. 返回执行摘要
    return ctx.summary()
