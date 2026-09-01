"""CLI entry point — argparse setup and command dispatch."""

import argparse
import sys

from ai_pod_cli.config import init_config_if_not_exists
from ai_pod_cli.agent_output import execute_json_command
from ai_pod_cli.commands.add import handle_add
from ai_pod_cli.commands.compose import handle_compose
from ai_pod_cli.commands.create import handle_create
from ai_pod_cli.commands.entry import handle_entry
from ai_pod_cli.commands.env import handle_config
from ai_pod_cli.commands.init import handle_init
from ai_pod_cli.commands.inspect import handle_inspect
from ai_pod_cli.commands.interface import handle_interface
from ai_pod_cli.commands.pod import handle_pod
from ai_pod_cli.commands.run import handle_run
from ai_pod_cli.commands.studio import handle_studio
from ai_pod_cli.commands.visualize import handle_visualize
from ai_pod_cli.commands.verify import handle_verify


def main():
    """Main CLI entry point for the AI Pod engine."""
    # 从 .env 文件加载环境变量（优先于系统环境变量）
    from dotenv import load_dotenv
    load_dotenv()

    # 从全局配置补充（~/.aipod/config.toml）
    _apply_global_env()

    # Windows 终端 GBK 兼容：强制 UTF-8 输出
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
        sys.stderr.reconfigure(encoding="utf-8")

    # init/config 命令不要求 beans_config.json 存在
    skip_init_cmds = ("init", "config", "studio")
    if len(sys.argv) > 1 and sys.argv[1] not in skip_init_cmds:
        init_config_if_not_exists()

    parser = argparse.ArgumentParser(
        description="AIPod — AI-native application framework",
        prog="aipod",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. init
    init_parser = subparsers.add_parser("init", help="Initialize project skeleton (no AI)")
    init_parser.add_argument("--install-deps", action="store_true", help="Auto-install Python dependencies")

    # 2. create
    create_parser = subparsers.add_parser("create", help="AI generates a component")
    create_parser.add_argument("--category", choices=["model", "service", "provider"], required=True, help="Project element category")
    create_parser.add_argument("--name", required=True, help="Component name (must match class name)")
    create_parser.add_argument("--desc", required=True, help="Component description for AI")
    create_parser.add_argument("--json", action="store_true", help="Emit a machine-readable command result")

    # 3. add
    add_parser = subparsers.add_parser("add", help="Register a hand-written component")
    add_parser.add_argument("--category", choices=["model", "service", "provider"], default="provider", help="Project element category (default: provider)")
    add_parser.add_argument("--name", required=True, help="Component name")
    add_parser.add_argument("--class-path", required=True, help="Full class path, e.g. mypackage.module.MyClass")
    add_parser.add_argument("--desc", required=True, help="Component description")

    # 4. compose
    compose_parser = subparsers.add_parser("compose", help="AI plans and generates a pipeline (no execution)")
    compose_parser.add_argument("cmd", nargs="?", default="", help="Natural language instruction (AI plans the execution chain)")
    compose_parser.add_argument("--name", default="", help="Pipeline file name (auto-generated from instruction if omitted)")
    compose_parser.add_argument("--list", action="store_true", help="List all saved pipelines")
    compose_parser.add_argument("--json", action="store_true", help="Emit a machine-readable command result")

    # 5. pod
    pod_parser = subparsers.add_parser("pod", help="AI decomposes a requirement into components + pipelines + entry")
    pod_parser.add_argument("desc", nargs="?", default="", help="Feature or system description")
    pod_parser.add_argument("--file", "-f", default="", help="Read requirements from a file (supports long specs)")
    pod_parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation, generate immediately")
    pod_parser.add_argument("--json", action="store_true", help="Emit a machine-readable command result")
    pod_parser.add_argument(
        "--stage", choices=("auto", "models", "providers", "services", "pipelines", "interfaces"),
        default="", help="Modify this existing layer and rebuild its downstream layers",
    )

    # 6. config
    config_parser = subparsers.add_parser("config", help="Manage global configuration (~/.aipod/config.toml)")
    config_parser.add_argument("action", nargs="?", default="list", choices=["list", "set", "get", "remove", "path"], help="Config action")
    config_parser.add_argument("key", nargs="?", default="", help="Config key (for set/get/remove)")
    config_parser.add_argument("value", nargs="?", default="", help="Config value (for set)")

    # 7. entry
    entry_parser = subparsers.add_parser("entry", help="AI generates a project entry point file")
    entry_parser.add_argument("desc", help="Project description (AI decides tech stack and generates entry file)")

    # 8. run
    run_parser = subparsers.add_parser("run", help="Run a route and persist an execution trace")
    run_parser.add_argument("route", help="Route name registered in routes.toml")
    run_parser.add_argument("--params", default="{}", help="Route parameters as a JSON object")
    run_parser.add_argument("--json", action="store_true", help="Print the complete execution trace as JSON")

    # 9. inspect
    inspect_parser = subparsers.add_parser("inspect", help="Inspect the project for AI agents")
    inspect_parser.add_argument("target", nargs="?", default="project", choices=["project", "components", "pipelines", "component", "pipeline", "runs", "run"], help="Project view to inspect")
    inspect_parser.add_argument("name", nargs="?", default="", help="Component, pipeline, or run id")
    inspect_parser.add_argument("--json", action="store_true", help="Print the stable Agent Project Model as JSON")
    inspect_parser.add_argument("--summary", action="store_true", help="Return only compact project counts and validation")

    # 10. visualize
    visualize_parser = subparsers.add_parser("visualize", help="Generate an interactive project graph")
    visualize_parser.add_argument("--output", "-o", default="aipod-graph.html", help="Output HTML file path")
    visualize_parser.add_argument("--open", action="store_true", help="Open the graph in the default browser")

    # 11. studio
    studio_parser = subparsers.add_parser("studio", help="Open the native AIPod Studio desktop UI")
    studio_parser.add_argument("path", nargs="?", default=".", help="AIPod project directory (default: current directory)")
    studio_parser.add_argument("--debug", action="store_true", help="Enable webview developer tools")

    # 12. interface
    interface_parser = subparsers.add_parser(
        "interface", help="Run AI-generated Interface adapters",
    )
    interface_parser.add_argument("action", choices=("list", "run", "smoke", "install", "uninstall"))
    interface_parser.add_argument("target", nargs="?", default="", help="Interface name or manifest path")
    interface_parser.add_argument("--payload", default="{}", help="JSON event payload for run")
    interface_parser.add_argument("--project-root", default=".", help="AIPod project root")
    interface_parser.add_argument(
        "inputs", nargs=argparse.REMAINDER,
        help="Raw Adapter arguments after --; overrides --payload",
    )

    # 13. verify
    verify_parser = subparsers.add_parser("verify", help="Run structural checks and an optional real project command")
    verify_parser.add_argument("--timeout", type=int, default=120, help="Command timeout in seconds")
    verify_parser.add_argument("--json", action="store_true", help="Emit structured repair evidence")
    verify_parser.add_argument("check", nargs=argparse.REMAINDER, help="Command to run after --, e.g. -- python app.py --smoke")

    args = parser.parse_args()

    handlers = {
        "init": handle_init,
        "create": handle_create,
        "add": handle_add,
        "compose": handle_compose,
        "pod": handle_pod,
        "config": handle_config,
        "entry": handle_entry,
        "run": handle_run,
        "inspect": handle_inspect,
        "interface": handle_interface,
        "visualize": handle_visualize,
        "studio": handle_studio,
        "verify": handle_verify,
    }
    if args.command in {"create", "compose", "pod"} and args.json:
        execute_json_command(args.command, handlers[args.command], args)
    elif args.command == "init":
        handle_init(args)
    elif args.command == "create":
        handle_create(args)
    elif args.command == "add":
        handle_add(args)
    elif args.command == "compose":
        handle_compose(args)
    elif args.command == "pod":
        handle_pod(args)
    elif args.command == "config":
        handle_config(args)
    elif args.command == "entry":
        handle_entry(args)
    elif args.command == "run":
        handle_run(args)
    elif args.command == "inspect":
        handle_inspect(args)
    elif args.command == "interface":
        handle_interface(args)
    elif args.command == "visualize":
        handle_visualize(args)
    elif args.command == "studio":
        handle_studio(args)
    elif args.command == "verify":
        handle_verify(args)


def _apply_global_env():
    """Load global config from ~/.aipod/config.toml and inject into os.environ.

    Only sets variables that are NOT already set (local .env takes priority).
    """
    import os
    try:
        from ai_pod_cli.commands.env import get_global_env, record_global_config_load_error
        global_env = get_global_env()
        record_global_config_load_error(None)
        for key, value in global_env.items():
            if not os.environ.get(key):
                os.environ[key] = value
    except Exception as error:
        # Commands that require a model can now distinguish an inaccessible
        # global config from a genuinely missing API key.
        from ai_pod_cli.commands.env import record_global_config_load_error
        record_global_config_load_error(error)
