"""Bean configuration registry — init, load, and save the beans_config.json file."""

import json
import os

CONFIG_FILE = "beans_config.json"
CONFIG_TOML = "config.toml"
ROUTES_TOML = "routes.toml"
MODULES_DIR = "modules"
PIPELINES_DIR = "pipelines"


# config.toml 默认内容（只放框架自身需要的配置，用户自行扩展）
DEFAULT_CONFIG_TOML = """\
# AIPodCli 项目配置
# 组件通过注入 ConfigStore 读取此文件
# 你可以自由添加自己的 [section] 和 key

"""

# routes.toml 默认内容
DEFAULT_ROUTES_TOML = """\
# Pipeline 路由配置
# 每条路由映射一个命令/端点到对应的 pipeline 文件
# 由 build 命令自动注册，也可手动编辑

"""


def init_config_if_not_exists():
    """初始化配置中心，预置基础设施组件"""
    # 创建 config.toml
    if not os.path.exists(CONFIG_TOML):
        with open(CONFIG_TOML, "w", encoding="utf-8") as f:
            f.write(DEFAULT_CONFIG_TOML)

    # 创建 routes.toml
    if not os.path.exists(ROUTES_TOML):
        with open(ROUTES_TOML, "w", encoding="utf-8") as f:
            f.write(DEFAULT_ROUTES_TOML)

    if not os.path.exists(CONFIG_FILE):
        initial_data = {
            "beans": [
                {
                    "id": "ConfigStore",
                    "category": "entity",
                    "type": "human_added",
                    "class_path": "ai_pod_cli.config_store.ConfigStore",
                    "methods": {
                        "get": {
                            "inputs": {"key": "str — dot-notation 键路径，如 database.sqlite_path", "default": "any — 默认值"},
                            "outputs": "any — 配置值",
                        },
                        "get_section": {
                            "inputs": {"section": "str — 段名，如 database"},
                            "outputs": "dict — 该段所有键值对",
                        },
                        "sections": {
                            "inputs": {},
                            "outputs": "list[str] — 所有段名",
                        },
                        "reload": {
                            "inputs": {},
                            "outputs": "无返回值 — 重新加载 config.toml",
                        },
                    },
                    "description": "集中式配置组件。从 config.toml 读取配置，通过 get('section.key') 访问。其他组件应注入 ConfigStore 来获取配置值，而非直接读环境变量。",
                },
                {
                    "id": "DbClient",
                    "category": "entity",
                    "type": "human_added",
                    "class_path": "ai_pod_cli.entities.DbClient",
                    "methods": {
                        "query": {
                            "inputs": {"sql": "str — SQL 查询语句"},
                            "outputs": {"stock": "int — 当前库存"},
                        }
                    },
                    "description": "基础数据库服务。提供 query(sql: str) 方法，返回包含库存的字典。",
                },
                {
                    "id": "SmsSender",
                    "category": "entity",
                    "type": "human_added",
                    "class_path": "ai_pod_cli.entities.SmsSender",
                    "methods": {
                        "send": {
                            "inputs": {"phone": "str — 手机号码", "msg": "str — 短信内容"},
                            "outputs": "无返回值",
                        }
                    },
                    "description": "短信发送服务。提供 send(phone: str, msg: str) 方法，无返回值。",
                },
            ]
        }
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(initial_data, f, indent=2, ensure_ascii=False)


def load_config() -> dict:
    """Load the bean configuration from disk."""
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_config_toml_text() -> str:
    """Read the raw config.toml content for AI prompts."""
    if os.path.exists(CONFIG_TOML):
        with open(CONFIG_TOML, "r", encoding="utf-8") as f:
            return f.read()
    return "(config.toml 为空)"


def load_config_toml_keys() -> str:
    """Parse config.toml and return a summary of available sections and keys (no values)."""
    if not os.path.exists(CONFIG_TOML):
        return "(config.toml 为空，暂无配置项)"

    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib

        with open(CONFIG_TOML, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return "(config.toml 解析失败)"

    if not data:
        return "(config.toml 为空，暂无配置项。你可以添加 [section] 和 key，组件通过 ConfigStore 读取)"

    lines = []
    for section, values in data.items():
        if isinstance(values, dict):
            keys = ", ".join(values.keys())
            lines.append(f"  [{section}]  {keys}")
        else:
            lines.append(f"  {section} = (top-level)")
    return "\n".join(lines)


def save_config(config: dict):
    """Persist the bean configuration to disk."""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def register_route(name: str, pipeline_path: str, description: str = ""):
    """Register or update a route in routes.toml using tomlkit."""
    import tomlkit

    if os.path.exists(ROUTES_TOML):
        with open(ROUTES_TOML, "r", encoding="utf-8") as f:
            doc = tomlkit.load(f)
    else:
        doc = tomlkit.document()
        doc.add(tomlkit.comment("Pipeline 路由配置"))
        doc.add(tomlkit.comment("由 build 命令自动注册，也可手动编辑"))
        doc.add(tomlkit.nl())

    table = tomlkit.table()
    table.add("pipeline", pipeline_path)
    if description:
        table.add("description", description)

    doc[name] = table

    with open(ROUTES_TOML, "w", encoding="utf-8") as f:
        tomlkit.dump(doc, f)
