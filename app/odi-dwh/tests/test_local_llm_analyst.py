import json

import local_llm_analyst as analyst


class FakeResponse:
    status_code = 200
    headers = {"content-type": "application/json"}

    def json(self):
        return {
            "choices": [{"message": {"content": "Short summary\n- No risk\n- Next step: test"}}],
            "usage": {"completion_tokens": 12},
        }


def test_analyze_blocks_secret_like_input(monkeypatch):
    calls = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeResponse()

    result = analyst.analyze_text("ORACLE_PASSWORD=secret", http_post=fake_post)

    assert result["status"] == "blocked_input"
    assert result["secret_printed"] is False
    assert calls == []


def test_analyze_loads_and_unloads_only_when_helper_loaded(monkeypatch):
    loaded = {"value": False}
    actions = []

    def fake_is_model_loaded(model):
        return loaded["value"]

    def fake_load(model):
        actions.append(("load", model))
        loaded["value"] = True

    def fake_unload(model):
        actions.append(("unload", model))
        loaded["value"] = False

    monkeypatch.setattr(analyst, "is_model_loaded", fake_is_model_loaded)
    monkeypatch.setattr(analyst, "load_model", fake_load)
    monkeypatch.setattr(analyst, "unload_model", fake_unload)

    result = analyst.analyze_text("ODI-12 clean handoff", http_post=lambda *a, **k: FakeResponse())

    assert result["status"] == "ready"
    assert result["loaded_before"] is False
    assert result["loaded_by_helper"] is True
    assert actions == [("load", analyst.DEFAULT_MODEL), ("unload", analyst.DEFAULT_MODEL)]


def test_analyze_does_not_unload_preloaded_model(monkeypatch):
    actions = []

    monkeypatch.setattr(analyst, "is_model_loaded", lambda model: True)
    monkeypatch.setattr(analyst, "load_model", lambda model: actions.append(("load", model)))
    monkeypatch.setattr(analyst, "unload_model", lambda model: actions.append(("unload", model)))

    result = analyst.analyze_text("ODI-10 review queue", http_post=lambda *a, **k: FakeResponse())

    assert result["status"] == "ready"
    assert result["loaded_before"] is True
    assert result["loaded_by_helper"] is False
    assert actions == []


def test_loaded_models_parses_lms_json(monkeypatch):
    class Proc:
        returncode = 0
        stdout = json.dumps([{"identifier": "qwen/qwen2.5-coder-14b"}])
        stderr = ""

    monkeypatch.setattr(analyst, "_run_lms", lambda *args, **kwargs: Proc())

    assert analyst.is_model_loaded("qwen/qwen2.5-coder-14b") is True
