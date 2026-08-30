"""`pod` command — AI decomposes a requirement into a set of components."""

import hashlib
import json
import os
import sys
from pathlib import Path

from ai_pod_cli.client import call_llm
from ai_pod_cli.config import load_beans, load_beans_summary, save_config, MODULES_DIR, load_config_toml_safe, append_deps_to_requirements, get_module_path, extract_model_fields, extract_sql_resources
from ai_pod_cli.validation import (
    repair_feedback, request_repair, validate_component_contract, validate_entry_contract,
    validate_pipeline_contract,
)
from ai_pod_cli.repair import (
    apply_code_patches, apply_file_patches, can_patch_code, classify_failures,
    file_patch_prompt, patch_prompt,
)
from ai_pod_cli.decision import reduce_decision_fragments, reduce_evidence


DECISION_PLAN_FILE = Path("aipod_plan.json")
STAGE_NAMES = ("models", "providers", "services", "pipelines", "interfaces")
STAGE_BUILD_TOOLS = (
    "generate_models", "generate_providers", "generate_services",
    "compose_pipelines", "generate_interfaces",
)


def _load_decision_plan(desc: str, explicit_stage: int | None = None) -> dict:
    """Load resumable design decisions, or start a new objective state."""
    state = None
    if DECISION_PLAN_FILE.exists():
        try:
            candidate = json.loads(DECISION_PLAN_FILE.read_text(encoding="utf-8"))
            if candidate.get("objective") == desc:
                state = candidate
        except (OSError, json.JSONDecodeError):
            state = None
    if state is None:
        state = {
            "version": 3,
            "objective": desc,
            "current_stage": STAGE_NAMES[explicit_stage or 0],
            "stages": {
                name: {"status": "pending", "plan": None}
                for name in STAGE_NAMES
            },
            "agent": {
                "status": "idle", "step": 0, "history": [],
                "verification": {"status": "pending", "attempts": 0, "repairs": 0},
            },
        }
        if explicit_stage is not None:
            for name in STAGE_NAMES[:explicit_stage]:
                state["stages"][name]["status"] = "complete"
    for name in STAGE_NAMES:
        state.setdefault("stages", {}).setdefault(name, {"status": "pending", "plan": None})
    state.setdefault("agent", {"status": "idle", "step": 0, "history": []})
    state["agent"].setdefault("status", "idle")
    state["agent"].setdefault("step", 0)
    state["agent"].setdefault("history", [])
    state["agent"].setdefault(
        "verification", {"status": "pending", "attempts": 0, "repairs": 0},
    )
    state["agent"]["verification"].setdefault("status", "pending")
    state["agent"]["verification"].setdefault("attempts", 0)
    state["agent"]["verification"].setdefault("repairs", 0)
    state["version"] = max(3, int(state.get("version", 1)))
    return state


def _save_decision_plan(state: dict) -> None:
    """Atomically persist compact decisions; never persist hidden reasoning text."""
    temporary = DECISION_PLAN_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, DECISION_PLAN_FILE)


def _resume_stage(state: dict) -> int | None:
    for index, name in enumerate(STAGE_NAMES):
        if state["stages"][name].get("status") != "complete":
            return index
    return None


def _set_stage_status(state: dict, stage: int, status: str) -> None:
    name = STAGE_NAMES[stage]
    state["current_stage"] = name
    state["stages"][name]["status"] = status
    _save_decision_plan(state)


def _save_pod_plan(
    pod_name: str, desc: str, components: list, pipelines: list,
    config_additions: dict, interfaces: list | None = None,
):
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

    if interfaces:
        lines.append("## Interface 规划")
        lines.append("")
        for i, interface in enumerate(interfaces, 1):
            lines.append(f"### {i}. {interface.get('name', '')} ({interface.get('kind', '')})")
            lines.append(f"> {interface.get('instruction', '')}")
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


def _generate_pod_entry(
    desc: str, generated: list[str], pipe_names: list[str], progress_callback=None,
    auto_repair: bool = False, interface: dict | None = None,
) -> tuple[str, list[str]] | None:
    """Pod 自己生成入口 prompt，包含本次生成的组件和管线的完整上下文。"""
    from ai_pod_cli.client import call_llm
    routes_map = _load_routes_map()
    interface = interface or {}
    interface_name = str(interface.get("name", "application"))
    interface_kind = str(interface.get("kind", "cli"))
    interface_instruction = str(interface.get("instruction", desc))
    required_routes = [
        name for name in routes_map
        if f"`{name}`" in interface_instruction
    ]

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

    当前只生成 Interface：{interface_name}（{interface_kind}）
    Interface 要求：{interface_instruction}

    你的任务：根据上述上下文，生成这个 Interface 的一个可直接运行的 Python 入口文件。

    【你必须自主决策】：
    1. 判断项目类型（CLI 工具、FastAPI Web 服务、定时任务等）
    2. 决定入口文件名
    3. 生成完整代码

    【代码规范】：
    - 入口通过容器获取一切：
      from ai_pod_cli.config import load_beans
      from ai_pod_cli.container import build_container
      beans = load_beans()
      container = build_container(beans)
    - PipelineRunner 通过容器获取（import: from ai_pod_cli.runner import PipelineRunner）：
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

    max_attempts = 3
    feedback = ""
    for attempt in range(1, max_attempts + 1):
        try:
            result = call_llm(
                system_prompt, f"生成入口: {desc}{feedback}", json_mode=True, temperature=0.2,
                progress_callback=progress_callback,
                progress_label=f"Generating {interface_kind} interface: {interface_name}",
            )
        except Exception as e:
            if attempt < max_attempts:
                feedback = repair_feedback([f"入口生成调用失败：{e}"])
                continue
            print(f"   ❌ 入口生成失败: {e}")
            return None

        entry_file = result.get("entry_file", "main.py")
        generated_code = result.get("code", "")
        extra_deps = result.get("extra_deps", [])
        violations = (
            validate_entry_contract(generated_code, list(routes_map))
            if generated_code else ["AI 未返回入口代码"]
        )
        for route_name in required_routes:
            if not any(
                quoted in generated_code
                for quoted in (f'"{route_name}"', f"'{route_name}'")
            ):
                violations.append(
                    f"Interface 规划要求调用路由 '{route_name}'，但入口代码没有引用它"
                )
        if not violations:
            break
        if not request_repair(
            violations, attempt, max_attempts,
            interactive=not auto_repair, auto_repair=auto_repair,
        ):
            return None
        feedback = repair_feedback(violations) + (
            "\n只修复当前入口文件。此前通过的组件和 Pipeline 已冻结，禁止建议修改它们。"
        )
    else:
        return None

    if os.path.exists(entry_file):
        with open(entry_file, "r", encoding="utf-8") as existing:
            existing_code = existing.read()
        existing_violations = validate_entry_contract(existing_code, list(routes_map))
        for route_name in required_routes:
            if not any(
                quoted in existing_code
                for quoted in (f'"{route_name}"', f"'{route_name}'")
            ):
                existing_violations.append(
                    f"现有入口未实现规划要求的路由 '{route_name}'"
                )
        if existing_violations:
            print(f"   ❌ {entry_file} 已存在，但与当前 Interface 规划不兼容；未覆盖文件。")
            for violation in existing_violations:
                print(f"      - {violation}")
            return None
        print(f"   ♻️  {entry_file} 已存在且符合当前 Interface 规划，复用。")
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


