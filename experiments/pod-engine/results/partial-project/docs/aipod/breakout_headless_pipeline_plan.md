# Pod Plan: breakout_headless_pipeline

> 生成时间: 2026-09-08 11:38:03

## 需求描述

创建一个可实际运行的 Python 2D 小型游戏引擎，并提供可玩的演示游戏，用来验证 AIPod 是否能开发非业务系统。

范围是一个可扩展的单机 2D 小引擎，不要求 3D、联网、编辑器或商业引擎完整能力。使用 pygame-ce 完成图形和键盘输入，运行期间不调用任何模型服务。

需要具备：
- 实体的位置、速度、尺寸和启用状态。
- 固定时间步的更新循环，与图形显示/输入处理正确配合。
- 矩形/圆形的基础碰撞检测及反弹，明确墙、球、挡板、砖块的行为。
- 一个完整打砖块演示：左右移动挡板、发球、消除砖块、分数、胜负状态、暂停和重置。
- 键盘左右方向键或 A/D 移动，空格发球，P 暂停，R 重置，关闭窗口可退出。
- 核心更新逻辑可以在没有窗口、没有用户输入时独立运行，便于自动测试。
- 可交互运行；也支持无窗口模式、固定帧数和固定随机种子，能明确结束，并保存最终画面 PNG 和可解析的 JSON 运行摘要。
- 默认演示分辨率 800x600，无需下载外部图片、字体或音频资产。
- 输出简明 README，说明依赖、交互启动、无窗口验证、保存截图及扩展实体/场景的方法。

完整构建后必须执行真实验证。至少覆盖：运行 120 帧后正常结束；墙面/挡板反弹；砖块命中及分数变化；暂停不更新物理位置；重置恢复初始状态。验证必须失败时返回非零状态，不能用空测试或固定成功输出代替。

请自主完成设计、代码生成、组装与验证，交付可以启动的引擎和演示。

## 组件拆解

共 0 个组件：

## Pipeline 规划

### 1. step_breakout_simulation
> 推进一次固定时间步的 Breakout 物理模拟。从 params 或 Context 读取 commands（字典，键为 left/right/fire/pause/reset，值为 boolean）。调用 BreakoutSimulationService.execute(commands)，将返回的 entities、game_state、score、lives、ball_attached 写入 Context，并作为本 Pipeline 摘要返回。此 Pipeline 不循环，适合交互式逐帧或测试步进。

### 2. run_headless_breakout
> 运行一场完整的无头 Breakout 游戏。使用 Runtime repeat 节点反复调用 BreakoutSimulationService.execute(commands) 推进固定时间步，直到 game_state 为 'won' 或 'lost'，或迭代次数达到 max_frames（取自 params.max_frames 或配置 run.max_frames，默认 120）。每次迭代将返回的 entities、game_state、score、lives、ball_attached 写入 Context，并维护递增的 Context 字段 frame_count。commands 若未由 params/Context 提供，则默认初始一帧发送 fire=true 让球离开挡板，之后按键均为 false。循环结束后，先调用 PngSnapshotService.execute(entities=context.entities, game_state=context.game_state, score=context.score, lives=context.lives, frame_count=context.frame_count, screenshot_path=params.screenshot_path 或配置 run.screenshot_path)，将写入的 screenshot_path 保存到 Context；再调用 RunSummaryService.execute(screenshot_path=context.screenshot_path, game_state=context.game_state, summary_path=params.summary_path 或配置 run.summary_path, outcome=('cleared' if game_state=='won' else 'lost' if game_state=='lost' else 'incomplete'), final_state=('terminal' if game_state in ('won','lost') else 'timeout'))。最终返回包含 frame_count、entities、game_state、score、lives、ball_attached、screenshot_path、summary_path 的摘要 dict。

## 建议新增配置

```toml
[run]
max_frames = 120  # 无头运行最大帧数；超过此帧数即使游戏未结束也强制停止
random_seed = 42  # 固定随机种子，保证可复现；设为 null 则使用系统随机
```
