"""Operator script for the ODI-68 DIM_PRODUCT SCD2 scenario run and rerun proof.

Not an endpoint and never imported by the web app. Run it by hand, after the
owner's fresh exact approval, from ``app/odi-dwh``::

    python odi68_dim_product_scd2_live_proof.py --approved yes
        --scope odi68_dim_product_odi_scd2_scenario_run_and_rerun_proof
        --review-evidence <review.json> --out <report.json>

``review.json`` is a JSON object with ``executor_review_status`` and
``scenario_generation_review_status`` both equal to ``"approved"`` (the
independent reviewers' sign-off, supplied by the operator). Its content is
never printed; any extra key, missing key or non-string value is refused.
The script prints and writes a count-only report restricted to an explicit
allowlist of fields (no session ids, key values or raw rows).

Exit codes: 0 proof passed; 1 proof ran but did not pass; 2 refused before
any callback ran (bad arguments, approval, scope, review evidence or an
unusable ``--out`` path); 3 the run finished but the report file could not be
written (the scalar report is still printed to stdout). The named backup is
retained; nothing is restored or dropped automatically.

The configured agent endpoint is checked before any ODI/DB callback. This is
a configuration-shape check, not an authentication or availability proof.
``--resume-with-retained-backup`` explicitly requests read-only verification of
the retained backup instead of creating it again. The default stays absent-only.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dim_product_odi_scd2_packaging_readiness import SCENARIO_NAME, SCENARIO_VERSION
from dim_product_odi_scd2_scenario import (
    RUN_APPROVAL_SCOPE,
    candidate_preflight_to_gate_report,
)
from dim_product_scd2_scenario_backup import call_sanitized

EXIT_PASSED = 0
EXIT_NOT_PASSED = 1
EXIT_BAD_ARGUMENTS = 2
EXIT_REPORT_WRITE_FAILED = 3
_EVIDENCE_KEYS = frozenset({"executor_review_status", "scenario_generation_review_status"})
_REPORT_FIELDS = frozenset({
    "mode", "status", "stage", "error_type",
    "scenario_run_attempt_count", "successful_scenario_run_count",
    "named_backup_retained", "backup_may_exist", "automatic_rollback_performed",
    "persistent_action_attempted", "manual_recovery_review_required",
    "raw_rows_included", "pk_values_included", "session_id_included",
    "raw_output_included", "secret_printed",
    "source_count", "target_count", "current_target_count", "unchanged_count",
    "remaining_candidate_count", "named_backup_column_count",
    "named_backup_row_count", "ods_backup_untouched",
    "backup_reused_verified", "backup_created_absent_only",
})
_SCENARIO_FIELDS = frozenset({"name", "version", "context"})
_MAX_TEXT = 80
_PROBE_TOOL = "odi.scenario_probe"
_PROBE_MODE = "scenario_finders_only"
_MAX_EVIDENCE_BYTES = 4096


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # never echo argv text
        raise ValueError("invalid arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(add_help=False)
    parser.add_argument("--approved", required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--review-evidence", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--resume-with-retained-backup", action="store_true")
    return parser


def _blocked(status: str) -> dict[str, Any]:
    return {
        "mode": "fixed_dim_product_scd2_scenario_run_and_rerun_proof",
        "status": status,
        "persistent_action_attempted": False,
        "raw_rows_included": False,
        "pk_values_included": False,
        "session_id_included": False,
        "secret_printed": False,
    }


def _load_review_evidence(path: str) -> Mapping[str, Any] | None:
    try:
        file = Path(path)
        if not file.is_file() or file.stat().st_size > _MAX_EVIDENCE_BYTES:
            return None
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(data, dict)
        or set(data) != _EVIDENCE_KEYS
        or any(not isinstance(v, str) or v != "approved" for v in data.values())
    ):
        return None
    return data


def _probe_consistent(raw: Mapping[str, Any], scenario: Mapping[str, Any]) -> bool:
    """True only when every identity and found-signal of a passed probe agrees."""
    checks = raw.get("checks")
    found = (
        [c for c in checks if isinstance(c, Mapping) and c.get("name") == "scenario_found"]
        if isinstance(checks, list)
        else []
    )
    latest = scenario.get("latest_version")
    return (
        raw.get("tool") == _PROBE_TOOL
        and raw.get("mode") == _PROBE_MODE
        and raw.get("repository_connection_attempted") is True
        and len(found) == 1
        and isinstance(found[0].get("status"), str)
        and found[0].get("status") in {"pass", "passed"}
        and latest == SCENARIO_VERSION
    )


def _probe_error() -> dict[str, Any]:
    """Class-only error; no untrusted probe text reaches the report."""
    return {
        "status": "error",
        "side_effects": False,
        "raw_rows_included": False,
        "pk_values_included": False,
        "scenario": {"name": SCENARIO_NAME, "version": SCENARIO_VERSION, "exists": False},
        "error_type": "InconsistentScenarioProbe",
    }


def scenario_probe_to_gate_report(raw: object) -> dict[str, Any]:
    """Adapt the orchestrator's scenario probe output to the run-readiness contract.

    Keeps only status, the fixed name/version and an ``exists`` flag. ``exists``
    is True only when the probe is fully self-consistent; a ``passed`` probe
    with any inconsistency is reported as a class-only error with
    ``exists=False``. Anything else unexpected becomes an error report.
    """
    scenario = raw.get("scenario") if isinstance(raw, Mapping) else None
    if (
        not isinstance(raw, Mapping)
        or raw.get("side_effects") is not False
        or not isinstance(raw.get("status"), str)
        or raw.get("status") not in {"passed", "attention_required"}
        or not isinstance(scenario, Mapping)
        or scenario.get("name") != SCENARIO_NAME
        or scenario.get("version") != SCENARIO_VERSION
    ):
        return _probe_error()
    consistent = raw["status"] != "passed" or _probe_consistent(raw, scenario)
    if not consistent:
        return _probe_error()
    report: dict[str, Any] = {
        "status": raw["status"],
        "side_effects": False,
        "raw_rows_included": False,
        "pk_values_included": False,
        "scenario": {
            "name": SCENARIO_NAME,
            "version": SCENARIO_VERSION,
            "exists": raw["status"] == "passed",
        },
    }
    return report


def _default_mapping_probe() -> Mapping[str, Any]:
    from odi_orchestrator import OdiOrchestrator

    return OdiOrchestrator().dim_product_scd2_mapping_probe(approved=True)


def agent_endpoint_ready(endpoint: object) -> bool:
    """Validate configured endpoint shape, not agent authentication or availability."""
    if not isinstance(endpoint, str) or not endpoint:
        return False
    try:
        parsed = urlsplit(endpoint)
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.port == 15101
            and parsed.path.rstrip("/") == "/oraclediagent"
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


def _default_agent_configuration_probe() -> bool:
    import config

    return agent_endpoint_ready(config.ODI_REST_URL)


def _default_scenario_probe() -> Mapping[str, Any]:
    from odi_orchestrator import OdiOrchestrator

    raw = OdiOrchestrator().scenario_probe(
        approved=True, scenario_name=SCENARIO_NAME, version=SCENARIO_VERSION
    )
    return scenario_probe_to_gate_report(raw)


def _default_candidate_runner() -> Mapping[str, Any]:
    from dim_product_scd2_candidate_preflight import (
        run_configured_dim_product_scd2_candidate_preflight,
    )

    return run_configured_dim_product_scd2_candidate_preflight()


def _configured_ddl_connection() -> Any:
    """DDL-capable target connection (TARGET_ORACLE_* defaults to the DDL user)."""
    import oracledb

    import config

    return oracledb.connect(
        user=config.TARGET_ORACLE_USER,
        password=config.TARGET_ORACLE_PASSWORD,
        dsn=config.TARGET_ORACLE_DSN,
    )


def _default_backup_creator(*, retained_backup: bool = False) -> Callable[..., Mapping[str, Any]]:
    if retained_backup:
        from dim_product_scd2_backup_creator import verify_retained_dim_product_backup

        return functools.partial(verify_retained_dim_product_backup, _configured_ddl_connection)
    from dim_product_scd2_backup_creator import create_dim_product_named_backup

    return functools.partial(create_dim_product_named_backup, _configured_ddl_connection)


def _default_executor() -> Callable[..., dict[str, Any]]:
    from dim_product_odi_scd2_scenario_run_executor import (
        execute_dim_product_scd2_scenario_run_and_rerun_proof,
    )

    return execute_dim_product_scd2_scenario_run_and_rerun_proof


def _default_scenario_runner() -> Callable[..., Mapping[str, Any]]:
    from dim_product_odi_scd2_scenario_run_executor import scenario_runner

    return scenario_runner


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_scalar(value: object) -> bool:
    return isinstance(value, (bool, int, type(None))) or (
        isinstance(value, str) and len(value) <= _MAX_TEXT
    )


def _clean(report: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only allowlisted scalar report fields and the scenario name/version/context."""
    out: dict[str, Any] = {}
    for key, value in report.items():
        if key in _REPORT_FIELDS and _safe_scalar(value):
            out[key] = value
        elif key == "scenario" and isinstance(value, Mapping):
            out[key] = {
                k: v for k, v in value.items()
                if k in _SCENARIO_FIELDS and _safe_scalar(v)
            }
    return out


