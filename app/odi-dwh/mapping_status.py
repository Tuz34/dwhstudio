"""Domain status vocabulary for the LangGraph mapping pipeline.

This module provides standard values and helper functions for
MAPPING_RUN_STATE.OVERALL_STATUS. mapping_graph nodes and the UI use these constants
to use the same vocabulary.

Standard vocabulary:
  Lifecycle:
    PENDING            — pipeline has not started
    RUNNING            — pipeline is running
    SUCCEEDED          — pipeline completed successfully
    FAILED             — pipeline failed with an unknown/general error

  Domain (in order):
    DQ_FAILED          — DQ reconciliation step failed
    ODI_SYNC_REQUIRED  — ODI metadata must be synchronized after DDL
    ODI_SYNCED         — ODI Datastore/Mapping synchronized
    IMPACT_READY       — Impact lineage records are ready
    RUN_READY          — Mapping is ready to run (scenario generated)
    RUN_FAILED         — Scenario execution failed

Backward compatibility:
    Legacy status strings ("running", "success", "failed", mixed
    case) are mapped to the new standard vocabulary by `normalize()`
    so the UI/backend can read old state records without handling
    multiple variants.
"""

from __future__ import annotations

# ── Lifecycle ──────────────────────────────────────────────────────────────
PENDING   = "PENDING"
RUNNING   = "RUNNING"
SUCCEEDED = "SUCCEEDED"
FAILED    = "FAILED"

# ── Domain ─────────────────────────────────────────────────────────────────
DQ_FAILED          = "DQ_FAILED"
ODI_SYNC_REQUIRED  = "ODI_SYNC_REQUIRED"
ODI_SYNCED         = "ODI_SYNCED"
IMPACT_READY       = "IMPACT_READY"
RUN_READY          = "RUN_READY"
RUN_FAILED         = "RUN_FAILED"

ALL = {
    PENDING, RUNNING, SUCCEEDED, FAILED,
    DQ_FAILED, ODI_SYNC_REQUIRED, ODI_SYNCED,
    IMPACT_READY, RUN_READY, RUN_FAILED,
}

# ── Legacy -> Current mapping ────────────────────────────────────────────────────
_LEGACY_MAP = {
    "running":  RUNNING,
    "pending":  PENDING,
    "success":  SUCCEEDED,
    "succeed":  SUCCEEDED,
    "ok":       SUCCEEDED,
    "done":     SUCCEEDED,
    "failed":   FAILED,
    "fail":     FAILED,
    "error":    FAILED,
}

_FAILURE = {FAILED, DQ_FAILED, RUN_FAILED}
_TERMINAL = {SUCCEEDED, FAILED, DQ_FAILED, RUN_FAILED, RUN_READY, IMPACT_READY, ODI_SYNCED}

_LABELS_TR = {
    PENDING:           "Pending",
    RUNNING:           "Running",
    SUCCEEDED:         "Succeeded",
    FAILED:            "Error",
    DQ_FAILED:         "DQ Failed",
    ODI_SYNC_REQUIRED: "ODI Sync Required",
    ODI_SYNCED:        "ODI Synced",
    IMPACT_READY:      "Impact Ready",
    RUN_READY:         "Ready to Run",
    RUN_FAILED:        "Run Failed",
}


def normalize(status: str | None) -> str:
    """Normalize a legacy or mixed-case status to the standard vocabulary."""
    if not status:
        return PENDING
    s = str(status).strip()
    if not s:
        return PENDING
    upper = s.upper()
    if upper in ALL:
        return upper
    return _LEGACY_MAP.get(s.lower(), upper)


def is_failure(status: str | None) -> bool:
    return normalize(status) in _FAILURE


def is_terminal(status: str | None) -> bool:
    return normalize(status) in _TERMINAL


def label_tr(status: str | None) -> str:
    return _LABELS_TR.get(normalize(status), normalize(status))
