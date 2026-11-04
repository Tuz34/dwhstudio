"""Pins for the ODI-79 Phase 0 metadata graph export contract."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from metadata_export.contract import (
    CONFLICT_KINDS,
    CONTRACT_VERSION,
    HIDDEN_OBJECT_HANDLING,
    HIDDEN_PLACEHOLDER_LABEL,
    NODE_TYPES,
    PRODUCER,
    RELATIONSHIP_TYPES,
    SENSITIVITY_VALUES,
    STABLE_ID_RE,
    STATUS_VALUES,
    VISIBILITY_VALUES,
    MetadataNode,
    Provenance,
)
from metadata_export.validate import load_fixture, load_schema, validate_document


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "metadata_export"

FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "cx_Oracle",
        "db",
        "flask",
        "flask_limiter",
        "flask_wtf",
        "httpx",
        "odi_automation",
        "odi_helpers",
        "odi_mcp",
        "oracle_mcp",
        "oracledb",
        "pandas",
        "psycopg",
        "psycopg2",
        "pyodbc",
        "pymssql",
        "requests",
        "source_connector",
        "sqlite3",
        "sqlalchemy",
    }
)


def _issues(document: dict) -> list[str]:
    result = validate_document(document)
    return [str(issue) for issue in result.issues]


def _copy(document: dict) -> dict:
    return json.loads(json.dumps(document))


def test_synthetic_fixture_validates():
    document = load_fixture()
    result = validate_document(document)
    assert result.valid, _issues(document)
    assert document["counts"]["nodes"] == len(document["nodes"]) == 13
    assert document["counts"]["edges"] == len(document["edges"]) == 11
    assert document["counts"]["conflicts"] == len(document["conflicts"]) == 1
    assert document["counts"]["hidden_placeholders"] == sum(
        node["visibility"] == "hidden" for node in document["nodes"]
    )
    assert {node["type"] for node in document["nodes"]} == set(NODE_TYPES)
    assert {node["visibility"] for node in document["nodes"]} == set(VISIBILITY_VALUES)
    assert {node["sensitivity"] for node in document["nodes"]} == set(SENSITIVITY_VALUES)
    assert {node["status"] for node in document["nodes"]} == set(STATUS_VALUES)
    assert {edge["relationship"] for edge in document["edges"]} == set(RELATIONSHIP_TYPES)
    assert {edge["explicit"] for edge in document["edges"]} == {True, False}
    assert document["producer"] == PRODUCER


def test_contract_version_is_pinned():
    schema = load_schema()
    assert CONTRACT_VERSION == "1.0.0"
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["properties"]["contract_version"]["const"] == "1.0.0"
    assert schema["properties"]["producer"]["const"] == "dwh-studio"
    assert schema["$defs"]["stable_id"]["pattern"] == STABLE_ID_RE.pattern
    document = load_fixture()
    assert document["contract_version"] == "1.0.0"
    assert document["producer"] == "dwh-studio"


@pytest.mark.parametrize(
    ("constant", "definition", "field_name", "expected"),
    [
        (
            NODE_TYPES,
            "node",
            "type",
            (
                "logical_entity",
                "physical_table",
                "column",
                "schema",
                "odi_mapping",
                "odi_scenario",
                "grant",
                "policy",
                "finding",
                "unknown",
            ),
        ),
        (
            VISIBILITY_VALUES,
            "node",
            "visibility",
            ("public", "internal", "restricted", "hidden"),
        ),
        (
            SENSITIVITY_VALUES,
            "node",
            "sensitivity",
            ("none", "low", "medium", "high", "pii", "secret"),
        ),
        (
            STATUS_VALUES,
            "node",
            "status",
            ("known", "unknown", "missing", "ambiguous", "conflicting"),
        ),
        (
            RELATIONSHIP_TYPES,
            "edge",
            "relationship",
            (
                "contains",
                "references_fk",
                "lineage",
                "maps_to",
                "grants",
                "classified_as",
                "conflicts_with",
                "unknown",
            ),
        ),
        (
            STATUS_VALUES,
            "edge",
            "status",
            ("known", "unknown", "missing", "ambiguous", "conflicting"),
        ),
        (
            CONFLICT_KINDS,
            "conflict",
            "kind",
            ("value_disagreement", "missing_counterpart", "identity_ambiguity", "unknown"),
        ),
        (
            HIDDEN_OBJECT_HANDLING,
            "redaction_policy",
            "hidden_object_handling",
            ("omit", "placeholder"),
        ),
    ],
)
def test_each_enum_is_pinned(constant, definition, field_name, expected):
    assert constant == expected
    schema = load_schema()
    assert schema["$defs"][definition]["properties"][field_name]["enum"] == list(expected)


def test_visibility_and_sensitivity_are_required_and_pinned():
    schema = load_schema()
    required = schema["$defs"]["node"]["required"]
    assert required == [
        "id",
        "type",
        "label",
        "platform",
        "source_adapter",
        "captured_at",
        "visibility",
        "sensitivity",
        "confidence",
        "status",
        "provenance",
    ]
    assert schema["$defs"]["node"]["properties"]["visibility"]["enum"] == [
        "public",
        "internal",
        "restricted",
        "hidden",
    ]
    assert schema["$defs"]["node"]["properties"]["sensitivity"]["enum"] == [
        "none",
        "low",
        "medium",
        "high",
        "pii",
        "secret",
    ]
    assert schema["$defs"]["edge"]["required"] == [
        "id",
        "source_id",
        "target_id",
        "relationship",
        "explicit",
        "provenance",
        "confidence",
        "status",
    ]

    missing_visibility = _copy(load_fixture())
    del missing_visibility["nodes"][0]["visibility"]
    visibility_result = validate_document(missing_visibility)
    assert not visibility_result.valid
    assert any(issue.path.endswith("/visibility") for issue in visibility_result.issues)

    missing_sensitivity = _copy(load_fixture())
    del missing_sensitivity["nodes"][0]["sensitivity"]
    sensitivity_result = validate_document(missing_sensitivity)
    assert not sensitivity_result.valid
    assert any(issue.path.endswith("/sensitivity") for issue in sensitivity_result.issues)

    unknown_visibility = _copy(load_fixture())
    unknown_visibility["nodes"][0]["visibility"] = "partners"
    assert not validate_document(unknown_visibility).valid


def test_hidden_placeholder_has_no_attributes():
    document = load_fixture()
    hidden = [node for node in document["nodes"] if node["visibility"] == "hidden"]
    assert len(hidden) == 1
    placeholder = hidden[0]
    assert placeholder["type"] == "unknown"
    assert placeholder["label"] == HIDDEN_PLACEHOLDER_LABEL
    assert placeholder["visibility"] == "hidden"
    assert "attributes" not in placeholder

    leaked_attributes = _copy(document)
    for node in leaked_attributes["nodes"]:
        if node["visibility"] == "hidden":
            node["attributes"] = {"business_name": "ORDERS_SYNTH"}
    attribute_result = validate_document(leaked_attributes)
    assert not attribute_result.valid
    assert any(issue.path.endswith("/attributes") for issue in attribute_result.issues)

    leaked_label = _copy(document)
    for node in leaked_label["nodes"]:
        if node["visibility"] == "hidden":
            node["label"] = "ORDERS_SYNTH"
    label_result = validate_document(leaked_label)
    assert not label_result.valid
    assert any(issue.path.endswith("/label") for issue in label_result.issues)


def test_omit_policy_drops_hidden_nodes_and_rejects_placeholders():
    rejected = _copy(load_fixture())
    rejected["redaction_policy"]["hidden_object_handling"] = "omit"
    assert not validate_document(rejected).valid

    document = _copy(load_fixture())
    hidden_ids = {node["id"] for node in document["nodes"] if node["visibility"] == "hidden"}
    document["nodes"] = [node for node in document["nodes"] if node["id"] not in hidden_ids]
    document["edges"] = [
        edge
        for edge in document["edges"]
        if edge["source_id"] not in hidden_ids and edge["target_id"] not in hidden_ids
    ]
    document["redaction_policy"]["hidden_object_handling"] = "omit"
    document["counts"]["nodes"] = len(document["nodes"])
    document["counts"]["edges"] = len(document["edges"])
    document["counts"]["hidden_placeholders"] = 0
    result = validate_document(document)
    assert result.valid, [str(issue) for issue in result.issues]
    assert all(node["visibility"] != "hidden" for node in document["nodes"])


def test_referential_integrity_failure_is_detected():
    document = _copy(load_fixture())
    document["edges"][0]["target_id"] = "n-missing-endpoint"
    result = validate_document(document)
    assert not result.valid
    assert any(
        issue.path.endswith("/target_id") and "does not match a node" in issue.message
        for issue in result.issues
    )

    dangling_source = _copy(load_fixture())
    dangling_source["edges"][0]["source_id"] = "n-missing-endpoint"
    source_result = validate_document(dangling_source)
    assert not source_result.valid
    assert any(issue.path.endswith("/source_id") for issue in source_result.issues)


def test_confidence_outside_unit_interval_is_rejected():
    document = _copy(load_fixture())
    document["nodes"][0]["confidence"] = 1.2
    assert not validate_document(document).valid


def test_dataclass_hidden_node_omits_attributes():
    node = MetadataNode(
        id="n-hidden-001",
        type="unknown",
        label=HIDDEN_PLACEHOLDER_LABEL,
        platform="redacted",
        source_adapter="redaction",
        captured_at="2026-01-15T12:00:00Z",
        visibility="hidden",
        sensitivity="secret",
        confidence=1,
        status="known",
        provenance=Provenance(adapter="redaction", evidence_ref="redacted"),
    )
    assert "attributes" not in node.to_dict()
    with pytest.raises(ValueError, match="attributes"):
        MetadataNode(
            id="n-hidden-001",
            type="unknown",
            label=HIDDEN_PLACEHOLDER_LABEL,
            platform="redacted",
            source_adapter="redaction",
            captured_at="2026-01-15T12:00:00Z",
            visibility="hidden",
            sensitivity="secret",
            confidence=1,
            status="known",
            provenance=Provenance(adapter="redaction", evidence_ref="redacted"),
            attributes={"business_name": "ORDERS_SYNTH"},
        )


def test_readme_records_ownership_boundary():
    text = " ".join((PACKAGE_ROOT / "README.md").read_text(encoding="utf-8").split())
    lowered = text.lower()
    assert "AIA owns product, UX, and grounding." in text
    assert "DWH Studio owns adapters and the local security boundary." in text
    assert "synthetic_graph_v1.json" in text
    assert "metadata_graph.schema.json" in text
    assert "without importing dwh studio" in lowered


def _imported_modules(source: str, filename: str) -> list[str]:
    tree = ast.parse(source, filename=filename)
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.append(node.module)
    return modules


def test_package_imports_no_framework_or_connector():
    python_files = sorted(PACKAGE_ROOT.rglob("*.py"))
    assert {path.name for path in python_files} >= {"__init__.py", "contract.py", "validate.py"}
    for path in python_files:
        modules = _imported_modules(path.read_text(encoding="utf-8"), str(path))
        for module_name in modules:
            root = module_name.split(".")[0]
            assert root not in FORBIDDEN_IMPORT_ROOTS, f"{path.name} imports {module_name}"
            assert "connector" not in module_name.lower(), f"{path.name} imports {module_name}"
            assert root not in {"oracledb", "flask"}, f"{path.name} imports {module_name}"
