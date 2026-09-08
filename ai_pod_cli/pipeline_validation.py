"""Explicit public Pipeline inputs and repeatable entry scenarios."""

import json
from pathlib import Path

from ai_pod_cli.contracts import canonical_contract, validate_contract_data


def validate_pipeline_inputs(inputs, cases) -> list[str]:
    """Validate supplied boundary data; never infer or synthesize caller values."""
    errors = []
    if not isinstance(inputs, dict):
        return ["Pipeline must declare an inputs object describing only real caller parameters"]
    if not isinstance(cases, list) or not cases:
        return ["Pipeline must declare non-empty verification_cases with actual entry params"]
    names = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict) or not isinstance(case.get("name"), str) or not case["name"].strip():
            errors.append(f"verification_cases[{index}] must have a non-empty name")
            continue
        name = case["name"].strip()
        if name in names:
            errors.append(f"Duplicate verification case: {name}")
        names.add(name)
        params = case.get("params")
        if not isinstance(params, dict):
            errors.append(f"{name}: params must be an explicit object ({{}} is valid)")
            continue
        extra = params.keys() - inputs.keys()
        if extra:
            errors.append(f"{name}: undeclared public inputs: {', '.join(sorted(extra))}")
        try:
            errors.extend(f"{name}: {error}" for error in validate_contract_data(params, inputs))
            json.dumps(params, allow_nan=False)
        except (TypeError, ValueError) as error:
            errors.append(f"{name}: invalid entry data: {error}")
    try:
        for spec in inputs.values():
            canonical_contract(spec)
        json.dumps(inputs, allow_nan=False)
    except (TypeError, ValueError) as error:
        errors.append(f"Invalid public input contract: {error}")
    return errors


def save_pipeline_inputs(pipeline_path: str, inputs: dict, cases: list[dict]) -> str:
    """Persist the declared boundary alongside its source, including JSON nulls."""
    path = Path(pipeline_path).with_suffix(".contract.json")
    path.write_text(json.dumps({"inputs": inputs, "verification_cases": cases},
                               ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path.as_posix()


def load_pipeline_inputs(raw_path: str) -> dict:
    path = Path(raw_path).resolve()
    if not path.is_relative_to(Path.cwd().resolve()):
        raise ValueError("Pipeline input contract must be inside the project")
    value = json.loads(path.read_text(encoding="utf-8"))
    errors = validate_pipeline_inputs(value.get("inputs"), value.get("verification_cases"))
    if errors:
        raise ValueError("; ".join(errors))
    return value
