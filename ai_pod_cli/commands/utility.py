"""Inspect and register reusable utility classes without invoking a model."""

import json
from pathlib import Path

from ai_pod_cli.utilities import list_utilities, read_utility, write_utility


def handle_utility(args):
    try:
        if args.action == "list":
            result = {"utilities": list_utilities(args.project_root, args.query)}
        elif args.action == "read":
            if not args.name:
                raise ValueError("utility read requires a name")
            result = read_utility(args.name, args.project_root)
        else:
            if not args.name or not args.file or not args.cases:
                raise ValueError("utility write requires a name, --file and --cases")
            verify = list(args.verify or [])
            if verify and verify[0] == "--":
                verify = verify[1:]
            result = write_utility(
                args.name, Path(args.file).read_text(encoding="utf-8"), args.description,
                json.loads(Path(args.cases).read_text(encoding="utf-8")),
                expected_sha256=args.expected_sha256, project_root=args.project_root,
                allow_update=bool(args.expected_sha256), verify_command=verify or None,
            )
    except (OSError, ValueError, TypeError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False))
        raise SystemExit(1) from error
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result
