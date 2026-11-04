from __future__ import annotations

from mapping_state_manager import redact_sensitive_text

def _read_mapping_km_options(mapping_name: str) -> dict:
    """Best-effort read-only ODI repository KM option read-back for UI detail."""
    try:
        from odi_orchestrator import OdiOrchestrator

        result = OdiOrchestrator().inspect_mapping_km_options(
            approved=True,
            mapping_name=(mapping_name or "").strip().upper(),
        )
        km_options = result.get("km_options") or {}
        physical = result.get("physical") or {}
        return {
            "status": result.get("status", "unknown"),
            "read_only": True,
            "source": "odi_repository",
            "repository_connection_attempted": result.get("repository_connection_attempted", False),
            "truncate_target_table": km_options.get("TRUNCATE_TARGET_TABLE"),
            "ikm_name": physical.get("target_ikm"),
            "lkm_name": physical.get("lkm"),
            "target_node": physical.get("target_node"),
        }
    except Exception as exc:
        return {
            "status": "unavailable",
            "read_only": True,
            "source": "odi_repository",
            "repository_connection_attempted": False,
            "error": redact_sensitive_text(str(exc).split("\n")[0]),
        }


# API: Git Repository (Phase 3)

# API: Environment Info

# API: Airflow Integration

# Run

