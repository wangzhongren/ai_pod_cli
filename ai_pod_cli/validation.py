"""Static validation for AI-generated AIPod artifacts.

Validation deliberately does not import or execute generated modules.  AIPod's
generation phase must remain reviewable and side-effect free; generated code is
executed only when a developer runs a pipeline.
"""

import ast

from ai_pod_cli.security import validate_code


def request_repair(violations: list[str], attempt: int, max_attempts: int) -> bool:
    """Show validation feedback and ask whether it should be sent back to the LLM.

    The default is to repair, while non-interactive input safely cancels without
    writing a partial artifact.
    """
    print(f"🛡️  [生成预检失败] 发现 {len(violations)} 处问题:")
    for violation in violations:
        print(f"   ❌ {violation}")

    if attempt >= max_attempts:
        print(f"   已达到 {max_attempts} 次生成上限，未修改项目文件。")
        return False

    try:
        answer = input("   是否将这些问题反馈给 AI 并重试？[Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n   已取消，未修改项目文件。")
        return False
    return answer in ("", "y", "yes", "是")


def repair_feedback(violations: list[str]) -> str:
    """Create a precise correction instruction for the next generation attempt."""
    details = "\n".join(f"- {violation}" for violation in violations)
    return (
        "\n\n上一轮生成未通过本地预检。请只返回修正后的完整 JSON，"
        "并确保 code 字段的代码解决以下问题：\n"
        f"{details}"
    )


def validate_component_contract(code: str, class_name: str, category: str) -> list[str]:
    """Return violations when generated component code misses its AIPod contract."""
    violations = validate_code(code)
    if violations:
        return violations

    tree = ast.parse(code)
    component = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name),
        None,
    )
    if component is None:
        return [f"未找到名称为 '{class_name}' 的组件类"]

    if category == "service":
        execute = next(
            (node for node in component.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "execute"),
            None,
        )
        if execute is None:
            return [f"service 组件 '{class_name}' 必须定义 execute(self, ctx) 方法"]

    return []


def validate_pipeline_contract(code: str) -> list[str]:
    """Return violations when generated pipeline code lacks its required entry point."""
    violations = validate_code(code)
    if violations:
        return violations

    tree = ast.parse(code)
    if not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run" for node in tree.body):
        return ["Pipeline 必须定义 run(ctx) 函数"]
    return []
