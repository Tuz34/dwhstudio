# ODI68 Offline Audit Candidate (B1/B2)

NOT DEPLOYABLE. No repository writer, XML exporter/importer, connection callback,
mapping update, scenario generation, or runtime execution is provided.
Main independently reviews this batch. No live retry is authorized.

Base: `dc7f9685dbcd4d6aaf4b2a6d960fa69a577e26df`.
Branch: `prep/odi68-ikm-audit-b`.
The earlier Schrodinger design note is preserved unchanged. This preparation
supersedes its suggestion of a separate effective-time input/entry step: the
candidate population statement supplies the effective time itself.

## Interface And Representation

`prepare_dim_product_scd2_ikm_audit_candidate(vendor_xml: bytes) -> AuditCandidate`

Only exact bytes of the Batch A pinned stock export are accepted. Type/hash
checks precede parsing. There is no public hash, profile, name, option, binding,
path or writer override. Batch A remains unchanged and supplies its eleven
verified constant replacements.

`result.report` is the only diagnostic surface. Successful preparation returns
`blocked_pending_bindings_and_runtime_verification`, never ready/passed, with
deployment_authorized, exportable_xml, identity_rewritten, batch_binding_verified,
source_binding_verified and rendered_metadata_verified all false.

`result.candidate` is an immutable split representation:

- `existing_objects_xml`: in-memory existing objects with seven text bodies
  changed. It preserves stock identities/name and DOES NOT contain the new task.
  Never import this component alone or serialize the whole dataclass.
- `new_task`: an actual template body and fixed order/connection/technology/commit
  metadata, with repository_id=None. No invented XML IDs or cross-references.
- `target_columns`: exact ordered 26-column fixed profile.
- `option_locks`: nine fixed option/value pairs.

Raw bytes and task bodies are excluded from repr. Failed preparation returns no
partial candidate. No vendor XML is a fixture or committed artifact. The bytes
must not be logged, printed, exported, or recursively serialized.

## Implemented Changes

| Surface | Exact change |
| --- | --- |
| Batch A five bodies | Retain eleven Y/N/9999 constant replacements |
| New order 253 | Populate candidate I$ audits and EFFECTIVE_FROM |
| Historize old rows, text 1318 | Add only UPDATE_DATE to the assignment tuple and SYSTIMESTAMP to its paired projection |
| Final insert, text 1307 | Replace only the source projection with the fixed ordered 26 expressions |
| Validate KM options, text 1319, order 72 | Prepend fixed option guard; retain entire original body |

Order 253 is checked absent and lies after flow loading/recycle, before flow
control at 282. Its target is only I$ and its filter is IND_UPDATE I. It assigns
EFFECTIVE_FROM from SYSDATE, provisional INSERT_DATE from SYSTIMESTAMP,
UPDATE_DATE as typed TIMESTAMP NULL, and two deliberately unresolved tokens for
caller batch and source system. Connection 1, ORACLE, no per-task commit.

The final insert reads all ordinary values, effective time, current flag, SK,
source and batch from I$ in the fixed order; only INSERT_DATE becomes a fresh
SYSTIMESTAMP and UPDATE_DATE a typed NULL. Its target list, I filter and table
references remain unchanged. The provisional staging timestamp therefore does
not replace the baseline's actual target-insert audit timestamp.

Historization retains its correlation and current-row/I-candidate predicates.
It does not assign old INSERT_DATE, SOURCE_SYSTEM or ETL_BATCH_ID. Only the old
effective end, inactive flag and expiry UPDATE_DATE can change. No SCD roles are
rewritten, and no second target-wide audit update is appended.

Locked options: CREATE_TARG_TABLE=0, TRUNCATE=0, DELETE_ALL=0, RECYCLE_ERRORS=0,
FLOW_CONTROL=1, COMMIT=1, STATIC_CONTROL=0, CONSTRAINTS=None, INDEXES=None.
Their original metadata/defaults must match uniquely. The template guard rejects
different values with a fixed error marker; it does not silently coerce options.
Other options and all option/link objects remain untouched. This is not proof
of deployment-level guard execution order; generated scenario checks remain due.

## Effective Clock And NULL Semantics

One candidate-population UPDATE evaluates SYSDATE as the Oracle statement clock.
Both old-row expiry and new-row insertion reuse the stored I$ EFFECTIVE_FROM;
neither resamples the effective clock. This preserves the baseline's shared
effective-date invariant without another time variable. Earlier flow/mapping
start values are overwritten for every I-candidate before target writes. Audit
INSERT_DATE and UPDATE_DATE remain their separate insert/expiry SYSTIMESTAMP
observations, not the effective clock.

The offline proof checks this exact data path and the single SYSDATE occurrence
in the new statement. It is not an Oracle execution/timing/precision proof.
The typed, rendered target DATE contract and supported target execution location
still require verification; no current deployed metadata is assumed correct.

The installed flow definition uses all target columns with DEST_WRI_DT and
explicit NULL, plus IND_UPDATE CHAR(1). Thus the fixed profile expects 27 flow
columns. Original flow loading omits the four unclassified audits, while final
insertion selects all target columns. Population before flow control fills the
mandatory audits; UPDATE_DATE must remain nullable.