def _execute_pod_build_tool(args):
    """Execute exactly one stage selected by the Pod Agent."""

    progress_callback = getattr(args, "progress_callback", None)

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
        from ai_pod_cli.commands.env import print_missing_model_config
        print_missing_model_config()
        sys.exit(1)

    # 确保 requirements.txt 存在（空依赖触发 header 写入）
    append_deps_to_requirements([])

    explicit_stage = int(args._pod_stage) if hasattr(args, "_pod_stage") else None
    decision_state = _load_decision_plan(desc, explicit_stage)
    if explicit_stage is None:
        resumed_stage = _resume_stage(decision_state)
        if resumed_stage is None:
            print("✅ 当前需求的 Canonical Plan 已全部完成，无需重新规划。")
            return
        stage = resumed_stage
        if stage > 0 or decision_state["stages"][STAGE_NAMES[stage]].get("plan"):
            print(f"♻️  [恢复计划] 从阶段 {stage + 1}/5 · {STAGE_NAMES[stage]} 继续。")
    else:
        stage = explicit_stage

    beans = load_beans()
    existing_beans = load_beans_summary()
    toml_keys = load_config_toml_safe()
    stage_names = STAGE_NAMES
    stage_name = stage_names[min(stage, len(stage_names) - 1)]
    stage_instructions = {
        0: "只规划共享 data model。components 只能包含 model；pipelines 必须为空。每个 Model 项只对应一个 Python 类。明确区分运行时 Value Model（不持久化）和 Persistent Model（需要 ModelRepository/数据库）；禁止把 Vector2、Transform、事件、碰撞结果等瞬时值规划成数据库表。",
        1: "只规划用户明确要求连接的外部基础设施 provider。不得因为需求出现 Web/CLI/Desktop/Worker 就创建 HTTP Server、调度器、Redis、消息队列、邮件或通知 Provider；这些属于后续 Interface，除非用户明确指定真实外部系统。没有明确外部系统时 components 必须为空并复用 ModelRepository。数据库能力只能复用内置 ModelRepository，禁止 DatabaseProvider、SchemaProvider 或 SQL Provider。",
        2: "只规划业务 service。数据库持久化必须依赖内置 ModelRepository，禁止 DatabaseProvider、SchemaProvider 和原始 SQL。components 只能包含 service；pipelines 必须为空。每个 Service 只对应一个 execute(ctx)。复杂输入输出必须引用已生成 Model 的完整类路径。Model 只能普通 import，不能写入 depends_on。",
        3: "只规划 Pipeline。components 必须为空；reuse_components 列出 Pipeline 使用的现有 Service；根据已经冻结的 Service inputs/outputs 规划 pipelines，禁止假设不存在的组件。",
        4: "只规划用户入口 Interface。components 和 pipelines 必须为空。读取 routes.toml 中已经冻结的 Pipeline，为需求规划必要的 CLI、Web、Desktop、Worker 或消息消费者入口；每个 Interface 必须明确使用哪些 route。",
    }[min(stage, 4)]

    system_prompt = f"""
    你是一个资深的软件架构师。当前系统是一个基于 Python `injector` 框架的 IoC/DI 容器低代码平台。

    目前系统中已有的组件池（Bean Pool）：
    {existing_beans}

    当前 config.toml 中的配置项（敏感值已隐藏）：
    {toml_keys}

    当前处于阶段 {stage + 1}/5：{stage_name}。
    {stage_instructions}

    你的任务是只规划当前阶段。后续阶段会在当前产物生成并验证之后重新调用规划器，禁止提前规划。

    【拆解规则】：
    1. 每个组件必须有明确的单一职责。
    2. 分类有三种：model（共享数据结构，类似 Java DTO）、service（业务组件，有 execute 方法）和 provider（基础设施提供者）。
       多个组件共享复杂对象时，先规划 model，Service 契约通过 {{"model":"完整类路径"}} 引用它。
       components 数组必须按 model → provider → service 排序，确保引用目标先生成。
    3. 依赖只能从已有的 Bean Pool 中选择，或者选择本次拆解中排在它前面的组件。
    4. 不要重复已有组件的功能。如果 Bean Pool 里已有合适的组件，直接引用它。
    5. 每个组件的 description 要足够详细，让后续 AI 生成时能写出完整代码。
    6. 当前阶段组件数量控制在 0~8 个；一个数组项必须严格对应一个 Python 类。
    7. 如果新组件需要 config.toml 中的新配置项，在 config_additions 中说明。

    【Pipeline 规划规则（仅 pipelines 阶段适用）】：
    1. 为每个 service 类型的组件规划至少一条 pipeline。
    2. pipeline 的 instruction 应该是具体的业务指令（如 "生成一个用户认证组件"）。
    3. pipeline 的 name 应该是简短的英文标识（如 create_auth）。

    请严格以标准 JSON 格式返回（不要包含 Markdown 块标记）：
    {{
        "pod_name": "这组组件的简短名称",
        "reuse_components": ["直接复用的已有 Bean ID"],
        "components": [
            {{
                "name": "组件类名（PascalCase）",
                "category": "model、service 或 provider",
                "description": "详细的组件描述，包括方法签名、输入输出、依赖说明",
                "depends_on": ["需要注入的组件ID_1", "组件ID_2"],
                "models": ["作为数据引用的 Model Bean ID，不注入"],
                "requires": ["执行前必须存在的语义字段"],
                "provides": ["执行后产生的语义字段"],
                "invariants": ["必须始终成立且可验证的架构约束"]
            }}
        ],
        "pipelines": [
            {{
                "name": "pipeline 英文标识",
                "instruction": "自然语言业务指令（AI 据此规划执行链）"
            }}
        ],
        "interfaces": [
            {{
                "name": "入口英文名称",
                "kind": "cli|web|desktop|worker|consumer",
                "instruction": "入口行为、使用的精确 Pipeline route 和运行方式"
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

    stage_record = decision_state["stages"][stage_name]
    if isinstance(stage_record.get("plan"), dict):
        plan = stage_record["plan"]
        print(f"📌 [复用冻结规划] {stage_name} 阶段不再调用规划器。")
    else:
        try:
            plan = call_llm(
                system_prompt,
                f"需求: {desc}",
                json_mode=True,
                temperature=0.2,
                max_tokens=8192,
                progress_callback=progress_callback,
                progress_label=f"Planning stage {stage + 1}/5: {stage_name}",
            )
        except Exception as e:
            print(f"❌ AI 拆解失败: {e}")
            return
        stage_record["plan"] = plan
        stage_record["status"] = "in_progress"
        decision_state["current_stage"] = stage_name
        _save_decision_plan(decision_state)

    reduction = reduce_decision_fragments(plan, beans.get("beans", []), stage_name)
    stage_record["reduction"] = reduction
    _save_decision_plan(decision_state)
    if reduction["warnings"]:
        print(f"⚠️  [Plan Reduce] {len(reduction['warnings'])} 个架构警告：")
        for warning in reduction["warnings"]:
            print(f"   ⚠️  {warning['code']}: {warning['message']} ({warning.get('component', '')})")
    if reduction["conflicts"]:
        print(f"❌ [Plan Reduce] {len(reduction['conflicts'])} 个决策冲突，代码生成已停止：")
        for conflict in reduction["conflicts"]:
            print(f"   ❌ {conflict['code']}: {conflict['message']}")
        stage_record["status"] = "conflict"
        _save_decision_plan(decision_state)
        raise SystemExit(1)

    pod_name = plan.get("pod_name", "unnamed_pod")
    components = plan.get("components", [])
    requested_reuse = [str(item) for item in plan.get("reuse_components", [])]
    pipelines = plan.get("pipelines", [])
    interfaces = plan.get("interfaces", [])
    config_additions = plan.get("config_additions", {})

    if stage <= 2:
        expected_category = stage_names[stage][:-1] if stage != 2 else "service"
        components = [
            item for item in components
            if item.get("category") == expected_category
        ]
        pipelines, interfaces = [], []
    elif stage == 3:
        components, interfaces = [], []
    else:
        components, pipelines = [], []

    existing_by_id = {
        bean.get("id"): bean for bean in beans.get("beans", [])
        if bean.get("status") != "invalid"
    }
    if stage == 2:
        model_ids = {
            bean_id for bean_id, bean in existing_by_id.items()
            if bean.get("category") == "model"
        }
        for component in components:
            original_dependencies = component.get("depends_on", [])
            component["depends_on"] = [
                dependency for dependency in original_dependencies
                if dependency not in model_ids
            ]
            removed_models = [
                dependency for dependency in original_dependencies
                if dependency in model_ids
            ]
            if removed_models:
                component["description"] = (
                    component.get("description", "")
                    + " Frozen Models " + ", ".join(removed_models)
                    + " must be imported from their registered class_path, never injected; "
                    "their frozen field annotations override any conflicting prose above."
                )
            dependency_set = set(component["depends_on"])
            lifecycle_providers = []
            for bean_id, bean in existing_by_id.items():
                if bean.get("category") != "provider" or "initialize" not in (bean.get("methods") or {}):
                    continue
                required = set(bean.get("dependencies") or [])
                if required and required.issubset(dependency_set):
                    lifecycle_providers.append(bean_id)
            for provider_id in lifecycle_providers:
                if provider_id not in component["depends_on"]:
                    component["depends_on"].append(provider_id)
            if lifecycle_providers:
                component["description"] = (
                    component.get("description", "")
                    + " Inject lifecycle provider(s) " + ", ".join(lifecycle_providers)
                    + " and call initialize() before using the associated infrastructure."
                )
    reused = [item for item in requested_reuse if item in existing_by_id]
    create_components = []
    for component in components:
        name = component.get("name")
        if name in existing_by_id:
            if name not in reused:
                reused.append(name)
        else:
            create_components.append(component)
    components = create_components

    # An empty Provider plan is a valid and desirable result when the requirement
    # does not name any external infrastructure.  The built-in providers remain
    # available and the five-stage loop must continue to Services.
    if stage != 1 and not components and not reused and not pipelines and not interfaces:
        print("❌ AI 未返回可创建或可复用的组件。")
        return

    # 打印拆解方案
    print(f"\n📋 [阶段规划 {stage + 1}/5 · {stage_name}] {pod_name}")
    print(
        f"   组件: {len(components)} 个  |  Pipeline: {len(pipelines)} 条"
        f"  |  Interface: {len(interfaces)} 个\n"
    )

    print(f"   📦 组件:")
    for name in reused:
        print(f"      ♻️  {name} (reuse)")
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

    if interfaces:
        print("   🖥️  Interfaces:")
        for i, interface in enumerate(interfaces, 1):
            print(f"      {i}. {interface.get('name', '')} ({interface.get('kind', '')})")
            print(f"         → {interface.get('instruction', '')}")
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
            answer = input(
                f"确认当前阶段：{len(components)} 个组件 + {len(pipelines)} 条 Pipeline"
                f" + {len(interfaces)} 个 Interface？[Y/n] "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            return
        if answer and answer not in ("y", "yes"):
            print("已取消。")
            return

    # 保存拆解方案到本地
    _save_pod_plan(
        pod_name, desc, components, pipelines, config_additions, interfaces,
    )

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
    reduced_fragments = {
        item.get("id"): item for item in reduction.get("fragments", [])
        if item.get("id")
    }

    for i, comp in enumerate(components, 1):
        name = comp["name"]
        category = comp["category"]
        description = comp["description"]

        print(f"🤖 [{i}/{len(components)}] 生成 {name} ({category})...")

        # 重新加载配置（因为每轮生成后 bean pool 会更新）
        beans = load_beans()
        beans_context = load_beans_summary()
        toml_keys = load_config_toml_safe()
        referenced_model_ids = reduced_fragments.get(name, {}).get("models", [])
        referenced_models = [
            bean for bean in beans.get("beans", [])
            if bean.get("id") in referenced_model_ids and bean.get("category") == "model"
        ]

        common = f"""
        当前系统组件池：
        {beans_context}

        【字段复用规则】：
        - 组件池中的所有组件都已通过上一轮验证并被冻结，只能读取其契约，禁止修改或重新设计它们。
        - 本轮唯一允许修复的对象是当前组件 {name}。
        - Bean Pool 中已有 Service outputs 构成当前项目的数据词汇表。
        - 新组件 inputs 与已有 output 语义相同时，必须原样复用字段名和类型。
        - 禁止创建 oxygen/oxygen_level、battery/battery_level 这类同义字段。
        - 代码真实读取/写入字段必须与返回 JSON 的 inputs/outputs 完全一致。
        - Contract 保持简单：str/int/float/bool/dict/list 可直接使用简写；已有业务 Model 则优先引用完整 class_path。
        - inputs/outputs 必须描述代码真实边界，但禁止为了 Contract 重复手写一套业务 Model 字段。
        - 必需字段缺失时必须抛出异常，禁止用空默认值或 continue 静默丢弃数据。

        当前 config.toml 中的配置项（敏感值已隐藏）：
        {toml_keys}

        当前组件由 Plan Reducer 绑定的 Models（数据引用，不注入）：
        {json.dumps(referenced_models, ensure_ascii=False, indent=2)}
        - 如果代码读取或输出上述 Model 对象，对应 Contract 必须使用
          {{"model": "精确 class_path"}}，或对列表元素使用包含该 class_path 的类型。

        请生成: {name} ({category}) — {description}

        【通用规范】：
        - 必须 from injector import inject，构造函数加 @inject
        - 类名必须与 {name} 完全一致
        - 构造函数只放组件类型依赖，不放 str/int/bool
        - **依赖的方法必须来自上方组件池中的方法签名！禁止调用不存在的方法！**
        - 配置通过 ConfigStore 读取：from ai_pod_cli.config_store import ConfigStore
        - ConfigStore 必须从 ai_pod_cli.config_store 导入，禁止从 modules 导入！
        - ModelRepository 必须使用 `from ai_pod_cli.repository import ModelRepository`，禁止从 modules 导入！
        - 禁止创建纯 ConfigStore 包装类
        - 无依赖时：@inject def __init__(self): pass
        - 第三方包必须在 extra_deps 中列出
        - **禁止 `from modules import X`！每个组件必须从自己的子目录单独导入**
        - 每个组件的文件名见上方组件池，必须**原样使用**，不要自己编文件名！
        - 必须逐字复制上方组件池中的 class_path 来 import，禁止根据类别猜测路径。
        - Model 固定从 modules.models.<文件名> 导入；禁止从 modules.services 或 modules.providers 导入 Model。
        - Provider 从 modules.providers.<文件名> 导入，Service 从 modules.services.<文件名> 导入。
        - 如果 Provider 暴露“冻结资源”，SQL/消息主题/外部资源名称必须逐字使用其中的表名和字段，禁止猜测复数形式或不存在的列。
        """

        if category == "model":
            create_prompt = common + f"""
        【model 规范】：
        - 必须 `from ai_pod_cli import Model`；只有持久化实体需要 `from sqlmodel import Field`
        - 运行时值对象使用 `class {name}(Model)`，不建表、不定义数据库主键
        - 需要 ModelRepository 持久化的实体使用 `class {name}(Model, table=True)`，并定义 `id: int | None = Field(default=None, primary_key=True)` 主键
        - 根据组件描述选择一种，禁止把 Vector2、Transform、InputState、CollisionResult、事件等瞬时数据变成数据库表
        - 所有字段必须有类型注解
        - 只定义数据，不使用 injector、PipelineContext、execute 或业务逻辑

        返回 JSON：
        {{
            "dependencies": [], "inputs": {{}}, "outputs": {{}},
            "ai_spec": "模型字段及语义说明",
            "code": "完整 Python 源代码", "extra_deps": []
        }}
        """
        elif category == "provider":
            create_prompt = common + f"""
        【provider 规范】：
        - 不需要 execute 方法，只提供业务方法（每个方法有明确入参和返回值）
        - 不涉及 PipelineContext
        - 优先使用 Python 标准库；除非需求明确不可替代，否则 extra_deps 必须为空
        - UTC 时间直接使用 datetime.timezone.utc；不要假设 Windows 已安装 IANA tzdata
        - import pygame 时，extra_deps 必须填写发行包名 pygame-ce，而不是 pygame

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
        - 持久化只能注入 ModelRepository，并调用 save/get/list/find/delete；严禁写 SQL，严禁使用 CREATE/INSERT/SELECT/UPDATE/DELETE 字符串
        - ModelRepository.find 同时支持 find(Model, field=value) 和 find(Model, {{"field": value}})
        - outputs 声明为 dict/list 时，返回和 ctx.set 的值必须可 JSON 序列化；SQLModel 实例调用 model_dump()，禁止把实例伪装成 dict

        返回 JSON：
        {{
            "dependencies": ["依赖ID"],
            "inputs": {{"参数": "str — 说明", "业务对象": {{"model": "modules.models.example.Example"}}}},
            "outputs": {{"输出键": "dict — 可序列化结果，或使用已有 Model 的完整 class_path"}},
            "ai_spec": "对 execute 方法的技术规格描述",
            "code": "完整 Python 源代码",
            "extra_deps": ["包名"]
        }}
        """

        try:
            max_attempts = 5
            feedback = ""
            generated_valid = False
            candidate_result = None
            for attempt in range(1, max_attempts + 1):
                if candidate_result is None:
                    result = call_llm(
                        create_prompt,
                        f"生成组件: {name}{feedback}",
                        json_mode=True,
                        temperature=0.1,
                        max_tokens=8192,
                        progress_callback=progress_callback,
                        progress_label=f"Generating component {i}/{len(components)}: {name}",
                    )
                else:
                    result = candidate_result
                    candidate_result = None

                code = result.get("code", "")
                dependencies = result.get("dependencies", [])
                inputs = result.get("inputs", {})
                outputs = result.get("outputs", {})
                methods = result.get("methods", {})
                ai_spec = result.get("ai_spec", "")
                extra_deps = result.get("extra_deps", [])

                if not code:
                    if attempt < max_attempts:
                        print(f"   ⚠️  AI 未返回代码，第 {attempt}/{max_attempts} 次重试...")
                        continue
                    print("   ❌ AI 未返回代码，跳过。")
                    break

                violations = validate_component_contract(
                    code, name, category, inputs, outputs, methods,
                )
                known_beans = {
                    bean.get("id"): bean for bean in beans.get("beans", [])
                    if bean.get("id")
                }
                known_ids = set(known_beans)
                if category == "provider":
                    model_beans = {
                        bean_id: item for bean_id, item in known_beans.items()
                        if item.get("category") == "model"
                    }
                    for method_name, contract in (methods or {}).items():
                        output_spec = contract.get("outputs", "any") if isinstance(contract, dict) else "any"
                        output_text = str(output_spec).lower()
                        for model_id, model_bean in model_beans.items():
                            if model_id.lower() in output_text and not (
                                isinstance(output_spec, dict) and model_bean.get("class_path")
                                in str(output_spec)
                            ):
                                violations.append(
                                    f"methods.{method_name}.outputs 返回 Model '{model_id}' 时必须引用完整 class_path "
                                    f"'{model_bean.get('class_path')}'"
                                )
                for dependency in dependencies:
                    if dependency not in known_ids:
                        violations.append(
                            f"依赖 ID '{dependency}' 不存在；必须原样使用组件池中的 ID："
                            + ", ".join(sorted(item for item in known_ids if item))
                        )
                    elif known_beans[dependency].get("category") == "model":
                        violations.append(
                            f"Model '{dependency}' 不能作为注入依赖；请从其 class_path "
                            f"'{known_beans[dependency].get('class_path')}' 直接 import，并从 dependencies 删除"
                        )
                evidence = reduce_evidence(violations)
                stage_record["last_evidence"] = evidence
                _save_decision_plan(decision_state)
                if violations:
                    if request_repair(
                        violations, attempt, max_attempts,
                        interactive=not args.json and not args.yes,
                        auto_repair=bool(getattr(args, "auto_repair", False) or args.yes),
                    ):
                        failure_kind = classify_failures(violations)
                        if can_patch_code(failure_kind):
                            try:
                                patch_result = call_llm(
                                    "你是严格的 Python 最小补丁生成器，只能按要求返回 JSON patches。",
                                    patch_prompt(code, violations, failure_kind),
                                    json_mode=True, temperature=0.0, max_tokens=8192,
                                    progress_callback=progress_callback,
                                    progress_label=f"Patching component {name} ({failure_kind})",
                                )
                                patched_code = apply_code_patches(
                                    code, patch_result.get("patches"), name, failure_kind,
                                )
                                candidate_result = dict(result)
                                candidate_result["code"] = patched_code
                                feedback = ""
                                print(f"   🩹 已应用 {failure_kind} 最小补丁；保留其余候选内容并重新验证。")
                                continue
                            except Exception as patch_error:
                                print(f"   ⚠️  最小补丁无效 ({patch_error})，回退到结构化重生成。")
                        feedback = repair_feedback(violations)
                        continue
                    break

                generated_valid = True
                break

            if not generated_valid:
                failed.append(name)
                print("   ⛔ 当前阶段立即停止；不会继续生成同层后续组件。")
                break

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
                "file": f"{name.lower()}.py",
                "dependencies": dependencies,
                "inputs": inputs,
                "outputs": outputs,
                "methods": methods,
                "description": f"{description}。技术规格: {ai_spec}",
            }
            if category == "model":
                new_bean["fields"] = extract_model_fields(code, name)
            if category == "provider":
                resources = extract_sql_resources(code)
                if resources:
                    new_bean["resources"] = resources
            beans["beans"] = [b for b in beans["beans"] if b["id"] != name]
            beans["beans"].append(new_bean)
            save_config(beans)

            generated.append(name)
            print(f"   ✅ {name} → {file_path}")

        except Exception as e:
            print(f"   ❌ 生成失败: {e}")
            failed.append(name)
            print("   ⛔ 当前阶段立即停止；不会继续生成同层后续组件。")
            break

    # 输出汇总
    print(f"\n{'='*50}")
    print(f"🧩 [Pod 阶段组件完成] {pod_name}")
    print(f"   ✅ 组件成功: {len(generated)} 个 — {', '.join(generated) if generated else '(无)'}")
    if failed:
        _set_stage_status(decision_state, stage, "in_progress")
        print(f"   ❌ 组件失败: {len(failed)} 个 — {', '.join(failed)}")
        print("   ⛔ Pod 已停止：核心组件未通过，保留此前已验证组件，不生成下游 Pipeline 或入口。")
        raise SystemExit(1)

    if stage < 3:
        _set_stage_status(decision_state, stage, "complete")
        print(f"\n✅ [{stage_name} 阶段已冻结] 控制权返回 Pod Agent。")
        return

    # 生成 pipelines
    generated_pipelines = []
    failed_pipelines = []
    reused_pipelines = []
    if pipelines and (generated or reused):
        print(f"\n🔗 [生成 Pipeline] {len(pipelines)} 条")
        from ai_pod_cli.commands.compose import handle_compose
        existing_routes = _load_routes_map()

        for i, pipe in enumerate(pipelines, 1):
            pipe_name = pipe.get("name", f"pipeline_{i}")
            instruction = pipe.get("instruction", "")
            print(f"\n   [{i}/{len(pipelines)}] {pipe_name}: {instruction}")
            if pipe_name in existing_routes:
                reused_pipelines.append(pipe_name)
                print(f"   ♻️  [Pipeline 复用] {pipe_name}")
                continue

            # 构造 compose 的 args
            class ComposeArgs:
                pass
            compose_args = ComposeArgs()
            compose_args.cmd = instruction
            compose_args.name = pipe_name
            compose_args.list = False
            compose_args.progress_callback = progress_callback
            compose_args.auto_repair = bool(getattr(args, "auto_repair", False) or args.yes)
            compose_args.json = getattr(args, "json", False)

            try:
                compose_succeeded = handle_compose(compose_args)
                route_was_registered = pipe_name in _load_routes_map()
                if compose_succeeded is True and route_was_registered:
                    generated_pipelines.append(pipe_name)
                else:
                    print(
                        "   ❌ Pipeline 工具未返回成功回执或未注册路由；"
                        "本条不会被标记为完成。"
                    )
                    failed_pipelines.append(pipe_name)
            except Exception as e:
                print(f"   ❌ Pipeline 生成失败: {e}")
                failed_pipelines.append(pipe_name)

        print(f"\n   🔗 Pipeline 成功: {len(generated_pipelines)} 条 — {', '.join(generated_pipelines) if generated_pipelines else '(无)'}")
        if failed_pipelines:
            print(f"   ❌ Pipeline 失败: {len(failed_pipelines)} 条 — {', '.join(failed_pipelines)}")

    if stage == 3:
        if failed_pipelines:
            _set_stage_status(decision_state, stage, "in_progress")
            print("   ⛔ Pipeline 阶段未完全通过，不规划 Interface。")
            raise SystemExit(1)
        _set_stage_status(decision_state, stage, "complete")
        print("\n✅ [pipelines 阶段已冻结] 控制权返回 Pod Agent。")
        return

    # Generate interfaces only after all routes have been frozen.
    entry_files = []
    available_components = list(dict.fromkeys(reused + generated))
    available_pipelines = list(_load_routes_map())
    if stage == 4:
        print(f"\n🚀 [生成 Interfaces] {len(interfaces)} 个")
        for index, interface in enumerate(interfaces, 1):
            print(
                f"   [{index}/{len(interfaces)}] {interface.get('name', '')}"
                f" ({interface.get('kind', '')})"
            )
            entry_info = _generate_pod_entry(
                desc, available_components, available_pipelines,
                progress_callback=progress_callback,
                auto_repair=bool(getattr(args, "auto_repair", False) or args.yes),
                interface=interface,
            )
            if not entry_info:
                _set_stage_status(decision_state, stage, "in_progress")
                print("   ⛔ Interface 阶段失败，已验证的前四阶段保持冻结。")
                raise SystemExit(1)
            entry_file, extra_deps = entry_info
            entry_files.append(entry_file)
            if extra_deps:
                append_deps_to_requirements(extra_deps)
                print(f"   📦 额外依赖: {', '.join(extra_deps)}")

    # 输出汇总
    if stage == 4:
        decision_state["stages"]["interfaces"]["artifacts"] = entry_files
        _set_stage_status(decision_state, stage, "complete")
    print(f"\n{'='*50}")
    print(f"🧩 [Pod 生成完毕] {pod_name}")
    print(f"   ✅ 组件: {len(generated)} 个 — {', '.join(generated) if generated else '(无)'}")
    if reused:
        print(f"   ♻️  复用组件: {len(reused)} 个 — {', '.join(reused)}")
    if failed:
        print(f"   ❌ 组件失败: {len(failed)} 个 — {', '.join(failed)}")
    if pipelines:
        print(f"   🔗 Pipeline: {len(generated_pipelines)} 条 — {', '.join(generated_pipelines) if generated_pipelines else '(无)'}")
        if reused_pipelines:
            print(f"   ♻️  复用 Pipeline: {len(reused_pipelines)} 条 — {', '.join(reused_pipelines)}")
        if failed_pipelines:
            print(f"   ❌ Pipeline 失败: {len(failed_pipelines)} 条 — {', '.join(failed_pipelines)}")
    if entry_files:
        print(f"   🚀 Interfaces: {', '.join(entry_files)}")

    if available_components:
        if entry_files:
            print(f"\n   运行: python {entry_files[0]}")
        else:
            print(f"\n   可以手动生成入口: aipod init \"{desc[:50]}\"")
    print(f"{'='*50}")


def _agent_project_observation(state: dict) -> dict:
    """Return compact public state that lets the Pod Agent choose its next tool."""
    beans = load_beans().get("beans", [])
    counts = {"model": 0, "provider": 0, "service": 0}
    for bean in beans:
        category = bean.get("category")
        if category in counts and bean.get("status") != "invalid":
            counts[category] += 1
    return {
        "current_stage": state.get("current_stage"),
        "stages": {
            name: state.get("stages", {}).get(name, {}).get("status", "pending")
            for name in STAGE_NAMES
        },
        "component_counts": counts,
        "routes": list(_load_routes_map()),
        "verification": {
            key: value
            for key, value in state.get("agent", {}).get("verification", {}).items()
            if key in {"status", "attempts", "repairs", "command", "repaired_file"}
        },
        "recent_actions": [
            {
                key: item.get(key)
                for key in ("step", "action", "stage", "status", "summary")
                if key in item
            }
            for item in state.get("agent", {}).get("history", [])[-6:]
        ],
    }


def _select_pod_build_tool(
    desc: str, state: dict, stage: int, progress_callback=None,
) -> dict:
    """Ask the Pod Agent Leader to select one governed Build Tool."""
    expected_action = STAGE_BUILD_TOOLS[stage]
    observation = _agent_project_observation(state)
    system_prompt = f"""
    You are the AIPod Pod Agent Leader. You construct software by selecting exactly one
    governed Build Tool at a time. You never generate source code in this response.

    Available Build Tools:
    - generate_models: plan, generate, validate, and freeze the current Model stage
    - generate_providers: plan, generate, validate, and freeze infrastructure Providers
    - generate_services: plan, generate, validate, and freeze business Services
    - compose_pipelines: compose and validate Pipelines from frozen Services
    - generate_interfaces: generate Interfaces from frozen Pipeline routes
    - retry_current: retry only the current failed or incomplete stage

    Governance:
    - The earliest incomplete stage is {STAGE_NAMES[stage]} and its normal tool is
      {expected_action}. Never skip it or modify a completed stage.
    - Select retry_current only when recent_actions shows that the current tool failed.
    - Return a compact public decision summary, never private chain-of-thought.
    - Return strict JSON only:
      {{"action":"tool name","summary":"why this is the next bounded action",
        "success_criteria":["observable condition"]}}
    """
    return call_llm(
        system_prompt,
        "Objective:\n" + desc + "\n\nCurrent observation:\n"
        + json.dumps(observation, ensure_ascii=False),
        json_mode=True,
        temperature=0.1,
        max_tokens=1024,
        progress_callback=progress_callback,
        progress_label=f"Pod Agent selecting tool for {STAGE_NAMES[stage]}",
    )


def _application_verification_command(state: dict) -> list[str]:
    """Choose one deterministic, non-interactive command from frozen Interface metadata."""
    verification = state["agent"]["verification"]
    existing = verification.get("command")
    if isinstance(existing, list) and existing:
        return [str(item) for item in existing]

    interface_stage = state.get("stages", {}).get("interfaces", {})
    plan = interface_stage.get("plan") or {}
    interfaces = plan.get("interfaces", []) if isinstance(plan, dict) else []
    artifacts = [
        str(item) for item in interface_stage.get("artifacts", [])
        if isinstance(item, str) and item
    ]
    for interface in interfaces:
        name = str(interface.get("name", "")).strip()
        if name and not name.endswith(".py"):
            name += ".py"
        if name and name not in artifacts:
            artifacts.append(name)
    artifacts.extend(
        name for name in ("main.py", "app.py", "server.py")
        if Path(name).exists() and name not in artifacts
    )

    entry_file = next((name for name in artifacts if Path(name).is_file()), "")
    if entry_file:
        command = [sys.executable, "-X", "utf8", entry_file]
        matching = next(
            (
                item for item in interfaces
                if str(item.get("name", "")).removesuffix(".py")
                == Path(entry_file).stem
            ),
            interfaces[0] if len(interfaces) == 1 else {},
        )
        instruction = str(matching.get("instruction", "")).lower()
        if "--smoke" in instruction:
            command.append("--smoke")
        elif "`smoke`" in instruction or " smoke" in instruction:
            command.append("smoke")
        return command
    if Path("tests").is_dir():
        return [sys.executable, "-X", "utf8", "-m", "unittest", "discover"]
    return []


def _project_verification_fingerprint() -> str:
    """Hash behavior-relevant project files so stale passes are never reused."""
    paths = [
        path for path in (
            Path("beans_config.json"), Path("routes.toml"), Path("config.toml"),
        )
        if path.is_file()
    ]
    paths.extend(sorted(Path.cwd().glob("*.py")))
    for directory in (Path("modules"), Path("pipelines"), Path("tests")):
        if directory.is_dir():
            paths.extend(sorted(directory.rglob("*.py")))
    digest = hashlib.sha256()
    for path in sorted(set(paths), key=lambda item: item.as_posix()):
        digest.update(path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _verify_application(desc: str, state: dict, timeout: int = 120) -> dict:
    """Execute the frozen application's real smoke/test command and persist evidence."""
    from ai_pod_cli.commands.verify import verify_project

    command = _application_verification_command(state)
    result = verify_project(command, timeout=max(1, timeout))
    latest = _load_decision_plan(desc)
    verification = latest["agent"]["verification"]
    verification["attempts"] = int(verification.get("attempts", 0)) + 1
    verification["command"] = command
    verification["status"] = "passed" if result["status"] == "passed" else "failed"
    verification["last_result"] = result
    verification["fingerprint"] = _project_verification_fingerprint()
    _save_decision_plan(latest)
    return result


