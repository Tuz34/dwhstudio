"""Operator script for the ODI-68 DIM_PRODUCT SCD2 scenario run and rerun proof.

Not an endpoint and never imported by the web app. Run it by hand, after the
owner's fresh exact approval, from ``app/odi-dwh``::

    python odi68_dim_product_scd2_live_proof.py --approved yes
        --scope odi68_dim_product_odi_scd2_scenario_run_and_rerun_proof
        --review-evidence <review.json> --out <report.json>

``review.json`` is a JSON object with ``executor_review_status`` and
``scenario_generation_review_status`` both equal to ``"approved"`` (the
independent reviewers' sign-off, supplied by the operator). Its content is
never printed. The script prints and writes a count-only report (no session
ids, key values or raw rows) and exits 0 only when the proof passed. The
named backup is retained; nothing is restored or dropped automatically.
"""

from __future__ import annotations

import argparse
import functools
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dim_product_odi_scd2_packaging_readiness import SCENARIO_NAME, SCENARIO_VERSION
from dim_product_odi_scd2_scenario import (
    RUN_APPROVAL_SCOPE,
    candidate_preflight_to_gate_report,
)
from dim_product_scd2_scenario_backup import call_sanitized

EXIT_PASSED = 0
EXIT_NOT_PASSED = 1
EXIT_BAD_ARGUMENTS = 2
_MAX_EVIDENCE_BYTES = 4096
_SCALARS = (bool, int, str, type(None))


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # never echo argv text
        raise ValueError("invalid arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(add_help=False)
    parser.add_argument("--approved", required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--review-evidence", required=True)
    parser.add_argument("--out", required=True)
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
    return data if isinstance(data, dict) else None


def scenario_probe_to_gate_report(raw: object) -> dict[str, Any]:
    """Adapt the orchestrator's scenario probe output to the run-readiness contract.

    Keeps only status, the fixed name/version and an ``exists`` flag; anything
    unexpected becomes an error report that the readiness gate rejects.
    """
    scenario = raw.get("scenario") if isinstance(raw, Mapping) else None
    if (
        not isinstance(raw, Mapping)
        or raw.get("side_effects") is not False
        or raw.get("status") not in {"passed", "attention_required"}
        or not isinstance(scenario, Mapping)
        or scenario.get("name") != SCENARIO_NAME
        or scenario.get("version") != SCENARIO_VERSION
    ):
        return {"status": "error"}
    return {
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


def _default_mapping_probe() -> Mapping[str, Any]:
    from odi_orchestrator import OdiOrchestrator

    return OdiOrchestrator().dim_product_scd2_mapping_probe(approved=True)


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


def _default_backup_creator() -> Callable[..., Mapping[str, Any]]:
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


def _clean(report: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only scalar fields and the fixed scenario triple."""
    out: dict[str, Any] = {}
    for key, value in report.items():
        if isinstance(value, _SCALARS):
            out[str(key)] = value
        elif key == "scenario" and isinstance(value, Mapping):
            out[key] = {str(k): v for k, v in value.items() if isinstance(v, _SCALARS)}
    return out


def run(
    argv: Sequence[str],
    *,
    executor: Callable[..., dict[str, Any]] | None = None,
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

    def emit(report: Mapping[str, Any], out_path: str | None) -> None:
        text = json.dumps(_clean(report), indent=2, sort_keys=True)
        print(text, file=out_stream)
        if out_path:
            Path(out_path).write_text(text + "\n", encoding="utf-8")

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

    # Arguments are valid: only now resolve and call anything that can touch ODI or the DB.
    mapping_probe = call_sanitized(mapping_probe_loader or _default_mapping_probe) or {}
    scenario_probe = call_sanitized(scenario_probe_loader or _default_scenario_probe) or {}
    run_candidate = candidate_runner or _default_candidate_runner

    def candidate_probe() -> dict[str, Any]:
        return candidate_preflight_to_gate_report(run_candidate(), observed_at=clock())

    result = (executor or _default_executor())(
        mapping_probe,
        scenario_probe,
        scenario_generation_review_status=evidence["scenario_generation_review_status"],
        executor_review_status=evidence["executor_review_status"],
        approved=True,
        approval_scope=args.scope,
        candidate_probe=candidate_probe,
        backup_creator=backup_creator or _default_backup_creator(),
        scenario_runner=scenario_runner or _default_scenario_runner(),
    )
    emit(result, args.out)
    return EXIT_PASSED if result.get("status") == "passed" else EXIT_NOT_PASSED


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
