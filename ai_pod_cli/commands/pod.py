"""`pod` command — AI decomposes a requirement into a set of components."""

import json
import os
import sys

from ai_pod_cli.client import call_llm
from ai_pod_cli.config import load_config, save_config, MODULES_DIR, load_config_toml_safe, append_deps_to_requirements, get_module_path
from ai_pod_cli.security import validate_code


def _save_pod_plan(pod_name: str, desc: str, components: list, pipelines: list, config_additions: dict):
    """将拆解方案保存为 Markdown 文件，方便人阅读和后续 AI 参考。"""
    from datetime import datetime

    lines = [
        f"# Pod Plan: {pod_name}",
        "",
        f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 需求描述",
        "",
        desc,
        "",
        "## 组件拆解",
        "",
        f"共 {len(components)} 个组件：",
        "",
    ]

    for i, comp in enumerate(components, 1):
        deps = comp.get("depends_on", [])
        dep_str = f" ← depends: {', '.join(deps)}" if deps else ""
        lines.append(f"### {i}. {comp['name']} ({comp['category']}){dep_str}")
        lines.append("")
        lines.append(comp.get("description", ""))
        lines.append("")

    if pipelines:
        lines.append("## Pipeline 规划")
        lines.append("")
        for i, pipe in enumerate(pipelines, 1):
            lines.append(f"### {i}. {pipe.get('name', '')}")
            lines.append(f"> {pipe.get('instruction', '')}")
            lines.append("")

    if config_additions:
        lines.append("## 建议新增配置")
        lines.append("")
        lines.append("```toml")
        for section, keys in config_additions.items():
            lines.append(f"[{section}]")
            for key, raw_value in keys.items():
                if isinstance(raw_value, dict):
                    val = raw_value.get("value", "")
                    comment = raw_value.get("comment", "")
                    lines.append(f"{key} = {val}  # {comment}")
                else:
                    lines.append(f"{key} = {raw_value}")
        lines.append("```")
        lines.append("")

    filename = f"{pod_name}_plan.md"
    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"📋 [方案已保存] {filename}\n")


def _generate_pod_entry(desc: str, generated: list[str], pipe_names: list[str]) -> tuple[str, list[str]] | None:
    """Pod 自己生成入口 prompt，包含本次生成的组件和管线的完整上下文。"""
    from ai_pod_cli.client import call_llm
    from ai_pod_cli.security import validate_code

    routes_map = _load_routes_map()

    # 构建本次生成的上下文
    comp_summary = "\n".join(f"   - {name}" for name in generated) if generated else "   (无)"
    pipe_summary = "\n".join(f"   - {name}" for name in pipe_names) if pipe_names else "   (无)"
    route_lines = "\n".join(f"   - {name}: {desc}" for name, desc in routes_map.items()) if routes_map else "   (无)"

    system_prompt = f"""
    你是一个资深的 Python 入口文件生成器。以下上下文来自 aipod pod 命令：

    用户需求: {desc}

    本次 Pod 生成的组件：
    {comp_summary}

    本次 Pod 规划的管线：
    {pipe_summary}

    routes.toml 中已注册的路由（必须使用这些精确名称调用 runner.run()）：
    {route_lines}

    你的任务：根据上述上下文，生成一个可直接运行的 Python 入口文件。

    【你必须自主决策】：
    1. 判断项目类型（CLI 工具、FastAPI Web 服务、定时任务等）
    2. 决定入口文件名
    3. 生成完整代码

    【代码规范】：
    - 入口通过容器获取一切：
      from ai_pod_cli.config import load_config
      from ai_pod_cli.container import build_container
      config = load_config()
      container = build_container(config)
    - PipelineRunner 通过容器获取：
      runner = container.get(PipelineRunner)
      runner.route_names()  — 列出所有路由
      runner.run("路由名", {{"key": "value"}})  — 执行管线
    - 禁止手动 new PipelineRunner()
    - 入口不 import 任何 modules/ 下的底层 Bean
    - 建议用 runner.route_names() 动态发现路由

    返回标准 JSON（不要 Markdown 块标记）：
    {{
        "project_type": "项目类型",
        "entry_file": "入口文件名",
        "code": "完整 Python 源代码",
        "extra_deps": ["额外 pip 包名"]
    }}
    """

    try:
        result = call_llm(system_prompt, f"生成入口: {desc}", json_mode=True, temperature=0.2)
    except Exception as e:
        print(f"   ❌ 入口生成失败: {e}")
        return None

    entry_file = result.get("entry_file", "main.py")
    generated_code = result.get("code", "")
    extra_deps = result.get("extra_deps", [])

    if not generated_code:
        print("   ❌ AI 未返回入口代码")
        return None

    violations = validate_code(generated_code, allow_file_io=True)
    if violations:
        print(f"   🛡️  入口安全检查 ({len(violations)} 处违规)，跳过")
        return None

    if os.path.exists(entry_file):
        print(f"   ⚠️  {entry_file} 已存在，跳过写入。")
        return entry_file, extra_deps

    with open(entry_file, "w", encoding="utf-8") as f:
        f.write(generated_code)
    print(f"   ✍️  [入口生成成功] {entry_file}")

    return entry_file, extra_deps


