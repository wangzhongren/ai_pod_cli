"""Entry point file generator — shared by init and pod commands."""

import os

from ai_pod_cli.client import call_llm
from ai_pod_cli.security import validate_code


def generate_entry(desc: str, routes_map: dict[str, str] | None = None) -> tuple[str, list[str]] | None:
    """Use AI to generate a project entry point based on description.

    Args:
        desc: Project description (AI decides tech stack and generates entry file).
        routes_map: Optional dict of {route_name: description} from routes.toml.
                    When provided, the AI must use these exact route names.

    Returns:
        Tuple of (entry_filename, extra_deps) or None if generation failed.
    """
    if not os.environ.get("OPENAI_API_KEY"):
        print("⚠️  OPENAI_API_KEY 未配置，跳过入口文件生成。")
        print("   请先设置大模型配置（一次设置，所有项目共用）：")
        print("   aipod config set OPENAI_API_KEY sk-your-key")
        print("   aipod config set OPENAI_BASE_URL https://api.deepseek.com/v1")
        print("   aipod config set OPENAI_MODEL deepseek-chat")
        print("   然后重新运行: aipod init \"描述\"")
        return None

    print(f"\n🚀 [入口生成] AI 正在根据描述生成项目入口...")
    print(f"📝 [描述] {desc[:200]}{'...' if len(desc) > 200 else ''}")

    # 构建路由上下文（如果提供）
    routes_context = ""
    if routes_map:
        routes_lines = []
        for name, desc in routes_map.items():
            desc_str = f" — {desc}" if desc else ""
            routes_lines.append(f"     - {name}{desc_str}")
        if routes_lines:
            routes_context = f"""
    当前 routes.toml 中**已注册**的路由（你的入口文件必须使用这些精确名称调用 runner.run()）：
    {chr(10).join(routes_lines)}
    同时建议使用 runner.route_names() 动态列出所有路由，避免硬编码导致不一致。
    """
        else:
            routes_context = """
    建议使用 runner.route_names() 动态列出所有可用路由，避免硬编码路由名。
    """
    else:
        routes_context = """
    建议使用 runner.route_names() 动态列出所有可用路由，避免硬编码路由名。
    """

    system_prompt = f"""
    你是一个资深的 Python 架构师。当前系统是一个基于 Python `injector` 框架的 IoC/DI 容器低代码平台。
    包名为 `ai_pod_cli`，已安装并可 import。

    你的任务是：根据人类的项目描述，自主完成以下决策和代码生成：

    【你需要自主决定的事项】：
    1. 判断项目类型（Flask Web API、FastAPI 微服务、CLI 工具、RabbitMQ 消费者、Kafka 流处理、APScheduler 定时任务、WebSocket 服务等）。
    2. 决定入口文件名（如 app.py、main.py、consumer.py、scheduler.py、server.py 等）。
    3. 生成完整的、可直接运行的入口文件代码。
    {routes_context}
    【核心代码规范】：
    - 入口文件通过容器获取一切，不要手动 new 任何组件：
      from ai_pod_cli.config import load_config
      from ai_pod_cli.container import build_container
      config = load_config()
      container = build_container(config)
    - PipelineRunner 已注册为容器 Bean，通过容器获取：
      runner = container.get(PipelineRunner)
      runner.route_names()  — 列出所有路由
      runner.run("route_name", {"key": "value"})  — 执行管线
    - 禁止手动 new PipelineRunner()，必须通过 container.get() 获取。
    - 入口文件不需要 import 任何 modules/ 下的底层 Bean，只通过管线完成业务。
    - 生成的代码必须是完整可运行的，包含所有必要的 import。
    - 加上清晰的中文注释。
    - 如果是 Web 框架，包含路由示例。
    - 如果是消息队列，包含消费循环。
    - 如果是 CLI，包含参数解析。

    请严格以标准 JSON 格式返回（不要包含 Markdown 块标记）：
    {{
        "project_type": "你判断的项目类型名称",
        "entry_file": "你决定的入口文件名",
        "code": "完整的入口文件 Python 源代码字符串",
        "extra_deps": ["该项目类型需要的额外 pip 包名列表，不包括 ai_pod_cli 已有的"]
    }}
    """

    try:
        result = call_llm(system_prompt, f"项目描述: {desc}", json_mode=True, temperature=0.2)

        entry_file = result.get("entry_file", "main.py")
        generated_code = result.get("code", "")
        extra_deps = result.get("extra_deps", [])

        if not generated_code:
            print("❌ AI 未返回有效代码，跳过入口生成。")
            return None

        # 安全检查
        violations = validate_code(generated_code, allow_file_io=True)
        if violations:
            print(f"🛡️  [安全检查失败] 入口文件包含 {len(violations)} 处违规:")
            for v in violations:
                print(f"   ❌ {v}")
            return None

        # 写入入口文件
        if os.path.exists(entry_file):
            print(f"⚠️  {entry_file} 已存在，跳过写入。")
            return entry_file, extra_deps

        with open(entry_file, "w", encoding="utf-8") as f:
            f.write(generated_code)
        print(f"✍️  [入口生成成功] {entry_file}")

        return entry_file, extra_deps

    except Exception as e:
        print(f"❌ 入口生成失败: {e}")
        return None