def _validate_repaired_artifact(relative_path: str, code: str) -> list[str]:
    """Run the existing deterministic validator appropriate for one repaired file."""
    normalized = Path(relative_path).as_posix()
    if normalized.startswith("pipelines/"):
        return validate_pipeline_contract(code)

    beans = load_beans().get("beans", [])
    for bean in beans:
        class_path = str(bean.get("class_path", ""))
        if not class_path or "." not in class_path:
            continue
        module_name, class_name = class_path.rsplit(".", 1)
        component_path = Path(*module_name.split(".")).with_suffix(".py").as_posix()
        if component_path == normalized:
            return validate_component_contract(
                code,
                class_name,
                str(bean.get("category", "service")),
                bean.get("inputs"),
                bean.get("outputs"),
                bean.get("methods"),
            )
    return validate_entry_contract(code, list(_load_routes_map()))


def _repair_current_artifact(desc: str, state: dict, progress_callback=None) -> dict:
    """Patch only the deepest project file selected by the last verification traceback."""
    from ai_pod_cli.commands.verify import _bounded_output

    verification = state["agent"]["verification"]
    result = verification.get("last_result") or {}
    suggested = result.get("repair", {}).get("suggested_files", [])
    root = Path.cwd().resolve()
    candidates: list[tuple[str, Path]] = []
    for raw_path in suggested:
        candidate = (root / str(raw_path)).resolve()
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError:
            continue
        if candidate.is_file() and candidate.suffix == ".py":
            candidates.append((relative, candidate))
    if not candidates:
        raise RuntimeError("验证失败，但没有 traceback 指向可安全修复的项目 Python 文件")

    relative_path, artifact = candidates[-1]
    source = artifact.read_text(encoding="utf-8")
    checks = result.get("checks", {})
    execution = checks.get("execution") or {}
    evidence = [
        *[str(item) for item in checks.get("structure", {}).get("issues", [])],
        str(execution.get("stdout", ""))[-8000:],
        str(execution.get("stderr", ""))[-8000:],
    ]
    evidence = [item for item in evidence if item]
    response = call_llm(
        "You repair one evidence-selected Python artifact with exact minimal patches. "
        "Never return hidden reasoning or a whole-file rewrite.",
        file_patch_prompt(_bounded_output(source, 50000), evidence, relative_path),
        json_mode=True,
        temperature=0.1,
        max_tokens=4096,
        progress_callback=progress_callback,
        progress_label=f"Repairing current artifact: {relative_path}",
    )
    repaired = apply_file_patches(source, response.get("patches"))
    violations = _validate_repaired_artifact(relative_path, repaired)
    if violations:
        raise ValueError("修复补丁未通过本地预检：" + "；".join(violations))
    artifact.write_text(repaired, encoding="utf-8")

    latest = _load_decision_plan(desc)
    latest_verification = latest["agent"]["verification"]
    latest_verification["repairs"] = int(latest_verification.get("repairs", 0)) + 1
    latest_verification["status"] = "repair_applied"
    latest_verification["repaired_file"] = relative_path
    _save_decision_plan(latest)
    return {"file": relative_path, "patch_count": len(response.get("patches", []))}


