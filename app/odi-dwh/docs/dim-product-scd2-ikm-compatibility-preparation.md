# ODI-68 Offline Compatibility Preparation (Batch A)

This preparation is not deployment authorization and does not diagnose the
previous live failure. It performs no file writes, exports, imports, database
connections, repository mutations, scenario generation or scenario execution.
The vendor XML must never be committed to this repository.

## Interface

`plan_dim_product_scd2_ikm_compatibility(vendor_xml: bytes) -> CompatibilityPlan`

The caller supplies bytes already read from the vendor XML. The module itself
has no path, file-reader, writer or runtime callback argument. The fixed input
SHA-256 is:

`B22B61DA35FCE13B4E15B2EE2CA29888EFF4DC2CD92D85BFF4DCA8B5FE5070F4`

Only `plan.report` is intended for diagnostics. `plan.transformed_xml` is an
in-memory bytes artifact, excluded from repr; do not serialize the whole
dataclass, print the artifact or write it to disk. Failed validation returns
no artifact. A valid transformation still returns
`status=blocked_pending_audit_contract` and `deployment_authorized=False`.

## Fixed Scope

Profile: `YN_9999_V1`. Proposed fork name:
`IKM Oracle SCD2 YN9999 ODI68 V1`.

The fork name is candidate metadata only. XML object identities and the stock
IKM name are deliberately not rewritten. The artifact is not an import package.
Future reviewed identity allocation must not overwrite the stock IKM.

| Task | Text ID | Active flag | Inactive flag | End date |
| --- | --- | ---: | ---: | ---: |
| Insert flow into I$ table | 1301 | 1 | 0 | 2 |
| Flag rows for update | 1309 | 1 | 0 | 1 |
| Update existing rows | 1317 | 1 | 0 | 1 |
| Historize old rows | 1318 | 1 | 1 | 1 |
| Update surrogate keys | 1321 | 1 | 0 | 0 |

Only the five specified task text bodies change: five active flag values become
quoted Y, one inactive flag value becomes quoted N, and five date sentinels become
9999-12-31 with the original date mask. Exact method-call literals, task names,
IDs, text links, order and site counts are required. XML parsing identifies the
objects and original CDATA byte spans; no XML reserialization occurs. Every byte
outside those five bodies is retained, including whitespace, comments and
unrelated bodies containing similar literals.

Unknown input hashes, malformed XML, DTDs/entities, missing/duplicate identities,
ambiguous CDATA and missing/duplicate sites fail closed. Reapplication is refused
because transformed bytes no longer match the pinned vendor hash. There is no
public hash/profile override.

## Deliberately Unresolved

Audit column population, SCD role selection, batch-ID provenance, timestamps,
source-system semantics and metadata/runtime option changes are not performed.
Preserving the existing Python baseline is the reference requirement, not a new
audit policy. The pending audit contract is a mandatory blocker even when all
11 compatibility sites match. No readiness, deployment, runtime correctness or
full-row rerun claim follows from this preparation.

## Verification And Review

Tests construct small synthetic XML in memory, including negative cases and an
independent expected byte artifact. Synthetic tests privately patch the hash
constant; production has no configurable allowlist. An optional installed-XML
test reads only the fixed SDK XML and checks its input/output hashes, counts,
reapplication refusal and unchanged on-disk source. It does not print SQL/XML.

Main independently reviews this batch before any later work. Audit-compatible
templates, fork identity/import, mapping updates and scenario 002 remain separate
reviewed scopes; this module grants no permission for any of them.
