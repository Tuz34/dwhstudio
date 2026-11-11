# Generic mapping: physical design and access points

`odi_automation/create_mapping.groovy` (the generic `/api/mapping/generate` path) builds the logical
mapping, sets the target staging location, assigns the approved IKM and then handles load KMs.

- Access points (AP nodes, named like `<SOURCE>_AP`) can appear or be recomputed after the staging
  location is resolved. The script therefore re-reads `design.getAllAPNodes()` in a final pass and
  assigns and verifies the configured load KM (`odi.lkm_name`, default fallback chain starting with
  `LKM SQL to Oracle (Built-In)`) on every AP that exists at that point. LKM assignment is skipped
  only when no AP exists. The DB Link LKM stays blocked unless `odi.allow_dblink_lkm=true`.
- Same logical schema (source and target datastores share the logical schema, so the same data
  server and physical schema): the source belongs in the target execution unit and needs no AP. The
  script tries `design.moveNodes` for the source node; any AP that still remains gets the load KM.
- Repository KM readiness: `mapping_preflight.groovy` runs before any DDL or MAPPING_LOG write. An
  approved IKM absent from the repository returns `error_code: ikm_not_imported` from
  `POST /api/mapping/generate` on both engines. The KM is not imported and no other KM is chosen.
  An ambiguous match or a failed lookup returns `repository_preflight_failed` with the step `code`.
- Validation: in SDK 12.2.1.4 both `validate()` overloads return TRUE when errors were found (the
  no-argument form delegates to the issue-list form). The script uses
  `mapping.validate(issues, false)` as the single gate (`hasValidationErrors`): true fails the
  mapping, false lets it commit. The earlier "true means valid" reading rolled back valid mappings.
  On failure the physical design's own messages are added to the collected issues.
- The Groovy error line stays one JSON object and gains additive keys `validation_messages` (at most
  20 entries, ERROR/FATAL/WARNING only; each complete entry, severity prefix included, is capped at
  300 characters; quoted text, URLs and key=value secrets are masked on a best-effort basis) and
  `physical_design` (`same_schema`, `access_points`, `access_points_with_lkm`, `access_point_names`,
  `physical_nodes`, `source_moved`). The success line carries `physical_design` too.
- These are raw Groovy stdout diagnostics only. On failure the script exits 1. The orchestrator
  accepts the last stdout line only if it matches the exact error schema, and returns a `Hata:` marker
  plus a rebuilt line holding only `status`, `action`, `replaced`, `kept_existing_on_failure` and
  `error_type`. Any other output returns the generic `GroovyProcessError` marker. `validation_messages`
  and `physical_design` are discarded on both paths, so they do not reach
  `POST /api/mapping/generate` responses. Redaction is best effort and the free-form SDK text
  must not be forwarded to any API or UI without a separately reviewed safe projection.
- Limits: same-schema detection compares logical schema identity, not the physical schemas resolved
  in the effective context. A rejected `design.moveNodes` is not fatal; it is logged and reported as
  `source_moved=false`, and any remaining access point still gets the load KM.
- The default graph engine runs the same read-only repository preflight as the classic engine
  before the first DDL step, so internal ODS/DWH sources carry their verified source identity.
  `run_mapping_graph` still compiles with an in-memory checkpoint and still writes that state
  through the existing persistence hook.
- Trusted source model: `ODI_SOURCE_MODEL_BINDINGS` is an optional server JSON object,
  `{"CONTEXT": {"PHYSICAL_OWNER": "MODEL_CODE"}}`. Unset or blank binds only
  `GLOBAL`/`DWH` to the built-in DWH model code. A non-blank value replaces that default
  and is validated before use.
  Two Oracle models for the same physical owner in the requested context still return
  `source_identity_ambiguous`. A configured pair keeps only that exact model code, and only
  when one repository model has it. That model still has to pass the physical schema, context,
  Oracle technology, resource, shortcut and datastore checks. A missing code, a second model
  with the same code, a wrong owner or context, a non-Oracle technology, a shortcut, or two
  conflicting datastores stays blocked. There is no first match and no fallback.
  `src_model` and `_odi_source_identity` do not choose the code. After Groovy returns, Python
  requires `source.model_code` to equal the server value. Invalid JSON, types, identifiers or
  duplicate keys return `source_model_config_invalid` with no repository connection.
  This is a static contract, not a live repository proof.
