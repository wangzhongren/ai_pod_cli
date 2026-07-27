"""Run the checked-in AIPod Todo example."""

import sys

from ai_pod_cli.runner import PipelineRunner


def main() -> None:
    title = " ".join(sys.argv[1:]) or "Buy groceries"
    result = PipelineRunner().run("create_todo", {"title": title})
    print(result["data"]["display"])


if __name__ == "__main__":
    main()
