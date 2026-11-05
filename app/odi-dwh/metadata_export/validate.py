"""Validate a metadata graph document against the Phase 0 contract.

``jsonschema`` is not a project dependency. This module checks the draft
2020-12 keywords the published schema uses, then checks referential integrity
and the redaction rules the schema cannot express with local references.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contract import is_contract_datetime


PACKAGE_ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = PACKAGE_ROOT / "schema" / "metadata_graph.schema.json"
FIXTURE_PATH = PACKAGE_ROOT / "fixtures" / "synthetic_graph_v1.json"

_ANNOTATIONS = frozenset(
    {
        "$comment",
        "$id",
        "$schema",
        "default",
        "deprecated",
        "description",
        "examples",
        "title",
    }
)
_KNOWN_KEYWORDS = _ANNOTATIONS | frozenset(
    {
        "$defs",
        "$ref",
        "additionalProperties",
        "allOf",
        "const",
        "else",
        "enum",
        "format",
        "if",
        "items",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "not",
        "pattern",
        "properties",
        "required",
        "then",
        "type",
    }
)
_SCHEMA_MAP_KEYWORDS = frozenset({"$defs", "properties"})
_SCHEMA_OBJECT_KEYWORDS = frozenset({"else", "if", "items", "not", "then"})


class SchemaError(ValueError):
    """The bundled schema uses a keyword this validator does not implement."""


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    path: str
    message: str

    def __str__(self) -> str:
        location = self.path or "/"
        return f"{location}: {self.message}"


@dataclass(frozen=True, slots=True)
class ValidationResult:
    valid: bool
    issues: tuple[ValidationIssue, ...]


def load_schema() -> dict[str, Any]:
    schema = _read_json(SCHEMA_PATH)
    if not isinstance(schema, dict):
        raise SchemaError("The metadata graph schema must be a JSON object.")
    _assert_supported(schema, "")
    return schema


def load_fixture() -> dict[str, Any]:
    document = _read_json(FIXTURE_PATH)
    if not isinstance(document, dict):
        raise SchemaError("The synthetic fixture must be a JSON object.")
    return document


def validate_document(document: Any, schema: Mapping[str, Any] | None = None) -> ValidationResult:
    """Validate ``document`` against the schema and the graph integrity rules."""
    active_schema: Mapping[str, Any]
    if schema is None:
        active_schema = load_schema()
    else:
        _assert_supported(schema, "")
        active_schema = schema
    issues: list[ValidationIssue] = []
    _reject_non_json_numbers(document, "", issues)
    _validate(document, active_schema, active_schema, "", issues)
    if isinstance(document, Mapping):
        _check_integrity(document, issues)
    return ValidationResult(valid=not issues, issues=tuple(issues))


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_supported(schema: Mapping[str, Any] | Any, path: str) -> None:
    if not isinstance(schema, Mapping):
        raise SchemaError(f"Schema at {path or '/'} must be an object.")
    unknown = sorted(set(schema) - _KNOWN_KEYWORDS)
    if unknown:
        names = ", ".join(unknown)
        raise SchemaError(f"Unsupported schema keyword(s) {names} at {path or '/'}.")
    if "$ref" in schema:
        extras = set(schema) - {"$ref"} - _ANNOTATIONS
        if extras:
            names = ", ".join(sorted(extras))
            raise SchemaError(f"$ref must stand alone at {path or '/'}; also found {names}.")
        if not isinstance(schema["$ref"], str) or not schema["$ref"].startswith("#/"):
            raise SchemaError(f"$ref at {path or '/'} must be a local JSON pointer.")
    for keyword in _SCHEMA_OBJECT_KEYWORDS:
        if keyword in schema:
            _assert_supported(schema[keyword], f"{path}/{keyword}")
    for keyword in _SCHEMA_MAP_KEYWORDS:
        if keyword not in schema:
            continue
        child_map = schema[keyword]
        if not isinstance(child_map, Mapping):
            raise SchemaError(f"{keyword} at {path or '/'} must be an object.")
        for name, child in child_map.items():
            _assert_supported(child, f"{path}/{keyword}/{name}")
    if "allOf" in schema:
        branches = schema["allOf"]
        if not isinstance(branches, list):
            raise SchemaError(f"allOf at {path or '/'} must be an array.")
        for index, branch in enumerate(branches):
            _assert_supported(branch, f"{path}/allOf/{index}")
    if "additionalProperties" in schema:
        additional = schema["additionalProperties"]
        if isinstance(additional, Mapping):
            _assert_supported(additional, f"{path}/additionalProperties")
        elif additional not in {True, False}:
            raise SchemaError(f"additionalProperties at {path or '/'} must be a boolean or a schema.")
    if "enum" in schema and not isinstance(schema["enum"], list):
        raise SchemaError(f"enum at {path or '/'} must be an array.")
    if "pattern" in schema:
        if not isinstance(schema["pattern"], str):
            raise SchemaError(f"pattern at {path or '/'} must be a string.")
        re.compile(schema["pattern"])
    if "format" in schema and schema["format"] != "date-time":
        raise SchemaError(f"Unsupported format {schema['format']!r} at {path or '/'}.")


def _resolve_ref(root: Mapping[str, Any], ref: str) -> Mapping[str, Any]:
    if not ref.startswith("#/"):
        raise SchemaError(f"Only local $ref pointers are supported, got {ref!r}.")
    node: Any = root
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, Mapping) or part not in node:
            raise SchemaError(f"Unresolvable $ref {ref}.")
        node = node[part]
    if not isinstance(node, Mapping):
        raise SchemaError(f"$ref {ref} did not resolve to a schema object.")
    return node


def _type_ok(instance: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return any(_type_ok(instance, item) for item in expected)
    if expected == "object":
        return isinstance(instance, Mapping)
    if expected == "array":
        return isinstance(instance, list)
    if expected == "string":
        return isinstance(instance, str)
    if expected == "boolean":
        return isinstance(instance, bool)
    if expected == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if expected == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if expected == "null":
        return instance is None
    raise SchemaError(f"Unsupported JSON Schema type {expected!r}.")


def _join(path: str, part: object) -> str:
    return f"{path}/{part}"


def _json_equal(left: Any, right: Any) -> bool:
    """JSON equality. Booleans are distinct from numbers, including 0 and 1."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if isinstance(left, float) and not math.isfinite(left):
            return False
        if isinstance(right, float) and not math.isfinite(right):
            return False
        return left == right
    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left == right
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, list) or isinstance(right, list):
        if not isinstance(left, list) or not isinstance(right, list) or len(left) != len(right):
            return False
        return all(_json_equal(item, other) for item, other in zip(left, right, strict=True))
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        if set(left) != set(right):
            return False
        return all(_json_equal(left[key], right[key]) for key in left)
    return False


