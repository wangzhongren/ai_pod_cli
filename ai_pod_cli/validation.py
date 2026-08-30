"""Static validation for AI-generated AIPod artifacts.

Validation deliberately does not import or execute generated modules.  AIPod's
generation phase must remain reviewable and side-effect free; generated code is
executed only when a developer runs a pipeline.
"""

import ast
import re

from ai_pod_cli.security import validate_code
from ai_pod_cli.contracts import normalize_type, semantic_field_similarity


_CONTROL_OUTPUT_KEYS = {"status", "error", "message", "reason", "ok"}


def _contains_model_ref(spec) -> bool:
    if not isinstance(spec, dict):
        return False
    if isinstance(spec.get("model"), str) and spec["model"].strip():
        return True
    return any(_contains_model_ref(value) for value in spec.values())


def _is_valid_boundary_schema(spec) -> bool:
    """Allow scalars, Models, and explicitly typed arrays of those values."""
    scalar_types = {"any", "str", "bool", "int", "float", "datetime", "datetime.datetime", "none", "null"}
    if isinstance(spec, dict) and spec.get("model"):
        return True
    field_type = normalize_type(spec)
    if all(item in scalar_types for item in field_type.split("|")):
        return True
    if field_type == "list" and isinstance(spec, dict) and "items" in spec:
        return _is_valid_boundary_schema(spec["items"])
    if field_type == "dict" and isinstance(spec, dict):
        additional = spec.get("additionalProperties")
        return additional is not None and _is_valid_boundary_schema(additional)
    return False


def extract_component_fields(code: str) -> dict[str, list[str]]:
    """Extract literal data reads/writes from a generated Service without running it."""
    tree = ast.parse(code)
    param_aliases = set()
    reads: set[str] = set()
    writes: set[str] = set()
    dynamic: set[str] = set()

    def literal_key(node):
        return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None

    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = [target.id for target in targets if isinstance(target, ast.Name)]
        if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name) and value.value.id == "ctx" and value.attr == "params":
            param_aliases.update(names)

    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):
            owner = node.value
            key = literal_key(node.slice)
            if isinstance(owner, ast.Attribute) and isinstance(owner.value, ast.Name) and owner.value.id == "ctx" and owner.attr == "params":
                (reads if key else dynamic).add(key or "ctx.params[]")
            elif isinstance(owner, ast.Name) and owner.id in param_aliases:
                (reads if key else dynamic).add(key or f"{owner.id}[]")
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner, method = node.func.value, node.func.attr
        key = literal_key(node.args[0]) if node.args else None
        is_ctx = isinstance(owner, ast.Name) and owner.id == "ctx"
        is_known_data = isinstance(owner, ast.Name) and owner.id in param_aliases
        if method == "get" and (is_ctx or is_known_data):
            (reads if key else dynamic).add(key or "dynamic get")
        if method == "set" and is_ctx:
            (writes if key else dynamic).add(key or "dynamic set")

    return {
        "reads": sorted(reads), "writes": sorted(writes), "dynamic": sorted(dynamic),
    }


def request_repair(
    violations: list[str], attempt: int, max_attempts: int, *,
    interactive: bool = True, auto_repair: bool = False,
) -> bool:
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

    if auto_repair:
        print("   Studio 将校验问题反馈给 AI 并自动重试。")
        return True

    if not interactive:
        print("   Agent JSON 模式不会等待交互确认，未修改项目文件。")
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
        f"{details}\n\n"
        "Contract 修复规则（必须照此输出 JSON 对象，不要写类型说明字符串）：\n"
        "- 单个 Model：{\"model\":\"modules.models.<module>.<Class>\"}\n"
        "- Model 数组：{\"type\":\"array\",\"items\":{\"model\":"
        "\"modules.models.<module>.<Class>\"}}\n"
        "- 标量数组：{\"type\":\"array\",\"items\":{\"type\":\"str\"}}\n"
        "- 动态 Map：{\"type\":\"object\",\"additionalProperties\":{\"type\":\"float\"}}\n"
        "- 禁止使用 \"list\"、\"list[Foo]\"、\"Foo — description\" 代替上述结构。\n"
        "- code 实际写入 ctx 的值必须满足该结构及冻结 Model 的每个字段类型。"
    )


