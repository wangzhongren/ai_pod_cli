"""Model, Provider, and Service artifact generation."""

import json
import os

from ai_pod_cli.client import call_llm
from ai_pod_cli.config import (
    append_deps_to_requirements, extract_model_fields, extract_sql_resources,
    get_module_path, load_beans, load_beans_summary, load_config_toml_safe,
    save_config,
)
from ai_pod_cli.decision import reduce_evidence
from ai_pod_cli.pod.state import save_decision_plan as _save_decision_plan
from ai_pod_cli.repair import (
    apply_code_patches, can_patch_code, classify_failures, patch_prompt,
)
from ai_pod_cli.validation import (
    repair_feedback, request_repair, validate_component_contract,
)


def generate_components(
    *, components: list[dict], reduction: dict, decision_state: dict,
    stage_record: dict, args, progress_callback=None,
) -> tuple[list[str], list[str]]:
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

    return generated, failed
