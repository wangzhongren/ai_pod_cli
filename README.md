# AIPodCli

**AI 原生 Python 开发框架** — 让 AI 接管组件生成、依赖注入和管道编排。

AIPodCli 是一个 AI 原生的 Python 开发框架。它把 AI 深度集成到开发流程的每个环节：你描述需求，AI 生成组件代码；你描述业务指令，AI 规划执行管道。框架底层基于 `injector` 实现 IoC/DI 容器，在运行时自动组装和运行你（和 AI）构建的系统。

**和传统开发框架的区别：**

| | 传统框架 | AIPodCli |
|---|---------|----------|
| 写组件 | 你写 | AI 生成，你审核 |
| 配依赖 | 你手动注入 | AI 自动分析，DI 容器装配 |
| 编排流程 | 你写胶水代码 | AI 规划管道，框架执行 |
| 配置管理 | 你写配置代码 | AI 建议配置项，自动追加到 TOML |
| 项目入口 | 你选技术栈 | AI 根据描述决定（Flask/CLI/MQ...） |

## 核心理念

```
人类描述需求 → AI 生成代码 → DI 容器自动组装 → Pipeline 管道执行
```

- **生成与执行分离**：`build` 只生成代码，开发者自行决定何时、如何运行
- **IoC/DI 容器**：基于 `injector`，组件间依赖通过构造函数自动注入
- **AI 代码生成**：大模型根据需求 + 已有组件池，自动生成符合规范的组件
- **管道语法**：`(S(A) | S(B) | S(C)).execute_all(ctx)` 串联组件
- **PipelineContext**：组件间通过共享上下文流转数据
- **ConfigStore**：集中式 TOML 配置，组件通过 DI 注入读取
- **AST 安全检查**：拦截 `eval`/`exec`/dunder 链等代码注入模式

## 快速开始

### 1. 安装

```bash
pip install aipodcli
```

### 2. 初始化项目

```bash
python -m ai_pod_cli init "一个电商库存管理的 RESTful API"
```

AI 会根据描述自主判断技术栈（Flask/FastAPI/CLI/RabbitMQ...），生成对应的入口文件：

```
.
├── cli.py (或 app.py / consumer.py)  ← AI 生成的入口
├── modules/                          ← AI 生成的组件
│   ├── __init__.py
│   └── requirements.txt
├── pipelines/                        ← AI 生成的管道文件
├── config.toml                       ← 项目配置（组件通过 ConfigStore 读取）
├── routes.toml                       ← 路由映射（build 自动注册）
├── beans_config.json                 ← 组件注册账本
├── .env                              ← 大模型 API 配置
└── .gitignore
```

### 3. 配置大模型

编辑 `.env`：

```bash
OPENAI_API_KEY=sk-your-real-key-here
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=deepseek-chat
```

支持任何 OpenAI 兼容 API（DeepSeek / Moonshot / Ollama 等）。

### 4. 完整工作流

```bash
# ① 创建组件（AI 自动生成代码 + 注册配置）
python -m ai_pod_cli create --category entity --name SqliteStore \
    --desc "SQLite存储组件，从ConfigStore读取database.sqlite_path，提供query/execute/insert方法"

python -m ai_pod_cli create --category entry --name DataCollector \
    --desc "生成随机销售记录写入ctx.set('raw_sales', list)"

python -m ai_pod_cli create --category entry --name DataWriter \
    --desc "依赖SqliteStore，从ctx读取raw_sales建表并逐条insert"

# ② 生成管道（AI 规划链路 → 保存文件 → 注册路由，不执行）
python -m ai_pod_cli build "采集销售数据并写入SQLite" --name collect_sales

# ③ 开发者自行运行
python cli.py collect_sales           # CLI 入口
# 或 flask run                        # Web 入口
# 或 python consumer.py               # 消息队列入口
```

## 命令参考

### `init` — 初始化项目

