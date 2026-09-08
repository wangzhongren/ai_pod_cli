# TinyDB 跨文件功能实验

固定真实仓库：msiemens/tinydb，版本 `4aa53111d72c9cbaafcdc039211caf49f4face6f`。
目标功能是新增可选 copy_on_read 深拷贝读取隔离，默认关闭以保持既有行为。
这不是声称上游当前行为有缺陷：上游文档明确说明缓存返回的文档应视为只读；本实验测试新增能力。

规模（含注释及文档字符串）：TinyDB Python 源码约 2255 行，现有测试约 2098 行。
目标文件 table.py 837 行、database.py 274 行，合计 1111 行。

## 验证方法

- 将 Git 跟踪文件复制到临时目录，确认测试导入的是隔离副本。
- 固定 222 项原有功能测试、既有 19 文件 mypy 类型检查和 34 项新增验收。
- 新验收覆盖 MemoryStorage、JSONStorage、CachingMiddleware，及 search/get/all/iteration
  的嵌套数据隔离、缓存命中、写入/失效、每表覆盖、旧自定义 Table、Document 子类、文件重新打开。
- 基线原有测试和 mypy 必须通过；新功能验收必须在基线上不通过。
- 用当前 AIPod 的 generate_source/call_llm 生成 XML-like 完整源码。
- 先生成 table.py，通过原有测试、类型检查和直接 Table 验收后记录哈希并冻结。
- 再将已冻结 Table 实现提供给 database.py 的生成调用，验证两者连接及完整新增验收。
- 候选失败时，最多两次调用 AIPod file_patch_prompt/apply_file_patches 做精确 JSON 补丁修复，
  每次重新执行同一组检查。不能改已冻结上游文件。
- AST 对比禁止修改目标类之外和列举读接口之外的既有方法。所有其他跟踪文件保持原哈希。
- 验收文件保存在生成副本之外，生成前固定内容哈希，不由模型修改。

这测试 AIPod 的真实源码生成、限制范围的修复和跨文件集成能力；阶段顺序由实验脚本驱动，
不是对完整 aipod pod 自动规划流程的端到端验证，也不是全库迁移。

## 复现

生成器 Python 需安装当前 AIPod。测试用 Python 需安装 pytest、PyYAML、mypy、types-PyYAML。

```sh
python experiments/tinydb-large/run.py /path/to/tinydb /absolute/path/to/test-venv/bin/python
```

模型沿用环境变量及 ~/.aipod/config.toml 中配置。源码预算 32768 token、300 秒超时；
内部重试最多三次。全量生成走 XML-like，规划元数据和精确补丁走 JSON。
结果、每次实际请求的用量、原有/新增测试日志、阶段状态与补丁均保留在打印的临时目录。
不会向 TinyDB 上游提交或推送任何内容。

仅在本机 Python 3.14 环境测试，没有执行上游所有 OS/Python 版本 CI 矩阵。
单次项目实验不足以得出普遍成功率、速度或费用优势结论。

## 诊断参数

默认 32K 源码请求在这次任务中把整个输出预算用于推理，返回 0 个源码字符。
保留失败后，使用以下实验参数另行重跑，生产默认值不变：

```sh
TINYDB_EVAL_SOURCE_TOKENS=65536 TINYDB_EVAL_SOURCE_TIMEOUT=600 TINYDB_EVAL_RETRIES=1 python experiments/tinydb-large/run.py /path/to/tinydb /absolute/path/to/test-venv/bin/python
```

DeepSeek 官方规格列出的输出上限为最大 384K（2026-09-07 查询）：
https://api-docs.deepseek.com/zh-cn/quick_start/pricing/
具体配置端点的行为仍以实际请求结果为准。

64K 非流式请求在约 359 秒后发生 APIConnectionError；之后用 TINYDB_EVAL_STREAM=1 启用 call_llm 已有的流式接收继续诊断，预算和验收不变。流式日志只记录推理字符数量，不保存推理文本。

流式诊断生成的 table.py 通过了阶段检查，但 database.py 的 8K 元数据请求达到长度上限。
随后使用 TINYDB_EVAL_RESUME=<已保存输出目录> 复用 frozen_files 中哈希匹配的上游文件，
并用 TINYDB_EVAL_METADATA_TOKENS=32768 恢复下游阶段。复用文件会重新检查，不再调用模型生成。
验收文件内容和哈希保持不变。恢复实验的元数据请求超时也使用配置的实验限时。

最终结果见 [实测报告](RESULTS.md)：256 项测试通过，类型检查通过；默认配置的失败和诊断恢复过程均保留。
