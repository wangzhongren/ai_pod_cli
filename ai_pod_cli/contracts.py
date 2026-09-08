"""Typed component contracts and static pipeline composition analysis."""

from __future__ import annotations

import re
import ast
from copy import deepcopy
import importlib
from datetime import datetime
from difflib import SequenceMatcher
from dataclasses import dataclass
from typing import Any


_TYPE_ALIASES = {
    "integer": "int", "number": "float", "boolean": "bool",
    "string": "str", "object": "dict", "array": "list",
    "mapping": "dict", "none": "null", "any": "any",
}


def _model_path(spec: Any) -> str | None:
    """Read a Model reference from structured or legacy contract metadata."""
    if isinstance(spec, dict):
        value = spec.get("model")
        return value.strip() if isinstance(value, str) and value.strip() else None
    if not isinstance(spec, str):
        return None
    candidate = re.split(r"\s*(?:—|–)\s*", spec.strip(), maxsplit=1)[0]
    parts = candidate.split(".")
    if (
        len(parts) >= 3
        and all(re.fullmatch(r"[A-Za-z_]\w*", part) for part in parts)
        and parts[-1][:1].isupper()
    ):
        return candidate
    return None


def _type_text(spec: str) -> str:
    token = re.split(r"\s*(?:—|–|-|:)\s*", spec.strip(), maxsplit=1)[0]
    token = re.sub(r"\s+", "", token)
    token = re.sub(r"\((?:optional|required)\)$", "", token, flags=re.I)
    qualified = re.fullmatch(
        r"(str|string|int|integer|float|number|bool|boolean)\([^)]*\)", token, re.I,
    )
    return qualified.group(1) if qualified else token


