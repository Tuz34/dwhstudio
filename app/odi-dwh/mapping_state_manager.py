"""Persistent mapping run state for LangGraph orchestration.

This module stores the latest graph state per batch/mapping so DQ, Impact,
ODI sync and UI flows can read the same durable status instead of relying on
in-memory LangGraph checkpoints.
"""

import json
import logging
import re
from datetime import datetime
from typing import Any

import oracledb

from config import ORACLE_DSN, ORACLE_PASSWORD, ORACLE_USER

logger = logging.getLogger("mapping_state_manager")


_SENSITIVE_PATTERNS = [
    re.compile(r"(?i)\b(password|passwd|pwd|secret|token)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"(?i)\b(dsn|connect_string|connection_string)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"(?i)jdbc:oracle:thin:@[^\s,;]+"),
]

PERSISTED_STATE_FIELDS = frozenset({
    "batch_id",
    "mapping_name",
    "scenario_name",
    "scenario_version",
    "context",
    "truncate_target_table",
    "current_step",
    "overall_status",
    "completed_steps",
    "validated_params",
    "error",
})
_PERSISTED_PARAM_FIELDS = (
    "mapping_name",
    "src_schema",
    "src_table",
    "src_model",
    "tgt_owner",
    "tgt_table",
    "tgt_model",
    "scenario_name",
    "scenario_version",
    "context",
    "truncate_target_table",
    "needs_skey",
)
_ERROR_MARKER_RE = re.compile(r"\b(?:ORA-\d{3,6}|ODI-\d{3,6}|[A-Z][A-Za-z0-9_]*(?:Error|Exception))\b")


def redact_sensitive_text(value: Any, max_len: int = 500) -> str:
    """Return short text safe for UI/Linear-facing run history and alerts."""
    text = str(value or "")
    for pattern in _SENSITIVE_PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(1)}=[REDACTED]" if m.lastindex else "[REDACTED_DSN]", text)
    return text[:max_len]


