# 正式 aipod pod 2D 引擎测试：未完成

本文保留首次运行的失败记录。后续框架修复及真实复测见 [FRAMEWORK_FIX.md](FRAMEWORK_FIX.md)。

本次按用户要求，从空项目直接执行正式 Python CLI：

```sh
aipod init
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy PYGAME_HIDE_SUPPORT_PROMPT=1 aipod pod --file requirements.md --yes
```

需求为 pygame-ce 2D 小引擎及可玩的打砖块演示，包含固定步长、碰撞、暂停/重置、
键盘输入、无窗口 120 帧、PNG、JSON 摘要与真实行为验证。使用当前 AIPod 工作区，
包含未提交的 64K 源码、32K JSON、600 秒默认值调整；pygame-ce 为 2.5.8。
模型为现有配置的 deepseek-v4-flash。

没有手写引擎源码、手工注册组件或替换生成器；代码生成、补丁修复、阶段顺序及重试均由 Pod 执行。

## 最终状态

- CLI 退出码：1。
- Agent 状态：blocked。
- Model：Vec2、GameEntity，完成。
- Provider：规划为空，阶段完成；使用框架已有 Provider。
- Service：BreakoutSimulationService、PngSnapshotService、RunSummaryService，完成组件验证。
- Pipeline：单帧 step_breakout_simulation 已生成并通过隔离运行检查；run_headless_breakout 未完成。
- Interface：pending，尚未规划/生成。
- 没有可启动的完整引擎或可玩的演示；没有完成 120 帧和碰撞/暂停/重置的应用级验收。

状态文件中 Pipeline 仍标记 in_progress；最终失败由 CLI 退出码、Build Tool 失败记录和
Agent blocked 共同确认，不能把部分阶段 complete 或单个文件存在当作应用成功。

## 过程

Pod 自行处理了 Vec2 不允许覆盖 __init__、动态 Context get/set、Vec2 位置参数调用、
输出契约缺少 last_frame_events 等生成问题。核心服务重生成时还曾重新引入 Vec2 调用错误，
随后再次自动修正。

最后停在跨组件契约检查。相同类型不兼容信息共出现 5 次；Pipeline 阶段整体重试后仍未通过。
最后一次尝试还出现了元数据响应携带源码的协议错误，随后 Build Tool 连续失败，Pod 自行停止。
已通过的单帧路由被复用，服务没有被手工修改。

## 经源码核对的主要卡点

原始契约实际上是两种等价意图的表示：

生产者 BreakoutSimulationService.outputs.entities：

```json
{"type":"array","items":{"model":"modules.models.gameentity.GameEntity"}}
```

消费者 PngSnapshotService.inputs.entities：

```text
List[modules.models.gameentity.GameEntity] — 当前帧所有实体
```

`normalize_type()` 将结构化 array 简化为 list，却保留字符串中的泛型部分，并将类名一并小写。
`types_compatible()` 随后比较 list 与 list[modules.models.gameentity.gameentity]，判定不兼容。
`schema_compatibility()` 也先在这一步返回错误，没有机会对齐同一个 GameEntity 元素约束。

已用上述两份实际元数据直接调用 normalize_type/schema_compatibility 复现。
因此日志中的“上游提供 list”不代表生成器遗漏了元素类型。更准确的原因是 AIPod 对结构化
Schema 与字符串泛型契约的规范化不一致。重新生成 Pipeline 源码不会改变这两份服务契约，
当前仅重试本阶段的策略也没有解决这个问题。

## 这次能说明什么

正式 Pod 能生成并检查游戏相关组件，但这次没有自动交付完整引擎。
它直接暴露了契约规范化及跨阶段失败恢复的不足；不能由这次失败断言所有非业务开发都不可行。
也不能用之前手写运行时实验或单组件生成成功，替代本次正式构建的失败结果。

本次只测试并保留结果，没有为通过演示而修改生成的服务契约，也没有修补 AIPod 解析器后重跑。

## 证据

- [需求](requirements.md)
- [完整 CLI 日志](results/pod.log)
- [结果摘要与原始冲突字段](results/summary.json)
- [生成的部分项目](results/partial-project/requirements.md)
- [冻结与失败状态](results/partial-project/aipod_plan.json)

原始可续接目录仅记录在本机未提交的 run-location.json。归档记录中的本机路径已用 `<tmp>` 和 `<aipod-checkout>`
替代；部分项目不是已验证交付物。
