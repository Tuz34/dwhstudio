from pathlib import Path

import local_model_eval as eval_safety


def test_odi68_phase1_dim_product_contract_is_metadata_only_and_reviewable():
    path = Path("docs/dwh-scd2-dim-product-contract.md")
    text = path.read_text(encoding="utf-8")
    lower = text.lower()

    assert "Phase 1 - Type 2 SCD DIM_PRODUCT metadata contract" in text
    assert "DWH.ODS_SH_PRODUCTS -> DWH.DIM_PRODUCT" in text
    assert "SH.PRODUCTS -> DWH.ODS_SH_PRODUCTS" in text
    assert "non-executable metadata contract" in lower

    for required_term in [
        "surrogate key",
        "natural key column",
        "effective-from",
        "effective-to",
        "current flag",
        "audit columns",
        "tracked attribute list",
        "date order",
        "single current row per natural key",
        "rerun guard",
        "rollback plan",
        "explicit operator approval",
        "count/status/metadata only",
        "no raw rows",
        "no pk values",
        "no ddl",
        "no dml",
        "no odi scenario run",
    ]:
        assert required_term in lower

    forbidden_sql_fragments = [
        "create table",
        "insert into",
        "merge into",
        "update ",
        "delete from",
        "truncate",
    ]
    for forbidden in forbidden_sql_fragments:
        assert forbidden not in lower

    assert "live-proven" not in lower
    eval_safety.assert_no_blocked_content(text)
