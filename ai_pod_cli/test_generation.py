"""Generate and persist component tests before requesting implementation source."""

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from ai_pod_cli.source_generation import _progress_options, generate_source


TEST_PLAN_PROMPT = """【先测试后实现】：
元数据必须包含非空 tests 数组，每项为
{"name":"test_specific_behavior","requirement":"明确的准备数据、调用参数和预期结果"}。
测试名称必须是唯一的 test_ 开头 Python 标识符。由需求推导测试，不根据实现猜测期望值。
覆盖正常行为、需求规定的拒绝/异常、边界及状态变化；异常用例还应验证没有不该发生的写入。
例如只允许管理员创建时，分别规划有效管理员成功、普通用户/缺失身份/无效账户被拒绝的测试。
配置、身份、数据库初始行、输入文件必须在测试里明确准备，框架不会自动猜测样本。
Provider 的外部系统用明确的依赖 Provider 替身隔离；当前被测组件必须真实执行。
这些测试将在实现生成前冻结，修复实现时不能更改测试或预期结果。
"""


def validate_test_plan(tests) -> list[str]:
    if not isinstance(tests, list) or not tests:
        return ["components.tests 必须是非空测试计划；先明确场景和预期，再生成实现"]
    errors, names = [], set()
    for case in tests:
        if not isinstance(case, dict):
            errors.append("每项测试必须包含 name 和 requirement")
            continue
        name = case.get("name", "")
        if not isinstance(name, str) or not re.fullmatch(r"test_[A-Za-z0-9_]+", name) or name in names:
            errors.append(f"测试名称无效或重复: {name}")
        else:
            names.add(name)
        if not isinstance(case.get("requirement"), str) or not case["requirement"].strip():
            errors.append(f"测试 {name} 缺少明确的 requirement")
    return errors


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def read_frozen_test(project_root, descriptor: dict) -> str:
    """Only allow the recorded, unchanged acceptance file within the project."""
    root = Path(project_root).resolve()
    path = Path(str(descriptor.get("path", "")))
    if path.is_absolute() or ".." in path.parts or path.parts[:2] != ("tests", "components"):
        raise ValueError("[AIPOD_TEST_INVALID] 冻结测试路径必须位于 tests/components/")
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise ValueError("[AIPOD_TEST_INVALID] 冻结测试丢失或路径越界")
    source = target.read_bytes().decode("utf-8")
    if hashlib.sha256(source.encode()).hexdigest() != descriptor.get("sha256"):
        raise ValueError("[AIPOD_TEST_INVALID] 冻结测试已被修改；必须显式修订需求和测试计划")
    return source