def _json_default(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _persisted_error(value: Any) -> str | None:
    if value in (None, ""):
        return None
    markers = list(dict.fromkeys(_ERROR_MARKER_RE.findall(str(value))))[:3]
    return "Operation failed." + (f" {', '.join(markers)}" if markers else "")


def _metadata_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return redact_sensitive_text(value, max_len=256)


def _persistable_state(state: dict[str, Any]) -> dict[str, Any]:
    persisted: dict[str, Any] = {}
    for key in PERSISTED_STATE_FIELDS - {"validated_params", "completed_steps", "error"}:
        if key in state:
            persisted[key] = _metadata_scalar(state.get(key))

    completed_steps = state.get("completed_steps")
    if isinstance(completed_steps, (list, tuple)):
        persisted["completed_steps"] = [
            redact_sensitive_text(step, max_len=100) for step in completed_steps[:50]
        ]

    params = state.get("validated_params") or state.get("raw_params") or {}
    if isinstance(params, dict):
        persisted_params = {
            key: _metadata_scalar(params.get(key))
            for key in _PERSISTED_PARAM_FIELDS
            if key in params
        }
        if persisted_params:
            persisted["validated_params"] = persisted_params

    error = _persisted_error(state.get("error"))
    if error:
        persisted["error"] = error
    return persisted


def _state_json(state: dict[str, Any]) -> str:
    return json.dumps(_persistable_state(state), ensure_ascii=False, default=_json_default, sort_keys=True)


def _read_maybe_clob(value: Any) -> Any:
    return value.read() if hasattr(value, "read") else value


def _iso_or_none(value: Any) -> str | None:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if value is None:
        return None
    return str(value)


def _duration_seconds(started: Any, updated: Any) -> int | None:
    if hasattr(started, "__sub__") and hasattr(updated, "__sub__"):
        try:
            delta = updated - started
            return max(0, int(delta.total_seconds()))
        except Exception:
            return None
    return None


def _mapping_name_from_state(state: dict[str, Any]) -> str:
    params = state.get("validated_params") or state.get("raw_params") or {}
    return str(params.get("mapping_name") or state.get("mapping_name") or "").upper()


def _push_failure_to_rag(state: dict[str, Any], status: str, batch_id: str, completed_text: str) -> None:
    """LangGraph hatalarini RAG `error_patterns` collection'ina kaydet (best-effort)."""
    try:
        from rag_manager import add_document
    except Exception:
        return
    mapping_name = _mapping_name_from_state(state) or "(unknown)"
    params = state.get("validated_params") or state.get("raw_params") or {}
    src = str(params.get("src_table") or "").strip()
    tgt = str(params.get("tgt_table") or "").strip()
    current_step = str(state.get("current_step") or "").strip()
    error_msg = _persisted_error(state.get("error")) or ""
    text_lines = [
        f"MAPPING ERROR: {mapping_name}",
        f"STATUS: {status}",
        f"STEP: {current_step or '-'}",
        f"SRC: {src or '-'} -> TGT: {tgt or '-'}",
        f"COMPLETED: {completed_text or '-'}",
        "",
        "ERROR:",
        error_msg or "(empty)",
    ]
    text = "\n".join(text_lines)[:6000]
    metadata = {
        "type": "mapping_error",
        "mapping_name": mapping_name,
        "batch_id": batch_id,
        "current_step": current_step,
        "overall_status": status,
        "src_table": src,
        "tgt_table": tgt,
        "timestamp": datetime.utcnow().isoformat(timespec="seconds"),
    }
    try:
        add_document("error_patterns", text, metadata, doc_id=f"err-{batch_id}")
        logger.info("LangGraph hatasi RAG/error_patterns icine kaydedildi: %s (batch=%s)",
                    mapping_name, batch_id)
    except Exception as exc:
        logger.debug("RAG add_document basarisiz: %s", exc)


def _push_success_to_rag(state: dict[str, Any], status: str, batch_id: str, completed_text: str) -> None:
    """Basarili LangGraph runlarini RAG `mapping_runs` collection'ina ozet kaydet.

    LLM'in "bu mapping nasil uretildi", "ODS_X mapping'i hangi adimlardan gecti" gibi
    sorulari icin kullanilabilir referans seti olusturur.
    """
    try:
        from rag_manager import add_document
    except Exception:
        return
    mapping_name = _mapping_name_from_state(state) or "(unknown)"
    params = state.get("validated_params") or state.get("raw_params") or {}
    src = str(params.get("src_table") or "").strip()
    tgt = str(params.get("tgt_table") or "").strip()
    src_model = str(params.get("src_model") or "").strip()
    tgt_model = str(params.get("tgt_model") or "").strip()
    needs_skey = bool(params.get("needs_skey"))
    snap_id = (state.get("snapshot_result") or {}).get("snap_id")
    target_table = (state.get("target_table_result") or {})
    columns_count = target_table.get("columns_count")
    text_lines = [
        f"MAPPING RUN: {mapping_name}",
        f"STATUS: {status}",
        f"SRC: {src or '-'} ({src_model or '-'})",
        f"TGT: {tgt or '-'} ({tgt_model or '-'})",
        f"BU TABLO ARTIK ODS KATMANINDA MEVCUTTUR. {src} tablosu {tgt} olarak modellenmistir.",
        f"NEEDS_SKEY: {needs_skey}",
        f"COLUMNS: {columns_count if columns_count is not None else '-'}",
        f"SNAPSHOT_ID: {snap_id if snap_id is not None else '-'}",
        f"COMPLETED: {completed_text or '-'}",
    ]
    text = "\n".join(text_lines)[:6000]
    metadata = {
        "type": "mapping_run",
        "mapping_name": mapping_name,
        "batch_id": batch_id,
        "overall_status": status,
        "src_table": src,
        "tgt_table": tgt,
        "src_model": src_model,
        "tgt_model": tgt_model,
        "needs_skey": needs_skey,
        "snapshot_id": snap_id,
        "timestamp": datetime.utcnow().isoformat(timespec="seconds"),
    }
    try:
        add_document("mapping_runs", text, metadata, doc_id=f"run-{batch_id}")
        logger.info("Basarili LangGraph run RAG/mapping_runs icine kaydedildi: %s (batch=%s)",
                    mapping_name, batch_id)
    except Exception as exc:
        logger.debug("RAG mapping_runs add_document basarisiz: %s", exc)


class MappingStateManager:
    """Stores LangGraph mapping run state in AUDIT_USER.MAPPING_RUN_STATE."""

    # Proses genelinde tek kez dogrulansın — her upsert'te DDL atmasin
    _table_ensured: bool = False

    def __init__(self):
        self.conn = None

    def _get_conn(self):
        if self.conn is None or not self.conn.is_healthy():
            self.conn = oracledb.connect(
                user=ORACLE_USER,
                password=ORACLE_PASSWORD,
                dsn=ORACLE_DSN,
            )
        return self.conn

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "MappingStateManager":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False

    def ensure_table(self) -> None:
        """Verify the canonical table exists without attempting runtime DDL."""
        if MappingStateManager._table_ensured:
            return

        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT COUNT(*) FROM ALL_TABLES "
                "WHERE OWNER = 'AUDIT_USER' AND TABLE_NAME = 'MAPPING_RUN_STATE'"
            )
            (exists,) = cursor.fetchone()

            if exists:
                MappingStateManager._table_ensured = True
                return
            raise RuntimeError(
                "Canonical audit schema is not initialized; review ddl/audit_schema_contract.sql."
            )
        finally:
            cursor.close()

    def upsert_state(self, state: dict[str, Any], thread_id: str | None = None) -> None:
        batch_id = str(state.get("batch_id") or "").strip()
        if not batch_id:
            return

        self.ensure_table()
        conn = self._get_conn()
        cursor = conn.cursor()
        mapping_name = _mapping_name_from_state(state)
        completed = state.get("completed_steps") or []
        completed_text = ",".join(str(s) for s in completed)[:2000]
        try:
            from mapping_status import normalize as _normalize_status
            normalized_status = _normalize_status(state.get("overall_status"))
        except Exception:
            normalized_status = state.get("overall_status")
        # state_json icindeki overall_status'u da standart sozluge cevir; UI ve API uyumlu kalsin
        persisted_state = _persistable_state(state)
        persisted_state["overall_status"] = normalized_status
        persisted_error = _persisted_error(state.get("error"))
        payload = {
            "batch_id": batch_id,
            "thread_id": thread_id,
            "mapping_name": mapping_name or None,
            "current_step": state.get("current_step"),
            "overall_status": normalized_status,
            "completed_steps": completed_text,
            "error_message": persisted_error,
            "state_json": _state_json(persisted_state),
        }
        try:
            cursor.execute("""
                MERGE INTO AUDIT_USER.MAPPING_RUN_STATE t
                USING (
                    SELECT :batch_id AS batch_id FROM dual
                ) s
                ON (t.BATCH_ID = s.batch_id)
                WHEN MATCHED THEN UPDATE SET
                    THREAD_ID = :thread_id,
                    MAPPING_NAME = :mapping_name,
                    CURRENT_STEP = :current_step,
                    OVERALL_STATUS = :overall_status,
                    COMPLETED_STEPS = :completed_steps,
                    ERROR_MESSAGE = :error_message,
                    STATE_JSON = :state_json,
                    UPDATED_AT = SYSTIMESTAMP
                WHEN NOT MATCHED THEN INSERT (
                    BATCH_ID, THREAD_ID, MAPPING_NAME, CURRENT_STEP,
                    OVERALL_STATUS, COMPLETED_STEPS, ERROR_MESSAGE, STATE_JSON
                ) VALUES (
                    :batch_id, :thread_id, :mapping_name, :current_step,
                    :overall_status, :completed_steps, :error_message, :state_json
                )
            """, payload)
            conn.commit()
            logger.info("MAPPING_RUN_STATE yazildi: batch=%s mapping=%s status=%s step=%s",
                        batch_id, mapping_name, normalized_status, state.get("current_step"))
        except Exception as exc:
            # Tani icin tam hatayi gomelim — yetki/baglanti/sema sorunlari sessiz kalmasin
            logger.error("MAPPING_RUN_STATE yazilamadi (batch=%s mapping=%s): %s",
                         batch_id, mapping_name, exc, exc_info=True)
        finally:
            cursor.close()

        # RAG'a yaz: hata durumunda error_patterns, basarida mapping_runs (best-effort)
        try:
            from mapping_status import is_failure, is_terminal, SUCCEEDED
            if is_failure(normalized_status):
                _push_failure_to_rag(state, normalized_status, batch_id, completed_text)
            elif is_terminal(normalized_status) and normalized_status == SUCCEEDED:
                _push_success_to_rag(state, normalized_status, batch_id, completed_text)
        except Exception as exc:
            logger.debug("RAG yazma atlandi: %s", exc)

    def get_by_mapping(self, mapping_name: str) -> dict[str, Any] | None:
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT STATE_JSON
                FROM AUDIT_USER.MAPPING_RUN_STATE
                WHERE UPPER(MAPPING_NAME) = UPPER(:mapping_name)
                ORDER BY UPDATED_AT DESC
                FETCH FIRST 1 ROWS ONLY
            """, {"mapping_name": mapping_name})
            row = cursor.fetchone()
            if not row:
                return None
            raw = row[0].read() if hasattr(row[0], "read") else row[0]
            return json.loads(raw)
        finally:
            cursor.close()

    def get_by_target_table(self, table_name: str, limit: int = 50) -> dict[str, Any] | None:
        """En son LangGraph state'leri tarayip validated_params.tgt_table eslesenini doner.

        DQ/Impact akislarinin tablo adindan tahmin yerine kalici state'i kullanabilmesi icin.
        """
        target = (table_name or "").strip().upper()
        if not target:
            return None
        # Tablo adindan ODS_/DWH_ vb. on eki gelmis olabilir; kisa ad ile karsilastirma yapacagiz
        target_short = target.split(".")[-1]
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT STATE_JSON
                FROM AUDIT_USER.MAPPING_RUN_STATE
                ORDER BY UPDATED_AT DESC
                FETCH FIRST :lim ROWS ONLY
            """, {"lim": int(limit)})
            for (state_clob,) in cursor.fetchall():
                try:
                    raw = state_clob.read() if hasattr(state_clob, "read") else state_clob
                    state = json.loads(raw)
                except Exception:
                    continue
                params = state.get("validated_params") or state.get("raw_params") or {}
                tgt = str(params.get("tgt_table") or "").strip().upper()
                if not tgt:
                    continue
                if tgt == target or tgt.split(".")[-1] == target_short:
                    return state
            return None
        finally:
            cursor.close()

    def list_recent_runs(self, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        """Return recent mapping/scenario run states for read-only operations history."""
        try:
            bounded_limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            bounded_limit = 50

        status_filter = str(status or "").strip().upper() or None
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            where = ""
            params: dict[str, Any] = {"lim": bounded_limit}
            if status_filter:
                where = "WHERE UPPER(OVERALL_STATUS) = :status"
                params["status"] = status_filter

            cursor.execute(f"""
                SELECT BATCH_ID, MAPPING_NAME, CURRENT_STEP, OVERALL_STATUS,
                       ERROR_MESSAGE, STATE_JSON, CREATED_AT, UPDATED_AT
                FROM AUDIT_USER.MAPPING_RUN_STATE
                {where}
                ORDER BY UPDATED_AT DESC
                FETCH FIRST :lim ROWS ONLY
            """, params)

            runs: list[dict[str, Any]] = []
            for (
                batch_id,
                mapping_name,
                current_step,
                overall_status,
                error_message,
                state_clob,
                created_at,
                updated_at,
            ) in cursor.fetchall():
                state: dict[str, Any] = {}
                try:
                    raw_state = _read_maybe_clob(state_clob)
                    state = json.loads(raw_state) if raw_state else {}
                except Exception:
                    state = {}

                error_text = state.get("error") or error_message
                runs.append({
                    "batch_id": batch_id,
                    "mapping_name": state.get("mapping_name") or mapping_name,
                    "scenario_name": state.get("scenario_name"),
                    "scenario_version": state.get("scenario_version"),
                    "context": state.get("context"),
                    "overall_status": state.get("overall_status") or overall_status,
                    "current_step": state.get("current_step") or current_step,
                    "truncate_target_table": state.get("truncate_target_table"),
                    "started_at": _iso_or_none(created_at),
                    "updated_at": _iso_or_none(updated_at),
                    "duration_seconds": _duration_seconds(created_at, updated_at),
                    "error": redact_sensitive_text(error_text, max_len=500) if error_text else None,
                })
            return runs
        finally:
            cursor.close()
