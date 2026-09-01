"""Bean configuration registry — init, load, and save the beans_config.json file."""

import json
import ast
import os
import re

CONFIG_FILE = "beans_config.json"
CONFIG_TOML = "config.toml"
ROUTES_TOML = "routes.toml"
MODULES_DIR = "modules"
PROVIDERS_DIR = "modules/providers"
SERVICES_DIR = "modules/services"
MODELS_DIR = "modules/models"

CATEGORY_DIR = {
    "provider": PROVIDERS_DIR,
    "service": SERVICES_DIR,
    "model": MODELS_DIR,
}


def get_module_path(category: str, name: str) -> tuple[str, str]:
    """Return (directory, class_path) for a component based on its category.

    Example: ('modules/providers', 'modules.providers.sqlitestore.SqliteStore')
    """
    dir_name = CATEGORY_DIR.get(category, MODULES_DIR)
    file_stem = name.lower()
    class_path = f"{dir_name.replace('/', '.')}.{file_stem}.{name}"
    return dir_name, class_path
PIPELINES_DIR = "pipelines"


# config.toml 默认内容（只放框架自身需要的配置，用户自行扩展）
DEFAULT_CONFIG_TOML = """\
# AIPodCli 项目配置
# 组件通过注入 ConfigStore 读取此文件
# 你可以自由添加自己的 [section] 和 key

[database]
url = "sqlite:///database.db"

"""

# routes.toml 默认内容
DEFAULT_ROUTES_TOML = """\
# Pipeline 路由配置
# 每条路由映射一个命令/端点到对应的 pipeline 文件
# 由 compose 命令自动注册，也可手动编辑

"""


REQUIREMENTS_FILE = "requirements.txt"
REQUIREMENTS_HEADER = """\
# AIPod 项目依赖
# 基础依赖已随 aipodcli 安装，此处列出 AI 生成组件引入的第三方包
"""

DEPENDENCY_ALIASES = {
    # pygame-ce intentionally keeps the ``pygame`` import namespace.
    "pygame": "pygame-ce",
}


def append_deps_to_requirements(deps: list[str]):
    """将第三方依赖追加写入根 requirements.txt，已存在的跳过，文件不存在时自动创建。"""
    existing = set()
    if os.path.exists(REQUIREMENTS_FILE):
        with open(REQUIREMENTS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    existing.add(line)
    else:
        # 新创建，写入 header
        with open(REQUIREMENTS_FILE, "w", encoding="utf-8") as f:
            f.write(REQUIREMENTS_HEADER)

    with open(REQUIREMENTS_FILE, "a", encoding="utf-8") as f:
        for dep in deps:
            dep = DEPENDENCY_ALIASES.get(dep.strip().lower(), dep.strip())
            if dep and dep not in existing:
                f.write(f"{dep}\n")
                existing.add(dep)


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
                    "category": "provider",
                    "type": "human_added",
                    "class_path": "ai_pod_cli.config_store.ConfigStore",
                    "file": "config_store.py",
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
                    "id": "ModelRepository",
                    "category": "provider",
                    "type": "human_added",
                    "class_path": "ai_pod_cli.repository.ModelRepository",
                    "file": "repository.py",
                    "dependencies": ["ConfigStore"],
                    "methods": {
                        "init_db": {"inputs": {}, "outputs": "null"},
                        "save": {"inputs": {"instance": "model"}, "outputs": "model"},
                        "get": {"inputs": {"model": "type", "object_id": "any"}, "outputs": "model|null"},
                        "list": {"inputs": {"model": "type"}, "outputs": "list[model]"},
                        "find": {"inputs": {"model": "type", "filters": "dict"}, "outputs": "list[model]"},
                        "delete": {"inputs": {"instance": "model"}, "outputs": "null"}
                    },
                    "description": "SQLModel 通用仓库。自动导入全部项目 Model 并建表；Service 只能通过它保存和读取持久化 Model，禁止手写 SQL。"
                },
                {
                    "id": "PipelineRunner",
                    "category": "provider",
                    "type": "human_added",
                    "class_path": "ai_pod_cli.runner.PipelineRunner",
                    "file": "runner.py",
                    "methods": {
                        "run": {
                            "inputs": {"route_name": "str — routes.toml 中的路由名", "params": "dict — 入口参数"},
                            "outputs": "dict — 执行摘要",
                        },
                        "route_names": {
                            "inputs": {},
                            "outputs": "list[str] — 所有已注册路由名",
                        },
                    },
                    "description": "管线运行器。从 routes.toml 加载并执行 pipeline。入口文件通过容器注入此组件来运行管线，无需直接依赖底层 Bean。",
                },
            ]
        }
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(initial_data, f, indent=2, ensure_ascii=False)


