from pathlib import Path

import local_model_eval as eval_safety


def test_odi68_phase_plan_is_metadata_only_and_reviewable():
    path = Path("docs/dwh-load-patterns-phase-plan.md")
    text = path.read_text(encoding="utf-8")
    lower = text.lower()

    for phase in [
        "Phase 1 - Product Type 1 ODS, then Type 2 dimension",
        "Phase 2 - Type 1 merge proof",
        "Phase 3 - Partitioned daily fact load",
        "Phase 4 - Delete handling",
        "Phase 5 - Supporting patterns",
    ]:
        assert phase in text

    assert "count/status/metadata only" in lower
    assert "no raw rows" in lower
    assert "no pk values" in lower
    assert "no ddl" in lower
    assert "no dml" in lower
    assert "no odi scenario run" in lower
    assert "explicit operator approval" in lower
    assert "rerun guard" in lower
    assert "rollback" in lower
    assert "live-proven" not in lower
    eval_safety.assert_no_blocked_content(text)
