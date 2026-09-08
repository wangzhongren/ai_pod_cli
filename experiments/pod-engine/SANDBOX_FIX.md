# 沙盒漏检修复与回归

修复对象是 Python AIPod 的验证流程。原生成引擎源码未修改，也未调用模型重新生成。
新验证器现在会拒绝这份引擎，实际捕获此前漏掉的缺字段、碰撞异常、帧数与暂停行为错误。

## 代码变化

1. **完整 Pipeline 不再合成输入。** 规划必须先声明真实公开 `inputs` 和
   `verification_cases`，源码修复期间保持不变。沙盒逐场景只传显式 `params`，
   每次使用独立项目副本；不补下游字段、不创建样本文件。
2. **公开入口独立保存。** `pipelines/*.contract.json` 保存入口与场景，routes.toml 引用它。
   Interface 使用声明的入口，不能把“下游缺少生产者”自动变成用户必填数据。
   复用旧 Pipeline 也重新执行入口场景，保留原源码。
3. **试跑与行为验收分开。** 样本可运行不能使应用完成。每个 Interface 必须声明 required
   behavior 检查，把需求映射到冻结的 `ClassName.test_method` 列表，并规划或提供测试文件。
4. **框架执行行为测试。** `python -m ai_pod_cli.behavior_tests <test_file.py>` 执行真实路由和
   unittest 断言，输出逐测试证据。空测试、未调用真实路由、仅恒定断言、被吞掉的 Runtime
   Failure、缺失或跳过已声明场景，都不能通过。Pipeline 提前 exit(0) 也不能伪装完成。
5. **修复不能放宽验收。** 行为测试及断言文件排除在自动修复目标之外。多条检查的失败证据
   完整保留；旧 smoke-only 通过记录失效，Pod 可重新规划 Interface 验收。

## 框架回归

226 项测试全部通过，包含真实临时项目、实际子进程、真实注册路由和断言。
其中新测试覆盖沙盒、入口生成接线、行为测试驱动和应用完成门禁。
原先依赖 `python -c pass` 就完成应用的旧测试，已改为真实行为检查或明确拒绝旧凭证。

```sh
python -m unittest tests.test_runtime tests.test_source_generation tests.test_contract_normalization tests.test_pipeline_sandbox tests.test_pipeline_generation tests.test_behavior_tests tests.test_application_verification
```

原始输出：[framework-tests.log](results/sandbox-fix/framework-tests.log)。

## 原引擎实测

使用同一个生成项目的未修改副本，以及明确标注为独立编写的
[行为验收文件](test_behavior_acceptance.py)。

| 检查 | 新验证器结果 |
| --- | --- |
| 原结构化/List[Model] 契约 | 继续兼容 |
| 真实无头入口 | 拒绝：摘要四个必填字段缺失，未被样本补齐 |
| 原先只有 adapter_smoke 的计划 | 拒绝：缺少 required behavior 验收 |
| 实际运行 120 帧 | 失败：观察到 60 次 Simulation 调用 |
| 砖块碰撞 | 失败：Vec2 位置参数触发 TypeError |
| 连续按暂停 | 失败：第二帧重新进入 PLAYING |
| 发球、墙反弹、挡板反弹、暂停松键、重置 | 通过 |

行为驱动运行 8 项测试，5 项通过、3 项失败，退出码 **1**。
缺字段在入口试跑时已能检测；没有异常退出的行为错误由明确场景的断言检测。

可重现命令：

```sh
python experiments/pod-engine/recheck_sandbox.py /path/to/generated-engine
```

此回归脚本确认“已知错误被检测到”，因此自身退出码为 0；被测引擎的行为驱动退出码仍为 1。
完整证据：[检测摘要](results/sandbox-fix/summary.json)、
[行为测试 JSON](results/sandbox-fix/behavior-proof.json)、
[行为异常与断言日志](results/sandbox-fix/behavior-stderr.log)。

## 边界

框架现在强制实际输入、真实执行、断言和已声明场景的覆盖；它不能数学证明任意测试正确，
也不能自动证明自然语言需求已被完整拆成场景。明显的常量断言会被拒绝，复杂断言仍需审查。
本轮证明的是已知漏检被修复，没有把引擎修成可用产品；没有验证模型重新生成整套应用后的成功率。