def load_beans_summary(*, include_services: bool = True) -> str:
    """Return a categorized summary of all beans with method signatures for AI prompts."""
    config = load_beans()
    models_changed = hydrate_model_fields(config)
    resources_changed = hydrate_provider_resources(config)
    if models_changed or resources_changed:
        save_config(config)
    providers = []
    services = []
    models = []
    for b in config.get("beans", []):
        if b.get("status") == "invalid":
            continue
        desc = b.get("description", "")[:100]
        if b.get("category") == "model":
            entry = f"  - {b['id']} ({b.get('class_path', '')})"
            if b.get("fields"):
                entry += f"\n      冻结字段: {b['fields']}"
            models.append(entry)
        elif b.get("category") == "provider":
            entry = f"  - {b['id']} ({b.get('file', '')}): {desc}"
            methods = b.get("methods", {})
            if methods:
                for m_name, m_info in methods.items():
                    m_inputs = m_info.get("inputs", {})
                    m_outputs = m_info.get("outputs", "")
                    sig = ", ".join(f"{k}: {v}" for k, v in m_inputs.items())
                    entry += f"\n      .{m_name}({sig}) -> {m_outputs}"
            resources = b.get("resources", {})
            if resources:
                entry += f"\n      冻结资源: {resources}"
            providers.append(entry)
        elif include_services:
            entry = f"  - {b['id']} ({b.get('file', '')}): {desc}"
            inputs = b.get("inputs", {})
            outputs = b.get("outputs", {})
            if inputs:
                entry += f"\n      输入: { {k: v for k, v in list(inputs.items())[:5]} }"
            if outputs:
                entry += f"\n      输出: { {k: v for k, v in list(outputs.items())[:5]} }"
            services.append(entry)

    lines = ["当前组件池：", "", "  【model（不可注入；字段类型已冻结）】"]
    lines.extend(models if models else ["  (无)"])
    lines.append("")
    lines.append("  【provider（可注入的依赖，附方法签名）】")
    lines.extend(providers if providers else ["  (无)"])
    if include_services:
        lines.append("")
        lines.append("  【service（有 execute，仅由 Pipeline 可见）】")
        lines.extend(services if services else ["  (无)"])
    return "\n".join(lines)


def extract_model_fields(code: str, class_name: str) -> dict[str, str]:
    """Extract a stable field map from a generated Model dataclass."""
    tree = ast.parse(code)
    model = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name),
        None,
    )
    if model is None:
        return {}
    return {
        node.target.id: ast.unparse(node.annotation)
        for node in model.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }


def hydrate_model_fields(config: dict) -> bool:
    """Backfill schemas for Models created before registry field metadata existed."""
    changed = False
    for bean in config.get("beans", []):
        if bean.get("category") != "model" or bean.get("fields"):
            continue
        path = os.path.join(MODULES_DIR, "models", bean.get("file", ""))
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as source:
                fields = extract_model_fields(source.read(), bean.get("id", ""))
        except (OSError, SyntaxError):
            continue
        if fields:
            bean["fields"] = fields
            changed = True
    return changed


def extract_sql_resources(code: str) -> dict:
    """Extract literal CREATE TABLE schemas exposed by an infrastructure Provider."""
    tables = {}
    pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w]*)\s*\((.*?)\)",
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(code):
        columns = {}
        for raw_column in match.group(2).split(","):
            parts = raw_column.strip().split()
            if len(parts) < 2 or parts[0].upper() in {"PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"}:
                continue
            columns[parts[0].strip('`"[]')] = parts[1].upper()
        tables[match.group(1)] = {"columns": columns}
    return {"tables": tables} if tables else {}


def hydrate_provider_resources(config: dict) -> bool:
    """Backfill literal infrastructure resource contracts from Provider source."""
    changed = False
    for bean in config.get("beans", []):
        if bean.get("category") != "provider" or bean.get("resources"):
            continue
        path = os.path.join(MODULES_DIR, "providers", bean.get("file", ""))
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as source:
                resources = extract_sql_resources(source.read())
        except OSError:
            continue
        if resources:
            bean["resources"] = resources
            changed = True
    return changed


def load_beans() -> dict:
    """Load the bean registry (beans_config.json) from disk."""
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_config_toml_text() -> str:
    """Read the raw config.toml content for AI prompts."""
    if os.path.exists(CONFIG_TOML):
        with open(CONFIG_TOML, "r", encoding="utf-8") as f:
            return f.read()
    return "(config.toml 为空)"


def load_config_toml_safe() -> str:
    """Parse config.toml and return sections, keys, and values (redact sensitive keys)."""
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

    SENSITIVE = {"key", "secret", "password", "token", "passwd", "api_key", "apikey"}

    def is_sensitive(k: str) -> bool:
        lower = k.lower().replace("_", "")
        return any(s in lower for s in SENSITIVE)

    lines = []
    for section, values in data.items():
        if isinstance(values, dict):
            lines.append(f"  [{section}]")
            for k, v in values.items():
                if is_sensitive(k):
                    lines.append(f"    {k} = ***")
                else:
                    lines.append(f"    {k} = {v}")
        else:
            if is_sensitive(section):
                lines.append(f"  {section} = ***")
            else:
                lines.append(f"  {section} = {values}")
    return "\n".join(lines)


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
