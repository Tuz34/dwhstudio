# ODI-68: unconstrained NUMBER construction

The fixed DIM_PRODUCT and generic SCD2 absent-only registrars, plus the classic
registrar's column-construction path, explicitly set
`OdiColumn.setLength(0)` and `setScale(-127)` for bare `NUMBER`. The installed
Oracle SDK recognizes this pair: raw getters retain `0/-127`, normalized
`getLength()/getScale()` return null, and `getDdlDataType()` renders `NUMBER`.
Rendering the Oracle writable pattern `NUMBER(%L,%P)` with those normalized
getters through the SDK's `SnpsStringTools.getDdlDataType` also yields `NUMBER`.

Without these setters, `new OdiColumn` retains length 50 even after
`setDataTypeCode("NUMBER")` and `setDataFormat("NUMBER")`; its DDL is
`NUMBER(50)`. Data format does not replace numeric metadata. Explicit precision
and scale are unchanged. In particular, SCD2 preserves explicit zero scale;
classic retains its pre-existing positive-only scale setter behavior. The
classic guard applies only to `NAME:NUMBER` (case-insensitive type), with no
supplied dimensions; explicit zero length is not treated as bare NUMBER. No
fixed precision is imposed on unconstrained NUMBER. Classic key registration,
key-only mode, datastore lookup, and existing refresh behavior are unchanged.

`tests/test_scd2_number_construction_sdk.py` executes only the actual column
construction fragments with in-memory SDK model/datastore objects. It covers
all ten fixed numeric columns (five unconstrained), generic precision/scale
boundaries, classic bare/bounded declarations, retained character semantics
and roles, and compilation of all three
complete scripts without executing their bodies. It creates no `OdiInstance`
and loads no connection properties. Tests skip if the local SDK is absent.

On base `0ca1a70922c22eee9e9de0d8216a9481c95e50ab`, the regression was RED:
2 failures (`NUMBER(50)` instead of `NUMBER`) and 8 passes. With the two
creation guards it is GREEN: 10 passes, no skips, using the installed SDK.
After fast-forwarding to `459742d8b7b7631b19803c3307ce520fc17bbd4a`, classic
coverage was RED: 2 bare-NUMBER failures and 22 passes. With the classic guard,
the combined SDK suite is GREEN: 24 passes, no skips.
Run from `app/odi-dwh` with Java and the SDK available:
`python -B -m pytest -q -o addopts= -p no:cacheprovider tests/test_scd2_number_construction_sdk.py`.

This is a creation-only correction, not a repair of existing repository
datastores, mapping attributes, generated scenarios, or physical tables. No
live execution or retry is authorized by this change. In particular, this
change does not authorize running classic registration to refresh existing
objects; that existing path is not an in-place metadata repair tool.
