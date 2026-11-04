# Metadata graph export contract (ODI-79 Phase 0)

This package is the versioned, neutral JSON contract for DWH Studio metadata.
AIA-17 can import a document that follows `schema/metadata_graph.schema.json`
without importing DWH Studio code.

Phase 0 is static. It publishes the document shape, one synthetic fixture, and
a local validator. It does not ship an adapter, an HTTP endpoint, or a live
metadata export.

## Ownership

AIA owns product, UX, and grounding. DWH Studio owns adapters and the local security boundary.

AIA decides how imported metadata is shown and explained. DWH Studio decides
which objects may leave the process and how hidden objects are redacted before
a document is written. `redaction_policy` is the header block a future adapter
must honor at that boundary. This slice does not include the adapter.

## Version

`contract_version` is `1.0.0` (`CONTRACT_VERSION` in `contract.py`).
`producer` is always `dwh-studio`. The schema uses JSON Schema draft 2020-12.

## Document shape

The header fields are `contract_version`, `generated_at`, `producer`,
`redaction_policy`, and `counts`. The body is `nodes`, `edges`, and
`conflicts`.

`counts` repeats the array lengths and the number of `visibility: hidden`
placeholders. The validator rejects a document whose counts disagree with the
body.

### Nodes

Every node carries a stable `id`, `type`, `label`, `platform`,
`source_adapter`, `captured_at`, `visibility`, `sensitivity`, `confidence`
(from 0 to 1 inclusive), `status`, and `provenance`. `attributes` is an
optional object.

`type` is one of `logical_entity`, `physical_table`, `column`, `schema`,
`odi_mapping`, `odi_scenario`, `grant`, `policy`, `finding`, `unknown`.

`visibility` is one of `public`, `internal`, `restricted`, `hidden`.

`sensitivity` is one of `none`, `low`, `medium`, `high`, `pii`, `secret`.

`provenance` is an object with `adapter` and `evidence_ref`.

### Edges

Every edge carries `id`, `source_id`, `target_id`, `relationship`, `explicit`,
`provenance`, `confidence`, and `status`. `explicit` is true when the
relationship was declared and false when it was inferred.

`relationship` is one of `contains`, `references_fk`, `lineage`, `maps_to`,
`grants`, `classified_as`, `conflicts_with`, `unknown`.

`source_id` and `target_id` must name nodes in the same document.

### Unknown, missing, ambiguous, and conflicting

`status` on nodes and edges is one of `known`, `unknown`, `missing`,
`ambiguous`, `conflicting`. A missing or unknown fact stays in the graph with
that status so a consumer does not have to guess why a field is empty.

When `status` is `conflicting`, the object lists `conflict_ids`. Those ids
point at the document-level `conflicts` array. Each conflict has `id`,
`subject_ids`, `summary`, and `kind` (`value_disagreement`,
`missing_counterpart`, `identity_ambiguity`, `unknown`). Objects that are not
conflicting do not carry `conflict_ids`.

### Hidden objects

`redaction_policy.hidden_object_handling` is `omit` or `placeholder`.

- `omit` leaves hidden objects out of `nodes` and `edges`. A document in this
  mode must not contain `visibility: hidden`.
- `placeholder` replaces a hidden object with an anonymous node:
  `type` is `unknown`, `label` is `<hidden>`, and `visibility` is `hidden`.
  The node has no `attributes` object. Edges may point at the placeholder id.
  The id must be an opaque token such as `n-hidden-001`.

The validator rejects a hidden node that keeps a business label or an
`attributes` object.

## How AIA consumes the fixture

1. Read `schema/metadata_graph.schema.json`. Treat it as the normative
   interface. Do not import DWH Studio Python modules.
2. Read `fixtures/synthetic_graph_v1.json`. The file is synthetic. It is a
   sample of the shape, not an export of a catalog.
3. Validate the JSON with any draft 2020-12 validator. Check that every edge
   endpoint exists, that `counts` match, and that hidden nodes have no label
   other than `<hidden>` and no `attributes`.
4. Render `nodes` and `edges`. Honor `visibility`, `sensitivity`, `status`,
   and `conflicts`. A hidden placeholder is non-identifying.
5. Inside this repository, `metadata_export.validate.validate_document` applies
   the schema subset this file uses and the referential and redaction checks.
   `jsonschema` is not a runtime dependency.

The fixture walks one synthetic order table: schema contains table contains
columns, a logical entity and an ODI mapping map to the table, a grant and a
contact policy attach to it, an inferred lineage points at a missing column,
a finding conflicts with `ORDER_ID` nullability, an unclassified object has an
unknown relationship, and a foreign-key target is the hidden placeholder.

## Local check

From `app/odi-dwh`, with the project virtualenv:

```bash
python -m pytest -q tests/test_metadata_export_contract.py
```