def _output_path_usable(raw: str) -> Path | None:
    """Resolve ``--out`` and check parent and existing-file write access without truncation."""
    try:
        if not raw:
            return None
        target = Path(raw).resolve()
        if not target.parent.is_dir() or target.is_dir():
            return None
        if target.exists():
            if not target.is_file():
                return None
            fd = os.open(target, os.O_WRONLY | os.O_APPEND)
            os.close(fd)
        fd, probe = tempfile.mkstemp(dir=target.parent, prefix=".odi68_probe_")
        os.close(fd)
        os.unlink(probe)
    except (OSError, ValueError):
        return None
    return target


def run(
    argv: Sequence[str],
    *,
    executor: Callable[..., dict[str, Any]] | None = None,
    agent_configuration_probe: Callable[[], object] | None = None,
    mapping_probe_loader: Callable[[], Mapping[str, Any]] | None = None,
    scenario_probe_loader: Callable[[], Mapping[str, Any]] | None = None,
    candidate_runner: Callable[[], Mapping[str, Any]] | None = None,
    backup_creator: Callable[..., Mapping[str, Any]] | None = None,
    scenario_runner: Callable[..., Mapping[str, Any]] | None = None,
    clock: Callable[[], datetime] = _utcnow,
    stdout: Any = None,
) -> int:
    """Validate arguments, wire the callbacks, run the proof, emit the report."""
    out_stream = stdout if stdout is not None else sys.stdout

    def emit(report: Mapping[str, Any], out_path: Path | None) -> bool:
        text = json.dumps(_clean(report), indent=2, sort_keys=True)
        print(text, file=out_stream)
        if out_path is None:
            return True
        try:
            out_path.write_text(text + "\n", encoding="utf-8")
        except OSError:
            print("report_write_failed", file=out_stream)
            return False
        return True

    if any(a in {"--help", "-h"} for a in argv):
        print(__doc__, file=out_stream)
        return EXIT_PASSED

    try:
        args = _parser().parse_args(list(argv))
    except ValueError:
        emit(_blocked("blocked_arguments"), None)
        return EXIT_BAD_ARGUMENTS
    if args.approved != "yes":
        emit(_blocked("blocked_until_exact_run_approval"), None)
        return EXIT_BAD_ARGUMENTS
    if args.scope != RUN_APPROVAL_SCOPE:
        emit(_blocked("blocked_run_approval_scope"), None)
        return EXIT_BAD_ARGUMENTS
    evidence = _load_review_evidence(args.review_evidence)
    if evidence is None or any(
        evidence.get(key) != "approved"
        for key in ("executor_review_status", "scenario_generation_review_status")
    ):
        emit(_blocked("blocked_review_evidence"), None)
        return EXIT_BAD_ARGUMENTS

    out_path = _output_path_usable(args.out)
    if out_path is None:
        emit(_blocked("blocked_output_path"), None)
        return EXIT_BAD_ARGUMENTS

    if call_sanitized(agent_configuration_probe or _default_agent_configuration_probe) is not True:
        emit(_blocked("blocked_agent_configuration"), None)
        return EXIT_BAD_ARGUMENTS

    # Arguments are valid: only now resolve and call anything that can touch ODI or the DB.
    mapping_probe = call_sanitized(mapping_probe_loader or _default_mapping_probe) or {}
    scenario_probe = call_sanitized(scenario_probe_loader or _default_scenario_probe) or {}
    run_candidate = candidate_runner or _default_candidate_runner

    def candidate_probe() -> dict[str, Any]:
        return candidate_preflight_to_gate_report(run_candidate(), observed_at=clock())

    run_options = {"retained_backup": True} if args.resume_with_retained_backup else {}
    result = (executor or _default_executor())(
        mapping_probe,
        scenario_probe,
        scenario_generation_review_status=evidence["scenario_generation_review_status"],
        executor_review_status=evidence["executor_review_status"],
        approved=True,
        approval_scope=args.scope,
        candidate_probe=candidate_probe,
        backup_creator=backup_creator or _default_backup_creator(retained_backup=args.resume_with_retained_backup),
        scenario_runner=scenario_runner or _default_scenario_runner(),
        **run_options,
    )
    if not emit(result, out_path):
        return EXIT_REPORT_WRITE_FAILED
    return EXIT_PASSED if result.get("status") == "passed" else EXIT_NOT_PASSED


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