def validate_component_contract(
    code: str, class_name: str, category: str,
    inputs: dict | None = None, outputs: dict | None = None,
    methods: dict | None = None,
) -> list[str]:
    """Return violations when generated component code misses its AIPod contract."""
    violations = validate_code(code)
    if violations:
        return violations

    tree = ast.parse(code)
    if any(
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "ctx"
        and node.attr == "output"
        for node in ast.walk(tree)
    ):
        violations.append(
            "PipelineContext 不存在 ctx.output；读取请使用 ctx.get(key)，写入请使用 ctx.set(key, value)"
        )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(alias.name == "ConfigStore" for alias in node.names):
            if node.module != "ai_pod_cli.config_store":
                violations.append(
                    "ConfigStore 必须使用 `from ai_pod_cli.config_store import ConfigStore` 导入"
                )
        if isinstance(node, ast.ImportFrom) and any(alias.name == "ModelRepository" for alias in node.names):
            if node.module != "ai_pod_cli.repository":
                violations.append(
                    "ModelRepository 必须使用 `from ai_pod_cli.repository import ModelRepository` 导入"
                )
    if violations:
        return list(dict.fromkeys(violations))
    component = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name),
        None,
    )
    if component is None:
        return [f"未找到名称为 '{class_name}' 的组件类"]

    if category == "model":
        bases = {
            base.id for base in component.bases if isinstance(base, ast.Name)
        }
        if "Model" not in bases:
            violations.append(f"model '{class_name}' 必须继承 ai_pod_cli.Model")
        # Both value objects and persistent entities are first-class Models.
        # Only persistent Models opt into SQLModel mapping with ``table=True``.
        return list(dict.fromkeys(violations))

    if category == "provider":
        # Provider APIs may return opaque infrastructure handles (sessions,
        # engines, clients). Model-specific enforcement is registry-aware and
        # therefore happens in the Pod generation loop.
        return list(dict.fromkeys(violations))

    if category == "service":
        sql_pattern = re.compile(
            r"^\s*(?:CREATE\s+(?:TABLE|INDEX)|INSERT\s+INTO|SELECT\s+.+\s+FROM|"
            r"UPDATE\s+\w+\s+SET|DELETE\s+FROM|ALTER\s+TABLE|DROP\s+(?:TABLE|INDEX))\b",
            re.IGNORECASE | re.DOTALL,
        )
        for node in ast.walk(component):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and sql_pattern.search(node.value):
                violations.append(
                    "Service 禁止编写原始 SQL；请注入 ModelRepository 并使用 save/get/list/find/delete"
                )
        execute = next(
            (node for node in component.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "execute"),
            None,
        )
        if execute is None:
            return [f"service 组件 '{class_name}' 必须定义 execute(self, ctx) 方法"]

        if inputs is not None or outputs is not None:
            actual = extract_component_fields(code)
            declared_inputs = set((inputs or {}).keys())
            declared_outputs = set((outputs or {}).keys())
            undeclared_reads = set(actual["reads"]) - declared_inputs
            undeclared_writes = set(actual["writes"]) - declared_outputs - _CONTROL_OUTPUT_KEYS
            for field in sorted(undeclared_reads):
                candidate = max(
                    declared_inputs,
                    key=lambda name: semantic_field_similarity(field, name),
                    default=None,
                )
                hint = (
                    f"；疑似应复用已声明字段 '{candidate}'"
                    if candidate and semantic_field_similarity(field, candidate) >= 0.86 else ""
                )
                violations.append(f"源码读取了未在 inputs 声明的字段 '{field}'{hint}")
            for field in sorted(undeclared_writes):
                candidate = max(
                    declared_outputs,
                    key=lambda name: semantic_field_similarity(field, name),
                    default=None,
                )
                hint = (
                    f"；疑似应复用已声明字段 '{candidate}'"
                    if candidate and semantic_field_similarity(field, candidate) >= 0.86 else ""
                )
                violations.append(f"源码写入了未在 outputs 声明的字段 '{field}'{hint}")
            if actual["dynamic"]:
                violations.append(
                    "源码使用了无法静态确认的数据字段：" + ", ".join(actual["dynamic"])
                )

    return list(dict.fromkeys(violations))


def validate_pipeline_contract(code: str) -> list[str]:
    """Return violations when generated pipeline code lacks its required entry point."""
    violations = validate_code(code)
    if violations:
        return violations

    tree = ast.parse(code)
    if not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run" for node in tree.body):
        return ["Pipeline 必须定义 run(ctx) 函数"]
    violations = []
    component_refs = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "S"
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        is_component_ref = (
            isinstance(owner, ast.Call)
            and isinstance(owner.func, ast.Name)
            and owner.func.id == "S"
        ) or (isinstance(owner, ast.Name) and owner.id in component_refs)
        if node.func.attr == "execute" and is_component_ref:
            violations.append(
                "S(Component) 返回运行时组件引用，只能调用 execute(ctx) 或 execute_all(ctx)；"
                "业务参数必须先写入 PipelineContext"
            )
        if node.func.attr == "exit" and isinstance(owner, ast.Name) and owner.id == "sys":
            violations.append("Pipeline 不得调用 sys.exit()；失败应写入 PipelineContext 并返回结果")
    return list(dict.fromkeys(violations))


def validate_entry_contract(code: str, route_names: list[str] | None = None) -> list[str]:
    """Validate an application entry without executing or changing stable artifacts."""
    violations = validate_code(code, allow_file_io=True)
    if violations:
        return violations

    tree = ast.parse(code)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    if not any(
        isinstance(node.func, ast.Name) and node.func.id == "build_container"
        for node in calls
    ):
        violations.append("入口必须通过 build_container(load_beans()) 构建容器")

    run_routes = []
    for node in calls:
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "run":
            continue
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            run_routes.append(node.args[0].value)
    if route_names and run_routes:
        unknown = sorted(set(run_routes) - set(route_names))
        for name in unknown:
            violations.append(f"入口调用了未注册的 Pipeline 路由 '{name}'")

    return list(dict.fromkeys(violations))
