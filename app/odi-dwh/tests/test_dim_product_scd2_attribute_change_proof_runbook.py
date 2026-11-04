from pathlib import Path


def test_attribute_change_proof_runbook_freezes_rollback_only_boundary():
    path = (
        Path(__file__).parents[1]
        / "docs"
        / "dwh-dim-product-scd2-attribute-change-proof.md"
    )
    text = path.read_text(encoding="utf-8").lower()

    for required in (
        "rollback-only database transaction",
        "no live proof was run",
        "no endpoint",
        "no configured connection helper",
        "serializable transaction",
        "exactly one changed candidate",
        "expires exactly one",
        "inserts exactly one",
        "affected counts must both be zero",
        "does not call `dwh.seq_dim_product.nextval`",
        "max(product_sk) + 1",
        "rollback is mandatory",
        "no raw rows",
        "pk values",
        "separate read-only postflight",
        "odi68_dim_product_scd2_attribute_change_rollback_proof",
        "does not create or run an odi mapping",
    ):
        assert required in text

    for forbidden in (
        "live-proven attribute change",
        "approval inherited",
        "automatic object removal",
    ):
        assert forbidden not in text
