"""Regression coverage for generated generic and structured Contract metadata."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from ai_pod_cli.contracts import (
    ContractField,
    analyze_pipeline_contracts,
    canonical_contract,
    materialize_contract_value,
    normalize_type,
    types_compatible,
    schema_compatibility,
    validate_contract_data,
    validate_contract_value,
)
from ai_pod_cli.model import Model
from ai_pod_cli.sandbox import sample_value


class GameEntity(Model):
    id: str
    x: float


class OtherEntity(Model):
    id: str
    x: float


ENTITY = f"{__name__}.GameEntity"
OTHER_ENTITY = f"{__name__}.OtherEntity"
STRUCTURED_ENTITIES = {"type": "array", "items": {"model": ENTITY}}


class ContractNormalizationTests(unittest.TestCase):
    def test_public_type_compatibility_keeps_generic_element_constraints(self):
        self.assertFalse(types_compatible("List[str]", "List[int]"))
        self.assertFalse(types_compatible(f"List[{ENTITY}]", f"List[{OTHER_ENTITY}]"))
        self.assertTrue(types_compatible(STRUCTURED_ENTITIES, f"List[{ENTITY}]"))

    def test_nested_optional_union_keeps_outer_items(self):
        spec = {"type": "Optional[Union[list, Optional[list]]]", "items": {"type": "int"}}
        self.assertEqual(validate_contract_value([1], spec), [])
        self.assertEqual(validate_contract_value(None, spec), [])
        self.assertTrue(validate_contract_value(["bad"], spec))

    def test_generic_list_spellings_keep_model_identity_and_element_schema(self):
        for constructor in ("List", "list", "typing.List"):
            spec = f"{constructor}[{ENTITY}] — entities from the current frame"
            with self.subTest(spec=spec):
                normalized = canonical_contract(spec)
                self.assertEqual(normalize_type(spec), "list")
                self.assertEqual(normalized["type"], "list")
                self.assertEqual(normalized["items"]["model"], ENTITY)
                self.assertEqual(schema_compatibility(STRUCTURED_ENTITIES, spec), [])
                self.assertEqual(schema_compatibility(spec, STRUCTURED_ENTITIES), [])

    def test_nested_generics_match_structured_contracts(self):
        structured = {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": {
                    "type": "array", "items": {"model": ENTITY},
                },
            },
        }
        generic = f"List[Dict[str, list[{ENTITY}]]]"
        normalized = canonical_contract(generic)
        self.assertEqual(normalized["items"]["type"], "dict")
        self.assertEqual(
            normalized["items"]["additionalProperties"]["items"]["model"], ENTITY,
        )
        self.assertEqual(schema_compatibility(structured, generic), [])
        self.assertEqual(schema_compatibility(generic, structured), [])

    def test_generic_syntax_in_structured_type_preserves_metadata(self):
        source = {
            "type": f"List[{ENTITY}]",
            "required": False,
            "default": [],
            "description": "Objects surviving this frame",
        }
        unchanged = copy.deepcopy(source)
        normalized = canonical_contract(source)
        self.assertEqual(normalized["type"], "list")
        self.assertEqual(normalized["items"]["model"], ENTITY)
        for name in ("required", "default", "description"):
            self.assertEqual(normalized[name], source[name])
        self.assertEqual(source, unchanged)
        self.assertEqual(canonical_contract(normalized), normalized)

    def test_field_export_retains_generic_items_after_round_trip(self):
        field = ContractField.from_spec("entities", f"List[{ENTITY}] — active entities")
        exported = field.as_dict()
        self.assertEqual(exported["type"], "list")
        self.assertEqual(exported["items"]["model"], ENTITY)
        self.assertEqual(exported["description"], "active entities")
        self.assertEqual(
            ContractField.from_spec("entities", exported).as_dict(), exported,
        )
        self.assertEqual(schema_compatibility(STRUCTURED_ENTITIES, exported), [])

    def test_field_export_retains_optional_default(self):
        exported = ContractField.from_spec("entities", {
            "type": f"list[{ENTITY}]",
            "required": False,
            "default": [],
            "description": "optional entities",
        }).as_dict()
        self.assertFalse(exported["required"])
        self.assertEqual(exported["default"], [])
        self.assertEqual(exported["description"], "optional entities")
        self.assertEqual(exported["items"]["model"], ENTITY)
        self.assertEqual(validate_contract_data({}, {"entities": exported}), [])

    def test_field_export_preserves_object_required_properties_and_default(self):
        spec = {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
            "default": {"id": "fallback"},
            "description": "Identity supplied by the engine",
        }
        field = ContractField.from_spec("identity", spec)
        exported = field.as_dict()
        self.assertFalse(field.required)
        self.assertEqual(exported["required"], ["id"])
        self.assertEqual(exported["default"], {"id": "fallback"})
        self.assertEqual(exported["description"], spec["description"])
        self.assertEqual(validate_contract_data({}, {"identity": exported}), [])
        self.assertEqual(
            validate_contract_value({}, exported), ["$.id: required field is missing"],
        )
        self.assertEqual(validate_contract_value({"id": "player1"}, exported), [])
        self.assertEqual(ContractField.from_spec("identity", exported).as_dict(), exported)

    def test_list_models_reject_different_identity_and_wrong_element_types(self):
        for incompatible in (
            f"List[{OTHER_ENTITY}]", "List[str]", "List[int]", "dict",
        ):
            with self.subTest(required=incompatible):
                self.assertTrue(schema_compatibility(STRUCTURED_ENTITIES, incompatible))
        errors = schema_compatibility(STRUCTURED_ENTITIES, f"List[{OTHER_ENTITY}]", "entities")
        self.assertEqual(errors[0]["path"], "entities[]")
        self.assertEqual(errors[0]["produced"], ENTITY)
        self.assertEqual(errors[0]["required"], OTHER_ENTITY)

    def test_primitive_list_compatibility_keeps_numeric_widening_direction(self):
        self.assertEqual(schema_compatibility("List[int]", "list[float]"), [])
        self.assertTrue(schema_compatibility("List[float]", "list[int]"))
        self.assertTrue(schema_compatibility("List[str]", "list[int]"))

    def test_dictionary_generics_validate_additional_values(self):
        spec = f"Dict[str, List[{ENTITY}]]"
        self.assertEqual(normalize_type(spec), "dict")
        self.assertEqual(canonical_contract(spec)["propertyNames"], {"type": "str"})
        self.assertEqual(validate_contract_value({"players": [{"id": "p1", "x": 1}]}, spec), [])
        errors = validate_contract_value({"players": [{"id": "p1"}]}, spec)
        self.assertTrue(any("$.players[0].x" in error for error in errors), errors)
        self.assertTrue(schema_compatibility("dict[str, str]", "Dict[str, int]"))

    def test_dictionary_generics_check_produced_properties_as_well_as_extra_values(self):
        for required in (["score"], []):
            incompatible = {
                "type": "object",
                "properties": {"score": {"type": "string"}},
                "required": required,
            }
            with self.subTest(required=required):
                self.assertTrue(schema_compatibility(incompatible, "Dict[str, int]"))
        compatible = {
            "type": "object",
            "properties": {"score": {"type": "integer"}},
            "required": ["score"],
            "additionalProperties": {"type": "integer"},
        }
        self.assertEqual(schema_compatibility(compatible, "Dict[str, int]"), [])

    def test_dictionary_generics_reject_non_string_keys(self):
        for spec in ("Dict[str, int]", "dict[str, int]", "typing.Dict[str, int]"):
            with self.subTest(spec=spec):
                self.assertEqual(validate_contract_value({"1": 4}, spec), [])
                self.assertTrue(validate_contract_value({1: 4}, spec))
                self.assertTrue(validate_contract_value({True: 4}, spec))
                self.assertTrue(validate_contract_value({("player",): 4}, spec))

    def test_runtime_validates_generic_containers_and_model_elements(self):
        for spec in (STRUCTURED_ENTITIES, f"List[{ENTITY}]"):
            with self.subTest(spec=spec):
                self.assertEqual(validate_contract_value([{"id": "p1", "x": 1}], spec), [])
                self.assertTrue(validate_contract_value("not a list", spec))
                self.assertTrue(validate_contract_value({"id": "p1", "x": 1}, spec))
                errors = validate_contract_value([{"id": "p1"}], spec)
                self.assertTrue(any("$[0].x" in error for error in errors), errors)
        self.assertTrue(validate_contract_value(["1"], "list[int]"))
        self.assertTrue(validate_contract_value([True], "list[int]"))

    def test_generic_model_elements_materialize_in_nested_lists_and_dicts(self):
        value = {"players": [{"id": "p1", "x": 1.5}]}
        spec = f"Dict[str, List[{ENTITY}]]"
        self.assertEqual(validate_contract_value(value, spec), [])
        materialized = materialize_contract_value(value, spec)
        self.assertIsInstance(materialized["players"][0], GameEntity)
        self.assertEqual(materialized["players"][0].id, "p1")
        # Existing instances must survive conversion, rather than being rebuilt.
        again = materialize_contract_value(materialized, spec)
        self.assertIs(again["players"][0], materialized["players"][0])
        self.assertEqual(value, {"players": [{"id": "p1", "x": 1.5}]})

    def test_optional_spellings_accept_none_without_weakening_non_null_items(self):
        spellings = (
            f"Optional[List[{ENTITY}]]",
            f"typing.Optional[list[{ENTITY}]]",
            f"Union[List[{ENTITY}], None]",
            f"list[{ENTITY}] | None",
        )
        for spec in spellings:
            with self.subTest(spec=spec):
                self.assertEqual(validate_contract_value(None, spec), [])
                self.assertIsNone(materialize_contract_value(None, spec))
                self.assertEqual(validate_contract_value([{"id": "p1", "x": 1}], spec), [])
                self.assertTrue(validate_contract_value([{"id": "p1"}], spec))
                self.assertTrue(validate_contract_value("not a list", spec))
                result = materialize_contract_value([{"id": "p1", "x": 1}], spec)
                self.assertIsInstance(result[0], GameEntity)
                self.assertEqual(schema_compatibility(spec, spellings[0]), [])
                self.assertEqual(schema_compatibility(STRUCTURED_ENTITIES, spec), [])
                self.assertTrue(schema_compatibility(spec, STRUCTURED_ENTITIES))

    def test_optional_object_keeps_outer_required_properties(self):
        spec = {
            "type": "Optional[dict]",
            "required": ["id"],
            "properties": {"id": {"type": "string"}},
        }
        for representation in (spec, ContractField.from_spec("identity", spec).as_dict()):
            with self.subTest(spec=representation):
                self.assertEqual(validate_contract_value(None, representation), [])
                self.assertEqual(validate_contract_value({"id": "p1"}, representation), [])
                self.assertTrue(validate_contract_value({}, representation))
                self.assertTrue(validate_contract_value({"id": 1}, representation))
        self.assertTrue(schema_compatibility({"type": "dict"}, spec))

    def test_optional_list_keeps_outer_items_validation_and_materialization(self):
        spec = {"type": "Optional[list]", "items": {"model": ENTITY}}
        self.assertEqual(validate_contract_value(None, spec), [])
        self.assertEqual(validate_contract_value([{"id": "p1", "x": 1}], spec), [])
        self.assertTrue(validate_contract_value([{"id": "p1"}], spec))
        self.assertTrue(validate_contract_value(["p1"], spec))
        materialized = materialize_contract_value([{"id": "p1", "x": 1}], spec)
        self.assertIsInstance(materialized[0], GameEntity)
        self.assertEqual(schema_compatibility(STRUCTURED_ENTITIES, spec), [])
        self.assertTrue(schema_compatibility("List[str]", spec))

    def test_closed_object_models_satisfy_generic_dictionary(self):
        for properties, required in (
            ({"player": {"model": ENTITY}}, ["player"]),
            ({}, []),
        ):
            produced = {
                "type": "object", "properties": properties,
                "required": required, "additionalProperties": False,
            }
            with self.subTest(produced=produced):
                self.assertEqual(schema_compatibility(produced, f"Dict[str, {ENTITY}]"), [])
        incompatible = {
            "type": "object", "properties": {"player": {"model": OTHER_ENTITY}},
            "required": ["player"], "additionalProperties": False,
        }
        self.assertTrue(schema_compatibility(incompatible, f"Dict[str, {ENTITY}]"))

    def test_union_validates_all_declared_branches_and_rejects_other_values(self):
        for spec in ("Union[int, str]", "typing.Union[str, int]", "int | str"):
            with self.subTest(spec=spec):
                self.assertEqual(validate_contract_value(1, spec), [])
                self.assertEqual(validate_contract_value("one", spec), [])
                self.assertTrue(validate_contract_value([], spec))
                self.assertTrue(validate_contract_value(True, spec))
                self.assertTrue(validate_contract_value(None, spec))
                self.assertEqual(schema_compatibility(spec, "str | int"), [])

    def test_engine_pipeline_accepts_both_contract_representations(self):
        for produced, required in (
            (STRUCTURED_ENTITIES, f"List[{ENTITY}] — entities to draw"),
            (f"List[{ENTITY}] — updated entities", STRUCTURED_ENTITIES),
        ):
            with self.subTest(produced=produced):
                analysis = analyze_pipeline_contracts(["Physics", "Draw"], [
                    {"id": "Physics", "outputs": {"entities": produced}},
                    {"id": "Draw", "inputs": {"entities": required}},
                ])
                self.assertTrue(analysis["valid"], analysis["issues"])
                self.assertEqual(analysis["links"][0]["matched"], ["entities"])
                self.assertEqual(analysis["inputs"], {})
                self.assertEqual(analysis["outputs"]["entities"]["items"]["model"], ENTITY)

    def test_pipeline_still_reports_different_nested_models(self):
        analysis = analyze_pipeline_contracts(["Physics", "Draw"], [
            {"id": "Physics", "outputs": {"entities": STRUCTURED_ENTITIES}},
            {"id": "Draw", "inputs": {"entities": f"List[{OTHER_ENTITY}]"}},
        ])
        self.assertFalse(analysis["valid"])
        self.assertEqual(analysis["issues"][0]["code"], "contract_schema_mismatch")
        self.assertEqual(analysis["issues"][0]["schema_mismatches"][0]["path"], "entities[]")

    def test_samples_obey_generic_contracts_and_include_model_instances(self):
        for spec in (
            ENTITY, f"List[{ENTITY}]", f"List[List[{ENTITY}]]",
            f"Dict[str, List[{ENTITY}]]", f"Optional[List[{ENTITY}]]",
            "List[int]", "Union[int, str]",
        ):
            with self.subTest(spec=spec):
                value = sample_value("payload", spec)
                self.assertEqual(validate_contract_value(value, spec), [])
        entities = sample_value("entities", f"List[{ENTITY}]")
        self.assertTrue(entities)
        self.assertIsInstance(entities[0], GameEntity)

    def test_engine_field_names_do_not_override_explicit_model_schemas(self):
        position = sample_value("position", {"model": ENTITY})
        self.assertIsInstance(position, GameEntity)
        self.assertEqual(validate_contract_value(position, {"model": ENTITY}), [])
        args = sample_value("args", f"List[{ENTITY}]")
        self.assertTrue(args)
        self.assertIsInstance(args[0], GameEntity)
        self.assertEqual(validate_contract_value(args, f"List[{ENTITY}]"), [])

    def test_optional_string_samples_keep_choices_in_legacy_description(self):
        for spec in (
            "Optional[str] — one of 'IN' | 'OUT' | 'ADJUST'",
            "str | None — one of 'IN' | 'OUT' | 'ADJUST'",
        ):
            with self.subTest(spec=spec):
                value = sample_value("kind", spec)
                self.assertIn(value, {"IN", "OUT", "ADJUST"})
                self.assertEqual(validate_contract_value(value, spec), [])

    def test_unknown_legacy_type_tokens_remain_compatible_without_claiming_support(self):
        self.assertEqual(normalize_type("TelemetryBatch — a legacy DTO"), "telemetrybatch")
        self.assertEqual(normalize_type("Tuple[int, int]"), "tuple[int,int]")
        self.assertEqual(canonical_contract("Tuple[int, int]")["type"], "tuple[int,int]")
        self.assertEqual(
            schema_compatibility("Tuple[int, int]", "tuple[int,int]"), [],
        )
        self.assertNotEqual(normalize_type("Tuple[int, int]"), "list")

    def test_supported_generic_malformed_annotations_are_rejected(self):
        for spec in ("List[]", "List[int, str]", "List[int", "Dict[int, str]"):
            with self.subTest(spec=spec):
                with self.assertRaises((ValueError, SyntaxError)):
                    canonical_contract(spec)
                with self.assertRaises((ValueError, SyntaxError)):
                    normalize_type(spec)

    def test_type_expressions_are_parsed_without_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            sentinel = Path(tmp) / "executed"
            expression = f"__import__('pathlib').Path({str(sentinel)!r}).write_text('unsafe')"
            for spec in (
                expression, f"List[{expression}]", f"Tuple[int, {expression}]",
                {"type": expression},
            ):
                with self.subTest(spec=spec):
                    with self.assertRaises((ValueError, SyntaxError)):
                        canonical_contract(spec)
                    with self.assertRaises((ValueError, SyntaxError)):
                        normalize_type(spec)
                    self.assertFalse(sentinel.exists())


if __name__ == "__main__":
    unittest.main()
