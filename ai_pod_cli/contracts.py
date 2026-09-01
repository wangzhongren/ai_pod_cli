"""Typed component contracts and static pipeline composition analysis."""

from __future__ import annotations

import re
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


def normalize_type(spec: Any) -> str:
    """Extract a stable type token from legacy or structured field metadata."""
    if _model_path(spec):
        return "model"
    if isinstance(spec, dict):
        spec = spec.get("type", "any")
    if not isinstance(spec, str) or not spec.strip():
        return "any"
    token = re.split(r"\s*(?:—|–|-|:)\s*", spec.strip(), maxsplit=1)[0]
    token = re.sub(r"\s+", "", token).lower()
    token = re.sub(r"\((?:optional|required)\)$", "", token)
    scalar_with_qualifier = re.match(
        r"^(str|string|int|integer|float|number|bool|boolean)\([^)]*\)$", token,
    )
    if scalar_with_qualifier:
        token = scalar_with_qualifier.group(1)
    return _TYPE_ALIASES.get(token, token or "any")


def types_compatible(produced: str, required: str) -> bool:
    """Return whether a produced value may satisfy a required field type."""
    produced, required = normalize_type(produced), normalize_type(required)
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
    mismatches: list[dict] = []
    produced_type, required_type = normalize_type(produced), normalize_type(required)
    if not types_compatible(produced_type, required_type):
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
    return mismatches


def validate_contract_value(value: Any, spec: Any, path: str = "$") -> list[str]:
    """Validate a runtime value against the supported contract schema subset."""
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
        if additional is not None:
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
            if key in properties else item
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
                schema=spec,
            )
        text = str(spec or "")
        parts = re.split(r"\s*(?:—|–)\s*", text, maxsplit=1)
        return cls(
            name=name, type=normalize_type(text),
            description=parts[1] if len(parts) > 1 else "", schema=spec,
        )

    def as_dict(self) -> dict:
        result = {
            "name": self.name, "type": self.type,
            "required": self.required, "description": self.description,
        }
        if isinstance(self.schema, dict):
            result.update({
                key: value for key, value in self.schema.items()
                if key not in {"name", "type", "required", "description"}
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
