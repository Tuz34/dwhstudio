"""The graph engine must run the same read-only repository preflight as the classic engine."""

from __future__ import annotations

import mapping_graph
from mapping_generator import MappingGenerator, RepositoryPreflightBlockedError


def _state() -> dict:
    return {"validated_params": {"src_model": "ORACLE_DWH", "src_owner": "DWH", "src_table": "ODS_X"},
            "batch_id": "t1", "completed_steps": [], "log": []}


def test_graph_create_table_runs_preflight_before_ddl(monkeypatch):
    calls: list[str] = []

    def preflight(self, params):
        calls.append("preflight")
        params["_odi_source_identity"] = {"model_code": "TGT_DWH"}
        params["_odi_ikm_id"] = "28"
        return {"status": "passed"}

    def create_table(self, params):
        calls.append("ddl")
        assert params["_odi_ikm_id"] == "28"
        return {"status": "SUCCESS"}

    monkeypatch.setattr(MappingGenerator, "_preflight_odi_repository", preflight)
    monkeypatch.setattr(MappingGenerator, "_create_target_table", create_table)

    out = mapping_graph.node_create_table(_state())

    assert calls == ["preflight", "ddl"]
    assert out["validated_params"]["_odi_source_identity"] == {"model_code": "TGT_DWH"}
    assert out["validated_params"]["_odi_ikm_id"] == "28"


def test_graph_create_table_blocked_preflight_stops_before_ddl(monkeypatch):
    def preflight(self, params):
        raise RepositoryPreflightBlockedError({"code": "source_identity_unverified"})

    def create_table(self, params):  # pragma: no cover - must not run
        raise AssertionError("DDL must not run after a blocked preflight")

    monkeypatch.setattr(MappingGenerator, "_preflight_odi_repository", preflight)
    monkeypatch.setattr(MappingGenerator, "_create_target_table", create_table)

    out = mapping_graph.node_create_table(_state())

    assert out["overall_status"] == "failed"
    assert "source_identity_unverified" in out["error"]