The flow equality predicate is preserved byte-for-byte and treats equal NULLs
as equal; its business comparisons are AND-combined. Stock Type 1 NULL-crossing
and Type 2 comparison expressions remain untouched. Audit fields must stay out
of SCD_INS/SCD_UPD comparisons. Fixed roles require one NK, five control roles
including that NK, seventeen tracked attributes, and four unclassified audits;
no Type 1 columns. Non-null unique natural keys remain a separate precondition.

## Binding And Metadata Gates

The two unresolved tokens are intentionally not valid finished expressions.
No batch value, session number fallback, default value, or source binding is
invented. Required source semantics remain SH; batch remains the caller's
validated numeric identifier. Scenario 002 must declare and consume the exact
reviewed variables and reject missing/invalid input before side effects. Version
001 and stock IKM are not modified by this code.

No binding verification or actual generated metadata is accepted as a parameter
to this preparer. The manifest is a candidate under the fixed contract, not a
probe asserting that contract exists in a repository. The existing fixed 001
mapping/runner cannot deploy or invoke this candidate unchanged.

Goodall's shared-OdiColumn finding is an explicit external gate: effective
precision can propagate to all bound mapping consumers without sync. Do not
assume existing precision is repaired, or claim only the DIM mapping is affected.
Main's read-only consumer inventory and separately reviewed metadata repair
remain prerequisites. This preparation neither reads nor changes shared columns.

Flow-control handling of nullable I$ SK/current flag before stock order 342,
actual writable datatypes/precision, exact column order, expression execution
location, caller-batch capacity, source binding, and exclusive execution remain
unverified. A new candidate does not clear these gates.

## SDK Duplication Evidence

Offline javap inspection of the installed SDK, with no import or class execution,
confirmed IMPORT_MODE_DUPLICATION=2 on IImportService. ImportServiceImpl's work
callback delegates to DwgObject.importFromXml. DwgObject.importFromObjectLst has
a mode-2 branch and invokes DwgDuplicationTools.batchDuplication. The duplication
implementation includes primary-key allocation/mapping, foreign-key reassignment,
and a new-UUID/internalSetGUID path in insertActionForDuplication. It also contains
name-collision handling. These are observed implementation facts, not a dry-run
of this KM or proof that every external reference will be duplicated.

Installed odi-core.jar SHA-256:
`B128844E39A30885DCD10DF2B472821267CA9ABD3365A833EAD0AAE0675ED0E1`.

Prefer a later bounded, independently reviewed SDK duplication operation to a
custom allocator. Verify new identities/internal links, intended external
references and exact fork name after duplication; preserve stock and 001. This
batch deliberately neither chooses new repository IDs nor implements that writer.

## Verification

RED began with the new module absent. Synthetic tests then exposed the installed
template's formatting/explicit-NULL differences; those contracts are now checked.
New tests plus unchanged Batch A tests: 165 passed, including 77 new tests.
Python quality gate passed; scoped Ruff passed. No full application/live suite.

Synthetic tests cover exact task/text/order links, collision/missing/duplicate
objects, malformed anchors, option drift, byte preservation outside the seven
text spans, no reapplication, safe diagnostics, exact fixed projection, staged
clock and audit assignments, and absence of I/O/runtime APIs. Fixed column order
is checked against the local frozen Python plan.

Optional installed checks verify the source hash, stable candidate digests,
31 existing tasks plus one unallocated task, seven changed bodies, and 340
unchanged objects. NULL equality has 153 synthetic attribute/value combinations.
An in-memory Groovy SimpleTemplateEngine check uses a fake odiRef, without
odi-core on its classpath, and sends bodies only over stdin. It renders four
new/changed templates, checks three expiry assignments and 26 insert expressions,
and rejects all nine option deviations. It outputs only a strict metadata
envelope. This is template-render evidence, not Oracle SQL execution or actual
ODI variable resolution. SDK bytecode inspection performs no repository calls.

Fixed installed input hash:
`B22B61DA35FCE13B4E15B2EE2CA29888EFF4DC2CD92D85BFF4DCA8B5FE5070F4`.
Modified existing-object bytes hash:
`135C59DD377D47AF215E1EAFB2E343695AB45BEDCB9C9F3200F90376F1E59B74`.
Unallocated new-task body hash:
`4EA591211060B89827325AC9F2638AA0CE26A209F2B6EF18E3651BF36477AEE9`.

Rerun focused checks without dumping template failures:

```text
python -B -m pytest -q -o addopts= -p no:cacheprovider --assert=plain --tb=short tests/test_dim_product_scd2_ikm_audit_candidate.py tests/test_dim_product_scd2_ikm_compatibility.py
python -B python_quality_gate.py
```

Before any live proof, retained backups remain preserved; count/status alone
cannot prove an unchanged 72-row history. Full-row two-way difference counts,
the allowed changed-row audit field set, and a different-batch unchanged rerun
still need separately authorized evidence. Stock I$ DDL and COMMIT behavior are
not the Python baseline's single serializable transaction with rerun-before-commit.
There is no automatic recovery or transaction-equivalence claim in this batch.
