from pathlib import Path

import local_model_eval as eval_safety


def test_odi68_phase2_scd1_merge_contract_is_metadata_only_and_reviewable():
    path = Path("docs/dwh-scd1-merge-contract.md")
    text = path.read_text(encoding="utf-8")
    lower = text.lower()

    assert "Phase 2 - Type 1 merge metadata contract" in text
    assert "HR.DEPARTMENTS -> DWH.ODS_HR_DEPARTMENTS" in text
    assert "non-executable metadata contract" in lower

    for required_term in [
        "natural key",
        "update-key",
        "tracked attribute list",
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
