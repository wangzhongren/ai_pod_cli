"""`create` command — AI generates a new component with DI and registers it in the config."""

import json
import os
import sys

from ai_pod_cli.client import call_llm
from ai_pod_cli.config import CONFIG_FILE, MODULES_DIR, load_beans, load_beans_summary, load_config_toml_safe, save_config, append_deps_to_requirements, get_module_path
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

    beans = load_beans()
    beans_summary = load_beans_summary()
    toml_keys = load_config_toml_safe()

    # 构造分类别的 System Prompt
    common_context = f"""
    当前系统是一个基于 Python `injector` 框架的 IoC/DI 容器低代码平台。

    目前系统中已经注册了以下可用的依赖组件池（Bean Pool）：
    {beans_summary}

    当前 config.toml 中的配置项（敏感值已隐藏，组件通过 ConfigStore 读取）：
    {toml_keys}
    如果组件需要的配置项还不存在，请在返回的 JSON 中通过 config_additions 字段建议新增（格式如 {{"section": {{"key": "说明"}}}}），系统会自动追加到 config.toml。

    【通用规范】：
    - 必须从 `injector` 引入 `inject`，构造函数加 `@inject` 装饰器。
    - 构造函数参数**只能声明组件类型依赖**（Bean Pool 中的类），禁止 str/int/bool 等原始类型。
    - **依赖的方法必须来自上方组件池中列出的方法签名！禁止调用不存在的方法！**
      比如 SqliteStore 只有 .insert() 和 .query()，就不能调用 .connect() 或 .execute()。
    - 配置通过注入 ConfigStore 读取：`from ai_pod_cli.config_store import ConfigStore`，用 `config_store.get("section.key", default)` 读取。
    - **禁止创建纯 ConfigStore 包装类**：ConfigStore 已可注入，不要生成只转发 get() 的组件。
    - 没有依赖时写 `@inject def __init__(self): pass`。
    - 禁止手动实例化任何依赖组件，全部通过 DI 注入。

    【import 路径规则（严格遵守，错了代码跑不起来）】：
    - 每个组件的 import 路径见上方组件池，文件名（括号里的 .py 文件）必须**原样使用**！
    - **禁止 `from modules import X`！禁止 `from modules import X, Y, Z`！** 每个组件必须从自己的子目录导入。
    - ConfigStore 必须从 ai_pod_cli.config_store 导入，禁止从 modules 导入！
    - ai_pod_cli.config_store.ConfigStore → from ai_pod_cli.config_store import ConfigStore
    - modules.providers.xxx.XXX → from modules.providers.xxx import XXX
    - modules.services.xxx.XXX → from modules.services.xxx import XXX

    【第三方依赖声明】：
    - 如果代码 import 了标准库和 ai_pod_cli 之外的第三方包，必须在 extra_deps 中列出。
    - 不需要则返回空数组。
    """

    if args.category == "provider":
        system_prompt = common_context + f"""
    你的任务：生成一个 **provider（基础设施提供者）** 组件。

    【provider 规范】：
    - provider 是基础设施组件（如 DB、缓存、HTTP 客户端、邮件发送器等），不需要 execute 方法。
    - 只需要提供业务方法（如 query、send、get、set 等），每个方法有明确的入参和返回值。
    - 组件名称: {args.name}，类名必须与此一致。

    【provider 模板】（RedisStore，依赖 ConfigStore 读取配置）：
    ```python
    from injector import inject
    from ai_pod_cli.config_store import ConfigStore

    class RedisStore:
        @inject
        def __init__(self, config_store: ConfigStore):
            host = config_store.get("redis.host", "localhost")
            port = config_store.get("redis.port", 6379)
            self.client = SomeRedisLib(host, port)

        def get(self, key: str) -> str:
            return self.client.get(key)

        def set(self, key: str, value: str):
            self.client.set(key, value)
    ```

    返回 JSON（不要 Markdown 块标记）：
    {{
        "dependencies": ["依赖ID"],
        "methods": {{
            "method_name": {{
                "inputs": {{"参数名": "类型"}},
                "outputs": "返回值类型 — 说明"
            }}
        }},
        "code": "完整 Python 源代码",
        "config_additions": {{"section": {{"key": {{"value": "", "comment": ""}}}}}},
        "extra_deps": ["包名"]
    }}
    inputs/outputs 和 ai_spec 不适用于 provider，留空即可。
    """
    else:
        system_prompt = common_context + f"""
    你的任务：生成一个 **service（业务服务）** 组件。

    【service 规范】：
    - 你只生成**一个 Python 类**，不是 pipeline 脚本！不要写 run() 函数，不要用 S()/Pod()/build_container。
    - service 是业务组件，必须实现 `execute(self, ctx: PipelineContext) -> dict` 方法。
    - 从 `ctx.params` 或 `ctx.get(key)` 读取输入，通过 `ctx.set(key, value)` 写入输出。
    - 必须 `from ai_pod_cli.context import PipelineContext`。
    - 组件名称: {args.name}，类名必须与此一致。

    【service 模板】（StockChecker，依赖 ConfigStore）：
    ```python
    from injector import inject
    from ai_pod_cli.context import PipelineContext
    from ai_pod_cli.config_store import ConfigStore

    class StockChecker:
        @inject
        def __init__(self, config_store: ConfigStore):
            self.api_url = config_store.get("stock.api_url", "https://api.example.com")

        def execute(self, ctx: PipelineContext) -> dict:
            sku_id = ctx.params.get("sku_id", ctx.get("sku_id"))

            import requests
            resp = requests.get(f"{{{{self.api_url}}}}/{{{{sku_id}}}}")
            stock_info = resp.json()

            ctx.set("stock", stock_info["stock"])
            ctx.set("sku_id", sku_id)

            if stock_info["stock"] <= 0:
                ctx.set("alert_sent", True)
                return {{{{"status": "failed", "reason": "out_of_stock"}}}}

            return {{{{"status": "success", "stock": stock_info["stock"]}}}}
    ```

    返回 JSON（不要 Markdown 块标记）：
    {{
        "dependencies": ["依赖ID"],
        "inputs": {{"参数名": "类型 — 说明"}},
        "outputs": {{"输出键": "类型 — 说明"}},
        "ai_spec": "对 execute 方法的技术规格描述",
        "code": "完整 Python 源代码",
        "config_additions": {{"section": {{"key": {{"value": "", "comment": ""}}}}}},
        "extra_deps": ["包名"]
    }}
    methods 不适用于 service，留空即可。
    """

    user_content = f"新组件名称: {args.name}\n组件分类: {args.category}\n人类诉求描述: {args.desc}"

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            result = call_llm(system_prompt, user_content, json_mode=True, temperature=0.1)

            dependencies = result.get("dependencies", [])
            ai_spec = result.get("ai_spec", "")
            generated_code = result.get("code", "")
            inputs = result.get("inputs", {})
            outputs = result.get("outputs", {})
            methods = result.get("methods", {})
            config_additions = result.get("config_additions", {})
            extra_deps = result.get("extra_deps", [])

            if not generated_code:
                if attempt < max_retries:
                    print(f"   ⚠️  AI 未返回有效代码，第 {attempt}/{max_retries} 次重试...")
                    continue
                print("❌ AI 未返回有效代码，已重试 3 次仍失败。")
                return
            break

        except Exception as e:
            if attempt < max_retries:
                print(f"   ⚠️  第 {attempt}/{max_retries} 次失败 ({e})，重试...")
                continue
            print(f"❌ 大模型调用或解析失败，原因为: {e}")
            return

    try:
        print(f"🔍 [AI 依赖分析成功] 大模型自动挑选了系统依赖: {dependencies}")
        if args.category == "service":
            print(f"📋 [数据契约] inputs: {list(inputs.keys())}, outputs: {list(outputs.keys())}")
        else:
            print(f"📋 [方法签名] {list(methods.keys())}")

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

        # 物理写入文件（按分类写入不同子目录）
        module_dir, class_path = get_module_path(args.category, args.name)
        os.makedirs(module_dir, exist_ok=True)
        file_path = os.path.join(module_dir, f"{args.name.lower()}.py")
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
            "class_path": class_path,
            "file": f"{args.name.lower()}.py",
            "dependencies": dependencies,
            "inputs": inputs,
            "outputs": outputs,
            "methods": methods,
            "description": f"人类诉求: {args.desc}。技术规格: {ai_spec}",
        }

        beans["beans"] = [b for b in beans["beans"] if b["id"] != args.name]
        beans["beans"].append(new_bean)
        save_config(beans)
        print(f"💾 [元数据入库成功] 账本配置中心更新完毕！\n")

    except Exception as e:
        print(f"❌ 组件生成失败，原因为: {e}")
