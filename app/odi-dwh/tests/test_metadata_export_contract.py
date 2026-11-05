"""Pins for the ODI-79 Phase 0 metadata graph export contract."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
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
from metadata_export.validate import SchemaError, load_fixture, load_schema, validate_document


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


def test_json_const_and_enum_keep_booleans_distinct_from_numbers():
    document = _copy(load_fixture())
    document["redaction_policy"]["strip_attributes"] = 1
    result = validate_document(document)
    assert not result.valid
    assert any(issue.path.endswith("/strip_attributes") for issue in result.issues)

    numeric_const = {"const": 1}
    assert not validate_document(True, numeric_const).valid
    assert validate_document(1, numeric_const).valid
    assert validate_document(1.0, numeric_const).valid
    assert not validate_document(False, {"const": 0}).valid
    assert validate_document(0, {"const": 0}).valid
    assert not validate_document(0, {"const": False}).valid
    assert validate_document(False, {"const": False}).valid

    boolean_enum = {"enum": [True]}
    assert not validate_document(1, boolean_enum).valid
    assert validate_document(True, boolean_enum).valid


def _nested_attributes(depth: int, leaf: object) -> dict[str, object]:
    current: dict[str, object] = {"nested_level": leaf}
    for _ in range(depth - 1):
        current = {"nested_level": current}
    return current


def _diagnostics(result: object) -> str:
    issues = getattr(result, "issues")
    lines = [repr(result), str(getattr(result, "valid"))]
    for issue in issues:
        lines.extend((issue.path, issue.message, str(issue)))
    return "\n".join(lines)


def test_nan_and_infinities_are_rejected_in_json_containers():
    nan_document = _copy(load_fixture())
    nan_document["nodes"][0]["attributes"] = {"synthetic_ratio": float("nan")}
    nan_result = validate_document(nan_document)
    assert not nan_result.valid
    assert any(issue.path.endswith("/attributes") and "finite" in issue.message for issue in nan_result.issues)
    assert all("synthetic_ratio" not in issue.path for issue in nan_result.issues)

    infinite_document = _copy(load_fixture())
    infinite_document["nodes"][0]["confidence"] = float("inf")
    infinite_result = validate_document(infinite_document)
    assert not infinite_result.valid
    assert any(issue.path.endswith("/confidence") for issue in infinite_result.issues)

    nested_document = _copy(load_fixture())
    nested_document["nodes"][0]["attributes"] = {"samples": [0.5, float("-inf")]}
    nested_result = validate_document(nested_document)
    assert not nested_result.valid
    assert any(issue.path.endswith("/attributes") and "finite" in issue.message for issue in nested_result.issues)
    assert all("/attributes/" not in issue.path for issue in nested_result.issues)


def test_hidden_placeholder_nonfinite_diagnostics_omit_attribute_sentinel():
    document = _copy(load_fixture())
    index = next(i for i, node in enumerate(document["nodes"]) if node["visibility"] == "hidden")
    document["nodes"][index]["attributes"] = {
        "SENTINEL_KEY": {"marker": "SENTINEL_VALUE", "ratio": float("nan")},
    }
    result = validate_document(document)
    diagnostic = _diagnostics(result)
    assert not result.valid
    assert "SENTINEL_KEY" not in diagnostic
    assert "SENTINEL_VALUE" not in diagnostic
    assert any(issue.path == f"/nodes/{index}/attributes" and "finite" in issue.message for issue in result.issues)
    assert all("/attributes/" not in issue.path for issue in result.issues)


_CYCLE_MESSAGE = "Value contains a cyclic container."

_MAPPING_CYCLE_SCRIPT = r"""
import json
from metadata_export.validate import _reject_non_json_numbers

cyclic = {}
cyclic["ratio"] = float("nan")
cyclic["SENTINEL_KEY"] = cyclic
cyclic["marker"] = "SENTINEL_VALUE"
issues = []
_reject_non_json_numbers(cyclic, "/nodes/0/attributes", issues)
print(json.dumps({
    "paths": [issue.path for issue in issues],
    "messages": [issue.message for issue in issues],
    "rendered": [str(issue) for issue in issues],
}))
"""

_CONTAINER_CYCLE_SCRIPT = r"""
import json
from metadata_export.validate import load_fixture, validate_document