def _json_in(instance: Any, options: Any) -> bool:
    if not isinstance(options, list):
        raise SchemaError("enum must be an array.")
    return any(_json_equal(instance, option) for option in options)


_NONFINITE_MESSAGE = "Numeric value is not a finite JSON number."
_CYCLE_MESSAGE = "Value contains a cyclic container."


def _attributes_boundary(path: str) -> bool:
    return path == "attributes" or path.endswith("/attributes")


def _reject_non_json_numbers(instance: Any, path: str, issues: list[ValidationIssue]) -> None:
    """Walk objects, arrays, and tuples on an explicit stack.

    Free-form ``attributes`` values stay out of diagnostic paths. The contract
    field name is kept; keys and indexes inside that object are not.
    A container already present on the active ancestor chain is a cycle.
    The same container reached again after that chain has moved on is acyclic
    and is walked again.
    """
    # Each frame is ("visit", value, path, inside_attributes) or ("leave", identity, "", False).
    frames: list[tuple[str, Any, str, bool]] = [("visit", instance, path, False)]
    ancestors: set[int] = set()
    while frames:
        kind, current, current_path, inside_flag = frames.pop()
        if kind == "leave":
            ancestors.discard(current)
            continue
        inside_attributes = inside_flag or _attributes_boundary(current_path)
        if isinstance(current, bool):
            continue
        if isinstance(current, float) and not math.isfinite(current):
            issues.append(ValidationIssue(current_path or "/", _NONFINITE_MESSAGE))
            continue
        if not isinstance(current, (Mapping, list, tuple)):
            continue
        identity = id(current)
        if identity in ancestors:
            issues.append(ValidationIssue(current_path or "/", _CYCLE_MESSAGE))
            continue
        ancestors.add(identity)
        frames.append(("leave", identity, "", False))
        children: list[tuple[Any, str, bool]] = []
        if isinstance(current, Mapping):
            for name, value in current.items():
                if inside_attributes:
                    children.append((value, current_path, True))
                elif name == "attributes":
                    children.append((value, _join(current_path, name), True))
                else:
                    children.append((value, _join(current_path, name), False))
        else:
            for index, item in enumerate(current):
                if inside_attributes:
                    children.append((item, current_path, True))
                else:
                    children.append((item, _join(current_path, index), False))
        for value, child_path, child_inside in reversed(children):
            frames.append(("visit", value, child_path, child_inside))


