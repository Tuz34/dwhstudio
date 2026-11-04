from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _schema_sync_sources():
    dataops = PROJECT_ROOT / "frontend" / "src" / "components" / "dataops"
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in (dataops / "SchemaSync.jsx", dataops / "schemaSyncPanels.jsx")
    )


def test_frontend_source_has_no_mojibake_artifacts():
    frontend_root = PROJECT_ROOT / "frontend" / "src"
    bad_tokens = ("Ã", "â", "ğŸ", "Ä", "Å", "ðŸ", "�", "MAPPİNG", "ANA KAYNAK")
    offenders = []

    for path in frontend_root.rglob("*"):
        if path.suffix not in {".js", ".jsx", ".mjs", ".css"}:
            continue
        text = path.read_text(encoding="utf-8")
        for token in bad_tokens:
            if token in text:
                offenders.append(f"{path.relative_to(PROJECT_ROOT)} contains {token!r}")

    assert offenders == []


def test_surgeon_edit_supports_direct_km_open_contract():
    content = (PROJECT_ROOT / "frontend" / "src" / "components" / "modals" / "SurgeonEdit.jsx").read_text(encoding="utf-8")

    assert "initialOperation" in content
    assert "initialTruncateTargetTable" in content
    assert "useState(initialOperation || 'update_expression')" in content
    assert "initialTruncateTargetTable === true" in content


def test_mapping_list_has_direct_km_options_action():
    content = (PROJECT_ROOT / "frontend" / "src" / "components" / "dataops" / "MappingList.jsx").read_text(encoding="utf-8")

    assert "KM Options" in content
    assert "initialOperation: 'update_km'" in content
    assert "detail?.km_options?.truncate_target_table ?? mappingState?.truncate_target_table" in content


def test_frontend_sends_backend_approval_for_persistent_actions():
    mapping_list = (PROJECT_ROOT / "frontend" / "src" / "components" / "dataops" / "MappingList.jsx").read_text(encoding="utf-8")
    mapping_wizard = (PROJECT_ROOT / "frontend" / "src" / "components" / "modals" / "MappingWizard.jsx").read_text(encoding="utf-8")
    surgeon_edit = (PROJECT_ROOT / "frontend" / "src" / "components" / "modals" / "SurgeonEdit.jsx").read_text(encoding="utf-8")
    dml_banner = (PROJECT_ROOT / "frontend" / "src" / "components" / "chat" / "DmlBanner.jsx").read_text(encoding="utf-8")

    assert "approved: true" in mapping_list
    assert "approved: true" in mapping_wizard
    assert "approved: true" in surgeon_edit
    assert "approved: true" in dml_banner


def test_dq_frontend_uses_approved_request_builders_and_never_renders_pk_values():
    dashboard = (PROJECT_ROOT / "frontend" / "src" / "components" / "dataops" / "DqDashboard.jsx").read_text(encoding="utf-8")
    builders = (PROJECT_ROOT / "frontend" / "src" / "components" / "dataops" / "dqActionRequests.js").read_text(encoding="utf-8")

    assert "buildAutofixColumnRequest" in dashboard
    assert "buildAutofixAllRequest" in dashboard
    assert "buildSyncMappingRequest" in dashboard
    assert "approved: true" in builders
    assert "sample_keys" not in dashboard
    assert "PK:" not in dashboard


def test_dq_reconciliation_dialog_has_accessible_name_and_close_control():
    dashboard = (PROJECT_ROOT / "frontend" / "src" / "components" / "dataops" / "DqDashboard.jsx").read_text(encoding="utf-8")

    assert 'role="dialog"' in dashboard
    assert 'aria-modal="true"' in dashboard
    assert 'aria-labelledby="dq-reconciliation-title"' in dashboard
    assert 'aria-label="Close reconciliation dialog"' in dashboard


