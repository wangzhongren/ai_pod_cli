# 框架修复及正式引擎复测

本文记录契约解析修复。随后针对沙盒漏检的修复见 [SANDBOX_FIX.md](SANDBOX_FIX.md)。

本轮修复了 Python AIPod 在首次正式 Pod 引擎构建中暴露的契约规范化问题。
原来的 Model、Service 和 beans_config.json 保持不变，同一组契约现在通过兼容性检查。
这不代表完整引擎验收通过：独立行为测试为 **5 项通过、3 项失败**。

## 修复内容

- `contracts.py`：将结构化 array/items 与 `List[Model]` 等字符串泛型归一为同一结构；
  保留元素约束及 Model 路径大小写。静态兼容性、运行时校验、嵌套 Model 物化和契约导出
  共用这一表示。覆盖 List、字符串键 Dict、Optional、Union 及嵌套组合。
- `sandbox.py` 与 Provider 校验：使用同一契约表示生成有效样本、识别 Model，避免下游消费者
  继续按旧字符串逻辑处理泛型。
- `source_generation.py`：元数据偶尔多带根级 code/content 时，丢弃这些字段并继续单独请求
  XML 源码；嵌套契约字段不受影响，禁止拿丢弃的 JSON 源码充当后备结果，路径检查仍生效。
- `compose.py`：修正 Context 数据池优先于 params 的说明；明确逐轮输入更新方法、repeat
  支持的节点类型及真值停止条件；需要逐帧转换或字符串状态判断时，说明如何在 Pipeline
  中使用有明确上限的循环。禁止临时创建 Service 绕过已冻结的组件账本。

本轮特定缺陷发生在 Python 生成与契约解析路径；没有为此修改 Node 的执行语义。
工作区原有的两端默认 token/timeout 调整继续保留。

## 框架验证

```sh
python -m unittest tests.test_contract_normalization tests.test_runtime tests.test_source_generation
```

**164 项通过**。回归覆盖等价契约、错误元素类型、不同 Model、嵌套物化、字典键、
可空类型、对象 required 字段保留、不可执行类型表达式、XML 路径及禁止 JSON 源码后备。
生成提示词通过语法检查，`git diff --check` 通过。

实际失败项目的三服务链重新调用 `analyze_pipeline_contracts` 得到 `valid=true, issues=[]`。
原先生成的模型、服务与 beans_config.json 共 10 个文件的哈希与续跑前一致，
详见 [哈希基线](resume-baseline.json) 和 [复测摘要](results/framework-fix-summary.json)。

## 正式 Pod 续跑

仍在同一项目执行 `aipod pod --file requirements.md --yes`，没有手写或手改引擎源码。
第一轮续跑跨过原契约错误，随后遇到非法 repeat body 与旧的元数据拒绝逻辑；
加载元数据容错修复后再次续跑，Pod 自动重试并保存 `run_headless_breakout`，
通过组件契约及隔离 Pipeline 运行，进入 Interface 阶段。

独立真实运行随即确认生成应用仍有明确行为错误，因此在 Interface 生成期间人工结束
本次续跑，退出码为 **130**。这不是 Pod 自行成功或自行 blocked；Interface 没有完成交付，
没有验证可交互窗口。新的 compose 提示词在此次 Pipeline 生成之后才完成修改，
本轮不声称它已在下一次模型生成中得到验证。

## 独立行为验收

[验收器](check_generated_engine.py) 由本轮编写，**不是 Pod 自产的测试**。
它在生成项目的未修改临时副本上执行真实注册路由；没有替换引擎实现、模拟服务或调用模型。
退出码为 **1**，详细输入、输出及 traceback 见 [JSON 报告](results/independent-acceptance/report.json)。

| 检查 | 结果 |
| --- | --- |
| 单帧 launch 发球 | 通过 |
| 左墙反弹 | 通过 |
| 挡板反弹 | 通过 |
| 暂停后松键，物理位置保持不变 | 通过 |
| 重置恢复初始实体、分数和生命 | 通过 |
| 默认无头 120 帧及 PNG/JSON 输出 | 失败 |
| 砖块碰撞并计分 | 失败 |
| 持续按暂停只触发一次状态切换 | 失败 |

失败证据：

- 无头循环真实调用 Simulation **60 次**，却报告 120 帧；默认发送 `fire`，实际服务需要
  `launch`，球未移动、物理时间为 0。PNG 已产生，但 RunSummary 缺少
  total_bricks、remaining_bricks、ball_position、paddle_position，JSON 摘要没有产生。
- 砖块碰撞执行到 `_reflect_with_normal` 时，生成的 `Vec2(...)` 使用了位置参数，触发
  `SQLModel.__init__() takes 1 positional argument but 3 were given`。
- 连续按 pause 两帧，第二帧从 PAUSED 重新进入 PLAYING；服务没有保存上一帧的真实按键状态。

此外，静态审查发现 headless 比较小写 `won/lost`，与服务输出的大写 `WON/LOST` 不一致；
该终局场景未包含在上述 8 项动态测试中，不作为已执行的测试计数。

## 证据文件

- [框架测试原始输出](results/framework-tests.log)
- [首次续跑日志](results/pod-resume.log)
- [加载修复后的续跑日志](results/pod-resume-fixed.log)
- [直接无头调用失败](results/direct-headless-check.log)
- [独立验收输出](results/independent-acceptance.log)
- [续跑生成的 Pipeline](results/resumed-project/pipelines/run_headless_breakout.py)
- [中断时的项目状态](results/resumed-project/aipod_plan.json)

首次失败的历史记录保留在 [RESULTS.md](RESULTS.md)。当前结论是原契约阻塞已修复，
完整生成应用仍未通过验收；不能用沙箱样本可执行替代游戏行为正确。