def _validate(
    instance: Any,
    schema: Mapping[str, Any],
    root: Mapping[str, Any],
    path: str,
    issues: list[ValidationIssue],
) -> None:
    if "$ref" in schema:
        _validate(instance, _resolve_ref(root, str(schema["$ref"])), root, path, issues)
        return
    expected = schema.get("type")
    if expected is not None and not _type_ok(instance, expected):
        issues.append(ValidationIssue(path or "/", f"Expected type {expected}."))
        return
    if "const" in schema and not _json_equal(instance, schema["const"]):
        issues.append(ValidationIssue(path or "/", f"Expected {schema['const']!r}."))
    if "enum" in schema and not _json_in(instance, schema["enum"]):
        issues.append(ValidationIssue(path or "/", "Value is outside the allowed set."))
    if "not" in schema:
        nested: list[ValidationIssue] = []
        _validate(instance, schema["not"], root, path, nested)
        if not nested:
            issues.append(ValidationIssue(path or "/", "Value is excluded by the schema."))
    if "allOf" in schema:
        for branch in schema["allOf"]:
            _validate(instance, branch, root, path, issues)
    if "if" in schema:
        probe: list[ValidationIssue] = []
        _validate(instance, schema["if"], root, path, probe)
        if not probe:
            if "then" in schema:
                _validate(instance, schema["then"], root, path, issues)
        elif "else" in schema:
            _validate(instance, schema["else"], root, path, issues)
    object_keywords = ("properties", "required", "additionalProperties")
    if isinstance(instance, Mapping) and any(keyword in schema for keyword in object_keywords):
        _validate_object(instance, schema, root, path, issues)
    array_keywords = ("items", "minItems", "maxItems")
    if isinstance(instance, list) and any(keyword in schema for keyword in array_keywords):
        _validate_array(instance, schema, root, path, issues)
    string_keywords = ("minLength", "maxLength", "pattern", "format")
    if isinstance(instance, str) and any(keyword in schema for keyword in string_keywords):
        _validate_string(instance, schema, path, issues)
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if any(keyword in schema for keyword in ("minimum", "maximum")):
            _validate_number(instance, schema, path, issues)


def _validate_object(
    instance: Mapping[str, Any],
    schema: Mapping[str, Any],
    root: Mapping[str, Any],
    path: str,
    issues: list[ValidationIssue],
) -> None:
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise SchemaError("properties must be an object.")
    for name in schema.get("required", []):
        if name not in instance:
            issues.append(ValidationIssue(_join(path, name), "Required property is missing."))
    additional = schema.get("additionalProperties", True)
    for name, value in instance.items():
        child_path = _join(path, name)
        if name in properties:
            _validate(value, properties[name], root, child_path, issues)
        elif additional is False:
            issues.append(ValidationIssue(child_path, "Additional property is not allowed."))
        elif isinstance(additional, Mapping):
            _validate(value, additional, root, child_path, issues)


