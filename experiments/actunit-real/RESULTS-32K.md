# ActUnit 重测：32,768 token + 300 秒，通过

日期：2026-09-07。项目、模型和验收与前次相同：ActUnit `0760b60`、
`deepseek-v4-flash`、12 项原有测试及 25 项固定回归验收。

## 结果

| 参数 | 结果 |
|---|---|
| 32,768 token / 120 秒 | 首次源码请求超时；随后重试在约 86 秒时主动停止，用于另行测试更长超时 |
| 32,768 token / 300 秒 / 单次请求 | 成功，无生成修复重试 |
| 原有测试 | 12/12 通过 |
| 固定回归验收 | 从 12 失败、13 通过变为 25/25 通过 |
| 变更范围 | 仅 src/actunit/runtime.py；验收内容哈希未变 |
| 总生成及验收耗时 | 约 139.8 秒 |

源码请求正常 stop，返回 3,845 字符的完整 XML-like 响应，耗时 127.912 秒。
模型报告 completion_tokens=19,283，其中 reasoning_tokens=18,393。
非推理 completion token 为 890；因此这次请求的大部分输出预算花在推理上。
这份成功响应确实超过旧的 8,192/16,384 token 上限，耗时也超过旧的 120 秒设置。
元数据请求约 11.3 秒，completion_tokens=1,423，其中 reasoning_tokens=1,326。

这支持为当前模型、任务提供更大的预算及更长等待时间，但不是不同模型或大型项目的通用性能结论。
本轮仍使用 JSON 元数据加 XML 源码，没有与同一真实任务的纯 JSON 源码做 A/B 对照。

## 实际补丁

模型输出仅改变以下逻辑，没有人工修改候选源码：

- model_dump() 保留校验后的默认字段。
- 用 Exception 捕获普通 handler 异常，保留之前的特定错误映射，未捕获 BaseException。
- 拒绝非 dict 结果。
- 用 tuple 做状态成员检查，避免 list/dict 状态触发不可哈希异常。

补丁已人工检查，原来的 ActUnit checkout 没有应用或提交这些改动。

## AIPod 设置

根据重测结果，Python generate_source 的正式默认值改为 source_max_tokens=32768、
source_timeout_seconds=300。元数据/规划预算保留调用方原配置。
call_llm 增加可选的单次请求 timeout_seconds 参数。
实验脚本显式传入参数，因此仍能复现旧的 8192/120 配置。
更新后，AIPod 的 132 项 Python 测试通过，包括预算分离、参数覆盖和请求透传检查。

## 证据

- [成功重测完整记录](results/tokens-32768-timeout-300.json)
- [保留的 120 秒超时记录](results/tokens-32768-timeout-120.json)
- [模型生成的修复补丁](results/runtime-32768.patch)
- [原有测试通过日志](results/32768-attempt-1-original.log)
- [25 项回归通过日志](results/32768-attempt-1-acceptance.log)
- [前次较小预算的失败记录](RESULTS.md)
