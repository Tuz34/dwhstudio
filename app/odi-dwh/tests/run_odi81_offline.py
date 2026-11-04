"""Reuse the guarded offline bootstrap; never import the live app/configuration."""

from run_pr16_offline import main

if __name__ == "__main__":
    raise SystemExit(main([
        "test_remote_run_contract", "test_remote_run_groovy",
        "test_groovy_execution_boundary", "test_products_ods_scenario_runner",
        "test_scd2_service", "test_scd2_postflight_and_odi",
    ]))