def _validate_array(
    instance: list[Any],
    schema: Mapping[str, Any],
    root: Mapping[str, Any],
    path: str,
    issues: list[ValidationIssue],
) -> None:
    minimum = schema.get("minItems")
    if isinstance(minimum, int) and len(instance) < minimum:
        issues.append(ValidationIssue(path or "/", f"Expected at least {minimum} items."))
    maximum = schema.get("maxItems")
    if isinstance(maximum, int) and len(instance) > maximum:
        issues.append(ValidationIssue(path or "/", f"Expected at most {maximum} items."))
    item_schema = schema.get("items")
    if isinstance(item_schema, Mapping):
        for index, item in enumerate(instance):
            _validate(item, item_schema, root, _join(path, index), issues)


def _validate_string(instance: str, schema: Mapping[str, Any], path: str, issues: list[ValidationIssue]) -> None:
    minimum = schema.get("minLength")
    if isinstance(minimum, int) and len(instance) < minimum:
        issues.append(ValidationIssue(path or "/", "String is shorter than minLength."))
    maximum = schema.get("maxLength")
    if isinstance(maximum, int) and len(instance) > maximum:
        issues.append(ValidationIssue(path or "/", "String is longer than maxLength."))
    pattern = schema.get("pattern")
    # Draft 2020-12 patterns are not implicitly anchored. The schema supplies ^ and $.
    if isinstance(pattern, str) and re.search(pattern, instance) is None:
        issues.append(ValidationIssue(path or "/", "String does not match the required pattern."))
    if schema.get("format") == "date-time" and not is_contract_datetime(instance):
        issues.append(ValidationIssue(path or "/", "String is not an ISO-8601 date-time with a timezone."))


def _validate_number(
    instance: int | float,
    schema: Mapping[str, Any],
    path: str,
    issues: list[ValidationIssue],
) -> None:
    minimum = schema.get("minimum")
    if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) and instance < minimum:
        issues.append(ValidationIssue(path or "/", f"Value is below the minimum of {minimum}."))
    maximum = schema.get("maximum")
    if isinstance(maximum, (int, float)) and not isinstance(maximum, bool) and instance > maximum:
        issues.append(ValidationIssue(path or "/", f"Value is above the maximum of {maximum}."))


def _check_integrity(document: Mapping[str, Any], issues: list[ValidationIssue]) -> None:
    nodes = document.get("nodes")
    edges = document.get("edges")
    conflicts = document.get("conflicts")
    node_ids = _index_ids(nodes, "nodes", issues)
    edge_ids = _index_ids(edges, "edges", issues)
    conflict_ids = _index_ids(conflicts, "conflicts", issues)
    if isinstance(edges, list) and node_ids is not None:
        _check_edge_endpoints(edges, node_ids, issues)
    if isinstance(conflicts, list) and node_ids is not None and edge_ids is not None:
        _check_conflict_subjects(conflicts, node_ids, edge_ids, issues)
    if isinstance(nodes, list) and conflict_ids is not None:
        _check_status_links(nodes, "nodes", conflict_ids, issues)
    if isinstance(edges, list) and conflict_ids is not None:
        _check_status_links(edges, "edges", conflict_ids, issues)
    if isinstance(nodes, list):
        _check_redaction(document, nodes, issues)
    _check_counts(document, nodes, edges, conflicts, issues)


def _index_ids(items: Any, label: str, issues: list[ValidationIssue]) -> dict[str, Mapping[str, Any]] | None:
    if not isinstance(items, list):
        return None
    found: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str):
            continue
        if item_id in found:
            issues.append(ValidationIssue(_join(f"/{label}", index) + "/id", f"Duplicate id {item_id!r}."))
            continue
        found[item_id] = item
    return found


def _check_edge_endpoints(
    edges: list[Any],
    node_ids: Mapping[str, Any],
    issues: list[ValidationIssue],
) -> None:
    for index, edge in enumerate(edges):
        if not isinstance(edge, Mapping):
            continue
        source_id = edge.get("source_id")
        target_id = edge.get("target_id")
        if isinstance(source_id, str) and source_id not in node_ids:
            issues.append(
                ValidationIssue(
                    f"/edges/{index}/source_id",
                    f"source_id {source_id!r} does not match a node.",
                )
            )
        if isinstance(target_id, str) and target_id not in node_ids:
            issues.append(
                ValidationIssue(
                    f"/edges/{index}/target_id",
                    f"target_id {target_id!r} does not match a node.",
                )
            )


