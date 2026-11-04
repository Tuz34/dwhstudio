"""Domain status vocabulary for the LangGraph mapping pipeline.

Bu modul, MAPPING_RUN_STATE.OVERALL_STATUS icin standart sozluk ve yardimci
fonksiyonlari saglar. mapping_graph node'lari ve UI bu sabitleri kullanarak
ayni dilde konusur.

Standart sozluk:
  Lifecycle:
    PENDING            — pipeline baslamadi
    RUNNING            — pipeline calisiyor
    SUCCEEDED          — pipeline basariyla bitti
    FAILED             — pipeline bilinmeyen/genel hata ile dustu

  Domain (sirayla):
    DQ_FAILED          — DQ uzlastirma adimi hata verdi
    ODI_SYNC_REQUIRED  — DDL sonrasi ODI metaverisi senkronize edilmeli
    ODI_SYNCED         — ODI Datastore/Mapping senkronize edildi
    IMPACT_READY       — Impact lineage kayitlari hazir
    RUN_READY          — Mapping calistirilmaya hazir (senaryo uretildi)
    RUN_FAILED         — Senaryo calistirma hatali bitti

Geriye donuk uyum:
    Eski eski durum stringleri ("running", "success", "failed", buyuk-kucuk
    karisik) `normalize()` fonksiyonu uzerinden yeni standart sozluge
    haritalanir. Boylece eski state kayitlari okunurken UI/Backend birden
    fazla varyantla ugrasmak zorunda kalmaz.
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

# ── Eski -> Yeni esleme ────────────────────────────────────────────────────
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
    PENDING:           "Bekliyor",
    RUNNING:           "Calisiyor",
    SUCCEEDED:         "Basarili",
    FAILED:            "Hata",
    DQ_FAILED:         "DQ Hatasi",
    ODI_SYNC_REQUIRED: "ODI Senkron Bekliyor",
    ODI_SYNCED:        "ODI Senkronize",
    IMPACT_READY:      "Impact Hazir",
    RUN_READY:         "Calistirilmaya Hazir",
    RUN_FAILED:        "Calistirma Hatasi",
}


def normalize(status: str | None) -> str:
    """Eski/karisik harf duzeniyle gelen durumu standart sozluge cevirir."""
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
