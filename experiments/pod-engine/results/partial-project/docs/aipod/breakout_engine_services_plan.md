# Pod Plan: breakout_engine_services

> 生成时间: 2026-09-08 10:44:41

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

共 3 个组件：

### 1. BreakoutSimulationService (service) ← depends: ConfigStore

Core fixed-timestep game simulation service for the Breakout demo. It does not open a window, read keyboard hardware, or render; it only updates a machine-readable Context. Constructor dependency: ConfigStore. Public entrypoint: execute(ctx) -> ctx. 

Coordinate contract: all GameEntity.rect entities use position as the AABB top-left corner; ball uses position as top-left of its bounding square, and its circle center is position + size/2. kind values used by this service: paddle, ball, brick. Enabled False semantic: disabled bricks are ignored entirely. 

State model stored on Context: ctx.entities is a list of GameEntity; ctx.game_state in READY|PLAYING|PAUSED|WON|LOST; ctx.score, ctx.lives, ctx.ball_attached, ctx.previous_commands, ctx.simulation_time, ctx.last_frame_events. 

Input contract: ctx.commands is a dict with booleans left, right, launch, pause, reset. left/right are continuous held flags; launch/pause/reset are one-shot/edge-triggered and compared against ctx.previous_commands. The service defaults missing commands to False and initializes ctx.previous_commands on first call. 

execute algorithm: 1) If ctx.entities is absent/empty, create a standard Breakout scene from ConfigStore: screen size, paddle near bottom, ball attached to paddle center top in READY state, a grid of enabled brick rects, no wall entities are stored; boundary walls are virtual and handled during collision. 2) If reset edge is true, rebuild the scene and return without changing physics. 3) If pause edge is true, toggle between READY/PLAYING and PAUSED; if entering PAUSED, no physics change is allowed for that execute call. 4) If state is PAUSED, WON, or LOST, do not integrate movement or change score; only still record last_frame_events as empty and update no positions. 5) If state is READY and launch edge is true, set ctx.ball_attached=False, set ctx.game_state=PLAYING, and give the ball a deterministic initial velocity at config-provided angle and speed. 6) When READY and not launching, move the paddle with left/right commands and move the attached ball horizontally with the paddle so it stays centered above the paddle. 7) When PLAYING, move paddle from left/right and clamp it to screen bounds and horizontal virtual wall region; move only entities with kind=ball because paddle is deterministic-controlled and bricks/walls are static. 8) Boundary collision after ball integration: top wall reflects vertical velocity, left and right virtual walls reflect horizontal velocity; there is no bottom wall. If the ball's top coordinate leaves below screen_height + margin, lose a life: if lives remain, decrement lives and reset ball_attached=True/game_state=READY; if lives become less than base_lives? Not used; if lives reaches 0 before the loss? Implement proper life loss rule in code and document it in code comments; service must never show negative lives. 9) Paddle collision: if ball is moving downward and its AABB overlaps paddle AABB, reflect vertical velocity and make the horizontal bounce angle depend on where the ball hits relative to the paddle center; normalize magnitude back to config ball_speed; place ball just above paddle. 10) Brick collision: after paddle handling, test the circle-rect intersection against every enabled brick. For each hit, set brick.enabled=False, increase ctx.score by ConfigStore game.points_per_brick, reflect ball velocity using penetration normal/previous direction, and append event brick_hit:<brick_id>. 11) After all collisions, if no enabled brick remains, set ctx.game_state=WON. 12) Record last_frame_events list for this step e.g. wall_top, wall_left, wall_right, paddle_hit, brick_hit:<id>, life_lost, win, reset. 13) Bump ctx.frame_count once for every execute call and advance ctx.simulation_time by dt only when physics is actually simulated. 

The service is deterministic: no random number generator is used and the same commands/dt sequence produces identical state. It therefore can be exercised headlessly by tests or by a later Pipeline repeat loop.

### 2. PngSnapshotService (service) ← depends: ConfigStore

Renders the current Breakout world to a PNG file using pygame-ce without requiring a visible display. Constructor dependency: ConfigStore. Public entrypoint: execute(ctx) -> ctx. 

The service reads ctx.entities (list of GameEntity), ctx.game_state, ctx.score, ctx.lives, ctx.frame_count and optionally ctx.screenshot_path. If ctx.screenshot_path is absent, use ConfigStore value run.screenshot_path and set ctx.screenshot_path to the resolved path. It creates the parent output directory when needed, creates a pygame.Surface of size game.screen_width x game.screen_height, fills the background, draws every enabled entity as follows: kind=ball as a filled circle using its position+size/2 as center; kind=paddle and kind=brick as filled rectangles using position/size. No external images/fonts/assets are loaded. It creates the surface with a plain pygame.Surface; if SDL needs initialization, it initializes pygame only for the dummy video driver and never opens a visible window. 

The current frame is saved with pygame.image.save(surface, path). The service never mutates ctx.entities, ctx.score, ctx.game_state, ctx.lives. It does not import or call any other Service.

### 3. RunSummaryService (service) ← depends: ConfigStore

Writes a machine-readable JSON summary of a completed/headless Breakout run. Constructor dependency: ConfigStore. Public entrypoint: execute(ctx) -> ctx. 

The summary path resolution order: ctx.summary_path if provided, else ConfigStore run.summary_path; the resolved path is set back as ctx.summary_path. It creates the parent directory if needed. JSON written contains at least: success (bool equal to game_state in WON|LOST? success is True when simulation reached an explicit terminal state without error, not necessarily won; include also outcome field), outcome, final_state, score, lives, frame_count, total_bricks, remaining_bricks, ball_position {x,y}, paddle_position {x,y}, output_files {screenshot_path, summary_path}, and an entities array with id/kind/enabled/position/size. All serialized values are JSON-native types; no custom object dumps. The service does not depend on simulation internals other than reading fields from Context. It does not call another Service. It must not produce non-zero exit by itself; file errors shall raise a clear exception so the test runner detects failure.

## 建议新增配置

```toml
[game]
screen_width = 800  # Playable screen width in pixels; default demo resolution.
screen_height = 600  # Playable screen height in pixels; default demo resolution.
fixed_timestep = 0.016666666666666666  # Fixed simulation timestep in seconds; 1/60.
paddle_width = 120  # Paddle rectangle width in pixels.
paddle_height = 16  # Paddle rectangle height in pixels.
paddle_bottom_margin = 24  # Paddle bottom offset from screen bottom in pixels.
paddle_speed = 420  # Paddle horizontal speed in pixels per second.
ball_diameter = 14  # Ball diameter in pixels.
ball_speed = 330  # Ball fixed speed magnitude in pixels per second.
ball_start_angle_deg = 60  # Initial launch angle from vertical in degrees.
base_lives = 3  # Starting life count.
points_per_brick = 10  # Score awarded for each brick destroyed.
brick_rows = 6  # Number of brick rows.
brick_cols = 10  # Number of brick columns.
brick_top_offset = 64  # Vertical offset where the brick grid starts.
brick_height = 20  # Individual brick height in pixels.
brick_horizontal_margin = 12  # Screen side margin for brick grid in pixels.
brick_gap = 4  # Gap between bricks in pixels.
[run]
screenshot_path = out/breakout_final.png  # Path where PngSnapshotService writes the final frame PNG.
summary_path = out/run_summary.json  # Path where RunSummaryService writes the parseable JSON run summary.
```