```bash
python -m ai_pod_cli init "项目描述"
python -m ai_pod_cli init              # 不传描述，只创建骨架
python -m ai_pod_cli init "..." --install-deps  # 同时安装依赖
```

| 参数 | 说明 |
|------|------|
| `desc` | 项目描述，AI 据此判断技术栈并生成入口文件 |
| `--install-deps` | 自动安装 Python 依赖 |

AI 会自主决定：
- **项目类型**：Flask / FastAPI / CLI / RabbitMQ / Kafka / APScheduler / WebSocket 等
- **入口文件名**：`app.py` / `main.py` / `consumer.py` / `scheduler.py` 等
- **额外依赖**：自动追加到 `modules/requirements.txt`

### `create` — AI 生成组件

```bash
python -m ai_pod_cli create --category entry --name StockChecker \
    --desc "查询指定商品库存，库存为0则发送短信通知管理员"
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `--name` | ✅ | 组件名称（必须与类名一致） |
| `--category` | ✅ | `entry`（业务组件）或 `entity`（基础实体） |
| `--desc` | ✅ | 需求描述 |

**AI 自动完成：**
1. 从组件池中挑选依赖（DI 注入）
2. 生成完整代码（含 `@inject` 构造函数 + `execute` 方法）
3. 定义数据契约（`inputs` / `outputs`）
4. 如需要新配置项，自动追加到 `config.toml`（通过 tomlkit）
5. AST 安全检查（拦截 `eval`/`exec`/dunder 链）
6. 写入 `modules/` 并注册到 `beans_config.json`

### `add` — 注册人工编写的组件

```bash
python -m ai_pod_cli add --name CacheClient \
    --class-path my_infra.cache.CacheClient \
    --desc "Redis 缓存客户端"
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `--name` | ✅ | 组件名称 |
| `--class-path` | ✅ | 完整类路径 |
| `--desc` | ✅ | 功能描述 |
| `--category` | ❌ | 默认 `entity` |

### `build` — AI 生成管道

```bash
# 生成管道文件 + 注册路由（不执行）
python -m ai_pod_cli build "采集销售数据并写入SQLite" --name collect_sales

# 列出所有已生成的管道
python -m ai_pod_cli build --list
```

| 参数 | 说明 |
|------|------|
| `cmd` | 自然语言业务指令 |
| `--name` | 自定义管道文件名（默认从指令自动生成） |
| `--list` | 列出所有已保存的管道 |

**build 做了什么：**
1. AI 分析指令，规划组件执行链
2. 生成 `pipelines/<name>.py` 管道文件
3. AST 安全检查
4. 自动注册到 `routes.toml`
5. **不执行** — 由开发者通过入口文件运行

## 架构

### 生成与执行分离

```
┌─────────────────────────────────┐
│         开发时（生成）            │
│                                 │
│  init "描述"  → 入口文件         │
│  create       → 组件代码         │
│  build "指令" → 管道文件 + 路由   │
└─────────────────────────────────┘
              ↓
┌─────────────────────────────────┐
│         运行时（执行）            │
│                                 │
│  python app.py                  │
│    → Flask/FastAPI/CLI/MQ      │
│    → PipelineRunner            │
│    → routes.toml 路由匹配       │
│    → 加载 pipeline.run(ctx)     │
│    → DI 容器组装 + 管道执行      │
└─────────────────────────────────┘
```

### PipelineRunner — 统一执行器

框架内置的 `PipelineRunner` 负责加载和执行管道：

```python
from ai_pod_cli.runner import PipelineRunner

runner = PipelineRunner()              # 读取 routes.toml
runner.route_names()                   # ['collect_sales', ...]
result = runner.run("collect_sales", {"sku_id": 1001})
```

AI 生成的入口文件（Flask/CLI/MQ 等）内部都调用 PipelineRunner。

### 管道语法

