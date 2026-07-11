"""`init` command — initialize project, optionally generate entry point via AI."""

import json
import os
import subprocess
import sys

from ai_pod_cli.config import (
    CONFIG_FILE, CONFIG_TOML, ROUTES_TOML,
    MODULES_DIR, PIPELINES_DIR, init_config_if_not_exists,
)


def handle_init(args):
    """【init 命令】初始化项目：创建目录、配置、可选 AI 生成入口文件"""

    created = []
    skipped = []

    # 1. 创建 modules/ 目录 + 配套文件
    if not os.path.exists(MODULES_DIR):
        os.makedirs(MODULES_DIR)
        created.append(f"📁 目录 {MODULES_DIR}/")
    else:
        skipped.append(f"📁 目录 {MODULES_DIR}/ (已存在)")

    modules_req = os.path.join(MODULES_DIR, "requirements.txt")
    if not os.path.exists(modules_req):
        with open(modules_req, "w", encoding="utf-8") as f:
            f.write("# AI 生成模块的第三方依赖\n# 由 create 命令自动追加，也可手动编辑\n")
        created.append(f"📄 {modules_req}")
    else:
        skipped.append(f"📄 {modules_req} (已存在)")

    modules_init = os.path.join(MODULES_DIR, "__init__.py")
    if not os.path.exists(modules_init):
        with open(modules_init, "w", encoding="utf-8") as f:
            f.write('"""AI-generated components live here."""\n')
        created.append(f"📄 {modules_init}")
    else:
        skipped.append(f"📄 {modules_init} (已存在)")

    # 2. 创建 pipelines/ 目录
    if not os.path.exists(PIPELINES_DIR):
        os.makedirs(PIPELINES_DIR)
        created.append(f"📁 目录 {PIPELINES_DIR}/")
    else:
        skipped.append(f"📁 目录 {PIPELINES_DIR}/ (已存在)")

    # 3. 创建 config.toml + routes.toml + beans_config.json
    toml_exists = os.path.exists(CONFIG_TOML)
    routes_exists = os.path.exists(ROUTES_TOML)
    beans_exists = os.path.exists(CONFIG_FILE)

    if not toml_exists or not routes_exists or not beans_exists:
        init_config_if_not_exists()

    if not toml_exists:
        created.append(f"📄 {CONFIG_TOML} (项目配置，组件通过 ConfigStore 读取)")
    else:
        skipped.append(f"📄 {CONFIG_TOML} (已存在)")

    if not routes_exists:
        created.append(f"📄 {ROUTES_TOML} (路由配置，compose 自动注册 pipeline)")
    else:
        skipped.append(f"📄 {ROUTES_TOML} (已存在)")

    if not beans_exists:
        created.append(f"📄 {CONFIG_FILE} (含 ConfigStore, DbClient, SmsSender)")
    else:
        skipped.append(f"📄 {CONFIG_FILE} (已存在)")

    # 4. 创建 .env.example + .env
    env_example = ".env.example"
    if not os.path.exists(env_example):
        with open(env_example, "w", encoding="utf-8") as f:
            f.write(
                "# AI Pod CLI 大模型配置\n"
                "# 复制本文件为 .env 并填入真实值\n"
                "\n"
                "OPENAI_API_KEY=sk-your-api-key-here\n"
                "OPENAI_BASE_URL=https://api.openai.com/v1\n"
                "OPENAI_MODEL=deepseek-chat\n"
            )
        created.append(f"📄 {env_example}")
    else:
        skipped.append(f"📄 {env_example} (已存在)")

    env_file = ".env"
    if not os.path.exists(env_file):
        import shutil
        shutil.copyfile(env_example, env_file)
        created.append(f"🔒 {env_file} (从 .env.example 复制，请编辑填入真实 Key)")
    else:
        skipped.append(f"🔒 {env_file} (已存在)")

    # 5. .gitignore
    gitignore = ".gitignore"
    if os.path.exists(gitignore):
        with open(gitignore, "r", encoding="utf-8") as f:
            content = f.read()
        if ".env" not in content:
            with open(gitignore, "a", encoding="utf-8") as f:
                f.write("\n# dotenv 本地配置\n.env\n")
    else:
        with open(gitignore, "w", encoding="utf-8") as f:
            f.write("# dotenv 本地配置\n.env\n")
        created.append(f"📄 {gitignore}")

    # 6. 安装依赖
    if args.install_deps:
        print("\n📦 [初始化] 正在安装 Python 依赖 (openai, injector, python-dotenv, tomlkit)...")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install",
                 "openai", "injector", "python-dotenv", "tomlkit", "-q"],
            )
            created.append("📦 依赖 openai, injector, python-dotenv, tomlkit")
        except subprocess.CalledProcessError as e:
            print(f"   ⚠️  依赖安装失败: {e}")
    else:
        skipped.append("📦 依赖安装 (跳过，使用 --install-deps 启用)")

    # 7. AI 生成入口文件（如果提供了描述）
    desc = getattr(args, "desc", "")
    if desc:
        entry_info = _generate_entry(desc)
        if entry_info:
            entry_file, extra_deps = entry_info
            created.append(f"🚀 入口文件 {entry_file} (AI 生成)")
            if extra_deps:
                with open(modules_req, "a", encoding="utf-8") as f:
                    for dep in extra_deps:
                        f.write(f"{dep}\n")
                created.append(f"📦 额外依赖: {', '.join(extra_deps)}")

    # 输出汇总
    print("\n🎉 [初始化完成] 项目就绪！\n")

    if created:
        print("   ✅ 已创建:")
        for item in created:
            print(f"      {item}")

    if skipped:
        print("\n   ⏭️  已跳过:")
        for item in skipped:
            print(f"      {item}")

    print(f"\n   下一步:")
    print(f"   1. python -m ai_pod_cli create --category entity --name <名称> --desc \"<描述>\"")
    print(f"   2. python -m ai_pod_cli compose \"<业务指令>\"")
    if desc:
        print(f"   3. python {entry_info[0] if entry_info else '<entry>'}  (启动项目)")


def _generate_entry(desc: str) -> tuple[str, list[str]] | None:
    """Use AI to generate the project entry point based on description."""
    from ai_pod_cli.client import call_llm
    from ai_pod_cli.security import validate_code

    if not os.environ.get("OPENAI_API_KEY"):
        print("⚠️  OPENAI_API_KEY 未配置，跳过入口文件生成。")
        return None

    print(f"\n🚀 [初始化] AI 正在根据描述生成项目入口...")
    print(f"📝 [描述] {desc}")

    system_prompt = f"""
    你是一个资深的 Python 架构师。当前系统是一个基于 Python `injector` 框架的 IoC/DI 容器低代码平台。
    包名为 `ai_pod_cli`，已安装并可 import。

    你的任务是：根据人类的项目描述，自主完成以下决策和代码生成：

    【你需要自主决定的事项】：
    1. 判断项目类型（Flask Web API、FastAPI 微服务、CLI 工具、RabbitMQ 消费者、Kafka 流处理、APScheduler 定时任务、WebSocket 服务等）。
    2. 决定入口文件名（如 app.py、main.py、consumer.py、scheduler.py、server.py 等）。
    3. 生成完整的、可直接运行的入口文件代码。

    【核心代码规范】：
    - 必须使用 `from ai_pod_cli.runner import PipelineRunner` 来加载和执行 pipeline。
    - PipelineRunner 的 API：
      - runner = PipelineRunner()  — 自动读取 routes.toml
      - runner.route_names()  — 返回所有路由名称列表
      - runner.run(route_name, params_dict)  — 执行指定路由的 pipeline，返回结果 dict
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