def prepare_component_tests(
    llm, system: str, user: str, source_path: str, *, component: dict,
    project_root, planned_tests=None, allow_revision: bool = False, **options,
) -> tuple[dict, dict, str]:
    """Freeze metadata and XML test source, reusing them across retries/resumes.

    Only an explicit new create/revision may replace a different specification.
    Old acceptance files remain on disk for review.
    """
    from ai_pod_cli.component_tests import SDK_PROMPT, validate_component_test_source

    root = Path(project_root).resolve()
    name = component["name"]
    if not re.fullmatch(r"[A-Za-z_]\w*", name, flags=re.ASCII):
        raise ValueError("Invalid component name")
    request_hash = _digest({"component": component, "tests": planned_tests})
    journal_path = root / ".aipod" / "component-tests.json"
    if not journal_path.resolve().is_relative_to(root):
        raise ValueError("[AIPOD_TEST_INVALID] 测试注册表路径越界")
    journal = json.loads(journal_path.read_text(encoding="utf-8")) if journal_path.exists() else {"version": 1, "components": {}}
    previous = journal["components"].get(name)
    if previous:
        if previous["request_hash"] == request_hash:
            # A retry of the same specification may never regenerate its oracle.
            if _digest({"request": request_hash, "metadata": previous["metadata"]}) != previous["test"]["spec_hash"]:
                raise ValueError("[AIPOD_TEST_INVALID] 冻结接口元数据已被修改")
            return previous["metadata"], previous["test"], read_frozen_test(root, previous["test"])
        if not allow_revision:
            raise ValueError("[AIPOD_TEST_SETUP] 组件需求已变化；请显式修订当前层后重新规划测试")
    metadata = llm(
        system + TEST_PLAN_PROMPT + "\n本轮只返回 JSON 元数据，不生成 code/content 源码。",
        user, json_mode=True, **_progress_options(options, "metadata"),
    )
    if not isinstance(metadata, dict):
        raise ValueError("Component metadata must be a JSON object")
    if "path" in metadata and metadata["path"] != source_path:
        raise ValueError("Component metadata path differs from the planned source path")
    metadata = {key: value for key, value in metadata.items() if key not in {"code", "content", "path"}}
    if planned_tests is not None:
        metadata["tests"] = planned_tests
    errors = validate_test_plan(metadata.get("tests"))
    registry_path = root / "beans_config.json"
    beans = json.loads(registry_path.read_text(encoding="utf-8")).get("beans", []) if registry_path.is_file() else []
    known = {bean["id"]: bean for bean in beans}
    dependencies = metadata.get("dependencies", [])
    if not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies):
        errors.append("dependencies 必须是 Bean ID 字符串数组")
    else:
        for dependency in dependencies:
            if dependency not in known or known[dependency].get("category") != "provider":
                errors.append(f"依赖 {dependency} 必须是注册表中可注入的 Provider；Model 仅作类型引用，Service 不能注入")
    for field in ("inputs", "outputs", "methods"):
        if not isinstance(metadata.get(field, {}), dict):
            errors.append(f"元数据 {field} 必须是对象")
    if errors:
        raise ValueError("[AIPOD_TEST_SETUP] " + "; ".join(errors))
    spec_hash = _digest({"request": request_hash, "metadata": metadata})
    test_path = f"tests/components/{name}_{spec_hash[:12]}.py"
    required = [f"ComponentTests.{case['name']}" for case in metadata["tests"]]
    test_system = SDK_PROMPT + "\n只生成测试源码。使用 unittest.TestCase，固定类名 ComponentTests。禁止实现、替换或导入被测组件源码。"
    test_user = (
        user + "\n被测组件（通过 ID 调用）：" + json.dumps(component, ensure_ascii=False)
        + "\n冻结接口元数据：" + json.dumps(metadata, ensure_ascii=False)
        + "\n依赖契约和开发规范：\n" + system
        + "\n根据 tests 逐项实现测试，明确配置/数据/期望值；每项必须真实调用目标并检查结果。"
    )
    feedback = ""
    for attempt in range(3):
        result = generate_source(
            llm, test_system, test_user + feedback, test_path,
            frozen_metadata={"tests": metadata["tests"]}, source_phase="tests", **options,
        )
        test_source = result["code"]
        errors = validate_component_test_source(test_source, required_tests=required)
        if not errors:
            break
        feedback = "\n测试静态检查失败：" + "\n".join(errors)
    else:
        raise ValueError("[AIPOD_TEST_SETUP] " + "; ".join(errors))
    target = root / test_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.resolve().is_relative_to(root):
        raise ValueError("[AIPOD_TEST_INVALID] 测试目录越界")
    if target.exists() and target.read_bytes() != test_source.encode("utf-8"):
        raise ValueError("[AIPOD_TEST_INVALID] 拒绝覆盖已有测试版本")
    target.write_bytes(test_source.encode("utf-8"))
    descriptor = {
        "path": test_path, "sha256": hashlib.sha256(test_source.encode()).hexdigest(),
        "spec_hash": spec_hash, "required_tests": required,
    }
    journal["components"][name] = {
        "request_hash": request_hash, "metadata": metadata, "test": descriptor,
        "history": (previous.get("history", []) + [previous["test"]]) if previous else [],
    }
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=journal_path.parent,
                                     prefix="component-tests-", suffix=".tmp", delete=False) as stream:
        pending = Path(stream.name)
        stream.write(json.dumps(journal, ensure_ascii=False, indent=2) + "\n")
    try:
        os.replace(pending, journal_path)
    finally:
        pending.unlink(missing_ok=True)
    return metadata, descriptor, test_source


def verify_frozen_component(project_root, bean: dict, source: str, *, timeout=30) -> list[str]:
    from ai_pod_cli.component_tests import verify_component_test

    descriptor = bean.get("component_test")
    if not isinstance(descriptor, dict):
        return ["[AIPOD_TEST_SETUP] 组件缺少冻结测试；请修订所在层并先规划测试"]
    try:
        test_source = read_frozen_test(project_root, descriptor)
    except ValueError as error:
        return [str(error)]
    return verify_component_test(
        project_root, bean, source, test_source, timeout=timeout,
        required_tests=descriptor.get("required_tests"),
    )
