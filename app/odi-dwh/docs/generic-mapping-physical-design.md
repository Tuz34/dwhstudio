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
- `mapping.validate()` failures report the SDK messages. The error line stays one JSON object and
  gains additive keys `validation_messages` (at most 20 entries, ERROR/FATAL/WARNING only, redacted,
  300 characters each) and `physical_design` (`same_schema`, `access_points`,
  `access_points_with_lkm`, `access_point_names`, `source_moved`, `physical_nodes`). The success line
  gains `physical_design` as well.
- The default graph engine runs the same read-only repository preflight as the classic engine
  before the first DDL step, so internal ODS/DWH sources carry their verified source identity.