def _append_agent_event(desc: str, event: dict) -> dict:
    """Persist one public Agent action/observation without hidden reasoning."""
    state = _load_decision_plan(desc)
    agent = state["agent"]
    agent["step"] = int(agent.get("step", 0)) + 1
    normalized = {"step": agent["step"], **event}
    history = list(agent.get("history", []))
    history.append(normalized)
    agent["history"] = history[-50:]
    agent["status"] = normalized.get("status", "running")
    agent["last_action"] = normalized.get("action")
    agent["last_observation"] = normalized.get("observation", {})
    _save_decision_plan(state)
    return state


def _set_agent_status(desc: str, status: str) -> None:
    state = _load_decision_plan(desc)
    state["agent"]["status"] = status
    _save_decision_plan(state)


def _read_pod_requirement(args) -> str:
    if getattr(args, "file", ""):
        if not os.path.exists(args.file):
            print(f"❌ 文件不存在: {args.file}")
            return ""
        with open(args.file, "r", encoding="utf-8") as file:
            return file.read().strip()
    return str(getattr(args, "desc", "") or "").strip()


def handle_pod(args):
    """Run the resumable Pod Agent over governed five-stage Build Tools."""
    desc = _read_pod_requirement(args)
    if not desc:
        print("❌ 请提供需求描述或 --file 文件路径。")
        return
    if not os.environ.get("OPENAI_API_KEY"):
        from ai_pod_cli.commands.env import print_missing_model_config
        print_missing_model_config()
        raise SystemExit(1)

    original_file = getattr(args, "file", "")
    args.file = ""
    args.desc = desc
    explicit_stage = int(args._pod_stage) if hasattr(args, "_pod_stage") else None
    state = _load_decision_plan(desc, explicit_stage)
    verification = state["agent"]["verification"]
    if (
        verification.get("status") == "passed"
        and verification.get("fingerprint") != _project_verification_fingerprint()
    ):
        verification["status"] = "pending"
    _save_decision_plan(state)
    _set_agent_status(desc, "running")
    max_steps = int(getattr(args, "_pod_agent_max_steps", 15))
    stage_failures: dict[int, int] = {}
    repair_tool_failures = 0

    print("🧠 [Pod Agent] 启动构建循环：Observe → Select Tool → Execute → Observe")
    if original_file:
        print(f"🧩 [Pod Agent] 需求来源: {original_file}")

    for _ in range(max_steps):
        state = _load_decision_plan(desc)
        stage = _resume_stage(state)
        if stage is None:
            verification = state["agent"]["verification"]
            verification_status = verification.get("status", "pending")
            if verification_status == "passed":
                _set_agent_status(desc, "complete")
                print("✅ [Pod Agent] 五阶段构建与应用运行验证均已完成。")
                return

            action = (
                "repair_current_artifact"
                if verification_status == "failed"
                else "verify_application"
            )
            step = state["agent"].get("step", 0) + 1
            print(f"\n🧠 [Pod Agent · Step {step}] {action} (application)")
            if action == "verify_application":
                result = _verify_application(
                    desc,
                    state,
                    int(getattr(args, "_pod_verify_timeout", 120)),
                )
                execution = result.get("checks", {}).get("execution")
                observation = {
                    "verification_status": result.get("status"),
                    "structure": result.get("checks", {}).get("structure", {}).get("status"),
                    "execution": execution.get("status") if execution else "skipped",
                    "command": execution.get("command", []) if execution else [],
                    "suggested_files": result.get("repair", {}).get("suggested_files", []),
                }
                event_status = "succeeded" if result.get("status") == "passed" else "failed"
                _append_agent_event(desc, {
                    "action": action,
                    "stage": "application",
                    "status": event_status,
                    "decision_status": "policy_selected",
                    "summary": "Run the frozen application's deterministic smoke or test command.",
                    "observation": observation,
                })
                if result.get("status") == "passed":
                    _set_agent_status(desc, "complete")
                    print("✅ [verify_application] 应用结构与实际运行命令均已通过。")
                    return
                print("🔁 [verify_application] 运行失败；下一步只允许修复证据指向的当前文件。")
                continue

            if int(verification.get("repairs", 0)) >= 3:
                _set_agent_status(desc, "blocked")
                print("⛔ [Pod Agent] 已达到 3 次受限修复上限，冻结上游保持不变。")
                raise SystemExit(1)
            try:
                repaired = _repair_current_artifact(
                    desc, state, getattr(args, "progress_callback", None),
                )
            except Exception as error:
                repair_tool_failures += 1
                _append_agent_event(desc, {
                    "action": action,
                    "stage": "application",
                    "status": "failed",
                    "decision_status": "policy_selected",
                    "summary": "Repair only the evidence-selected current artifact.",
                    "observation": {"error": f"{type(error).__name__}: {error}"},
                })
                if repair_tool_failures >= 2:
                    _set_agent_status(desc, "blocked")
                    print("⛔ [repair_current_artifact] 连续失败，未修改冻结上游。")
                    raise SystemExit(1)
                print("🔁 [repair_current_artifact] 补丁未通过约束，将重试当前修复工具。")
                continue
            repair_tool_failures = 0
            _append_agent_event(desc, {
                "action": action,
                "stage": "application",
                "status": "succeeded",
                "decision_status": "policy_selected",
                "summary": "Applied a bounded patch to the traceback-selected artifact.",
                "observation": repaired,
            })
            print(
                f"🩹 [repair_current_artifact] 已修复 {repaired['file']}；"
                "下一步重新执行同一验证命令。"
            )
            continue

        decision = _select_pod_build_tool(
            desc, state, stage, getattr(args, "progress_callback", None),
        )
        requested_action = str(decision.get("action", ""))
        expected_action = STAGE_BUILD_TOOLS[stage]
        last_actions = state.get("agent", {}).get("history", [])
        retry_allowed = bool(
            last_actions
            and last_actions[-1].get("stage") == STAGE_NAMES[stage]
            and last_actions[-1].get("status") == "failed"
        )
        if requested_action == "retry_current" and retry_allowed:
            action = expected_action
            decision_status = "retry"
        elif requested_action == expected_action:
            action = requested_action
            decision_status = "selected"
        else:
            action = expected_action
            decision_status = "policy_corrected"

        print(
            f"\n🧠 [Pod Agent · Step {state['agent'].get('step', 0) + 1}] "
            f"{action} ({STAGE_NAMES[stage]})"
        )
        summary = str(decision.get("summary", ""))[:400]
        if summary:
            print(f"   决策摘要: {summary}")
        if decision_status == "policy_corrected":
            print(
                f"   🛡️ Stage Policy 将无效动作 '{requested_action or '(empty)'}' "
                f"约束为 '{expected_action}'。"
            )

        args._pod_stage = stage
        args.auto_repair = bool(getattr(args, "auto_repair", False) or args.yes)
        try:
            _execute_pod_build_tool(args)
        except SystemExit as error:
            stage_failures[stage] = stage_failures.get(stage, 0) + 1
            _append_agent_event(desc, {
                "action": action,
                "requested_action": requested_action,
                "stage": STAGE_NAMES[stage],
                "status": "failed",
                "decision_status": decision_status,
                "summary": summary,
                "observation": {
                    "exit_code": error.code if isinstance(error.code, int) else 1,
                    "stage_status": _load_decision_plan(desc)["stages"][STAGE_NAMES[stage]]["status"],
                },
            })
            if stage_failures[stage] >= 2:
                _set_agent_status(desc, "blocked")
                print("⛔ [Pod Agent] 当前 Build Tool 连续失败，已保留冻结上游与 Agent 状态。")
                raise
            print("🔁 [Pod Agent] 已观察到失败；下一步只允许重试当前 Build Tool。")
            continue
        except Exception as error:
            _append_agent_event(desc, {
                "action": action,
                "requested_action": requested_action,
                "stage": STAGE_NAMES[stage],
                "status": "failed",
                "decision_status": decision_status,
                "summary": summary,
                "observation": {"error": f"{type(error).__name__}: {error}"},
            })
            _set_agent_status(desc, "blocked")
            raise

        updated = _load_decision_plan(desc)
        _append_agent_event(desc, {
            "action": action,
            "requested_action": requested_action,
            "stage": STAGE_NAMES[stage],
            "status": "succeeded",
            "decision_status": decision_status,
            "summary": summary,
            "success_criteria": decision.get("success_criteria", []),
            "observation": _agent_project_observation(updated),
        })

    _set_agent_status(desc, "blocked")
    print(f"⛔ [Pod Agent] 达到最大步骤数 {max_steps}，已保存状态，可再次运行续接。")
    raise SystemExit(1)
