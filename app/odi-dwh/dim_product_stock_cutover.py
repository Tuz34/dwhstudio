"""Default-off fixed stock cutover using reviewed metadata and remote executors.

No database, repository or configuration is opened at import. All target data
changes are performed by the ownership-checked ODI scenario, never local SQL.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import dim_product_audit_role_repair as audit
import dim_product_scd2_stock_checkpoint as checkpoint
import execution_identity
import remote_run_contract
import repository_write_contract as write_contract
from dim_product_scd2_scenario_backup import call_sanitized
from scd2_builder import TARGET_MODEL_BY_OWNER
from scd2_manager import _TRACKED_ATTRIBUTES

MAPPING = "MAP_ODS_PRODUCTS_TO_DIM_PRODUCT"
SCENARIO = MAPPING + "_SCN"
VERSION = "008"
CONTEXT = "GLOBAL"
GENERATION_SCOPE = "odi87_dim_product_stock_scenario_generation"
RUN_SCOPE = "odi87_dim_product_stock_scenario_run_and_rerun_proof"
SCOPES = (checkpoint.CREATE_SCOPE, audit.STABLE_REPAIR_SCOPE, GENERATION_SCOPE, RUN_SCOPE)
REVIEWS = ("checkpoint", "audit", "generator", "runner", "cutover")


def full_approvals() -> dict[str, bool]:
    """Operator convenience only; callers still must explicitly opt into execution."""
    return dict.fromkeys(SCOPES, True)


def _base() -> dict[str, Any]:
    return dict(mode="fixed_stock_scd2_cutover", status="blocked", stage="approval", steps=[],
                scenario={"name": SCENARIO, "version": VERSION, "context": CONTEXT}, counts={},
                side_effects=False, write_attempted=False, outcome_unknown=False,
                manual_review_required=False, business_parity_proven=False,
                full_target_snapshot_equal=False, rerun_full_snapshot_equal=False,
                physical_dml_count_proven=False, raw_rows_included=False,
                pk_values_included=False, secret_printed=False)


def _count(cursor: Any, sql: str) -> int:
    cursor.execute(sql)
    row = cursor.fetchone()
    if row is None or type(row[0]) is not int or row[0] < 0:
        raise ValueError("InvalidAggregate")
    return row[0]


def _business_read(factory: Callable[[], Any], resources: list[Any]) -> dict[str, int]:
    connection = factory()
    resources.append(connection)
    if connection.autocommit is not False:
        raise ValueError("AutocommitRejected")
    cursor = connection.cursor()
    resources.append(cursor)
    cursor.execute("SET TRANSACTION READ ONLY")
    columns = ','.join(f'"{name}"' for name in ("PROD_ID", *_TRACKED_ATTRIBUTES))
    source = f'SELECT {columns} FROM "DWH"."ODS_SH_PRODUCTS"'
    target = f'SELECT {columns} FROM "DWH"."DIM_PRODUCT" WHERE "CURRENT_FLAG"=\'1\''
    statements = {
        "source_count": 'SELECT COUNT(*) FROM "DWH"."ODS_SH_PRODUCTS"',
        "source_null_keys": 'SELECT COUNT(*) FROM "DWH"."ODS_SH_PRODUCTS" WHERE "PROD_ID" IS NULL',
        "source_duplicate_keys": 'SELECT COUNT(*) FROM (SELECT "PROD_ID" FROM "DWH"."ODS_SH_PRODUCTS" GROUP BY "PROD_ID" HAVING COUNT(*)>1)',
        "source_minus_current": f"SELECT COUNT(*) FROM ({source} MINUS {target})",
        "current_minus_source": f"SELECT COUNT(*) FROM ({target} MINUS {source})",
    }
    return {name: _count(cursor, sql) for name, sql in statements.items()}


def business_proof(factory: Callable[[], Any]) -> dict[str, Any]:
    """Read-only aggregate parity over all 17 tracked columns and the natural key."""
    resources: list[Any] = []
    try:
        counts = call_sanitized(_business_read, factory, resources)
    finally:
        clean = checkpoint._cleanup(resources)
    valid = clean and isinstance(counts, dict) and all(
        value == (72 if name == "source_count" else 0) for name, value in counts.items())
    return {"status": "passed" if valid else "failed", "counts": counts if valid else {},
            "raw_rows_included": False, "pk_values_included": False, "secret_printed": False}


def _step(report: dict[str, Any], name: str, child: object, statuses: tuple[str, ...] = ("passed",)) -> bool:
    valid = isinstance(child, Mapping) and isinstance(child.get("status"), str) and child["status"] in statuses
    report["steps"].append({"action": name, "status": "passed" if valid else "failed"})
    if isinstance(child, Mapping):
        if child.get("side_effects") is True:
            report["side_effects"] = True
        if child.get("outcome_unknown") is True or child.get("side_effects", False) is None:
            report["outcome_unknown"] = True
            if report["side_effects"] is not True:
                report["side_effects"] = None
        if child.get("manual_review_required") is True or child.get("manual_recovery_review_required") is True:
            report["manual_review_required"] = True
    if not valid:
        report.update(status="failed", stage=name, business_parity_proven=False,
                      full_target_snapshot_equal=False, rerun_full_snapshot_equal=False,
                      manual_review_required=report["write_attempted"] or report["manual_review_required"])
    return valid


def _witness(report: dict[str, Any], stage: str, factory: Callable[[], Any]) -> bool:
    backup = checkpoint.verify_stock_checkpoint(factory)
    if backup.get("full_snapshot_equal") is not True:
        backup = {**backup, "status": "failed"}
    if not _step(report, stage + "_checkpoint", backup):
        return False
    business = business_proof(factory)
    if not _step(report, stage + "_business", business):
        return False
    report["counts"] = business["counts"]
    return True


def _generation_matches(child: object, *, existing: bool = False) -> bool:
    if not isinstance(child, Mapping):
        return False
    scenario, checks = child.get("scenario"), child.get("checks")
    if (child.get("tool") != "odi.generate_scenario_metadata" or child.get("mode") != "scenario_metadata_generation"
            or child.get("status") != ("already_ready" if existing else "passed")
            or child.get("side_effects") is not (False if existing else True)
            or child.get("repository_connection_attempted") is not True or not isinstance(scenario, Mapping)
            or scenario.get("name") != SCENARIO or scenario.get("version") != VERSION or not isinstance(checks, list)):
        return False
    names: dict[str, str] = {}
    for check in checks:
        if (not isinstance(check, Mapping) or not isinstance(check.get("name"), str)
                or check.get("status") not in ("pass", "warning") or check["name"] in names):
            return False
        names[check["name"]] = check["status"]
    required = "scenario_already_ready" if existing else "scenario_generated"
    return names.get(required) == names.get("forbidden_operations_not_called") == "pass"


def _approved(live: object, approvals: object, reviews: object, exclusive: object, receipt_dir: object) -> bool:
    return (live is True and exclusive is True and isinstance(approvals, Mapping)
            and all(approvals.get(scope) is True for scope in SCOPES) and isinstance(reviews, Mapping)
            and all(reviews.get(name) == "approved" for name in REVIEWS)
            and isinstance(receipt_dir, (str, Path)) and Path(receipt_dir).is_absolute()
            and call_sanitized(Path(receipt_dir).is_dir) is True)


def _identity(report: dict[str, Any], stage: str, orchestrator: Any) -> bool:
    evidence = call_sanitized(orchestrator.prove_execution_identity,
        model_code=TARGET_MODEL_BY_OWNER["DWH"], context_code=CONTEXT, schema_name="DWH")
    valid = isinstance(evidence, tuple) and len(evidence) == 2
    proof = evidence[1] if valid else None
    valid = (valid and isinstance(proof, execution_identity.ExecutionIdentityProof)
             and proof.model_code == TARGET_MODEL_BY_OWNER["DWH"] and proof.context_code == CONTEXT
             and proof.schema_name == "DWH")
    return _step(report, stage, {"status": "passed" if valid else "failed"})


def execute_stock_cutover(*, live: object = False, approvals: object = None, reviews: object = None,
                          exclusive_operator_attested: object = False, receipt_dir: object = None,
                          connection_factory: Callable[[], Any], orchestrator: Any) -> dict[str, Any]:
    """Back up, repair stable audit metadata, create exact absent 008 and run twice.

    Old 001 and its historical Y/N evidence remain untouched. Existing 008 is
    not reused: only a new generation in this invocation can authorize its runs.
    """
    report = _base()
    if (not _approved(live, approvals, reviews, exclusive_operator_attested, receipt_dir)
            or not callable(connection_factory)):
        return report
    if not _identity(report, "execution_identity", orchestrator):
        return report
    # An existing checkpoint is usable only when it still matches every target column.
    retained = checkpoint.verify_stock_checkpoint(connection_factory)
    if retained.get("status") != "passed":
        report["write_attempted"] = True
        retained = checkpoint.create_stock_checkpoint(connection_factory, approved=True,
            approval_scope=checkpoint.CREATE_SCOPE, review_status=reviews["checkpoint"])
    if not _step(report, "checkpoint", retained) or not _witness(report, "before_metadata", connection_factory):
        return report
    profile = call_sanitized(orchestrator.dim_product_audit_role_action, action="stable_preflight", approved=True,
        approval_scope=audit.STABLE_PREFLIGHT_SCOPE)
    if not isinstance(profile, Mapping) or profile.get("status") not in ("ready_for_repair", "already_repaired"):
        _step(report, "stable_audit_preflight", None)
        return report
    if profile["status"] == "ready_for_repair":
        report["write_attempted"] = True
        repaired = call_sanitized(orchestrator.dim_product_audit_role_action, action="stable_repair", approved=True,
            approval_scope=audit.STABLE_REPAIR_SCOPE, implementation_review_status=reviews["audit"],
            no_active_odi_session_writes_attested=True, receipt_dir=receipt_dir)
        if not _step(report, "stable_audit_repair", repaired, ("repaired", "already_repaired")):
            if repaired is None:
                report["outcome_unknown"] = True
            return report
        profile = call_sanitized(orchestrator.dim_product_audit_role_action, action="stable_preflight", approved=True,
            approval_scope=audit.STABLE_PREFLIGHT_SCOPE)
    if not _step(report, "stable_audit_probe", profile, ("already_repaired",)):
        return report
    if profile.get("expression_profile") != audit.STABLE_PROFILE_ID:
        _step(report, "stable_audit_profile", None)
        return report
    mapping = call_sanitized(orchestrator.dim_product_scd2_mapping_probe, approved=True)
    if not _step(report, "mapping_probe", mapping):
        return report
    report["write_attempted"] = True
    generated = call_sanitized(orchestrator.generate_scenario_metadata, approved=True,
        mapping_name=MAPPING, scenario_name=SCENARIO, version=VERSION)
    if not _generation_matches(generated):
        state = write_contract.failure(generated, "odi.generate_scenario_metadata", "scenario_metadata_generation", generator=True)
        if state is None:
            state = {"side_effects": False} if _generation_matches(generated, existing=True) else write_contract.unverified()
        _step(report, "scenario_generation", {"status": "failed", **state})
        return report
    _step(report, "scenario_generation", generated)
    for run in (1, 2):
        if not _identity(report, f"identity_before_run_{run}", orchestrator):
            return report
        if not _witness(report, f"before_run_{run}", connection_factory):
            return report
        profile = call_sanitized(orchestrator.dim_product_audit_role_action,
            action="stable_preflight", approved=True, approval_scope=audit.STABLE_PREFLIGHT_SCOPE)
        if not _step(report, f"audit_before_run_{run}", profile, ("already_repaired",)):
            return report
        if profile.get("expression_profile") != audit.STABLE_PROFILE_ID:
            _step(report, f"audit_profile_before_run_{run}", None)
            return report
        raw = call_sanitized(orchestrator.run_scenario_remote, approved=True,
            mapping_name=MAPPING, scenario_name=SCENARIO, version=VERSION, context=CONTEXT)
        remote = remote_run_contract.parse(raw, "run")
        if not _step(report, f"scenario_run_{run}", remote):
            return report
        if not _witness(report, f"after_run_{run}", connection_factory):
            return report
    report.update(status="passed", stage="completed", business_parity_proven=True,
                  full_target_snapshot_equal=True, rerun_full_snapshot_equal=True)
    return report


def run_configured_stock_cutover(*, live: object = False, approvals: object = None, reviews: object = None,
                                exclusive_operator_attested: object = False, receipt_dir: object = None) -> dict[str, Any]:
    """Operator adapter; does not import configuration or instantiate ODI before gates."""
    if not _approved(live, approvals, reviews, exclusive_operator_attested, receipt_dir):
        return _base()
    import oracledb

    from config import ORACLE_DSN, ORACLE_PASSWORD, ORACLE_USER
    from odi_orchestrator import OdiOrchestrator

    def factory() -> Any:
        return oracledb.connect(user=ORACLE_USER, password=ORACLE_PASSWORD, dsn=ORACLE_DSN)

    return execute_stock_cutover(connection_factory=factory, orchestrator=OdiOrchestrator(), live=live,
        approvals=approvals, reviews=reviews, exclusive_operator_attested=exclusive_operator_attested, receipt_dir=receipt_dir)
