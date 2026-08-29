"""Launch the native AIPod Studio desktop application."""

from ai_pod_cli.studio import StudioError, launch_studio


def handle_studio(args) -> None:
    try:
        launch_studio(args.path, debug=args.debug)
    except StudioError as error:
        print(f"❌ 无法启动 AIPod Studio：{error}")
        raise SystemExit(1) from error
