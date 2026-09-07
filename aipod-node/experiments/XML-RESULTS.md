# XML-like 源码生成接入与实测 — 2026-09-07

代码基于 `2ab399c` 加当前未提交修改，模型配置名称为 `deepseek-v4-flash`。

## 实现范围

- Model、Provider、Service 及 Interface 附属文件改用文本响应中的 XML-like + CDATA。
- 阶段选择、组件规划和精确文本补丁修复继续使用 JSON。
- Pipeline 与 Interface 主入口继续由本地模板生成。
- 客户端增加 `completeJson` / `completeText`；保留 `complete` 作为 JSON 别名。
- 文本请求不发送 `response_format: json_object`，不追加 JSON 输出指令。
- TypeScript 编解码器实现 ActUnit 的 create/path/content 子集，没有依赖 Python 子进程。
- 仅允许一份候选文件、精确计划路径；拒绝重复动作、额外操作、属性、嵌套参数和路径漂移。
- 支持 CDATA 拆分、XML 实体、DSML 标签前缀；源码中的 DSML 字符串不被替换。
- 解码不会直接执行动作；沿用现有候选验证、暂存、类型检查、冻结和最终验证流程。
- XML 格式错误进入三次生成尝试的反馈流程；截断响应明确失败，不接受为完整源码。

这不是整个 ActUnit Runtime 的移植。与其通用解码器相比，本实现要求响应严格为一个完整动作，
不提取带前后说明的第一个元素，也不启用未转义内容的启发式修复。

## 自动化验证

39 项 Node 测试全部通过：常规执行通过 37 项，另 2 项回环端口集成测试单独补跑通过。
新增验证包括文本请求不启用 JSON 模式、拒绝截断响应、CDATA/引号/反斜杠/Unicode/行尾往返、
路径错误反馈重试、Interface JSON 文件通过 XML 传输，以及非法 XML 不提交文件、不完成阶段。

另用 ActUnit `0760b60` 的真实 Python `XmlActionCodec` 解码 3 份 Node 编码样例：
包含 XML 字符、动作关闭标签和 CDATA 结束符，源码全部保持一致。
这只验证所测子集，不代表两个解码器对所有 XML 输入行为相同。

## 真实模型对照

使用同一份合成订单夹具和独立验收，要求仅修改 PriceOrder 的满减规则。
JSON 对照通过实验适配器模拟原先的 content 封装，其他运行时和验收相同。

| 试验 | 每请求限时 | Agent 结果 | 模型调用 | Agent 耗时 | 业务验收 | 无关源码改动 |
|---|---:|---|---:|---:|---:|---:|
| XML 首轮 | 90 秒 | 源码生成请求超时 | 3 | 107.321 秒 | 7/10，未实现满减 | 0 |
| JSON 对照 | 90 秒 | 完成 | 5 | 103.676 秒 | 10/10 | 0 |
| XML 重试 | 120 秒，匹配客户端默认值 | 完成 | 5 | 152.501 秒 | 10/10 | 0 |

成功的 XML 源码响应一次解码通过，没有发生格式重试。源码使用 context.typed，
只修改了 PriceOrder、price Pipeline、priceCli Interface。
10000、10001、20000 分分别得到 9000、9001、19000 分，库存和通知验收保持通过。

首轮 XML/JSON 试验有时间重叠，重试的限时不同，且每组样本很少；以上耗时不能作为严谨性能比较。
目前可以确认 XML 生成已接通、业务验收和范围约束正常，不能声称成功率、token 成本或速度优于 JSON。
首轮超时没有被删除或计为成功。

## 证据与复现

- [XML 首轮超时记录](results/orders-source-xml-90s.json)
- [JSON 对照记录](results/orders-source-json-90s.json)
- [XML 重试完整记录](results/orders-source-xml-120s.json)
- [模型实际生成的 XML](results/orders-source-example.xml)
- [可复现脚本与参数说明](README.md)

```sh
node aipod-node/experiments/orders.mjs --live --auto-only --repetitions=1
node aipod-node/experiments/orders.mjs --live --auto-only --repetitions=1 --source-format=json
node aipod-node/experiments/orders.mjs --live --auto-only --repetitions=1 --request-timeout-ms=120000
```

运行前在 aipod-node 中执行 npm run build，再回到仓库根目录运行以上命令。
