"""Governed build tools for the five frozen Pod stages."""

import json
import os
import sys

from ai_pod_cli.client import call_llm
from ai_pod_cli.config import (
    append_deps_to_requirements, load_beans, load_beans_summary,
    load_config_toml_safe,
)
from ai_pod_cli.decision import reduce_decision_fragments
from ai_pod_cli.pod.state import (
    STAGE_NAMES, load_decision_plan as _load_decision_plan,
    normalize_interface_plan, resume_stage as _resume_stage,
    save_decision_plan as _save_decision_plan,
    set_stage_status as _set_stage_status,
)
from ai_pod_cli.pod.planning import save_pod_plan as _save_pod_plan
from ai_pod_cli.pod.routes import load_routes_map as _load_routes_map
from ai_pod_cli.pod.tools.components import generate_components, verify_reused_components
from ai_pod_cli.pod.tools.interfaces import (
    generate_interfaces, generate_pod_entry as _generate_pod_entry,
)
from ai_pod_cli.pod.tools.pipelines import generate_pipelines



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

    rebuild_from = getattr(args, "_pod_rebuild_from", None)
    rebuild_active = rebuild_from is not None and stage >= int(rebuild_from)
    revision = decision_state.get("revision", {}) if rebuild_active else {}
    revision_instruction = str(revision.get("instruction", "")).strip()
    previous_stage_evidence = (
        decision_state.get("stages", {}).get(STAGE_NAMES[stage], {}).get("last_evidence")
        or {}
    )

    beans = load_beans()
    existing_beans = load_beans_summary()
    toml_keys = load_config_toml_safe()
    stage_names = STAGE_NAMES
    stage_name = stage_names[min(stage, len(stage_names) - 1)]
    frozen_routes = _load_routes_map() if stage == 4 else {}
    visible_components = (
        "[hidden from Interface planning; use frozen routes only]"
        if stage == 4 else existing_beans
    )
    visible_config = (
        "[hidden from Interface planning]" if stage == 4 else toml_keys
    )
    visible_evidence = {} if stage == 4 else previous_stage_evidence
    stage_instructions = {
        0: "只规划共享 data model。components 只能包含 model；pipelines 必须为空。每个 Model 项只对应一个 Python 类。明确区分运行时 Value Model（不持久化）和 Persistent Model（需要 ModelRepository/数据库）；禁止把 Vector2、Transform、事件、碰撞结果等瞬时值规划成数据库表。",
        1: "只规划用户明确要求连接的外部基础设施 provider。不得因为需求出现 Web/CLI/Desktop/Worker 就创建 HTTP Server、调度器、Redis、消息队列、邮件或通知 Provider；这些属于后续 Interface，除非用户明确指定真实外部系统。没有明确外部系统时 components 必须为空并复用 ModelRepository。数据库能力只能复用内置 ModelRepository，禁止 DatabaseProvider、SchemaProvider 或 SQL Provider。",
        2: "只规划业务 service。数据库持久化必须依赖内置 ModelRepository，禁止 DatabaseProvider、SchemaProvider 和原始 SQL。components 只能包含 service；pipelines 必须为空。每个 Service 只对应一个 execute(ctx)。components.models 必须填写 Bean Pool 中已有 Model 的精确 Bean ID，禁止填写或猜测 class_path；后续代码生成器会从冻结注册表取得精确 class_path。Model 只能普通 import，不能写入 depends_on。",
        3: "只规划 Pipeline。components 必须为空；reuse_components 列出 Pipeline 使用的现有 Service；根据已经冻结的 Service inputs/outputs 规划 pipelines，禁止假设不存在的组件。默认同步串行；只有需求明确需要时才声明 async、parallel 或 stream。并发分支必须给出 merge、failure_policy 和 concurrency，流式执行必须给出 concurrency、batch_size 和 failure_policy。",
        4: "只规划用户入口 Interface。Interface 的全部能力视图就是已冻结 route；不得知道、猜测或导入 Model、Provider、Service 和其类路径。reuse_components、components 和 pipelines 必须为空。把 Interface 规划为可交付单元：多个 Artifact、安装/运行/卸载命令、权限、平台支持级别和多项验证。复杂 Adapter 必须拆成 adapter_entry 和多个 adapter_module，每个文件只承担一个输入适配职责。所有 Artifact 必须位于 interfaces/<interface-id>/ 下。verify 命令必须非交互、可重复且不得执行安装、卸载或修改用户系统。",
    }[min(stage, 4)]

    system_prompt = f"""
    你是一个资深的软件架构师。当前系统是一个基于 Python `injector` 框架的 IoC/DI 容器低代码平台。

    目前系统中已有的组件池（Bean Pool）：
    {visible_components}

    当前 config.toml 中的配置项（敏感值已隐藏）：
    {visible_config}

    Interface 可见的已冻结 routes（非 Interface 阶段为空）：
    {json.dumps(frozen_routes, ensure_ascii=False, indent=2)}

    当前处于阶段 {stage + 1}/5：{stage_name}。
    {stage_instructions}

    本次局部修改要求：{revision_instruction or "无（正常构建或续跑）"}
    {"当前层及下游已解冻。如需修改现有同层组件，可以在 components 中使用相同 Bean ID 返回替换规划。" if rebuild_active else ""}
    上一次当前层验证证据：{json.dumps(visible_evidence, ensure_ascii=False)}

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
    8. components.models 只接受 Model Bean ID（例如 `Transform`），不接受
       `modules.models.transform.Transform`。不得根据类名猜测文件路径；精确 class_path
       将由本地 Reducer 从冻结 Bean Pool 解析。

    【Pipeline 规划规则（仅 pipelines 阶段适用）】：
    1. 为每个 service 类型的组件规划至少一条 pipeline。
    2. pipeline 的 instruction 应该是具体的业务指令（如 "生成一个用户认证组件"）。
    3. pipeline 的 name 应该是简短的英文标识（如 create_auth）。
    4. execution.mode 只能是 sequential、async、parallel 或 stream。默认 sequential。
       parallel/stream 的策略由 Runtime 执行，不要要求 Service 自己管理 asyncio 任务。

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
                "models": ["Bean Pool 中精确的 Model Bean ID，不是 class_path；仅作为数据引用，不注入"],
                "requires": ["执行前必须存在的语义字段"],
                "provides": ["执行后产生的语义字段"],
                "invariants": ["必须始终成立且可验证的架构约束"]
            }}
        ],
        "pipelines": [
            {{
                "name": "pipeline 英文标识",
                "instruction": "自然语言业务指令（AI 据此规划执行链）",
                "execution": {{
                    "mode": "sequential|async|parallel|stream",
                    "concurrency": 1,
                    "failure_policy": "fail_fast|collect_all|ignore|stop|skip",
                    "merge": "strict|overwrite|collect",
                    "batch_size": 1
                }}
            }}
        ],
        "interfaces": [
            {{
                "name": "interface-id",
                "kind": "cli|web|desktop|worker|consumer|macos_quick_action|native_extension",
                "platform": "cross-platform|macos|windows|linux",
                "instruction": "交付单元的整体行为和精确 Pipeline route",
                "adapter": {{
                    "entry_path": "interfaces/interface-id/adapter.py",
                    "class_name": "GeneratedInterfaceAdapter"
                }},
                "artifacts": [
                    {{"path": "interfaces/interface-id/adapter.py", "role": "adapter_entry", "format": "python", "instruction": "只定义继承 ai_pod_cli.interface.InterfaceAdapter 的入口类；复杂逻辑拆到其他 adapter_module Artifact"}},
                    {{"path": "interfaces/interface-id/transport.py", "role": "adapter_module", "format": "python", "instruction": "只实现消息、HTTP、UI 或其他项目特有输入适配；文件之间使用相对导入"}},
                    {{"path": "interfaces/interface-id/install.sh", "role": "installer", "format": "shell", "instruction": "平台需要时生成安装脚本，否则不规划"}},
                    {{"path": "interfaces/interface-id/metadata.json", "role": "metadata", "format": "json", "instruction": "平台需要时生成声明元数据"}}
                ],
                "lifecycle": {{
                    "run": ["{{python}}", "-m", "ai_pod_cli", "interface", "--project-root", "{{project_root}}", "run", "interface-id"],
                    "install": ["sh", "interfaces/interface-id/install.sh"],
                    "uninstall": ["sh", "interfaces/interface-id/uninstall.sh"]
                }},
                "permissions": ["filesystem_write"],
                "support": {{"level": "supported|supported_with_manual_step|prototype_only|unsupported", "manual_steps": []}},
                "verify": [
                    {{"name": "adapter_smoke", "kind": "runtime", "required": true, "command": ["{{python}}", "-m", "ai_pod_cli", "interface", "--project-root", "{{project_root}}", "smoke", "interface-id"], "timeout": 30}}
                ]
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
        if stage == 4:
            normalize_interface_plan(plan)
        stage_record["plan"] = plan
        stage_record["status"] = "in_progress"
        decision_state["current_stage"] = stage_name
        _save_decision_plan(decision_state)

    if stage == 4:
        normalize_interface_plan(plan)
        plan["reuse_components"] = []
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
        stage_record["status"] = "pending"
        stage_record["plan"] = None
        stage_record["last_evidence"] = {
            "status": "rejected",
            "repair_scope": stage_name,
            "evidence": [
                {
                    key: conflict.get(key)
                    for key in ("code", "component", "dependency", "model", "message")
                    if conflict.get(key) is not None
                }
                for conflict in reduction["conflicts"]
            ],
        }
        _save_decision_plan(decision_state)
        print(f"🔄 [{stage_name} 规划已丢弃] 下一次重试将根据冲突证据重新规划。")
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
            existing_category = existing_by_id[name].get("category")
            if rebuild_active and existing_category == component.get("category"):
                if name in reused:
                    reused.remove(name)
                create_components.append(component)
            elif name not in reused:
                reused.append(name)
        else:
            create_components.append(component)
    components = create_components

    # An empty Provider plan is a valid and desirable result when the requirement
    # does not name any external infrastructure.  The built-in providers remain
    # available and the five-stage loop must continue to Services.
    legacy_interface_verification = bool(
        stage == 4
        and decision_state.get("agent", {}).get("verification", {}).get("command")
    )
    if (
        stage != 1 and not components and not reused and not pipelines and not interfaces
        and not legacy_interface_verification
    ):
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

    generated, failed = generate_components(
        components=components, reduction=reduction, decision_state=decision_state,
        stage_record=stage_record, args=args, progress_callback=progress_callback,
    )
    # 输出汇总
    print(f"\n{'='*50}")
    print(f"🧩 [Pod 阶段组件完成] {pod_name}")
    print(f"   ✅ 组件成功: {len(generated)} 个 — {', '.join(generated) if generated else '(无)'}")
    if failed:
        _set_stage_status(decision_state, stage, "in_progress")
        print(f"   ❌ 组件失败: {len(failed)} 个 — {', '.join(failed)}")
        print("   ⛔ Pod 已停止：核心组件未通过，保留此前已验证组件，不生成下游 Pipeline 或入口。")
        raise SystemExit(1)

    if stage <= 2:
        expected_category = ("model", "provider", "service")[stage]
        reused_checks, reused_violations = verify_reused_components(
            reused, existing_by_id, expected_category,
        )
        if reused_violations:
            stage_record["status"] = "in_progress"
            stage_record["plan"] = None
            stage_record["last_evidence"] = {
                "status": "rejected", "evidence": reused_violations,
                "repair_scope": STAGE_NAMES[stage],
            }
            _save_decision_plan(decision_state)
            print("❌ [复用组件重新验证失败]")
            for violation in reused_violations:
                print(f"   ❌ {violation}")
            raise SystemExit(1)
        stage_record.setdefault("runtime_checks", []).extend(reused_checks)
        _save_decision_plan(decision_state)

    if stage < 3:
        _set_stage_status(decision_state, stage, "complete")
        print(f"\n✅ [{stage_name} 阶段已冻结] 控制权返回 Pod Agent。")
        return

    generated_pipelines, failed_pipelines, reused_pipelines = generate_pipelines(
        pipelines=pipelines, generated=generated, reused=reused, args=args,
        progress_callback=progress_callback, load_routes=_load_routes_map,
        replace_existing=rebuild_active,
    )
    if stage == 3:
        if failed_pipelines:
            _set_stage_status(decision_state, stage, "in_progress")
            print("   ⛔ Pipeline 阶段未完全通过，不规划 Interface。")
            raise SystemExit(1)
        for pipeline_name in generated_pipelines:
            stage_record.setdefault("runtime_checks", []).append({
                "pipeline": pipeline_name, "status": "passed",
                "check": "isolated_pipeline_execution",
            })
        _save_decision_plan(decision_state)
        _set_stage_status(decision_state, stage, "complete")
        print("\n✅ [pipelines 阶段已冻结] 控制权返回 Pod Agent。")
        return

    entry_files, available_components = generate_interfaces(
        desc=desc, interfaces=interfaces, reused=reused, generated=generated,
        args=args, decision_state=decision_state, stage=stage,
        progress_callback=progress_callback, replace_existing=rebuild_active,
    )

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
