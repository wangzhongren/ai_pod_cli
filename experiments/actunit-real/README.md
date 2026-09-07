# ActUnit 真实项目修复实验

目标仓库：wangzhongren/ActUnit，固定版本 `0760b60a8ec8bb065da501455ac77811462fcbf9`。
这是已有开源库的实际缺陷修复，测试范围为 `src/actunit/runtime.py` 单文件。

使用 AIPod Python 生产代码中的 `generate_source()` 和 `call_llm()`：先获取 JSON 元数据，
再获取 XML-like + CDATA 源码，经精确路径检查和 Python 语法检查后放入隔离项目副本。
这测试的是新生成通道，不是把 ActUnit 全库迁移成 AIPod 五层架构。

## 固定验收

模型收到当前源码和业务要求。验收文件在生成项目外，内容哈希在生成前固定，模型不能通过
正常的输出路径修改它。生成后运行仓库原有测试及 25 项独立回归用例：

- 默认值、default_factory、显式值和 Pydantic 类型转换正确传入 handler。
- RuntimeError、KeyError、TypeError 封装为 EXECUTION_FAILED。
- 非字典返回值、非法 status 和不可哈希的 status 封装为 INVALID_RESULT。
- KeyboardInterrupt/SystemExit 保持向上传播。
- 原有权限校验、参数校验、错误码、结果状态和数据保留。
- 不同调用的默认列表互不影响。

既有测试基线：12 项通过。新增验收基线：12 项失败、13 项通过。

## 复现

在包含当前 XML 生成改动的 AIPod checkout 中安装 Python 依赖与 pytest，再运行：

```sh
python experiments/actunit-real/run.py /path/to/ActUnit /absolute/path/to/venv/bin/python
```

第二个 Python 只需要 pytest 与 pydantic；脚本通过 PYTHONPATH 确保导入隔离副本，
不会误测原来的 editable-install 仓库。运行生成器的 Python 需要安装当前 AIPod。
模型配置读取现有环境变量和 `~/.aipod/config.toml`。只有公开源码和修复要求会发送给模型。

默认一次生成尝试，使用 call_llm 的三次内部重试配置，初始 max_tokens=32768（可通过 ACTUNIT_EVAL_MAX_TOKENS 调整）。
可通过 ACTUNIT_EVAL_ATTEMPTS 和 ACTUNIT_EVAL_RETRIES 调整外层尝试及内部重试。
为复现最初两次禁用内部重试的诊断，设置 ACTUNIT_EVAL_ATTEMPTS=2、ACTUNIT_EVAL_RETRIES=1、ACTUNIT_EVAL_MAX_TOKENS=8192。

输出目录、测试日志、生成源码、原始模型响应、diff 和 report.json 都保留在临时目录。
脚本只写入复制的仓库，不提交、推送或修改原来的 ActUnit checkout。

`report.calls` 统计生成器的逻辑调用；一次 call_llm 可能发出多次 HTTP 请求，不能将该字段
直接当作底层请求数或 token 成本。本实验不是性能基准，也不代表大型项目的成功率。

输出记录现在还包含每次实际模型请求的 max_tokens、结束原因、耗时和可获得的 token 用量，用于区分输出预算不足与请求超时。

最新重测结果见 [32K 重测报告](RESULTS-32K.md)。成功配置可复现为：

```sh
OPENAI_TIMEOUT_SECONDS=300 ACTUNIT_EVAL_RETRIES=1 ACTUNIT_EVAL_MAX_TOKENS=32768 python experiments/actunit-real/run.py /path/to/ActUnit /absolute/path/to/venv/bin/python
```

实验将上述预算和等待时间显式传给 source_max_tokens/source_timeout_seconds。
AIPod Python 源码生成器本身现已默认使用 32,768 token 和 300 秒，元数据调用沿用原设置。

Published evidence replaces machine-specific absolute paths with `<tmp>` and `<aipod-checkout>`; results and acceptance criteria are unchanged.