```python
from ai_pod_cli.container import build_container, Pod

def run(ctx: PipelineContext):
    container = build_container(load_config())
    S = Pod(container)

    # 管道符串联：依次执行 A → B → C，自动记录轨迹
    (S(DataCollector) | S(DataCleaner) | S(DataWriter)).execute_all(ctx)

    # 条件分支
    if ctx.get("stock", 0) <= 0:
        (S(StockNotifier)).execute_all(ctx)

    return ctx.summary()
```

### PipelineContext — 数据流转

```python
class DataProcessor:
    def execute(self, ctx: PipelineContext) -> dict:
        # 读取入口参数
        count = ctx.params.get("order_count", 5)

        # 读取上游数据
        raw = ctx.get("raw_orders", [])

        # 业务处理...

        # 写入共享数据池，供下游使用
        ctx.set("processed_orders", result)
        return {"status": "success"}
```

### ConfigStore — 集中配置

组件通过 DI 注入 ConfigStore 读取 `config.toml`：

```toml
# config.toml
[database]
sqlite_path = "data.db"    # build 时 AI 自动建议追加

[redis]
host = "localhost"
port = 6379
```

```python
class SqliteStore:
    @inject
    def __init__(self, config_store: ConfigStore):
        db_path = config_store.get("database.sqlite_path", "data.db")
        self.conn = sqlite3.connect(db_path)
```

### routes.toml — 路由映射

`build` 命令自动注册，也可手动编辑：

```toml
[collect_sales]
pipeline = "pipelines/collect_sales.py"
description = "采集销售数据并写入SQLite"

[stock_alert]
pipeline = "pipelines/stock_alert.py"
description = "库存不足时发送预警"
```

## 项目结构

```
ai-code-cli/
├── pyproject.toml
├── requirements.txt
├── .env / .env.example
├── config.toml              ← 项目配置（用户 + AI 共同维护）
├── routes.toml              ← 路由映射（build 自动注册）
├── beans_config.json        ← 组件账本
├── cli.py                   ← AI 生成的入口（CLI 示例）
├── modules/                 ← AI 生成的组件
│   ├── sqlitestore.py
│   ├── datacollector.py
│   └── datawriter.py
├── pipelines/               ← AI 生成的管道
│   └── collect_sales.py
└── ai_pod_cli/              ← 引擎核心
    ├── cli.py               # CLI 参数解析
    ├── client.py            # 大模型客户端（含重试）
    ├── config.py            # 配置管理
    ├── config_store.py      # ConfigStore 实体
    ├── container.py         # DI 容器 + Pod 管道包装
    ├── context.py           # PipelineContext
    ├── runner.py            # PipelineRunner 执行器
    ├── security.py          # AST 安全检查
    ├── entities.py          # 内置实体（DbClient, SmsSender）
    └── commands/
        ├── init.py          # init 命令
        ├── create.py        # create 命令
        ├── add.py           # add 命令
        └── build.py         # build 命令
```

## 安全

AI 生成的代码经过 AST 静态扫描，拦截以下模式：

| 类型 | 拦截内容 |
|------|---------|
| 危险函数 | `eval()`, `exec()`, `compile()`, `__import__()` |
| dunder 链 | `__subclasses__`, `__mro__`, `__globals__`, `__builtins__` 等 |

不限制 `import`——这是本地开发工具，开发者对自己的代码负责。

## 配置

| 文件 | 用途 | 谁维护 |
|------|------|--------|
| `.env` | 大模型 API 配置 | 开发者 |
| `config.toml` | 项目配置（DB路径、端口等） | 开发者 + AI 建议追加 |
| `routes.toml` | 管道路由映射 | AI 自动注册 + 开发者手动编辑 |
| `beans_config.json` | 组件账本 | AI 自动维护 |

## 依赖

| 包 | 用途 |
|----|------|
| `openai` | 大模型 API 客户端 |
| `injector` | IoC/DI 容器框架 |
| `python-dotenv` | `.env` 环境变量加载 |
| `tomlkit` | TOML 读写（保留格式和注释） |

## License

MIT
