from pathlib import Path


def test_candidate_and_load_runbook_freezes_review_and_live_boundaries():
    path = (
        Path(__file__).parents[1]
        / "docs"
        / "dwh-dim-product-scd2-candidate-load-runbook.md"
    )
    text = path.read_text(encoding="utf-8").lower()

    for required in (
        "read-only candidate preflight",
        "set transaction read only",
        "count/status/metadata only",
        "new current version",
        "changed current version",
        "unchanged current version",
        "delete-policy blocker",
        "serializable transaction",
        "expire then insert",
        "same-transaction rerun",
        "rollback before commit",
        "odi68_dim_product_scd2_load_and_rerun_proof",
        "independent review",
        "no endpoint",
        "no configured connection helper",
        "no raw rows",
        "no pk values",
        "no live scd2 data load was run",
    ):
        assert required in text

    for forbidden in (
        "live-proven scd2 load",
        "approval inherited",
        "automatic cleanup",
    ):
        assert forbidden not in text
