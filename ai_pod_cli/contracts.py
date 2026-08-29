"""Typed component contracts and static pipeline composition analysis."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from dataclasses import dataclass
from typing import Any


_TYPE_ALIASES = {
    "integer": "int", "number": "float", "boolean": "bool",
    "string": "str", "object": "dict", "array": "list",
    "mapping": "dict", "none": "null", "any": "any",
}


def normalize_type(spec: Any) -> str:
    """Extract a stable type token from legacy or structured field metadata."""
    if isinstance(spec, dict):
        spec = spec.get("type", "any")
    if not isinstance(spec, str) or not spec.strip():
        return "any"
    token = re.split(r"\s*(?:—|–|-|:)\s*", spec.strip(), maxsplit=1)[0]
    token = re.sub(r"\s+", "", token).lower()
    token = re.sub(r"\((?:optional|required)\)$", "", token)
    return _TYPE_ALIASES.get(token, token or "any")


def types_compatible(produced: str, required: str) -> bool:
    """Return whether a produced value may satisfy a required field type."""
    produced, required = normalize_type(produced), normalize_type(required)
    if "any" in (produced, required) or produced == required:
        return True
    # An int is valid wherever a general numeric float is accepted.
    return produced == "int" and required == "float"


@dataclass(frozen=True)
class ContractField:
    name: str
    type: str = "any"
    required: bool = True
    description: str = ""

    @classmethod
    def from_spec(cls, name: str, spec: Any) -> "ContractField":
        if isinstance(spec, dict):
            return cls(
                name=name,
                type=normalize_type(spec),
                required=bool(spec.get("required", "default" not in spec)),
                description=str(spec.get("description", "")),
            )
        text = str(spec or "")
        parts = re.split(r"\s*(?:—|–)\s*", text, maxsplit=1)
        return cls(name=name, type=normalize_type(text), description=parts[1] if len(parts) > 1 else "")

    def as_dict(self) -> dict:
        return {
            "name": self.name, "type": self.type,
            "required": self.required, "description": self.description,
        }


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
                        issues.append({
                            "code": "semantic_field_drift", "component": service_id,
                            "field": name, "produced_field": candidate[0],
                            "produced": candidate[1].type, "required": required.type,
                        })
                    else:
                        external.setdefault(name, required)
                        missing.append(name)
                continue
            if types_compatible(produced.type, required.type):
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
    }