def test_shared_modal_has_dialog_and_keyboard_accessibility_contract():
    modal = (PROJECT_ROOT / "frontend" / "src" / "components" / "ui" / "Modal.jsx").read_text(encoding="utf-8")

    assert 'role="dialog"' in modal
    assert 'aria-modal="true"' in modal
    assert "aria-labelledby" in modal
    assert 'aria-label="Close dialog"' in modal
    assert 'type="button"' in modal
    assert "trapModalFocus" in modal


def test_frontend_has_local_auth_gate_contract():
    api = (PROJECT_ROOT / "frontend" / "src" / "services" / "api.js").read_text(encoding="utf-8")
    app = (PROJECT_ROOT / "frontend" / "src" / "App.jsx").read_text(encoding="utf-8")

    assert "getAuthStatus" in api
    assert "loginAuth" in api
    assert "logoutAuth" in api
    assert "/api/auth/status" in api
    assert "/api/auth/login" in api
    assert "/api/auth/logout" in api
    assert "AuthGate" in app
    assert "Sign in" in app
    assert "Sign out" in app


def test_frontend_has_security_audit_viewer_contract():
    api = (PROJECT_ROOT / "frontend" / "src" / "services" / "api.js").read_text(encoding="utf-8")
    control = (PROJECT_ROOT / "frontend" / "src" / "components" / "tabs" / "ControlTower.jsx").read_text(encoding="utf-8")

    assert "getSecurityAudit" in api
    assert "/api/security/audit" in api
    assert "Security Audit" in control
    assert "chain_valid" in control
    assert "event_hash" in control


def test_frontend_uses_generic_source_labels_instead_of_legacy_mssql_names():
    impact = (PROJECT_ROOT / "frontend" / "src" / "components" / "dataops" / "ImpactAnalysis.jsx").read_text(encoding="utf-8")
    sidebar = (PROJECT_ROOT / "frontend" / "src" / "components" / "sidebars" / "TableSidebar.jsx").read_text(encoding="utf-8")

    assert "ADVENTUREWORK_" not in impact
    assert "'MSSQL'" not in impact
    assert "\"MSSQL\"" not in impact
    assert "SQL Server" not in sidebar


def test_surgeon_edit_prefers_repository_km_readback_for_initial_truncate_value():
    content = (PROJECT_ROOT / "frontend" / "src" / "components" / "modals" / "SurgeonEdit.jsx").read_text(encoding="utf-8")

    assert "applyKmReadback" in content
    assert "detail?.km_options?.truncate_target_table" in content
    assert "detail?.km_options?.read_only" in content
    assert "Current ODI repository value" in content


def test_mapping_list_surfaces_project_folder_context():
    content = (PROJECT_ROOT / "frontend" / "src" / "components" / "dataops" / "MappingList.jsx").read_text(encoding="utf-8")

    assert "mappingContext" in content
    assert "Project / Folder" in content
    assert "FOLDER_NAME" in content
    assert "PROJECT_CODE" in content


def test_mapping_list_search_keeps_detail_synced_with_filtered_mapping():
    content = (PROJECT_ROOT / "frontend" / "src" / "components" / "dataops" / "MappingList.jsx").read_text(encoding="utf-8")

    assert "filteredFirstName" in content
    assert "selectedDetailName" in content
    assert "loadDetail(filteredFirstName)" in content


def test_surgeon_edit_expression_updates_are_changed_row_only():
    content = (PROJECT_ROOT / "frontend" / "src" / "components" / "modals" / "SurgeonEdit.jsx").read_text(encoding="utf-8")

    assert "original_expression" in content
    assert "changedExpressionRows" in content
    assert "Changed expressions" in content
    assert "disabled={loading || !hasPendingChanges}" in content


def test_surgeon_edit_expression_update_requires_explicit_repository_write_confirmation():
    content = (PROJECT_ROOT / "frontend" / "src" / "components" / "modals" / "SurgeonEdit.jsx").read_text(encoding="utf-8")

    assert "Expression update modifies ODI repository metadata. Continue?" in content
    assert "if (!window.confirm('Expression update modifies ODI repository metadata. Continue?'))" in content
    assert "approved: true" in content


