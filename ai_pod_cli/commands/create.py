"""`create` command — AI generates a new component with DI and registers it in the config."""

import json
import os
import sys

from ai_pod_cli.client import call_llm
from ai_pod_cli.config import CONFIG_FILE, MODULES_DIR, load_config, load_config_toml_keys, save_config, append_deps_to_requirements
from ai_pod_cli.security import validate_code, SecurityError


def handle_create(args):
    """【create 命令】大模型解构诉求，自动挑选依赖并生成代码"""
    print(f"🤖 [CLI] 正在启动 AI 为您创造组件: '{args.name}'...")
    print(f"📝 [CLI] 您的核心诉求: {args.desc}")

    if not os.environ.get("OPENAI_API_KEY"):
        print("❌ OPENAI_API_KEY 未配置。请先设置：")
        print("   aipod config set OPENAI_API_KEY sk-your-key")
        sys.exit(1)

    append_deps_to_requirements([])

    config = load_config()
    existing_beans_context = json.dumps(config["beans"], indent=2, ensure_ascii=False)
    toml_keys = load_config_toml_keys()

    # 构造强约束的 System Prompt
    system_prompt = f"""
    你是一个资深的软件架构专家和代码生成器。当前系统是一个基于 Python `injector` 框架的 IoC/DI 容器低代码平台，
    使用 PipelineContext 作为组件间数据流转的共享上下文。

    目前系统中已经注册了以下可用的依赖组件池（Bean Pool）：
    {existing_beans_context}

    当前 config.toml 中可用的配置段和键名（组件通过注入 ConfigStore，用 config_store.get("section.key", default) 读取）：
    {toml_keys}
    如果组件需要的配置项还不存在，请在返回的 JSON 中通过 config_additions 字段建议新增（格式如 {{"section": {{"key": "说明"}}}}），系统会自动追加到 config.toml。

    你的任务是：根据人类传入的新组件名称、分类和诉求描述，完成以下三件事：
    1. 从已有的组件池中挑选出这个新组件真正需要的外部依赖（只返回 id 数组，不要生造）。
    2. 为该组件生成完整的 Python 源代码。
    3. 定义该组件的数据契约（inputs/outputs），供后续 AI 编排使用。

    【核心代码生成规范】：
    - 必须 `from ai_pod_cli.context import PipelineContext`。
    - 必须从 `injector` 中引入 `inject`。
    - 类的名称必须与人类指定的名称完全一致！
    - 构造函数 `__init__` 必须加上 `@inject` 装饰器。
    - 构造函数的参数**只能声明组件类型的依赖**（即 Bean Pool 中其他组件的类），禁止声明 str、int、bool 等原始类型参数。
    - **配置值通过注入 ConfigStore 读取**：`from ai_pod_cli.config_store import ConfigStore`，然后在构造函数中声明 `config_store: ConfigStore` 参数，在代码中用 `config_store.get("section.key", default)` 读取。这是首选方式。
    - 如果没有组件依赖（包括不需要 ConfigStore），构造函数写 `@inject def __init__(self): pass`。
    - 禁止自己初始化任何外部工具，全部通过 DI 注入。
    - 必须实现 `execute(self, ctx: PipelineContext) -> dict` 方法。
    - 从 `ctx.params` 或 `ctx.get(key)` 读取输入数据。
    - 通过 `ctx.set(key, value)` 写入输出数据供下游组件使用。
    - execute 方法的返回值也应为 dict。
    - 如果组件分类为 entity（基础设施实体），可以不提供 execute 方法，只需提供其业务方法（如 query、send 等）。

    【代码模板示例】（假设类名为 StockChecker，依赖 ConfigStore 读取配置和 API key）：
    ```python
    from injector import inject
    from ai_pod_cli.context import PipelineContext
    from ai_pod_cli.config_store import ConfigStore


    class StockChecker:
        \"\"\"库存检查组件\"\"\"

        @inject
        def __init__(self, config_store: ConfigStore):
            self.config_store = config_store
            # 通过 ConfigStore 读取配置
            self.api_url = config_store.get("stock.api_url", "https://api.example.com/stock")

        def execute(self, ctx: PipelineContext) -> dict:
            \"\"\"业务执行入口\"\"\"
            # 1. 从上下文读取输入参数
            sku_id = ctx.params.get("sku_id", ctx.get("sku_id"))

            # 2. 通过注入的依赖执行业务逻辑
            import requests
            resp = requests.get(f"{{self.api_url}}/{{sku_id}}")
            stock_info = resp.json()

            # 3. 将中间结果写入上下文供下游组件使用
            ctx.set("stock", stock_info["stock"])
            ctx.set("sku_id", sku_id)

            # 4. 业务判断与副作用
            if stock_info["stock"] <= 0:
                ctx.set("alert_sent", True)
                return {{"status": "failed", "reason": "out_of_stock"}}

            return {{"status": "success", "stock": stock_info["stock"]}}
    ```

    【entity 实体模板示例】（假设类名为 RedisStore，依赖 ConfigStore 读取配置）：
    ```python
    from injector import inject
    from ai_pod_cli.config_store import ConfigStore


    class RedisStore:
        \"\"\"Redis 存储实体\"\"\"

        @inject
        def __init__(self, config_store: ConfigStore):
            # 通过 ConfigStore 读取 config.toml 中的配置
            host = config_store.get("redis.host", "localhost")
            port = config_store.get("redis.port", 6379)
            self.client = SomeRedisLib(host, port)

        def get(self, key: str) -> str:
            return self.client.get(key)

        def set(self, key: str, value: str):
            self.client.set(key, value)
    ```

    【import 路径规则（严格遵守，不允许捏造）】：
    - **ConfigStore 是框架内置组件，必须从 ai_pod_cli.config_store 导入：`from ai_pod_cli.config_store import ConfigStore`**
    - **禁止从 modules.config_store 导入 ConfigStore，modules 下没有这个文件！**
    - class_path 为 `ai_pod_cli.entities.XXX` → `from ai_pod_cli.entities import XXX`
    - class_path 为 `ai_pod_cli.config_store.ConfigStore` → `from ai_pod_cli.config_store import ConfigStore`
    - class_path 为 `modules.xxx.XXX` → `from modules.xxx import XXX`
    - 如果没有依赖，构造函数只需 `@inject def __init__(self): pass`
    - 禁止在代码中手动实例化任何依赖组件

    【第三方依赖声明】：
    - 如果生成的代码 import 了标准库和 ai_pod_cli 之外的第三方包（如 requests, redis, pymysql, flask, fastapi, aiohttp 等），必须在 extra_deps 字段中列出这些包名。
    - 如果只使用标准库和 ai_pod_cli 内部的包，extra_deps 返回空数组。

    请严格以标准 JSON 格式返回（不要包含 Markdown 块标记）：
    {{
        "dependencies": ["选中的依赖ID_1", "选中的依赖ID_2"],
        "inputs": {{
            "参数名1": "类型 — 说明",
            "参数名2": "类型 — 说明"
        }},
        "outputs": {{
            "输出键1": "类型 — 说明",
            "输出键2": "类型 — 说明"
        }},
        "ai_spec": "对 execute 方法的技术规格描述",
        "code": "完整的 Python 源代码字符串",
        "config_additions": {{
            "section_name": {{
                "key_name": {{"value": "实际默认值(字符串加引号/数字不加)", "comment": "中文说明"}}
            }}
        }},
        "extra_deps": ["包名1", "包名2"]
    }}
    config_additions 用于建议需要新增到 config.toml 的配置项，如果不需要新增则返回空对象 {{}}。
    extra_deps 是代码中 import 的第三方 pip 包名（不含标准库和 ai_pod_cli），不需要则为空数组 []。
    注意：value 字段必须是合法的 TOML 值——字符串加双引号如 "data.db"，数字不加引号如 6379，布尔值用 true/false。
    comment 字段是中文注释说明，不要放在 value 里。
    }}
    """

    user_content = f"新组件名称: {args.name}\n组件分类: {args.category}\n人类诉求描述: {args.desc}"

    try:
        result = call_llm(system_prompt, user_content, json_mode=True, temperature=0.1)

        dependencies = result.get("dependencies", [])
        ai_spec = result.get("ai_spec", "")
        generated_code = result.get("code", "")
        inputs = result.get("inputs", {})
        outputs = result.get("outputs", {})
        config_additions = result.get("config_additions", {})
        extra_deps = result.get("extra_deps", [])

        print(f"🔍 [AI 依赖分析成功] 大模型自动挑选了系统依赖: {dependencies}")
        print(f"📋 [数据契约] inputs: {list(inputs.keys())}, outputs: {list(outputs.keys())}")

        # 将 AI 建议的新配置追加到 config.toml（使用 tomlkit 保留格式和注释）
        if config_additions:
            import tomlkit
            from ai_pod_cli.config import CONFIG_TOML

            with open(CONFIG_TOML, "r", encoding="utf-8") as f:
                doc = tomlkit.load(f)

            added_count = 0
            for section, keys in config_additions.items():
                if section not in doc:
                    doc.add(section, tomlkit.table())
                for key, raw_value in keys.items():
                    # 新格式: {"value": "data.db", "comment": "说明"}
                    if isinstance(raw_value, dict):
                        parsed_value = raw_value.get("value", "")
                        comment = raw_value.get("comment", "")
                        # AI 可能返回 "\"localhost\"" 或 "localhost"，统一处理
                        if isinstance(parsed_value, str):
                            stripped = parsed_value.strip('"').strip("'")
                            parsed_value = stripped
                    elif isinstance(raw_value, str):
                        # 兼容旧格式: "\"data.db\"  # 说明"
                        value_str = raw_value.strip()
                        comment = ""
                        if "#" in value_str:
                            val_part, _, comment = value_str.partition("#")
                            value_str = val_part.strip()
                            comment = comment.strip()
                        try:
                            parsed_value = json.loads(value_str)
                        except (json.JSONDecodeError, ValueError):
                            parsed_value = value_str
                    else:
                        # AI 直接返回数字/布尔值
                        parsed_value = raw_value
                        comment = ""

                    item = tomlkit.item(parsed_value)
                    if comment:
                        item.comment(comment)
                    doc[section][key] = item
                    added_count += 1

            with open(CONFIG_TOML, "w", encoding="utf-8") as f:
                tomlkit.dump(doc, f)

            print(f"⚙️  [配置追加] 已将 {added_count} 个新配置项写入 {CONFIG_TOML}")

        # 安全检查：AST 扫描生成代码中的危险操作
        violations = validate_code(generated_code)
        if violations:
            print(f"🛡️  [安全检查失败] AI 生成的代码包含 {len(violations)} 处违规:")
            for v in violations:
                print(f"   ❌ {v}")
            print(f"\n   组件未写入。请使用更明确的描述重试，或手动编辑后使用 add 命令注册。")
            return
        print(f"🛡️  [安全检查通过] 代码未发现危险操作")

        # 物理写入文件
        os.makedirs(MODULES_DIR, exist_ok=True)
        file_path = os.path.join(MODULES_DIR, f"{args.name.lower()}.py")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(generated_code)
        print(f"✍️  [AI 代码生成成功] 模块物理文件已保存至: {file_path}")

        # 将第三方依赖追加到根 requirements.txt（去重）
        if extra_deps:
            append_deps_to_requirements(extra_deps)
            print(f"📦 额外依赖: {', '.join(extra_deps)}")

        # 更新账本元数据（含 inputs/outputs 数据契约）
        new_bean = {
            "id": args.name,
            "category": args.category,
            "type": "ai_created",
            "class_path": f"{MODULES_DIR}.{args.name.lower()}.{args.name}",
            "dependencies": dependencies,
            "inputs": inputs,
            "outputs": outputs,
            "description": f"人类诉求: {args.desc}。技术规格: {ai_spec}",
        }

        config["beans"] = [b for b in config["beans"] if b["id"] != args.name]
        config["beans"].append(new_bean)
        save_config(config)
        print(f"💾 [元数据入库成功] 账本配置中心更新完毕！\n")

    except Exception as e:
        print(f"❌ 大模型调用或解析失败，原因为: {e}")
