"""`compose` command — AI generates pipeline file and registers it in routes.toml."""

import json
import os
import re
from datetime import datetime

from ai_pod_cli.client import call_llm
from ai_pod_cli.config import (
    load_beans, load_beans_summary, load_config_toml_safe, PIPELINES_DIR, register_route,
)
from ai_pod_cli.validation import repair_feedback, request_repair, validate_pipeline_contract
from ai_pod_cli.contracts import analyze_pipeline_contracts


def _slugify(text: str) -> str:
    """将中文或任意文本转为安全的文件名。"""
    safe = re.sub(r'[^\w一-鿿]+', '_', text.strip())
    return safe[:60] or "pipeline"


def _save_pipeline(code: str, name: str, instruction: str) -> str:
    """将 AI 生成的 pipeline Python 代码保存到 pipelines/ 目录。"""
    os.makedirs(PIPELINES_DIR, exist_ok=True)
    filename = f"{name}.py"
    filepath = os.path.join(PIPELINES_DIR, filename)

    header = (
        f'"""Pipeline: {instruction}\n'
        f'Generated: {datetime.now().isoformat()}\n'
        f'"""'
    )
    full_code = header + "\n\n" + code

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(full_code)

    return filepath


def _list_pipelines() -> list[dict]:
    """列出所有已保存的 pipeline (.py 文件)。"""
    if not os.path.exists(PIPELINES_DIR):
        return []

    pipelines = []
    for f in sorted(os.listdir(PIPELINES_DIR)):
        if f == "__init__.py":
            continue

        filepath = os.path.join(PIPELINES_DIR, f)

        if f.endswith(".py"):
            instruction = ""
            try:
                with open(filepath, "r", encoding="utf-8") as fh:
                    first_lines = "".join(fh.readline() for _ in range(3))
                match = re.search(r'Pipeline:\s*(.+)', first_lines)
                if match:
                    instruction = match.group(1).strip()
            except Exception:
                pass

            pipelines.append({
                "file": f,
                "name": f.replace(".py", ""),
                "instruction": instruction,
            })

    return pipelines