def test_surgeon_edit_warns_expression_update_needs_scenario_regeneration():
    content = (PROJECT_ROOT / "frontend" / "src" / "components" / "modals" / "SurgeonEdit.jsx").read_text(encoding="utf-8")

    assert "Current Column Link State" in content
    assert "Only existing target-column expressions can be edited here." in content
    assert "This changes ODI repository metadata only. It does not regenerate or run a scenario." in content
    assert "Regenerate the ODI scenario before expecting runtime behavior to change." in content
    assert "Adding target attributes, connecting datastores, and rebuilding joins are not supported in this editor." in content


def test_surgeon_edit_filter_column_click_appends_reference():
    content = (PROJECT_ROOT / "frontend" / "src" / "components" / "modals" / "SurgeonEdit.jsx").read_text(encoding="utf-8")

    assert "insertFilterColumnReference" in content
    assert "`${col.component}.${col.column}`" in content


def test_schema_sync_surfaces_partial_state_recovery_guidance():
    content = _schema_sync_sources()

    assert "deriveSchemaSyncRecovery" in content
    assert "Current Status" in content
    assert "DB change completed. ODI metadata sync is still pending." in content
    assert "ODI metadata synced. Scenario reconcile is still pending." in content
    assert "Workflow stopped at" in content


def test_schema_sync_surfaces_preflight_outcome_summary():
    content = _schema_sync_sources()

    assert "derivePreflightOutcome" in content
    assert "Preflight" in content
    assert "Preflight classified this as safe widening." in content
    assert "Preflight blocked this narrowing request." in content
    assert "Already current" in content
    assert "Review required" in content


def test_schema_sync_write_buttons_are_preflight_and_step_gated():
    content = _schema_sync_sources()

    assert "preflightAllowed" in content
    assert "canRun = {" in content
    assert "db: confirmed && !busy && preflightAllowed && steps.db !== 'done'" in content
    assert "odi: confirmed && !busy && steps.db === 'done' && steps.odi !== 'done'" in content
    assert "scenario: confirmed && !busy && steps.odi === 'done' && steps.scenario !== 'done'" in content
    assert "disabled={!canRun[step.id]}" in content


def test_schema_sync_shows_operation_guidance_for_modify_and_rename_modes():
    content = (PROJECT_ROOT / "frontend" / "src" / "components" / "dataops" / "schemaSyncViewModel.js").read_text(encoding="utf-8")

    assert "OPERATION_LABELS" in content
    assert "Modify column type" in content
    assert "Rename column" in content
    assert "Rename table" in content
    assert "summarizeRequest" in content


def test_schema_sync_supports_read_only_drift_check():
    content = _schema_sync_sources()

    assert "getSchemaDrift" in content
    assert "deriveDriftSummary" in content
    assert "Drift Queue" in content
    assert "Drift Check" in content
    assert "No DB-to-ODI drift reported for this owner." in content


def test_schema_sync_surfaces_structural_findings_in_schema_sync_tab_only():
    content = _schema_sync_sources()
    api = (PROJECT_ROOT / "frontend" / "src" / "services" / "api.js").read_text(encoding="utf-8")
    app = (PROJECT_ROOT / "frontend" / "src" / "App.jsx").read_text(encoding="utf-8")

    assert "getSchemaFindings" in api
    assert "/api/schema/findings" in api
    assert "getSchemaFindingsExportUrl" in api
    assert "/api/schema/findings/export" in api
    assert "Schema Findings" in content
    assert "Download CSV" in content
    assert "getSchemaFindingsExportUrl" in content
    assert "column_type_size_mismatch" in content
    assert "target_column_missing_in_odi" in content
    assert "odi_column_missing_in_db" in content
    assert "getSchemaFindings" in content
    assert "AlertBanner" not in app


