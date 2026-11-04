from pathlib import Path


def test_odi_scd2_packaging_runbook_freezes_separate_approval_boundaries():
    path = (
        Path(__file__).parents[1]
        / "docs"
        / "dwh-dim-product-odi-scd2-packaging-readiness.md"
    )
    text = path.read_text(encoding="utf-8").lower()

    for required in (
        "ikm oracle slowly changing dimension",
        "map_ods_products_to_dim_product",
        "map_ods_products_to_dim_product_scn",
        "generic type 1 builder is not reusable",
        "read-only odi capability probe",
        "count/status/metadata only",
        "server-side executor remains the operational baseline",
        "separate mapping-builder implementation",
        "independent review",
        "exact mapping creation approval",
        "separate scenario generation approval",
        "separate scenario run approval",
        "no odi connection was opened",
        "no odi object was created or run",
    ):
        assert required in text

    for forbidden in (
        "approval inherited",
        "live-proven odi scd2 mapping",
        "generic builder is reusable",
    ):
        assert forbidden not in text