def _load_routes_map() -> dict[str, str]:
    """读取 routes.toml，返回 {route_name: description} 映射。"""
    from ai_pod_cli.config import ROUTES_TOML

    routes_map = {}
    if os.path.exists(ROUTES_TOML):
        try:
            import tomlkit
            with open(ROUTES_TOML, "r", encoding="utf-8") as f:
                doc = tomlkit.load(f)
            for name, value in doc.items():
                if isinstance(value, dict):
                    desc = value.get("description", "")
                    routes_map[name] = str(desc) if desc else ""
                else:
                    routes_map[name] = ""
        except Exception:
            pass
    return routes_map


def handle_pod(args):
    """【pod 命令】AI 将一个大需求拆解为一组组件，逐个生成并加入 Bean Pool"""

    # 读取需求描述：优先 --file，其次 desc
    desc = ""
    if args.file:
        if not os.path.exists(args.file):
            print(f"❌ 文件不存在: {args.file}")
            return
        with open(args.file, "r", encoding="utf-8") as f:
            desc = f.read().strip()
        print(f"🧩 [pod] 从文件读取需求: {args.file}")
    elif args.desc:
        desc = args.desc
    else:
        print("❌ 请提供需求描述或 --file 文件路径。")
        return

    print(f"📝 [需求] {desc[:200]}{'...' if len(desc) > 200 else ''}")

    if not os.environ.get("OPENAI_API_KEY"):
        print("❌ OPENAI_API_KEY 未配置。请先设置：")
        print("   aipod config set OPENAI_API_KEY sk-your-key")
        sys.exit(1)

    # 确保 requirements.txt 存在（空依赖触发 header 写入）
    append_deps_to_requirements([])

    config = load_config()
    existing_beans = json.dumps(config["beans"], indent=2, ensure_ascii=False)
    toml_keys = load_config_toml_safe()

    system_prompt = f"""
    你是一个资深的软件架构师。当前系统是一个基于 Python `injector` 框架的 IoC/DI 容器低代码平台。

    目前系统中已有的组件池（Bean Pool）：
    {existing_beans}

    当前 config.toml 中的配置项（敏感值已隐藏）：
    {toml_keys}

    你的任务是：将人类描述的一个大需求，拆解成多个可独立生成的组件，并规划配套的 pipeline。

    【拆解规则】：
    1. 每个组件必须有明确的单一职责。
    2. 分类只有两种：service（业务组件，有 execute 方法）和 provider（基础设施提供者，如 DB、缓存、HTTP 客户端等）。
    3. 依赖只能从已有的 Bean Pool 中选择，或者选择本次拆解中排在它前面的组件。
    4. 不要重复已有组件的功能。如果 Bean Pool 里已有合适的组件，直接引用它。
    5. 每个组件的 description 要足够详细，让后续 AI 生成时能写出完整代码。
    6. 组件数量控制在 2~6 个，不要过度拆解。
    7. 如果新组件需要 config.toml 中的新配置项，在 config_additions 中说明。

    【Pipeline 规划规则】：
    1. 为每个 service 类型的组件规划至少一条 pipeline。
    2. pipeline 的 instruction 应该是具体的业务指令（如 "生成一个用户认证组件"）。
    3. pipeline 的 name 应该是简短的英文标识（如 create_auth）。

    请严格以标准 JSON 格式返回（不要包含 Markdown 块标记）：
    {{
        "pod_name": "这组组件的简短名称",
        "components": [
            {{
                "name": "组件类名（PascalCase）",
                "category": "service 或 provider",
                "description": "详细的组件描述，包括方法签名、输入输出、依赖说明",
                "depends_on": ["依赖的组件ID_1", "依赖的组件ID_2"]
            }}
        ],
        "pipelines": [
            {{
                "name": "pipeline 英文标识",
                "instruction": "自然语言业务指令（AI 据此规划执行链）"
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
        plan = call_llm(system_prompt, f"需求: {desc}", json_mode=True, temperature=0.2)
    except Exception as e:
        print(f"❌ AI 拆解失败: {e}")
        return

    pod_name = plan.get("pod_name", "unnamed_pod")
    components = plan.get("components", [])
    pipelines = plan.get("pipelines", [])
    config_additions = plan.get("config_additions", {})

    if not components:
        print("❌ AI 未返回任何组件。")
        return

    # 打印拆解方案
    print(f"\n📋 [拆解方案] {pod_name}")
    print(f"   组件: {len(components)} 个  |  Pipeline: {len(pipelines)} 条\n")

    print(f"   📦 组件:")
    for i, comp in enumerate(components, 1):
        deps = comp.get("depends_on", [])
        dep_str = f" ← depends: {', '.join(deps)}" if deps else ""
        print(f"      {i}. {comp['name']} ({comp['category']}){dep_str}")
        print(f"         {comp.get('description', '')[:80]}")
    print()

    if pipelines:
        print(f"   🔗 Pipeline:")
        for i, pipe in enumerate(pipelines, 1):
            print(f"      {i}. {pipe['name']}")
            print(f"         → {pipe['instruction']}")
        print()

    if config_additions:
        print(f"⚙️  建议新增配置项:")
        for section, keys in config_additions.items():
            for key, raw_value in keys.items():
                if isinstance(raw_value, dict):
                    val = raw_value.get("value", "")
                    comment = raw_value.get("comment", "")
                    print(f"   [{section}] {key} = {val}  # {comment}")
                else:
                    print(f"   [{section}] {key} = {raw_value}")
        print()

    # 用户确认
    if not args.yes:
        try:
            answer = input(f"确认生成 {len(components)} 个组件 + {len(pipelines)} 条 pipeline？[Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            return
        if answer and answer not in ("y", "yes"):
            print("已取消。")
            return

    # 保存拆解方案到本地
    _save_pod_plan(pod_name, desc, components, pipelines, config_additions)

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
        toml_keys = load_config_toml_safe()

        common = f"""
        当前系统组件池：
        {beans_context}

        当前 config.toml 中的配置项（敏感值已隐藏）：
        {toml_keys}

        请生成: {name} ({category}) — {description}

        【通用规范】：
        - 必须 from injector import inject，构造函数加 @inject
        - 类名必须与 {name} 完全一致
        - 构造函数只放组件类型依赖，不放 str/int/bool
        - 配置通过 ConfigStore 读取：from ai_pod_cli.config_store import ConfigStore
        - ConfigStore 必须从 ai_pod_cli.config_store 导入，禁止从 modules 导入！
        - 禁止创建纯 ConfigStore 包装类
        - 无依赖时：@inject def __init__(self): pass
        - 第三方包必须在 extra_deps 中列出
        - import 路径：modules.providers.xxx → from modules.providers.xxx / modules.services.xxx → from modules.services.xxx
        """

        if category == "provider":
            create_prompt = common + f"""
        【provider 规范】：
        - 不需要 execute 方法，只提供业务方法（每个方法有明确入参和返回值）
        - 不涉及 PipelineContext

        返回 JSON：
        {{
            "dependencies": ["依赖ID"],
            "methods": {{"method_name": {{"inputs": {{...}}, "outputs": "返回值 — 说明"}}}},
            "code": "完整 Python 源代码",
            "extra_deps": ["包名"]
        }}
        """
        else:
            create_prompt = common + f"""
        【service 规范】：
        - 必须 from ai_pod_cli.context import PipelineContext
        - 必须实现 execute(self, ctx: PipelineContext) -> dict
        - 从 ctx.params / ctx.get() 读输入，ctx.set() 写输出

        返回 JSON：
        {{
            "dependencies": ["依赖ID"],
            "inputs": {{"参数名": "类型 — 说明"}},
            "outputs": {{"输出键": "类型 — 说明"}},
            "ai_spec": "对 execute 方法的技术规格描述",
            "code": "完整 Python 源代码",
            "extra_deps": ["包名"]
        }}
        """

        try:
            result = call_llm(create_prompt, f"生成组件: {name}", json_mode=True, temperature=0.1)

            code = result.get("code", "")
            dependencies = result.get("dependencies", [])
            inputs = result.get("inputs", {})
            outputs = result.get("outputs", {})
            methods = result.get("methods", {})
            ai_spec = result.get("ai_spec", "")
            extra_deps = result.get("extra_deps", [])

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

            # 写入文件（按分类写入不同子目录）
            module_dir, class_path = get_module_path(category, name)
            os.makedirs(module_dir, exist_ok=True)
            file_path = os.path.join(module_dir, f"{name.lower()}.py")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(code)

            # 将第三方依赖写入根 requirements.txt
            if extra_deps:
                append_deps_to_requirements(extra_deps)
                print(f"   📦 额外依赖: {', '.join(extra_deps)}")

            # 注册到 bean pool
            new_bean = {
                "id": name,
                "category": category,
                "type": "ai_created",
                "class_path": class_path,
                "dependencies": dependencies,
                "inputs": inputs,
                "outputs": outputs,
                "methods": methods,
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
    print(f"   ✅ 组件成功: {len(generated)} 个 — {', '.join(generated) if generated else '(无)'}")
    if failed:
        print(f"   ❌ 组件失败: {len(failed)} 个 — {', '.join(failed)}")

    # 生成 pipelines
    generated_pipelines = []
    failed_pipelines = []
    if pipelines and generated:
        print(f"\n🔗 [生成 Pipeline] {len(pipelines)} 条")
        from ai_pod_cli.commands.compose import handle_compose

        for i, pipe in enumerate(pipelines, 1):
            pipe_name = pipe.get("name", f"pipeline_{i}")
            instruction = pipe.get("instruction", "")
            print(f"\n   [{i}/{len(pipelines)}] {pipe_name}: {instruction}")

            # 构造 compose 的 args
            class ComposeArgs:
                pass
            compose_args = ComposeArgs()
            compose_args.cmd = instruction
            compose_args.name = pipe_name
            compose_args.list = False

            try:
                handle_compose(compose_args)
                generated_pipelines.append(pipe_name)
            except Exception as e:
                print(f"   ❌ Pipeline 生成失败: {e}")
                failed_pipelines.append(pipe_name)

        print(f"\n   🔗 Pipeline 成功: {len(generated_pipelines)} 条 — {', '.join(generated_pipelines) if generated_pipelines else '(无)'}")
        if failed_pipelines:
            print(f"   ❌ Pipeline 失败: {len(failed_pipelines)} 条 — {', '.join(failed_pipelines)}")

    # 生成入口文件
    entry_file = None
    if generated:
        print(f"\n🚀 [生成入口文件]")
        entry_info = _generate_pod_entry(desc, generated, generated_pipelines)
        if entry_info:
            entry_file, extra_deps = entry_info
            if extra_deps:
                append_deps_to_requirements(extra_deps)
                print(f"   📦 额外依赖: {', '.join(extra_deps)}")

    # 输出汇总
    print(f"\n{'='*50}")
    print(f"🧩 [Pod 生成完毕] {pod_name}")
    print(f"   ✅ 组件: {len(generated)} 个 — {', '.join(generated) if generated else '(无)'}")
    if failed:
        print(f"   ❌ 组件失败: {len(failed)} 个 — {', '.join(failed)}")
    if pipelines:
        print(f"   🔗 Pipeline: {len(generated_pipelines)} 条 — {', '.join(generated_pipelines) if generated_pipelines else '(无)'}")
        if failed_pipelines:
            print(f"   ❌ Pipeline 失败: {len(failed_pipelines)} 条 — {', '.join(failed_pipelines)}")
    if entry_file:
        print(f"   🚀 入口: {entry_file}")

    if generated:
        if entry_file:
            print(f"\n   运行: python {entry_file}")
        else:
            print(f"\n   可以手动生成入口: aipod init \"{desc[:50]}\"")
    print(f"{'='*50}")