def _annotation_path(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _annotation_path(node.value) + "." + node.attr
    raise ValueError("Unsupported contract type expression")


def _annotation_schema(node: ast.AST) -> dict:
    # Parse syntax only: annotations never execute code or resolve imports here.
    if isinstance(node, ast.Constant):
        if node.value is None:
            return {"type": "null"}
        if isinstance(node.value, str):
            return _annotation_schema(ast.parse(node.value, mode="eval").body)
        raise ValueError("Unsupported contract type literal")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return {"anyOf": [_annotation_schema(node.left), _annotation_schema(node.right)]}
    if isinstance(node, ast.Subscript):
        name = _annotation_path(node.value).lower().removeprefix("typing.")
        args = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
        if name in {"list", "array"} and len(args) == 1:
            return {"type": "list", "items": _annotation_schema(args[0])}
        if name in {"dict", "mapping"} and len(args) == 2:
            if _annotation_schema(args[0]) != {"type": "str"}:
                raise ValueError("Dictionary contracts require string keys")
            return {"type": "dict", "propertyNames": {"type": "str"},
                    "additionalProperties": _annotation_schema(args[1])}
        if name == "optional" and len(args) == 1:
            return {"anyOf": [_annotation_schema(args[0]), {"type": "null"}]}
        if name == "union" and len(args) >= 2:
            return {"anyOf": [_annotation_schema(arg) for arg in args]}
        # Keep unsupported legacy annotations visible rather than treating them
        # as a supported container with unchecked element types.
        if name in {"list", "array", "dict", "mapping", "optional", "union"}:
            raise ValueError("Unsupported generic contract arity")
        return {"type": re.sub(r"\s+", "", ast.unparse(node)).lower()}
    path = _annotation_path(node)
    if _model_path(path):
        return {"type": "model", "model": path}
    name = path.lower().removeprefix("typing.")
    return {"type": _TYPE_ALIASES.get(name, name)}


def canonical_contract(spec: Any) -> dict:
    """Normalize legacy annotations and structured schemas without losing items.

    Model paths retain their case. This is a small syntax parser, not eval().
    Unknown bare legacy type names remain unchanged for backwards compatibility.
    """
    if isinstance(spec, dict):
        result = deepcopy(spec)
        if _model_path(spec):
            result.update(type="model", model=_model_path(spec))
        elif "type" in spec:
            base = canonical_contract(spec["type"])
            result = {**base, **result}
            if "type" in base:
                result["type"] = base["type"]
            else:
                result.pop("type", None)
        elif "anyOf" not in spec:
            result["type"] = "any"
        if isinstance(result.get("properties"), dict):
            result["properties"] = {key: canonical_contract(value) for key, value in result["properties"].items()}
        for key in ("items", "additionalProperties", "propertyNames"):
            if isinstance(result.get(key), (dict, str)):
                result[key] = canonical_contract(result[key])
        if result.get("type") == "dict" and isinstance(result.get("additionalProperties"), dict):
            result.setdefault("propertyNames", {"type": "str"})
        if isinstance(result.get("anyOf"), list):
            branches = []
            pending = [canonical_contract(branch) for branch in result["anyOf"]]
            while pending:
                branch = pending.pop(0)
                if "anyOf" in branch:
                    pending[0:0] = branch["anyOf"]
                    continue
                keys = ("properties", "additionalProperties", "propertyNames") if branch.get("type") == "dict" else ("items",) if branch.get("type") == "list" else ()
                for key in keys:
                    if key not in result:
                        continue
                    if key not in branch or branch[key] == {"type": "any"}:
                        branch[key] = deepcopy(result[key])
                    elif key == "properties":
                        for name, child in result[key].items():
                            if name in branch[key] and branch[key][name] != child:
                                raise ValueError("Conflicting union property constraints are unsupported")
                            branch[key][name] = deepcopy(child)
                    elif branch[key] != result[key] and result[key] != {"type": "any"}:
                        raise ValueError("Conflicting union container constraints are unsupported")
                if branch.get("type") == "dict" and isinstance(result.get("required"), list):
                    existing = branch.get("required", [])
                    branch["required"] = sorted(set(existing if isinstance(existing, list) else []) | set(result["required"]))
                branches.append(branch)
            result["anyOf"] = branches
        return result
    if not isinstance(spec, str) or not spec.strip():
        return {"type": "any"}
    token = _type_text(spec)
    try:
        expression = ast.parse(token, mode="eval")
    except SyntaxError as error:
        if re.match(r"^(?:typing\.)?(?:list|array|dict|mapping|optional|union)\[", token, re.I):
            raise ValueError("Malformed generic contract type") from error
        return {"type": token.lower() or "any"}
    if any(isinstance(node, (ast.Call, ast.Lambda, ast.NamedExpr, ast.ListComp,
                             ast.SetComp, ast.DictComp, ast.GeneratorExp))
           for node in ast.walk(expression)):
        raise ValueError("Contract type expressions cannot execute code")
    return _annotation_schema(expression.body)


def normalize_type(spec: Any) -> str:
    """Return the outer type; canonical_contract retains nested type information."""
    schema = canonical_contract(spec)
    return "any" if "anyOf" in schema else schema.get("type", "any")


def types_compatible(produced: Any, required: Any) -> bool:
    """Return whether a produced value may satisfy a required field type."""
    return not schema_compatibility(produced, required)


def _outer_types_compatible(produced: str, required: str) -> bool:
    if "any" in (produced, required) or produced == required:
        return True
    # An int is valid wherever a general numeric float is accepted.
    return produced == "int" and required == "float"


def _required_properties(spec: Any) -> set[str]:
    if not isinstance(spec, dict):
        return set()
    required = spec.get("required", [])
    return set(required) if isinstance(required, list) else set()


def schema_compatibility(produced: Any, required: Any, path: str = "") -> list[dict]:
    """Return nested schema mismatches using a small, backwards-compatible JSON Schema subset."""
    produced, required = canonical_contract(produced), canonical_contract(required)
    if "anyOf" in produced or "anyOf" in required:
        alternatives = required.get("anyOf", [required])
        errors = []
        for branch in produced.get("anyOf", [produced]):
            if not any(not schema_compatibility(branch, target, path) for target in alternatives):
                errors.append({"path": path or "$", "produced": branch, "required": required})
        return errors
    mismatches: list[dict] = []
    produced_type, required_type = normalize_type(produced), normalize_type(required)
    if not _outer_types_compatible(produced_type, required_type):
        return [{"path": path or "$", "produced": produced_type, "required": required_type}]
    if required_type == "model":
        produced_model = _model_path(produced)
        required_model = _model_path(required)
        if produced_model != required_model:
            return [{
                "path": path or "$", "produced": produced_model or "unknown model",
                "required": required_model or "unknown model",
            }]
        return []
    if not isinstance(required, dict) or not isinstance(produced, dict):
        return mismatches

    if required_type == "dict":
        produced_props = produced.get("properties", {})
        required_props = required.get("properties", {})
        if not isinstance(produced_props, dict) or not isinstance(required_props, dict):
            return mismatches
        produced_required = _required_properties(produced)
        for name in _required_properties(required):
            child_path = f"{path}.{name}" if path else name
            if name not in produced_props or name not in produced_required:
                mismatches.append({
                    "path": child_path, "produced": "missing", "required": "required field",
                })
            else:
                mismatches.extend(schema_compatibility(
                    produced_props[name], required_props.get(name, {}), child_path,
                ))
    elif required_type == "list" and "items" in required:
        mismatches.extend(schema_compatibility(
            produced.get("items", {}), required["items"], f"{path}[]" if path else "$[]",
        ))
    if required_type == "dict" and isinstance(required.get("additionalProperties"), dict):
        # Named properties also flow through a string-map contract, including
        # optional properties that may be present at runtime.
        for name, child in produced.get("properties", {}).items():
            if name not in required.get("properties", {}):
                mismatches.extend(schema_compatibility(
                    child, required["additionalProperties"],
                    f"{path}.{name}" if path else name,
                ))
        if produced.get("additionalProperties") is not False:
            mismatches.extend(schema_compatibility(
                produced.get("additionalProperties", {}), required["additionalProperties"],
                f"{path}.*" if path else "$.*",
            ))
    return mismatches


def validate_contract_value(value: Any, spec: Any, path: str = "$") -> list[str]:
    """Validate a runtime value against the supported contract schema subset."""
    spec = canonical_contract(spec)
    if "anyOf" in spec:
        branch_errors = [validate_contract_value(value, branch, path) for branch in spec["anyOf"]]
        if any(not errors for errors in branch_errors):
            return []
        return [f"{path}: no union alternative matched", *dict.fromkeys(error for errors in branch_errors for error in errors)]
    expected = normalize_type(spec)
    if expected == "model":
        model_path = _model_path(spec) or ""
        try:
            module_name, class_name = model_path.rsplit(".", 1)
            model_class = getattr(importlib.import_module(module_name), class_name)
            return model_class.validate(value, path)
        except (ImportError, AttributeError, ValueError) as error:
            return [f"{path}: cannot load model {model_path}: {error}"]
    checks = {
        "str": lambda item: isinstance(item, str),
        "bool": lambda item: isinstance(item, bool),
        "int": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "float": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "dict": lambda item: isinstance(item, dict),
        "list": lambda item: isinstance(item, list),
        "null": lambda item: item is None,
        "datetime": lambda item: isinstance(item, datetime),
        "datetime.datetime": lambda item: isinstance(item, datetime),
    }
    if expected != "any" and expected in checks and not checks[expected](value):
        return [f"{path}: expected {expected}, got {type(value).__name__}"]
    if not isinstance(spec, dict):
        return []
    errors: list[str] = []
    if expected == "dict" and isinstance(value, dict):
        if isinstance(spec.get("propertyNames"), dict):
            for name in value:
                errors.extend(validate_contract_value(name, spec["propertyNames"], f"{path}.<key>"))
        properties = spec.get("properties", {})
        for name in _required_properties(spec):
            child_path = f"{path}.{name}"
            if name not in value:
                errors.append(f"{child_path}: required field is missing")
            elif isinstance(properties, dict) and name in properties:
                errors.extend(validate_contract_value(value[name], properties[name], child_path))
        if isinstance(properties, dict):
            for name in value.keys() & properties.keys() - _required_properties(spec):
                errors.extend(validate_contract_value(value[name], properties[name], f"{path}.{name}"))
        additional = spec.get("additionalProperties")
        if additional is False:
            for name in value.keys() - properties.keys():
                errors.append(f"{path}.{name}: additional property is not allowed")
        elif isinstance(additional, (dict, str)):
            for name in value.keys() - set(properties):
                errors.extend(validate_contract_value(value[name], additional, f"{path}.{name}"))
    elif expected == "list" and isinstance(value, list) and "items" in spec:
        for index, item in enumerate(value):
            errors.extend(validate_contract_value(item, spec["items"], f"{path}[{index}]"))
    return errors


def validate_contract_data(data: dict, fields: Any, prefix: str = "$") -> list[str]:
    """Validate named context fields, including required top-level values."""
    if not isinstance(fields, dict):
        return []
    errors: list[str] = []
    for name, spec in fields.items():
        required_flag = spec.get("required") if isinstance(spec, dict) else None
        required = (
            required_flag if isinstance(required_flag, bool)
            else not isinstance(spec, dict) or "default" not in spec
        )
        if name not in data:
            if required:
                errors.append(f"{prefix}.{name}: required field is missing")
            continue
        errors.extend(validate_contract_value(data[name], spec, f"{prefix}.{name}"))
    return errors


def materialize_contract_value(value: Any, spec: Any) -> Any:
    """Convert validated structured values into their declared runtime types."""
    spec = canonical_contract(spec)
    if "anyOf" in spec:
        for branch in spec["anyOf"]:
            if not validate_contract_value(value, branch):
                return materialize_contract_value(value, branch)
        raise ValueError("Value does not match any union alternative")
    expected = normalize_type(spec)
    if expected == "model":
        model_path = _model_path(spec) or ""
        module_name, class_name = model_path.rsplit(".", 1)
        model_class = getattr(importlib.import_module(module_name), class_name)
        return value if isinstance(value, model_class) else model_class.model_validate(value)
    if not isinstance(spec, dict):
        return value
    if expected == "dict" and isinstance(value, dict):
        properties = spec.get("properties", {})
        if not isinstance(properties, dict):
            return value
        return {
            key: materialize_contract_value(item, properties[key])
            if key in properties else materialize_contract_value(item, spec["additionalProperties"])
            if isinstance(spec.get("additionalProperties"), dict) else item
            for key, item in value.items()
        }
    if expected == "list" and isinstance(value, list) and "items" in spec:
        return [materialize_contract_value(item, spec["items"]) for item in value]
    return value


def materialize_contract_data(data: dict, fields: Any) -> dict:
    """Materialize present named fields after successful Contract validation."""
    if not isinstance(fields, dict):
        return {}
    return {
        name: materialize_contract_value(data[name], spec)
        for name, spec in fields.items()
        if name in data
    }


@dataclass(frozen=True)
class ContractField:
    name: str
    type: str = "any"
    required: bool = True
    description: str = ""
    schema: Any = None

    @classmethod
    def from_spec(cls, name: str, spec: Any) -> "ContractField":
        if isinstance(spec, dict):
            required_flag = spec.get("required")
            return cls(
                name=name,
                type=normalize_type(spec),
                required=(
                    required_flag if isinstance(required_flag, bool)
                    else "default" not in spec
                ),
                description=str(spec.get("description", "")),
                schema=canonical_contract(spec),
            )
        text = str(spec or "")
        parts = re.split(r"\s*(?:—|–)\s*", text, maxsplit=1)
        return cls(
            name=name, type=normalize_type(text),
            description=parts[1] if len(parts) > 1 else "", schema=canonical_contract(spec),
        )

    def as_dict(self) -> dict:
        result = {
            "name": self.name, "type": self.type,
            "required": self.required, "description": self.description,
        }
        if isinstance(self.schema, dict):
            result.update({
                key: value for key, value in self.schema.items()
                if key not in {"name", "type", "description"}
            })
        return result


def fields_from_metadata(metadata: Any) -> dict[str, ContractField]:
    if not isinstance(metadata, dict):
        return {}
    return {str(name): ContractField.from_spec(str(name), spec) for name, spec in metadata.items()}


_SEMANTIC_QUALIFIERS = {
    "current", "value", "level", "percent", "percentage", "data", "info", "result",
}


def _semantic_tokens(name: str) -> list[str]:
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return [
        token for token in re.split(r"[^a-z0-9]+", expanded.lower())
        if token and token not in _SEMANTIC_QUALIFIERS
    ]


def semantic_field_similarity(left: str, right: str) -> float:
    """Estimate whether two contract names are likely aliases, conservatively."""
    if left == right:
        return 1.0
    left_tokens, right_tokens = _semantic_tokens(left), _semantic_tokens(right)
    if left_tokens and left_tokens == right_tokens:
        return 0.98
    if not left_tokens or not right_tokens:
        return 0.0
    return SequenceMatcher(None, "_".join(left_tokens), "_".join(right_tokens)).ratio()


def analyze_pipeline_contracts(service_ids: list[str], components: list[dict]) -> dict:
    """Infer a pipeline contract and validate the types flowing through its context.

    Inputs not produced by an earlier component become external pipeline inputs.
    A same-named value with an incompatible type is a composition error.
    """
    by_id = {item.get("id"): item for item in components}
    available: dict[str, ContractField] = {}
    external: dict[str, ContractField] = {}
    links: list[dict] = []
    issues: list[dict] = []
    warnings: list[dict] = []

    for index, service_id in enumerate(service_ids):
        component = by_id.get(service_id)
        if component is None:
            continue
        inputs = fields_from_metadata(component.get("inputs", {}))
        matched, missing, mismatches, semantic_drifts = [], [], [], []
        for name, required in inputs.items():
            produced = available.get(name)
            if produced is None:
                if required.required:
                    candidates = [
                        (available_name, available_field, semantic_field_similarity(name, available_name))
                        for available_name, available_field in available.items()
                        if types_compatible(available_field.type, required.type)
                    ]
                    candidate = max(candidates, key=lambda item: item[2], default=None)
                    if index and candidate and candidate[2] >= 0.86:
                        drift = {
                            "required_field": name, "produced_field": candidate[0],
                            "similarity": round(candidate[2], 3),
                        }
                        semantic_drifts.append(drift)
                        warnings.append({
                            "code": "semantic_field_drift", "component": service_id,
                            "field": name, "produced_field": candidate[0],
                            "produced": candidate[1].type, "required": required.type,
                        })
                    else:
                        external.setdefault(name, required)
                        missing.append(name)
                continue
            if types_compatible(produced.type, required.type):
                nested = schema_compatibility(produced.schema, required.schema, name)
                if nested:
                    mismatch = {
                        "field": name, "produced": produced.type,
                        "required": required.type, "schema_mismatches": nested,
                    }
                    mismatches.append(mismatch)
                    issues.append({
                        "code": "contract_schema_mismatch", "component": service_id,
                        "field": name, "schema_mismatches": nested,
                    })
                else:
                    matched.append(name)
            else:
                mismatch = {"field": name, "produced": produced.type, "required": required.type}
                mismatches.append(mismatch)
                issues.append({
                    "code": "contract_type_mismatch", "component": service_id,
                    "field": name, "produced": produced.type, "required": required.type,
                })
        if index:
            links.append({
                "from": service_ids[index - 1], "to": service_id,
                "compatible": not mismatches, "matched": matched,
                "external_inputs": missing, "mismatches": mismatches,
                "semantic_drifts": semantic_drifts,
            })
        available.update(fields_from_metadata(component.get("outputs", {})))

    return {
        "inputs": {name: field.as_dict() for name, field in external.items()},
        "outputs": {name: field.as_dict() for name, field in available.items()},
        "links": links,
        "valid": not issues,
        "issues": issues,
        "warnings": warnings,
    }


def analyze_parallel_contracts(
    branches: list[list[str]], components: list[dict], *, merge: str = "strict",
) -> dict:
    """Analyze isolated branch contracts and reject ambiguous output merging."""
    if merge not in {"strict", "overwrite", "collect"}:
        raise ValueError("parallel merge must be strict, overwrite, or collect")
    analyses = [analyze_pipeline_contracts(branch, components) for branch in branches]
    inputs: dict = {}
    outputs: dict = {}
    writers: dict[str, list[tuple[int, dict]]] = {}
    issues = [issue for analysis in analyses for issue in analysis["issues"]]
    warnings = [warning for analysis in analyses for warning in analysis["warnings"]]
    for index, analysis in enumerate(analyses):
        inputs.update(analysis["inputs"])
        for name, field in analysis["outputs"].items():
            writers.setdefault(name, []).append((index, field))

    for name, entries in writers.items():
        declared_types = {entry[1].get("type", "any") for entry in entries}
        if len(entries) > 1 and merge == "strict":
            issues.append({
                "code": "parallel_write_conflict", "field": name,
                "branches": [entry[0] for entry in entries],
                "message": "multiple branches write this field without an explicit reducer",
            })
        elif len(declared_types) > 1:
            warnings.append({
                "code": "parallel_output_type_drift", "field": name,
                "branches": [entry[0] for entry in entries],
                "types": sorted(declared_types), "merge": merge,
            })
        selected = entries[-1][1]
        if merge == "collect" and len(entries) > 1:
            selected = {"type": "array", "items": selected}
        outputs[name] = selected

    return {
        "mode": "parallel", "merge": merge, "branches": analyses,
        "inputs": inputs, "outputs": outputs,
        "valid": not issues, "issues": issues, "warnings": warnings,
    }


def analyze_stream_contracts(
    service_ids: list[str], components: list[dict], *, batch_size: int | None = None,
) -> dict:
    """Analyze per-item data flow for a streaming component chain."""
    if batch_size is not None and batch_size < 1:
        raise ValueError("stream batch_size must be at least 1")
    analysis = analyze_pipeline_contracts(service_ids, components)
    return {**analysis, "mode": "stream", "batch_size": batch_size}
