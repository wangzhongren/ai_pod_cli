"""`pod` command — AI decomposes a requirement into a set of components."""

import json
import os
import sys

from ai_pod_cli.client import call_llm
from ai_pod_cli.config import load_config, save_config, MODULES_DIR, load_config_toml_keys
from ai_pod_cli.security import validate_code


def handle_pod(args):
    """【pod 命令】AI 将一个大需求拆解为一组组件，逐个生成并加入 Bean Pool"""
    print(f"🧩 [pod] 拆解需求: '{args.desc}'")

    if not os.environ.get("OPENAI_API_KEY"):
        print("❌ 错误: 请先配置环境变量 OPENAI_API_KEY")
        sys.exit(1)

    config = load_config()
    existing_beans = json.dumps(config["beans"], indent=2, ensure_ascii=False)
    toml_keys = load_config_toml_keys()

    system_prompt = f"""
    你是一个资深的软件架构师。当前系统是一个基于 Python `injector` 框架的 IoC/DI 容器低代码平台。

    目前系统中已有的组件池（Bean Pool）：
    {existing_beans}

    当前 config.toml 中可用的配置段和键名：
    {toml_keys}

    你的任务是：将人类描述的一个大需求，拆解成多个可独立生成的组件。

    【拆解规则】：
    1. 每个组件必须有明确的单一职责。
    2. 分类只有两种：entry（业务组件，有 execute 方法）和 entity（基础设施实体，有自定义方法）。
    3. 依赖只能从已有的 Bean Pool 中选择，或者选择本次拆解中排在它前面的组件。
    4. 不要重复已有组件的功能。如果 Bean Pool 里已有合适的组件，直接引用它。
    5. 每个组件的 description 要足够详细，让后续 AI 生成时能写出完整代码。
    6. 组件数量控制在 2~6 个，不要过度拆解。
    7. 如果新组件需要 config.toml 中的新配置项，在 config_additions 中说明。

    请严格以标准 JSON 格式返回（不要包含 Markdown 块标记）：
    {{
        "pod_name": "这组组件的简短名称",
        "components": [
            {{
                "name": "组件类名（PascalCase）",
                "category": "entry 或 entity",
                "description": "详细的组件描述，包括方法签名、输入输出、依赖说明",
                "depends_on": ["依赖的组件ID_1", "依赖的组件ID_2"]
            }}
        ],
        "config_additions": {{
            "section_name": {{
                "key_name": {{"value": "默认值", "comment": "说明"}}
            }}
        }}
    }}
    config_additions 为建议新增到 config.toml 的配置项，不需要则为空对象 {{}}。
    """

    try:
        plan = call_llm(system_prompt, f"需求: {args.desc}", json_mode=True, temperature=0.2)
    except Exception as e:
        print(f"❌ AI 拆解失败: {e}")
        return

    pod_name = plan.get("pod_name", "unnamed_pod")
    components = plan.get("components", [])
    config_additions = plan.get("config_additions", {})

    if not components:
        print("❌ AI 未返回任何组件。")
        return

    # 打印拆解方案
    print(f"\n📋 [拆解方案] {pod_name} ({len(components)} 个组件)\n")
    for i, comp in enumerate(components, 1):
        deps = comp.get("depends_on", [])
        dep_str = f" ← depends: {', '.join(deps)}" if deps else ""
        print(f"   {i}. {comp['name']} ({comp['category']}){dep_str}")
    print()

    # 追加配置到 config.toml
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
                if isinstance(raw_value, dict):
                    parsed_value = raw_value.get("value", "")
                    comment = raw_value.get("comment", "")
                    if isinstance(parsed_value, str):
                        parsed_value = parsed_value.strip('"').strip("'")
                elif isinstance(raw_value, str):
                    parsed_value = raw_value.strip('"').strip("'")
                    comment = ""
                else:
                    parsed_value = raw_value
                    comment = ""

                item = tomlkit.item(parsed_value)
                if comment:
                    item.comment(comment)
                doc[section][key] = item
                added_count += 1

        with open(CONFIG_TOML, "w", encoding="utf-8") as f:
            tomlkit.dump(doc, f)
        print(f"⚙️  [配置追加] {added_count} 个新配置项写入 {CONFIG_TOML}\n")

    # 逐个生成组件
    generated = []
    failed = []

    for i, comp in enumerate(components, 1):
        name = comp["name"]
        category = comp["category"]
        description = comp["description"]

        print(f"🤖 [{i}/{len(components)}] 生成 {name} ({category})...")

        # 重新加载配置（因为每轮生成后 bean pool 会更新）
        config = load_config()
        beans_context = json.dumps(config["beans"], indent=2, ensure_ascii=False)

        create_prompt = f"""
        你是一个资深的 Python 代码生成器。当前系统组件池：
        {beans_context}

        请为以下组件生成完整的 Python 代码：
        - 名称: {name}
        - 分类: {category}
        - 描述: {description}

        【代码规范】：
        - 必须 from ai_pod_cli.context import PipelineContext
        - 必须 from injector import inject
        - 类名必须与 {name} 完全一致
        - @inject 构造函数只能放组件类型依赖，不能放 str/int 等原始类型
        - 配置值通过注入 ConfigStore 读取：from ai_pod_cli.config_store import ConfigStore
        - entry 类型必须实现 execute(self, ctx: PipelineContext) -> dict
        - entity 类型不需要 execute，提供描述中的业务方法
        - 从 ctx.params 或 ctx.get() 读输入，ctx.set() 写输出
        - import 路径：ai_pod_cli.entities.XXX → from ai_pod_cli.entities import XXX
                       modules.xxx.XXX → from modules.xxx import XXX
                       ai_pod_cli.config_store.ConfigStore → from ai_pod_cli.config_store import ConfigStore
        - 无依赖时：@inject def __init__(self): pass

        请严格以标准 JSON 格式返回：
        {{
            "dependencies": ["依赖ID_1"],
            "inputs": {{"参数名": "类型 — 说明"}},
            "outputs": {{"输出键": "类型 — 说明"}},
            "ai_spec": "技术规格描述",
            "code": "完整 Python 源代码"
        }}
        """

        try:
            result = call_llm(create_prompt, f"生成组件: {name}", json_mode=True, temperature=0.1)

            code = result.get("code", "")
            dependencies = result.get("dependencies", [])
            inputs = result.get("inputs", {})
            outputs = result.get("outputs", {})
            ai_spec = result.get("ai_spec", "")

            if not code:
                print(f"   ❌ AI 未返回代码，跳过。")
                failed.append(name)
                continue

            # 安全检查
            violations = validate_code(code)
            if violations:
                print(f"   🛡️  安全检查失败 ({len(violations)} 处违规)，跳过。")
                for v in violations:
                    print(f"      ❌ {v}")
                failed.append(name)
                continue

            # 写入文件
            os.makedirs(MODULES_DIR, exist_ok=True)
            file_path = os.path.join(MODULES_DIR, f"{name.lower()}.py")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(code)

            # 注册到 bean pool
            new_bean = {
                "id": name,
                "category": category,
                "type": "ai_created",
                "class_path": f"{MODULES_DIR}.{name.lower()}.{name}",
                "dependencies": dependencies,
                "inputs": inputs,
                "outputs": outputs,
                "description": f"{description}。技术规格: {ai_spec}",
            }
            config["beans"] = [b for b in config["beans"] if b["id"] != name]
            config["beans"].append(new_bean)
            save_config(config)

            generated.append(name)
            print(f"   ✅ {name} → {file_path}")

        except Exception as e:
            print(f"   ❌ 生成失败: {e}")
            failed.append(name)

    # 输出汇总
    print(f"\n{'='*50}")
    print(f"🧩 [Pod 生成完毕] {pod_name}")
    print(f"   ✅ 成功: {len(generated)} 个 — {', '.join(generated) if generated else '(无)'}")
    if failed:
        print(f"   ❌ 失败: {len(failed)} 个 — {', '.join(failed)}")

    if generated:
        print(f"\n   组件已加入 Bean Pool，可以 compose 了:")
        print(f"   aipod compose \"<业务指令>\"")
    print(f"{'='*50}")