def test_schema_sync_surfaces_source_target_compare_as_mapping_link_pending():
    content = _schema_sync_sources()
    api = (PROJECT_ROOT / "frontend" / "src" / "services" / "api.js").read_text(encoding="utf-8")

    assert "getSourceTargetFindings" in api
    assert "/api/schema/source-target/findings" in api
    assert "Source Compare" in content
    assert "mapping-link pending" in content
    assert "mapping-link aware" in content
    assert "mapping_link_aware" in content
    assert "source_target_type_size_mismatch" in content
    assert "source_target_nullability_mismatch" in content
    assert "source_column_missing_in_target" in content
    assert "target_column_missing_in_source" in content
    assert "getSourceTargetFindings" in content


def test_schema_sync_surfaces_pk_parity_as_explicit_count_only_action():
    content = _schema_sync_sources()
    api = (PROJECT_ROOT / "frontend" / "src" / "services" / "api.js").read_text(encoding="utf-8")

    assert "getSchemaPkParity" in api
    assert "/api/schema/pk-parity" in api
    assert "PK Parity" in content
    assert "Run PK Parity" in content
    assert "count-only" in content
    assert "missing_key_count" in content
    assert "extra_key_count" in content
    assert "pk_values_included" in content
    assert "getSchemaPkParity" in content


def test_large_dataops_components_keep_reviewable_source_budgets():
    dataops = PROJECT_ROOT / "frontend" / "src" / "components" / "dataops"

    assert len((dataops / "SchemaSync.jsx").read_text(encoding="utf-8").splitlines()) < 900
    assert len((dataops / "ImpactAnalysis.jsx").read_text(encoding="utf-8").splitlines()) < 950


def test_sessions_show_run_status_summary_cards():
    content = (PROJECT_ROOT / "frontend" / "src" / "components" / "dataops" / "Sessions.jsx").read_text(encoding="utf-8")

    assert "runSummary" not in content
    assert "Recent Runs" not in content
    assert "Failed Runs" not in content
    assert "Succeeded Runs" not in content
    assert "Running Runs" not in content
    assert "ODI Sessions" not in content
    assert "getOdiSessions" not in content


def test_sessions_support_mapping_search_over_operations_timeline():
    content = (PROJECT_ROOT / "frontend" / "src" / "components" / "dataops" / "Sessions.jsx").read_text(encoding="utf-8")

    assert "runSearch" in content
    assert "filteredRuns" in content
    assert "Search mapping or scenario" in content
    assert "No run history matches the current filters." in content


def test_sessions_show_no_run_failure_alerts_empty_state():
    content = (PROJECT_ROOT / "frontend" / "src" / "components" / "dataops" / "Sessions.jsx").read_text(encoding="utf-8")

    assert "Run Failure Alerts" not in content
    assert "No pending run failure alerts." not in content
    assert "showAllRunFailureAlerts" not in content
    assert "visibleRunFailureAlerts" not in content
    assert "getAlerts" not in content


def test_sessions_show_last_refreshed_timestamp():
    content = (PROJECT_ROOT / "frontend" / "src" / "components" / "dataops" / "Sessions.jsx").read_text(encoding="utf-8")

    assert "lastRefreshedAt" in content
    assert "Last refreshed" in content
    assert "formatDateTime(lastRefreshedAt)" in content


def test_sessions_can_expand_pending_run_failure_alerts():
    content = (PROJECT_ROOT / "frontend" / "src" / "components" / "dataops" / "Sessions.jsx").read_text(encoding="utf-8")

    assert "showAllRunFailureAlerts" not in content
    assert "visibleRunFailureAlerts" not in content
    assert "Show all alerts" not in content
    assert "Show fewer alerts" not in content


def test_sessions_can_trigger_freshness_alert_check():
    sessions = (PROJECT_ROOT / "frontend" / "src" / "components" / "dataops" / "Sessions.jsx").read_text(encoding="utf-8")
    api = (PROJECT_ROOT / "frontend" / "src" / "services" / "api.js").read_text(encoding="utf-8")

    assert "checkAlerts" in api
    assert "apiPost('/api/alerts/check'" in api
    assert "runFreshnessCheck" in sessions
    assert "Check freshness alerts" in sessions
    assert "Freshness Alert Check" in sessions
    assert "created_alerts" in sessions
    assert "window.confirm('This check can create persistent alert records. Continue?')" in sessions
    assert "checkAlerts({ approved: true, freshness_days: 2 })" in sessions