def _check_conflict_subjects(
    conflicts: list[Any],
    node_ids: Mapping[str, Any],
    edge_ids: Mapping[str, Any],
    issues: list[ValidationIssue],
) -> None:
    known = set(node_ids) | set(edge_ids)
    for index, conflict in enumerate(conflicts):
        if not isinstance(conflict, Mapping):
            continue
        subjects = conflict.get("subject_ids")
        if not isinstance(subjects, list):
            continue
        for subject_index, subject_id in enumerate(subjects):
            if isinstance(subject_id, str) and subject_id not in known:
                issues.append(
                    ValidationIssue(
                        f"/conflicts/{index}/subject_ids/{subject_index}",
                        f"subject_id {subject_id!r} does not match a node or an edge.",
                    )
                )


def _check_status_links(
    items: list[Any],
    label: str,
    conflict_ids: Mapping[str, Any],
    issues: list[ValidationIssue],
) -> None:
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            continue
        status = item.get("status")
        linked = item.get("conflict_ids")
        path = f"/{label}/{index}/conflict_ids"
        if status == "conflicting":
            if not isinstance(linked, list) or not linked:
                issues.append(ValidationIssue(path, "A conflicting object must list conflict_ids."))
                continue
            for link_index, conflict_id in enumerate(linked):
                if not isinstance(conflict_id, str) or conflict_id not in conflict_ids:
                    issues.append(
                        ValidationIssue(
                            f"{path}/{link_index}",
                            f"conflict_id {conflict_id!r} does not match a conflict.",
                        )
                    )
        elif linked:
            issues.append(ValidationIssue(path, "conflict_ids is only allowed when status is conflicting."))


def _check_redaction(document: Mapping[str, Any], nodes: list[Any], issues: list[ValidationIssue]) -> None:
    policy = document.get("redaction_policy")
    if not isinstance(policy, Mapping):
        return
    handling = policy.get("hidden_object_handling")
    placeholder_label = policy.get("placeholder_label")
    placeholder_type = policy.get("placeholder_type")
    strip_attributes = policy.get("strip_attributes") is True
    for index, node in enumerate(nodes):
        if not isinstance(node, Mapping):
            continue
        visibility = node.get("visibility")
        label = node.get("label")
        hidden = visibility == "hidden" or label == placeholder_label
        if handling == "omit" and visibility == "hidden":
            issues.append(
                ValidationIssue(
                    f"/nodes/{index}",
                    "Hidden object was emitted, but the redaction policy omits hidden objects.",
                )
            )
        if not hidden:
            continue
        if label != placeholder_label:
            issues.append(ValidationIssue(f"/nodes/{index}/label", "Hidden object leaks a label."))
        if node.get("type") != placeholder_type:
            issues.append(ValidationIssue(f"/nodes/{index}/type", "Hidden object must use the placeholder type."))
        if visibility != "hidden":
            issues.append(
                ValidationIssue(
                    f"/nodes/{index}/visibility",
                    "Placeholder label is only valid when visibility is hidden.",
                )
            )
        if strip_attributes and "attributes" in node:
            issues.append(ValidationIssue(f"/nodes/{index}/attributes", "Hidden object leaks attributes."))


def _check_counts(
    document: Mapping[str, Any],
    nodes: Any,
    edges: Any,
    conflicts: Any,
    issues: list[ValidationIssue],
) -> None:
    counts = document.get("counts")
    if not isinstance(counts, Mapping):
        return
    expected: dict[str, int] = {}
    if isinstance(nodes, list):
        expected["nodes"] = len(nodes)
        expected["hidden_placeholders"] = sum(
            1 for node in nodes if isinstance(node, Mapping) and node.get("visibility") == "hidden"
        )
    if isinstance(edges, list):
        expected["edges"] = len(edges)
    if isinstance(conflicts, list):
        expected["conflicts"] = len(conflicts)
    for key, value in expected.items():
        if counts.get(key) != value:
            issues.append(ValidationIssue(f"/counts/{key}", f"Count does not match the document (expected {value})."))
