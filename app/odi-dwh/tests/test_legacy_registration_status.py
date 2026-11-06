"""Legacy text must not turn an ODI registration error into a success."""
from types import SimpleNamespace
import sys

import pytest

from mapping_generator import MappingGenerator
from mapping_graph import _repository_step_status
import target_table_service


@pytest.mark.parametrize("error", [
    "Hata: registration failed; zaten mevcut",
    "hata: registration failed; ZATEN MEVCUT",
    "HATA: registration failed; already exists",
])
@pytest.mark.parametrize("side", ["src_result", "tgt_result"])
def test_graph_does_not_hide_errors_behind_existing_token(error, side):
    result = {"src_result": "Datastore already exists", "tgt_result": "Registered"}
    result[side] = error
    assert _repository_step_status(result)[0] == "WARNING"


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize("error", [
    "Hata: registration failed; zaten mevcut",
    "hata: registration failed; ZATEN MEVCUT",
    "HATA: registration failed; already exists",
])
def test_classic_registration_does_not_log_error_as_success(monkeypatch, side, error):
    monkeypatch.setattr(target_table_service, "read_table_schema", lambda *_args: None)
    outputs = ["Datastore already exists", "Registered"]
    outputs[side] = error
    iterator = iter(outputs)
    fake_orchestrator = SimpleNamespace(register_datastore=lambda **kwargs: next(iterator))
    monkeypatch.setitem(sys.modules, "odi_orchestrator", SimpleNamespace(OdiOrchestrator=lambda: fake_orchestrator))
    logs = []
    generator = SimpleNamespace(
        _ensure_odi_repository_bindings=lambda params: None,
        _source_columns_for=lambda params: [],
        _odi_register_column_defs=lambda columns: [],
        _resolved_odi_source_model_code=lambda params: "SOURCE_MODEL",
        _resolved_odi_target_model_code=lambda params: "TARGET_MODEL",
        _get_source_columns_oracle=lambda table: [],
        _source_schema_df_to_legacy_columns=lambda frame, connector: [],
        _add_log=lambda *entry: logs.append(entry),
    )
    result = MappingGenerator._register_odi_datastores(generator, {
        "src_model": "ORACLE_ODS", "src_table": "SOURCE_TABLE",
        "tgt_model": "ORACLE_DWH", "tgt_table": "TARGET_TABLE", "tgt_owner": "DWH",
    })
    assert list(result.values()) == outputs
    successes = [entry for entry in logs if entry[1] == "SUCCESS"]
    assert len(successes) == 1
    assert successes[0][2].startswith("Target:" if side == 0 else "Source:")


@pytest.mark.parametrize("result", [
    {"src_result": "Datastore already exists", "tgt_result": "Registered"},
    {"src_result": {"skipped": True, "reason": "already_exists"}, "tgt_result": "Registered"},
])
def test_non_error_existing_registration_still_passes(result):
    assert _repository_step_status(result)[0] == "SUCCESS"


def test_error_on_one_side_is_not_hidden_by_other_side_legacy_token():
    assert _repository_step_status({
        "src_result": "Hata: registration failed",
        "tgt_result": "Datastore zaten mevcut",
    })[0] == "WARNING"