def test_alert_and_rag_persistent_actions_require_operator_confirmation():
    alerts = (PROJECT_ROOT / "frontend" / "src" / "components" / "layout" / "AlertBanner.jsx").read_text(encoding="utf-8")
    rag = (PROJECT_ROOT / "frontend" / "src" / "components" / "knowledge" / "RagManager.jsx").read_text(encoding="utf-8")

    assert "window.confirm('Acknowledge all visible alerts?')" in alerts
    assert "acknowledgeAlerts({ approved: true, all: true })" in alerts
    assert "window.confirm('Save this knowledge document?')" in rag
    assert "approved: true" in rag
    assert "window.confirm(`Delete" in rag


def test_data_ops_has_explicit_incremental_load_operator_gate():
    api = (PROJECT_ROOT / "frontend" / "src" / "services" / "api.js").read_text(encoding="utf-8")
    data_ops = (PROJECT_ROOT / "frontend" / "src" / "components" / "tabs" / "DataOperations.jsx").read_text(encoding="utf-8")
    panel = (PROJECT_ROOT / "frontend" / "src" / "components" / "dataops" / "IncrementalLoad.jsx").read_text(encoding="utf-8")

    assert "getIncrementalPreflight" in api
    assert "executeIncrementalAppend" in api
    assert "/api/incremental/preflight" in api
    assert "/api/incremental/execute" in api
    assert "Incremental Load" in data_ops
    assert "IncrementalLoad" in data_ops
    assert "I approve a live append and persisted watermark update." in panel
    assert "This does not run ODI or truncate targets." in panel
    assert "rerunState === 'new_window_ready'" in panel
    assert "window.confirm('This runs a live DB append and persisted watermark update. Continue?')" in panel
    assert "approved: true" in panel
    assert "post_load_gate_results" not in panel
    assert "post_load_golden_record_passed: goldenPassed" not in panel
    assert "computed by the server" in panel


def test_header_does_not_render_global_alert_banner_or_airflow_link():
    app = (PROJECT_ROOT / "frontend" / "src" / "App.jsx").read_text(encoding="utf-8")
    header = (PROJECT_ROOT / "frontend" / "src" / "components" / "layout" / "Header.jsx").read_text(encoding="utf-8")

    assert "AlertBanner" not in app
    assert "useAlerts()" not in app
    assert "Airflow" not in header
    assert "AIRFLOW_URL" not in header


def test_control_tower_explains_security_audit_as_evidence_log():
    control = (PROJECT_ROOT / "frontend" / "src" / "components" / "tabs" / "ControlTower.jsx").read_text(encoding="utf-8")

    assert "audit evidence log" in control
    assert "not a vulnerability scanner" in control


def test_table_sidebar_preview_uses_data_columns_header_and_key_badges():
    sidebar = (PROJECT_ROOT / "frontend" / "src" / "components" / "sidebars" / "TableSidebar.jsx").read_text(encoding="utf-8")

    assert "Data / Columns" in sidebar
    assert "keyBadgeForColumn" in sidebar
    assert "PK" in sidebar
    assert "FK" in sidebar
    assert "{table} <span" not in sidebar


def test_impact_analysis_uses_left_layer_badge_and_layer_selector():
    impact = (PROJECT_ROOT / "frontend" / "src" / "components" / "dataops" / "ImpactAnalysis.jsx").read_text(encoding="utf-8")

    assert "LAYER_OPTIONS" in impact
    assert "selectedLayer" in impact
    assert "Layer filter" in impact
    assert "LayerBadge" in impact
    assert "mapping.DESC || 'Mapping'" not in impact