def fresh():
    return json.loads(json.dumps(load_fixture()))

def pack(result):
    return {
        "valid": result.valid,
        "paths": [issue.path for issue in result.issues],
        "messages": [issue.message for issue in result.issues],
        "rendered": [str(issue) for issue in result.issues],
    }

listed = fresh()
through_list = {}
through_list["SENTINEL_KEY"] = "SENTINEL_VALUE"
through_list["via"] = [through_list, float("nan")]
listed["nodes"][0]["attributes"] = through_list

tupled = fresh()
through_tuple = {}
through_tuple["SENTINEL_KEY"] = "SENTINEL_VALUE"
through_tuple["via"] = (float("-inf"), through_tuple)
tupled["nodes"][0]["attributes"] = through_tuple

print(json.dumps({
    "list": pack(validate_document(listed)),
    "tuple": pack(validate_document(tupled)),
}))
"""


def _child_env() -> dict[str, str]:
    kept = ("SYSTEMROOT", "PATH", "PATHEXT", "TEMP", "TMP", "WINDIR", "COMSPEC")
    env = {key: os.environ[key] for key in kept if key in os.environ}
    env["PYTHONPATH"] = str(PACKAGE_ROOT.parent)
    env["RUN_LIVE_DB_TESTS"] = "false"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run_bounded_script(script: str, timeout: float = 2.0) -> dict[str, object]:
    """Run a validator snippet in a child and kill the child when the timeout expires."""
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=PACKAGE_ROOT.parent,
        env=_child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail(f"validator exceeded {timeout:.0f}s and was killed")
    assert proc.returncode == 0, stderr
    payload = json.loads(stdout)
    assert isinstance(payload, dict)
    return payload


def _assert_redacted_attribute_diagnostics(payload: dict[str, object], path: str) -> None:
    paths = payload["paths"]
    messages = payload["messages"]
    rendered = payload["rendered"]
    assert isinstance(paths, list)
    assert isinstance(messages, list)
    assert isinstance(rendered, list)
    diagnostic = "\n".join(str(line) for line in rendered)
    assert "SENTINEL_KEY" not in diagnostic
    assert "SENTINEL_VALUE" not in diagnostic
    assert paths
    assert all(item == path for item in paths)
    assert all("/attributes/" not in str(item) for item in paths)
    assert _CYCLE_MESSAGE in messages
    assert any("finite" in str(item) for item in messages)


def test_mapping_self_cycle_finishes_with_redacted_diagnostics():
    payload = _run_bounded_script(_MAPPING_CYCLE_SCRIPT)
    _assert_redacted_attribute_diagnostics(payload, "/nodes/0/attributes")


def test_list_and_tuple_cycles_finish_with_redacted_diagnostics():
    payload = _run_bounded_script(_CONTAINER_CYCLE_SCRIPT)
    for kind in ("list", "tuple"):
        section = payload[kind]
        assert isinstance(section, dict)
        assert section["valid"] is False
        _assert_redacted_attribute_diagnostics(section, "/nodes/0/attributes")


def test_shared_acyclic_containers_are_not_cycles():
    document = _copy(load_fixture())
    shared_map = {"finite": 1, "nested": {"ok": True}}
    shared_list = [0.25, {"finite": 0.5}]
    shared_tuple = (0.25, 0.75)
    document["nodes"][0]["attributes"] = {
        "left": shared_map,
        "right": shared_map,
        "samples": shared_list,
        "samples_again": shared_list,
        "pair": shared_tuple,
        "pair_again": shared_tuple,
        "empty_left": (),
        "empty_right": (),
    }
    result = validate_document(document)
    assert result.valid, _diagnostics(result)
    assert all("cyclic" not in issue.message for issue in result.issues)

    nan_document = _copy(load_fixture())
    shared_nan = {"ratio": float("nan")}
    nan_document["nodes"][0]["attributes"] = {"left": shared_nan, "right": shared_nan}
    rejected = validate_document(nan_document)
    diagnostic = _diagnostics(rejected)
    assert not rejected.valid
    assert any(issue.path == "/nodes/0/attributes" and "finite" in issue.message for issue in rejected.issues)
    assert all("cyclic" not in issue.message for issue in rejected.issues)
    assert "ratio" not in diagnostic


def test_deep_attribute_nest_returns_a_validation_result():
    document = _copy(load_fixture())
    document["nodes"][0]["attributes"] = _nested_attributes(1500, {"finite": 1})
    result = validate_document(document)
    assert type(result).__name__ == "ValidationResult"
    assert result.valid, _diagnostics(result)


@pytest.mark.parametrize("leaf,expected_valid", [("1.0", True), ('float("nan")', False)])
def test_heavily_shared_attributes_finish_without_exponential_revisits(leaf, expected_valid):
    script = f'''
import json
from metadata_export.validate import load_fixture, validate_document
document = load_fixture()
value = [{leaf}]
for _ in range(40):
    value = [value, value]
document["nodes"][0]["attributes"] = {{"SENTINEL_KEY": value}}
result = validate_document(document)
print(json.dumps({{"valid": result.valid, "issues": [str(issue) for issue in result.issues]}}))
'''
    payload = _run_bounded_script(script, timeout=5.0)
    assert payload["valid"] is expected_valid
    assert "SENTINEL_KEY" not in str(payload["issues"])
    if not expected_valid:
        assert any("finite" in issue for issue in payload["issues"])
        assert len(payload["issues"]) <= 2


def test_cyclic_nonstring_redaction_labels_return_invalid_without_recursive_equality():
    script = '''
import json
from metadata_export.validate import load_fixture, validate_document
document = load_fixture()
left, right = [], []
left.append(left)
right.append(right)
document["nodes"][0]["label"] = left
document["redaction_policy"]["placeholder_label"] = right
result = validate_document(document)
print(json.dumps({"valid": result.valid, "issues": [str(issue) for issue in result.issues]}))
'''
    payload = _run_bounded_script(script, timeout=5.0)
    assert payload["valid"] is False
    assert any("Expected type string" in issue for issue in payload["issues"])


def test_deep_nonstring_conflict_id_has_fixed_bounded_diagnostic():
    document = _copy(load_fixture())
    item = next(node for node in document["nodes"] if node.get("status") == "conflicting")
    value = "SENTINEL_PRIVATE_VALUE"
    for _ in range(1500):
        value = [value]
    item["conflict_ids"] = [value]
    result = validate_document(document)
    assert not result.valid
    assert any("does not match a conflict" in issue.message for issue in result.issues)
    assert all(len(issue.message) < 160 for issue in result.issues)
    assert "SENTINEL_PRIVATE_VALUE" not in _diagnostics(result)


def test_ephemeral_mapping_containers_are_retained_until_the_walk_finishes():
    script = '''
import json
import weakref
from collections.abc import Mapping
from metadata_export.validate import load_fixture, validate_document
seen = []
released_during_walk = []
class TrackedList(list):
    pass
class DynamicChildren(Mapping):
    def __init__(self, poison): self.poison = poison
    def __iter__(self): return iter(range(50))
    def __len__(self): return 50
    def __getitem__(self, index):
        if self.poison and index == 0:
            released_during_walk.append(any(ref() is None for ref in seen))
        value = TrackedList([float("nan") if self.poison and index == 25 else 1.0])
        seen.append(weakref.ref(value))
        return value
class DynamicOuter(Mapping):
    def __iter__(self): return iter(range(2))
    def __len__(self): return 2
    def __getitem__(self, index): return DynamicChildren(index == 1)
document = load_fixture()
document["nodes"][0]["attributes"] = DynamicOuter()
result = validate_document(document)
print(json.dumps({"valid": result.valid, "released": released_during_walk,
                  "messages": [issue.message for issue in result.issues]}))
'''
    payload = _run_bounded_script(script, timeout=5.0)
    assert payload["released"] == [False]
    assert payload["valid"] is False
    assert any("finite" in message for message in payload["messages"])


@pytest.mark.parametrize("bad_field", ["absent", "placeholder_label", "placeholder_type"])
def test_custom_schema_cannot_bypass_malformed_redaction_policy(bad_field):
    document = _copy(load_fixture())
    node = document["nodes"][0]
    node["visibility"] = "hidden"
    node["label"] = "SENTINEL_REAL_LABEL"
    node["attributes"] = {"SENTINEL_KEY": "SENTINEL_VALUE"}
    document["counts"]["hidden_placeholders"] = sum(
        entry["visibility"] == "hidden" for entry in document["nodes"]
    )
    policy = document["redaction_policy"]
    if bad_field == "absent":
        policy.pop("placeholder_label")
    else:
        policy[bad_field] = 7
    result = validate_document(document, {})
    assert not result.valid
    assert any(issue.path == "/redaction_policy" for issue in result.issues)
    assert "SENTINEL" not in _diagnostics(result)


def test_deep_nonfinite_attribute_is_rejected_without_recursion_error():
    document = _copy(load_fixture())
    document["nodes"][0]["attributes"] = _nested_attributes(1100, float("nan"))
    result = validate_document(document)
    assert type(result).__name__ == "ValidationResult"
    assert not result.valid
    assert any(issue.path == "/nodes/0/attributes" and "finite" in issue.message for issue in result.issues)
    assert all("nested_level" not in f"{issue.path}\n{issue.message}" for issue in result.issues)


def test_tuple_nan_is_rejected_and_finite_tuple_values_stay_in_bounds():
    finite = _copy(load_fixture())
    finite["nodes"][0]["attributes"] = {"pair": (0.25, 0.75)}
    assert validate_document(finite).valid

    nonfinite = _copy(load_fixture())
    nonfinite["nodes"][0]["attributes"] = {"pair": (0.25, float("nan"))}
    rejected = validate_document(nonfinite)
    assert not rejected.valid
    assert any(issue.path == "/nodes/0/attributes" and "finite" in issue.message for issue in rejected.issues)
    assert all("pair" not in issue.path for issue in rejected.issues)

    array_position = _copy(load_fixture())
    conflicting = next(node for node in array_position["nodes"] if node.get("status") == "conflicting")
    conflicting["conflict_ids"] = tuple(conflicting["conflict_ids"])
    assert not validate_document(array_position).valid


def test_enum_shape_is_checked_before_an_instance_visits_it():
    schema = {"type": "object", "properties": {"skipped": {"enum": "not-an-array"}}}
    with pytest.raises(SchemaError, match="enum"):
        validate_document({}, schema)


def test_duplicate_node_id_is_rejected():
    document = _copy(load_fixture())
    document["nodes"][1]["id"] = document["nodes"][0]["id"]
    result = validate_document(document)
    assert not result.valid
    assert any("Duplicate id" in issue.message and issue.path.endswith("/id") for issue in result.issues)


def test_count_mismatch_is_rejected():
    document = _copy(load_fixture())
    document["counts"]["nodes"] = int(document["counts"]["nodes"]) + 1
    result = validate_document(document)
    assert not result.valid
    assert any(issue.path == "/counts/nodes" for issue in result.issues)


def test_conflicting_object_without_conflict_ids_is_rejected():
    document = _copy(load_fixture())
    conflicting = next(node for node in document["nodes"] if node.get("status") == "conflicting")
    del conflicting["conflict_ids"]
    result = validate_document(document)
    assert not result.valid
    assert any(
        issue.path.endswith("/conflict_ids") and "must list conflict_ids" in issue.message
        for issue in result.issues
    )


def test_dangling_conflict_id_is_rejected():
    document = _copy(load_fixture())
    conflicting = next(node for node in document["nodes"] if node.get("status") == "conflicting")
    conflicting["conflict_ids"] = ["cfl-missing"]
    result = validate_document(document)
    assert not result.valid
    assert any("does not match a conflict" in issue.message for issue in result.issues)


def test_nonboolean_explicit_is_rejected():
    document = _copy(load_fixture())
    document["edges"][0]["explicit"] = 1
    result = validate_document(document)
    assert not result.valid
    assert any(issue.path.endswith("/explicit") and "boolean" in issue.message for issue in result.issues)


def test_wrong_contract_version_is_rejected():
    document = _copy(load_fixture())
    document["contract_version"] = "9.9.9"
    result = validate_document(document)
    assert not result.valid
    assert any(issue.path.endswith("/contract_version") for issue in result.issues)


def test_missing_edge_field_is_rejected():
    document = _copy(load_fixture())
    del document["edges"][0]["relationship"]
    result = validate_document(document)
    assert not result.valid
    assert any(
        issue.path.endswith("/relationship") and "missing" in issue.message.lower()
        for issue in result.issues
    )


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
