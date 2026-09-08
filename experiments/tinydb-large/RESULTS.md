# TinyDB 较大项目实测：跨文件功能通过，默认配置首轮失败

完成日期：2026-09-08。
项目：[msiemens/tinydb](https://github.com/msiemens/tinydb)，固定版本
`4aa53111d72c9cbaafcdc039211caf49f4face6f`。

## 规模与任务

- Python 核心源码 2,255 行，已有测试 2,098 行，合计约 4,353 行（含注释、文档字符串）。
- 目标文件 table.py 837 行、database.py 274 行，合计 1,111 行。
- 添加可选 `copy_on_read`：开启后，读出的嵌套字典、列表、Document ID 和自定义属性与
  数据库存储、查询缓存隔离；关闭时保留原有共享行为。
- 同时连接 TinyDB 全局选项、Table 选项、每表覆盖和不同存储后端。

这比之前 ActUnit 百行文件修复涉及更多代码、读取分支与跨文件接口。
它是新增功能实验，不把上游已经明确说明的“缓存文档应视为只读”行为当作未修复漏洞。

## 最终验证

| 检查 | 结果 |
|---|---|
| 原有测试 | 222/222 通过 |
| 固定新增验收 | 34/34 通过 |
| 两组混合运行 | 256/256 通过 |
| mypy | 19 个文件无问题 |
| Python 3.10 语法解析 | 两份候选通过 |
| 改动文件 | 仅 tinydb/table.py、tinydb/database.py |
| 未授权既有方法变化 | AST 检查为 0 |
| 验收文件变化 | 哈希不变 |
| 恢复时重写上游 | 0；table.py 复用且哈希不变 |
| 完整源码后的补丁修复 | 0 次；两份完整候选均通过对应验证 |

验收覆盖 MemoryStorage、JSONStorage、CachingMiddleware；get 的三种参数方式、search
缓存命中、all、iteration；嵌套数据修改、Document 子类属性、查询缓存实际复用、写入后
缓存失效、存储构造参数不泄漏、旧 Table 子类兼容、JSON 文件重新打开和其他表保持不变。

原始项目的 222 项测试与类型检查通过。新增验收在基线上为 15 失败、1 通过、18 个 setup
错误，共 34 项；这些失败/错误源于缺少新增参数和行为。没有在模型输出后放宽验收。

## 真实过程与失败记录

1. 默认 32K 非流式源码请求：230.790 秒后用尽 32,768 token，全部是推理，源码字符为 0。
   随后相同预算的重试被中止，保留记录。
2. 64K、600 秒非流式诊断：约 359 秒后 APIConnectionError，未产生候选。
3. 相同 64K/600 秒改为流式：table.py 输出完整 XML，约 195.835 秒；通过原有测试、mypy、
   直接 Table 验收后冻结。database.py 的 8K JSON 元数据请求随后达到长度上限。
4. 恢复阶段：复制并验证已冻结 table.py，不再调用模型生成它。将元数据预算提高到 32K，
   保留 64K 源码预算、600 秒限时与流式接收，生成 database.py 并通过完整验收。

这不是默认配置一次成功。生产默认参数未修改；诊断参数和中断/失败记录全部保留。
流式模式只记录推理字符数量和 token 用量，不保存或展示推理文本。

## 用量与耗时

| 成功源码请求 | completion token | 其中推理 token | XML 字符数 | 请求耗时 |
|---|---:|---:|---:|---:|
| table.py | 27,951 | 21,443 | 30,913 | 195.835 秒 |
| database.py | 22,978 | 20,636 | 10,660 | 176.157 秒 |

产出两份源码的两次请求合计约 372 秒；对应两次成功元数据请求另约 17 秒。
上述数字不包括前序失败、中断、元数据截断或人工检查时间，不能当作完整实验墙钟时间。
两次成功源码实际 completion token 均低于 32K，因此不能认定 64K 是必要条件，
也不能仅凭这几次尝试把成功完全归因于流式模式。未完成同条件多轮统计或费用比较。

## 审视与范围限制

本轮证明：AIPod 的 Python 源码生成通道可以产出这个较大库的两份相互配合的实现，
在固定功能验收、原有回归、类型检查和方法范围检查下通过；实验恢复可以复用已验证文件。

阶段顺序、AST 冻结与恢复逻辑由实验脚本驱动，调用的是 AIPod 生产 generate_source、
call_llm 和预备的补丁函数；不是对完整 aipod pod 自动规划/五层生成流程的端到端证明。
实际没有触发候选后的补丁修复，所以本次不能声称验证了复杂错误的自动修复成功率。

最终 diff 已阅读。生成的 Table 在关闭新功能时，缓存返回由切片改为调用私有方法的列表循环，
功能兼容测试通过，但可能增加开销；本轮没有性能基准，不能保证性能无退化。
只在本机 Python 3.14 验证，未执行 TinyDB 所有 OS/Python 版本 CI 矩阵。

## 结果文件

- [table.py 补丁](results/table.patch)
- [database.py 补丁](results/database.patch)
- [256 项混合测试](results/combined-tests.log)
- [类型检查](results/database-0-types.log)
- [最终恢复记录](results/resumed-stream-success.json)
- [已冻结上游与元数据失败记录](results/stream-64k-table-complete.json)
- [32K 首轮失败](results/default-32k.json)
- [64K 非流式连接失败](results/nonstream-64k.json)
- [固定验收](acceptance_cases.py)
- [复现说明](README.md)

实验仅修改隔离副本，TinyDB 原 checkout 保持干净；没有向上游提交或推送。
公开保存的记录将机器绝对路径替换为 `<tmp>` / `<aipod-checkout>`，不改变测试结果。
补丁基于 TinyDB MIT 许可源码，原许可文本保存在 [TinyDB-LICENSE.txt](results/TinyDB-LICENSE.txt)。
