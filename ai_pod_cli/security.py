"""AST-based security validation for AI-generated code.

This is a LOCAL developer tool — module imports are NOT restricted.
The security check only catches genuinely dangerous patterns that could
be exploited via prompt injection:
  - eval() / exec() — arbitrary code execution
  - __import__() — dynamic import bypass
  - dunder chain access (__class__.__mro__.__subclasses__ etc.)
"""

import ast


# 只拦截真正的代码注入手段，不限制 import
BLOCKED_CALLS = {
    "eval", "exec", "compile",
    "__import__",
}

# 禁止的 dunder 属性访问（防止通过原型链逃逸沙箱）
BLOCKED_DUNDERS = {
    "__subclasses__", "__mro__", "__bases__",
    "__globals__", "__code__",
    "__builtins__", "__import__",
    "__loader__", "__spec__",
}


class SecurityError(Exception):
    """Raised when AI-generated code fails security validation."""

    def __init__(self, violations: list[str]):
        self.violations = violations
        detail = "\n".join(f"  - {v}" for v in violations)
        super().__init__(f"代码安全检查未通过，发现 {len(violations)} 处违规:\n{detail}")


def validate_code(code: str, **_kwargs) -> list[str]:
    """Validate AI-generated Python code for injection patterns.

    Args:
        code: The Python source code to validate.
        **_kwargs: Ignored (kept for backward compatibility with allow_file_io etc.).

    Returns:
        A list of violation descriptions (empty if code is safe).
    """
    violations = []

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"语法错误: {e}"]

    for node in ast.walk(tree):
        # 检查危险函数调用: eval(), exec(), __import__()
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in BLOCKED_CALLS:
                violations.append(
                    f"第 {node.lineno} 行: 禁止调用 '{func.id}()'（代码注入风险）"
                )

        # 检查 dunder 链访问（原型链逃逸）
        if isinstance(node, ast.Attribute) and node.attr in BLOCKED_DUNDERS:
            violations.append(
                f"第 {node.lineno} 行: 禁止访问 '.{node.attr}'（原型链逃逸风险）"
            )

    return violations


def assert_safe(code: str, **kwargs) -> None:
    """Validate code and raise SecurityError if unsafe."""
    violations = validate_code(code, **kwargs)
    if violations:
        raise SecurityError(violations)
