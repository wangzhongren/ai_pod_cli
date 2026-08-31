"""Interface generation with explicit, non-interactive proof commands."""

import os

from ai_pod_cli.client import call_llm
from ai_pod_cli.config import append_deps_to_requirements
from ai_pod_cli.pod.routes import load_routes_map
from ai_pod_cli.pod.state import set_stage_status
from ai_pod_cli.validation import (
    repair_feedback, request_repair, validate_entry_contract,
)


def generate_pod_entry(
    desc: str, generated: list[str], pipe_names: list[str], progress_callback=None,
    auto_repair: bool = False, interface: dict | None = None,
) -> tuple[str, list[str]] | None:
    """Pod 自己生成入口 prompt，包含本次生成的组件和管线的完整上下文。"""
    from ai_pod_cli.client import call_llm
    routes_map = load_routes_map()
    interface = interface or {}
    interface_name = str(interface.get("name", "application.py"))
    if not interface_name.endswith(".py"):
        interface_name += ".py"
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
    2. 入口文件名必须是 `{interface_name}`
    3. 生成完整代码
    4. 必须实现 `--smoke` 非交互验证模式：不启动长期服务、不连接外部系统，验证关键初始化后以 0 退出

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
        if entry_file != interface_name:
            violations.append(
                f"Interface 入口必须使用计划声明的文件名 '{interface_name}'"
            )
        if "--smoke" not in generated_code:
            violations.append("Interface 必须实现 --smoke 非交互验证模式")
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
        if "--smoke" not in existing_code:
            existing_violations.append("现有 Interface 未实现 --smoke 非交互验证模式")
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


def generate_interfaces(
    *, desc: str, interfaces: list[dict], reused: list[str], generated: list[str],
    args, decision_state: dict, stage: int, progress_callback=None,
) -> tuple[list[str], list[str]]:
    """Generate only declared Interfaces after all routes have been frozen."""
    entry_files: list[str] = []
    available_components = list(dict.fromkeys(reused + generated))
    available_pipelines = list(load_routes_map())
    print(f"\n🚀 [生成 Interfaces] {len(interfaces)} 个")
    for index, interface in enumerate(interfaces, 1):
        print(
            f"   [{index}/{len(interfaces)}] {interface.get('name', '')}"
            f" ({interface.get('kind', '')})"
        )
        entry_info = generate_pod_entry(
            desc, available_components, available_pipelines,
            progress_callback=progress_callback,
            auto_repair=bool(getattr(args, "auto_repair", False) or args.yes),
            interface=interface,
        )
        if not entry_info:
            set_stage_status(decision_state, stage, "in_progress")
            print("   ⛔ Interface 阶段失败，已验证的前四阶段保持冻结。")
            raise SystemExit(1)
        entry_file, extra_deps = entry_info
        entry_files.append(entry_file)
        if extra_deps:
            append_deps_to_requirements(extra_deps)
            print(f"   📦 额外依赖: {', '.join(extra_deps)}")
    return entry_files, available_components
