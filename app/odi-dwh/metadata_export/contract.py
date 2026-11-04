"""In-memory types for the versioned metadata graph export contract.

The JSON Schema in ``schema/metadata_graph.schema.json`` is the normative
document shape. These types are the DWH Studio side of that same contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, NotRequired, TypedDict


CONTRACT_VERSION: str = "1.0.0"
PRODUCER: str = "dwh-studio"
HIDDEN_PLACEHOLDER_LABEL: str = "<hidden>"

NODE_TYPES: tuple[str, ...] = (
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
)
VISIBILITY_VALUES: tuple[str, ...] = ("public", "internal", "restricted", "hidden")
SENSITIVITY_VALUES: tuple[str, ...] = ("none", "low", "medium", "high", "pii", "secret")
RELATIONSHIP_TYPES: tuple[str, ...] = (
    "contains",
    "references_fk",
    "lineage",
    "maps_to",
    "grants",
    "classified_as",
    "conflicts_with",
    "unknown",
)
STATUS_VALUES: tuple[str, ...] = ("known", "unknown", "missing", "ambiguous", "conflicting")
HIDDEN_OBJECT_HANDLING: tuple[str, ...] = ("omit", "placeholder")
CONFLICT_KINDS: tuple[str, ...] = (
    "value_disagreement",
    "missing_counterpart",
    "identity_ambiguity",
    "unknown",
)

NodeType = Literal[
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
]
Visibility = Literal["public", "internal", "restricted", "hidden"]
Sensitivity = Literal["none", "low", "medium", "high", "pii", "secret"]
Relationship = Literal[
    "contains",
    "references_fk",
    "lineage",
    "maps_to",
    "grants",
    "classified_as",
    "conflicts_with",
    "unknown",
]
ObjectStatus = Literal["known", "unknown", "missing", "ambiguous", "conflicting"]
HiddenObjectHandling = Literal["omit", "placeholder"]
ConflictKind = Literal["value_disagreement", "missing_counterpart", "identity_ambiguity", "unknown"]

STABLE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")


class ProvenanceDict(TypedDict):
    adapter: str
    evidence_ref: str


class GraphNodeDict(TypedDict):
    id: str
    type: NodeType
    label: str
    platform: str
    source_adapter: str
    captured_at: str
    visibility: Visibility
    sensitivity: Sensitivity
    confidence: float
    status: ObjectStatus
    provenance: ProvenanceDict
    attributes: NotRequired[dict[str, Any]]
    conflict_ids: NotRequired[list[str]]


class GraphEdgeDict(TypedDict):
    id: str
    source_id: str
    target_id: str
    relationship: Relationship
    explicit: bool
    provenance: ProvenanceDict
    confidence: float
    status: ObjectStatus
    conflict_ids: NotRequired[list[str]]


class ConflictDict(TypedDict):
    id: str
    subject_ids: list[str]
    summary: str
    kind: ConflictKind


class RedactionPolicyDict(TypedDict):
    hidden_object_handling: HiddenObjectHandling
    placeholder_label: str
    placeholder_type: Literal["unknown"]
    placeholder_visibility: Literal["hidden"]
    strip_attributes: Literal[True]
    description: str


class CountDict(TypedDict):
    nodes: int
    edges: int
    conflicts: int
    hidden_placeholders: int


class MetadataGraphDict(TypedDict):
    contract_version: str
    generated_at: str
    producer: Literal["dwh-studio"]
    redaction_policy: RedactionPolicyDict
    counts: CountDict
    nodes: list[GraphNodeDict]
    edges: list[GraphEdgeDict]
    conflicts: list[ConflictDict]


def is_contract_datetime(value: object) -> bool:
    """Return True when ``value`` is an ISO-8601 date-time with a timezone."""
    if not isinstance(value, str) or "T" not in value:
        return False
    candidate = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")


def _require_stable_id(value: object, field_name: str) -> None:
    if not isinstance(value, str) or STABLE_ID_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a stable id token.")


def _check_choice(value: object, allowed: tuple[str, ...], field_name: str) -> None:
    if value not in allowed:
        joined = ", ".join(allowed)
        raise ValueError(f"{field_name} must be one of: {joined}.")


def _check_confidence(value: object, field_name: str = "confidence") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number from 0 to 1.")
    if not 0 <= float(value) <= 1:
        raise ValueError(f"{field_name} must be a number from 0 to 1.")
    return float(value)


def _check_conflict_link(status: str, conflict_ids: tuple[str, ...], field_name: str) -> None:
    if status == "conflicting" and not conflict_ids:
        raise ValueError(f"{field_name} must list conflict_ids when status is conflicting.")
    if status != "conflicting" and conflict_ids:
        raise ValueError(f"{field_name} may list conflict_ids only when status is conflicting.")
    for conflict_id in conflict_ids:
        _require_stable_id(conflict_id, f"{field_name}.conflict_ids")


@dataclass(frozen=True, slots=True)
class Provenance:
    adapter: str
    evidence_ref: str

    def __post_init__(self) -> None:
        _require_text(self.adapter, "provenance.adapter")
        _require_text(self.evidence_ref, "provenance.evidence_ref")

    def to_dict(self) -> ProvenanceDict:
        return {"adapter": self.adapter, "evidence_ref": self.evidence_ref}


@dataclass(frozen=True, slots=True)
class MetadataNode:
    id: str
    type: str
    label: str
    platform: str
    source_adapter: str
    captured_at: str
    visibility: str
    sensitivity: str
    confidence: float
    status: str
    provenance: Provenance
    attributes: dict[str, Any] | None = None
    conflict_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_stable_id(self.id, "id")
        _check_choice(self.type, NODE_TYPES, "type")
        _require_text(self.label, "label")
        _require_text(self.platform, "platform")
        _require_text(self.source_adapter, "source_adapter")
        if not is_contract_datetime(self.captured_at):
            raise ValueError("captured_at must be an ISO-8601 date-time with a timezone.")
        _check_choice(self.visibility, VISIBILITY_VALUES, "visibility")
        _check_choice(self.sensitivity, SENSITIVITY_VALUES, "sensitivity")
        _check_confidence(self.confidence)
        _check_choice(self.status, STATUS_VALUES, "status")
        if not isinstance(self.provenance, Provenance):
            raise ValueError("provenance must be a Provenance value.")
        if self.attributes is not None and not isinstance(self.attributes, dict):
            raise ValueError("attributes must be an object when it is present.")
        _check_conflict_link(self.status, self.conflict_ids, "node")
        hidden = self.visibility == "hidden" or self.label == HIDDEN_PLACEHOLDER_LABEL
        if hidden:
            if self.type != "unknown" or self.label != HIDDEN_PLACEHOLDER_LABEL or self.visibility != "hidden":
                raise ValueError("A hidden node must use the anonymous placeholder shape.")
            if self.attributes is not None:
                raise ValueError("A hidden node must not carry attributes.")

    def to_dict(self) -> GraphNodeDict:
        payload: GraphNodeDict = {
            "id": self.id,
            "type": self.type,  # type: ignore[typeddict-item]
            "label": self.label,
            "platform": self.platform,
            "source_adapter": self.source_adapter,
            "captured_at": self.captured_at,
            "visibility": self.visibility,  # type: ignore[typeddict-item]
            "sensitivity": self.sensitivity,  # type: ignore[typeddict-item]
            "confidence": self.confidence,
            "status": self.status,  # type: ignore[typeddict-item]
            "provenance": self.provenance.to_dict(),
        }
        if self.attributes is not None:
            payload["attributes"] = dict(self.attributes)
        if self.conflict_ids:
            payload["conflict_ids"] = list(self.conflict_ids)
        return payload


@dataclass(frozen=True, slots=True)
class MetadataEdge:
    id: str
    source_id: str
    target_id: str
    relationship: str
    explicit: bool
    provenance: Provenance
    confidence: float
    status: str
    conflict_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_stable_id(self.id, "id")
        _require_stable_id(self.source_id, "source_id")
        _require_stable_id(self.target_id, "target_id")
        _check_choice(self.relationship, RELATIONSHIP_TYPES, "relationship")
        if not isinstance(self.explicit, bool):
            raise ValueError("explicit must be a boolean.")
        if not isinstance(self.provenance, Provenance):
            raise ValueError("provenance must be a Provenance value.")
        _check_confidence(self.confidence)
        _check_choice(self.status, STATUS_VALUES, "status")
        _check_conflict_link(self.status, self.conflict_ids, "edge")

    def to_dict(self) -> GraphEdgeDict:
        payload: GraphEdgeDict = {
            "id": self.id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "relationship": self.relationship,  # type: ignore[typeddict-item]
            "explicit": self.explicit,
            "provenance": self.provenance.to_dict(),
            "confidence": self.confidence,
            "status": self.status,  # type: ignore[typeddict-item]
        }
        if self.conflict_ids:
            payload["conflict_ids"] = list(self.conflict_ids)
        return payload


@dataclass(frozen=True, slots=True)
class MetadataConflict:
    id: str
    subject_ids: tuple[str, ...]
    summary: str
    kind: str

    def __post_init__(self) -> None:
        _require_stable_id(self.id, "id")
        if not self.subject_ids:
            raise ValueError("subject_ids must name at least one node or edge.")
        for subject_id in self.subject_ids:
            _require_stable_id(subject_id, "subject_ids")
        _require_text(self.summary, "summary")
        _check_choice(self.kind, CONFLICT_KINDS, "kind")

    def to_dict(self) -> ConflictDict:
        return {
            "id": self.id,
            "subject_ids": list(self.subject_ids),
            "summary": self.summary,
            "kind": self.kind,  # type: ignore[typeddict-item]
        }


@dataclass(frozen=True, slots=True)
class RedactionPolicy:
    hidden_object_handling: str
    description: str
    placeholder_label: str = HIDDEN_PLACEHOLDER_LABEL
    placeholder_type: str = "unknown"
    placeholder_visibility: str = "hidden"
    strip_attributes: bool = True

    def __post_init__(self) -> None:
        _check_choice(self.hidden_object_handling, HIDDEN_OBJECT_HANDLING, "hidden_object_handling")
        _require_text(self.description, "description")
        if self.placeholder_label != HIDDEN_PLACEHOLDER_LABEL:
            raise ValueError(f"placeholder_label must be {HIDDEN_PLACEHOLDER_LABEL!r}.")
        if self.placeholder_type != "unknown":
            raise ValueError("placeholder_type must be 'unknown'.")
        if self.placeholder_visibility != "hidden":
            raise ValueError("placeholder_visibility must be 'hidden'.")
        if self.strip_attributes is not True:
            raise ValueError("strip_attributes must be true.")

    def to_dict(self) -> RedactionPolicyDict:
        return {
            "hidden_object_handling": self.hidden_object_handling,  # type: ignore[typeddict-item]
            "placeholder_label": self.placeholder_label,
            "placeholder_type": "unknown",
            "placeholder_visibility": "hidden",
            "strip_attributes": True,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class MetadataGraphDocument:
    generated_at: str
    nodes: tuple[MetadataNode, ...]
    edges: tuple[MetadataEdge, ...]
    redaction_policy: RedactionPolicy
    conflicts: tuple[MetadataConflict, ...] = ()
    contract_version: str = CONTRACT_VERSION
    producer: str = PRODUCER

    def __post_init__(self) -> None:
        if self.contract_version != CONTRACT_VERSION:
            raise ValueError(f"contract_version must be {CONTRACT_VERSION}.")
        if self.producer != PRODUCER:
            raise ValueError(f"producer must be {PRODUCER!r}.")
        if not is_contract_datetime(self.generated_at):
            raise ValueError("generated_at must be an ISO-8601 date-time with a timezone.")
        if not isinstance(self.redaction_policy, RedactionPolicy):
            raise ValueError("redaction_policy must be a RedactionPolicy value.")
        for node in self.nodes:
            if not isinstance(node, MetadataNode):
                raise ValueError("nodes must contain MetadataNode values.")
        for edge in self.edges:
            if not isinstance(edge, MetadataEdge):
                raise ValueError("edges must contain MetadataEdge values.")
        for conflict in self.conflicts:
            if not isinstance(conflict, MetadataConflict):
                raise ValueError("conflicts must contain MetadataConflict values.")

    def to_dict(self) -> MetadataGraphDict:
        hidden_placeholders = sum(1 for node in self.nodes if node.visibility == "hidden")
        return {
            "contract_version": self.contract_version,
            "generated_at": self.generated_at,
            "producer": "dwh-studio",
            "redaction_policy": self.redaction_policy.to_dict(),
            "counts": {
                "nodes": len(self.nodes),
                "edges": len(self.edges),
                "conflicts": len(self.conflicts),
                "hidden_placeholders": hidden_placeholders,
            },
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
        }