def handle_compose(args):
    """【compose 命令】AI 编排器：生成 pipeline 文件 → 注册到 routes.toml"""

    # --- --list: 列出所有已保存的 pipeline ---
    if args.list:
        pipelines = _list_pipelines()
        if not pipelines:
            print(f"📂 {PIPELINES_DIR}/ 目录为空，还没有保存任何 pipeline。")
            return

        print(f"📂 已保存的 Pipeline ({len(pipelines)} 条):\n")
        for p in pipelines:
            print(f"   🐍 {p['file']}")
            print(f"      指令: {p['instruction']}")
            print()
        return

    # --- 默认: AI 生成新的 Python pipeline ---
    if not args.cmd:
        print("❌ 请提供指令描述。")
        return

    print(f"🎬 [compose] 人类宏观指令: '{args.cmd}'")

    beans = load_beans()
    existing_beans_context = load_beans_summary()
    toml_keys = load_config_toml_safe()

    # 收集所有组件的 class_path 用于生成 import
    component_imports = {}
    for bean in beans["beans"]:
        cid = bean["id"]
        cpath = bean["class_path"]
        module_path, class_name = cpath.rsplit(".", 1)
        component_imports[cid] = {"module": module_path, "class": class_name}

    imports_hint = "\n".join(
        f"    - {cid} ({bean['category']}): from {info['module']} import {info['class']}"
        for cid, info in component_imports.items() if (bean := next((b for b in beans["beans"] if b["id"] == cid), {}))
    )

    system_prompt = f"""
    你是一个智能编排引擎。当前系统中注册了以下组件账本：
    {existing_beans_context}

    组件账本中的 inputs/outputs 是强制数据契约：
    - 账本中的组件已经验证并冻结；本轮唯一允许修复的对象是当前 Pipeline。
    - Pipeline 必须适配已有组件，禁止建议回头修改组件。
    - 下游输入语义与上游输出相同时必须使用完全相同的字段名。
    - 禁止在 Pipeline 中通过复制或重命名制造 oxygen/oxygen_level 一类同义字段。
    - 如果所需字段没有上游生产者，它只能是明确的入口参数，不能伪装成同义新字段。

    各组件的 import 路径：
    {imports_hint}

    当前 config.toml 中的配置项（敏感值已隐藏）：
    {toml_keys}
    组件通过注入 ConfigStore（from ai_pod_cli.config_store import ConfigStore）并用 config_store.get("section.key", default) 读取配置。

    你的任务是：根据人类的自然语言指令，生成一个完整的 Python pipeline 脚本。

    【生成的代码规范】：
    1. 必须定义一个 `run(ctx)` 函数作为入口，ctx 是 PipelineContext 类型。
    2. 在 run 函数内部：
       - from ai_pod_cli.config import load_beans  ← 仅用于 build_container()！
         load_beans() 加载的是 beans_config.json（bean 注册表），不是用户配置！
       - from ai_pod_cli.container import build_container, Pod
       - beans = load_beans(); container = build_container(beans)
       - S = Pod(container)
    3. **管线中禁止直接读取配置值！配置通过 ConfigStore 在组件内部读取，不在管线中读。**
       load_beans() 只用于构建容器，不要用它的返回值读配置！
    4. 使用管道符 | 串联 **service** 组件（有 execute 方法的）：
       (S(组件A) | S(组件B)).execute_all(ctx)
       这会自动依次执行各组件并记录轨迹。
       **重要**：只有 service 类型组件可以放入管道链！provider 类型组件（如 ConfigStore、SqliteStore 等）
       没有 execute 方法，只能作为依赖注入到 service 中，绝对不能放进 S() 管道链里！
    5. 严格按组件账本中的 inputs 键准备输入。Service 的 execute(ctx) 默认从 ctx.params 读取输入，
       因此调用前使用 `ctx.params.update({{"action": "...", ...}})` 写入准确的输入键。
       **禁止**把输入包装成 `xxx_input` 后只调用 ctx.set("xxx_input", payload)，除非该名称明确列在组件 inputs 中。
       ctx.set()/ctx.get() 用于组件输出和后续步骤之间的状态传递；从状态取值后仍需写入下一个 Service 声明的输入键。
    6. 需要条件分支时，用 if/else 分别串联不同的管道。
    7. 最后 return ctx.summary()。
    8. 加上清晰的中文注释。
    9. 外部调用不稳定时，可为组件声明运行策略：
       S(RemoteService).retry(3, delay_seconds=0.2).fallback(CacheService)
       retry 的 attempts 表示重试次数；fallback 可传组件类或 S(Component) 引用。
       不要默认添加策略，只在需求或组件性质明确需要时使用。

    【PipelineContext 的 API】：
    - ctx.params: dict — 入口参数
    - ctx.set(key, value): 写入数据池
    - ctx.get(key, default=None): 读取数据池
    - ctx.record_step(component_id, result): 记录执行步骤
    - ctx.summary(): 返回执行摘要 dict

    【import 规则（严格遵守）】：
    - 每个组件的 import 路径见上方列表，文件名必须**原样复制**，一个字不改！
    - **禁止** `from modules import X`，必须写完整路径！

    【代码模板示例】：
    ```python
    from ai_pod_cli.context import PipelineContext
    from ai_pod_cli.config import load_beans
    from ai_pod_cli.container import build_container, Pod
    from modules.services.stockchecker import StockChecker
    from modules.services.stocknotifier import StockNotifier


    def run(ctx: PipelineContext):
        beans = load_beans()
        container = build_container(beans)
        S = Pod(container)

        # 步骤 1: 检查库存
        (S(StockChecker)).execute_all(ctx)

        # 条件分支：库存不足时通知管理员
        if ctx.get("stock", 0) <= 0:
            (S(StockNotifier)).execute_all(ctx)

        return ctx.summary()
    ```

    【多组件串联示例】：
    ```python
    # 依次执行 A → B → C（自动记录每步轨迹）
    (S(ComponentA) | S(ComponentB) | S(ComponentC)).execute_all(ctx)
    ```

    请严格以标准 JSON 格式返回（不要包含 Markdown 块标记）：
    {{
        "pipeline_ids": ["组件ID_1", "组件ID_2"],
        "code": "完整的 Python pipeline 脚本代码（只包含代码，不含 ```python 标记）"
    }}
    """

    max_attempts = 3
    feedback = ""
    for attempt in range(1, max_attempts + 1):
        try:
            result = call_llm(
                system_prompt, f"指令: {args.cmd}{feedback}", json_mode=True, temperature=0.1,
                progress_callback=getattr(args, "progress_callback", None),
                progress_label=f"Composing pipeline: {args.name or args.cmd[:40]}",
            )

            pipeline_ids = result.get("pipeline_ids", [])
            generated_code = result.get("code", "")

            print(f"🔗 [AI 编排] 执行链: {' → '.join(pipeline_ids) if pipeline_ids else '(空)'}")

            if not generated_code:
                if attempt < max_attempts:
                    print(f"   ⚠️  AI 未返回有效代码，第 {attempt}/{max_attempts} 次重试...")
                    continue
                print("❌ AI 未返回有效代码，已重试 3 次仍失败。")
                return

            violations = validate_pipeline_contract(generated_code)
            known_services = {
                bean.get("id") for bean in beans.get("beans", [])
                if bean.get("category") == "service"
            }
            if not pipeline_ids:
                violations.append("pipeline_ids 不能为空，且必须使用组件池中的精确 Service ID")
            for component_id in pipeline_ids:
                if component_id not in known_services:
                    violations.append(
                        f"Pipeline 组件 ID '{component_id}' 不存在或不是 Service；可用 ID："
                        + ", ".join(sorted(item for item in known_services if item))
                    )
            contract = analyze_pipeline_contracts(pipeline_ids, beans.get("beans", []))
            for issue in contract["issues"]:
                if issue["code"] == "semantic_field_drift":
                    violations.append(
                        f"疑似同义字段漂移：{issue['component']} 需要 '{issue['field']}'，"
                        f"上游提供 '{issue['produced_field']}'；请复用上游字段名"
                    )
                elif issue["code"] == "contract_type_mismatch":
                    violations.append(
                        f"字段类型不兼容：{issue['component']}.{issue['field']} 需要 "
                        f"{issue['required']}，上游提供 {issue['produced']}"
                    )
                elif issue["code"] == "contract_schema_mismatch":
                    details = ", ".join(
                        f"{item['path']} ({item['produced']} -> {item['required']})"
                        for item in issue["schema_mismatches"]
                    )
                    violations.append(
                        f"嵌套 Schema 不兼容：{issue['component']}.{issue['field']}：{details}"
                    )
            if violations:
                if not request_repair(
                    violations, attempt, max_attempts,
                    interactive=not args.json,
                    auto_repair=getattr(args, "auto_repair", False),
                ):
                    return
                feedback = repair_feedback(violations)
                continue
            break

        except Exception as e:
            if attempt < max_attempts:
                print(f"   ⚠️  第 {attempt}/{max_attempts} 次失败 ({e})，重试...")
                continue
            print(f"❌ AI 编排失败: {e}")
            return

    print("🛡️  [生成预检通过] 代码语法、基础安全规则和 Pipeline 契约均有效；运行修复交由后续 Agent")

    # 保存 pipeline 文件
    name = args.name or _slugify(args.cmd)
    filepath = _save_pipeline(generated_code, name, args.cmd)
    print(f"💾 [Pipeline 已保存] {filepath}")

    # 注册到 routes.toml
    register_route(
        name=name,
        pipeline_path=filepath,
        description=args.cmd,
    )
    print(f"📋 [路由已注册] {name} → {filepath}")

    print(f"\n🎉 [compose 完成] Pipeline 已生成并注册。")
    print(f"   运行方式: 通过你的入口文件调用 PipelineRunner().run(\"{name}\", params)")
