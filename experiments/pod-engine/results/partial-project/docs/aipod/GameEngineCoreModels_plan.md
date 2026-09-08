# Pod Plan: GameEngineCoreModels

> 生成时间: 2026-09-08 10:35:48

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

共 2 个组件：

### 1. Vec2 (model)

Runtime 2D vector value object representing x/y coordinates, velocities, size vectors, etc. It is NOT a persistent Model and must never be registered with ModelRepository as an ORM table. Class will be implemented as a Python dataclass (or simple class) with immutable-style arithmetic helpers for game code. Constructor: Vec2(x: float, y: float). Fields: x: float, y: float. Methods: copy() -> Vec2; __add__(Vec2) -> Vec2; __sub__(Vec2) -> Vec2; __mul__(float) -> Vec2; __rmul__(float) -> Vec2; length() -> float; length_squared() -> float; normalize() -> Vec2; dot(Vec2) -> float; __iter__; __repr__; __eq__. All arithmetic creates the correct result, and invalid types raise clear exceptions. Keep implementation stdlib-only with no pygame dependency so it can run headless.

### 2. GameEntity (model)

Runtime value model describing a simulated game entity. Used by later physics, rendering, and game logic services. NOT a persistent model: never create an ORM table for this class. Class will be a Python dataclass with normal mutable fields because the world updates positions and velocities every tick. Fields: id: str (unique within a scene); kind: str (semantic category such as "ball", "paddle", "brick", "wall", or any future extension); position: Vec2 (entity center point); velocity: Vec2 (units per second); size: Vec2 (for shape="rect" this is width/height; for shape="circle" both components are equal and equal the diameter); shape: str (must be "rect" or "circle", default "rect"); enabled: bool (default True, disabled entities are ignored by physics/collision/render); static: bool (default False, static entities do not move but still collide, used for wall/bricks). Convenience read-only properties in generated code: radius -> float: returns size.x/2 when shape == "circle", otherwise raises/AttributeError; center -> Vec2: alias of position. Method: copy() -> GameEntity.
