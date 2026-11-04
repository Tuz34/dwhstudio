"""Synthetic-only AI boundary tests; never load deployment configuration."""

import importlib
import json
import os
import socket
import subprocess
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor

import pytest


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Live IO forbidden in AI boundary tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    for key in (
        "AI_TRUSTED_ORIGINS",
        "LOCAL_OPENAI_BASE_URL",
        "LOCAL_OPENAI_API_KEY",
        "LOCAL_OPENAI_API_KEY_ORIGIN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "LLM_PROVIDER",
        "LOCAL_OPENAI_MODEL",
        "OLLAMA_CHAT_URL",
        "OLLAMA_TAGS_URL",
        "OLLAMA_EMBEDDINGS_URL",
        "OLLAMA_MODEL",
    ):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def modules(monkeypatch):
    config = types.ModuleType("config")
    for name, value in {
        "ANTHROPIC_API_KEY": "synthetic-key",
        "LLM_PROVIDER": "ollama",
        "LOCAL_OPENAI_API_KEY": "synthetic-key",
        "LOCAL_OPENAI_BASE_URL": "http://localhost:1234/v1",
        "LOCAL_OPENAI_MODEL": "synthetic-model",
        "OLLAMA_CHAT_URL": "http://localhost:11434/api/chat",
        "OLLAMA_TAGS_URL": "http://localhost:11434/api/tags",
        "OLLAMA_EMBEDDINGS_URL": "http://localhost:11434/api/embeddings",
        "OLLAMA_MODEL": "synthetic-model",
    }.items():
        setattr(config, name, value)
    monkeypatch.setitem(sys.modules, "config", config)
    for name in ("ai_agent", "ai_orchestrator"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    agent = importlib.import_module("ai_agent")
    orchestrator = importlib.import_module("ai_orchestrator")
    yield agent, orchestrator
    sys.modules.pop("ai_agent", None)
    sys.modules.pop("ai_orchestrator", None)


@pytest.fixture
def api(modules, monkeypatch, tmp_path):
    from flask import Flask

    import routes.deps as deps

    agent, orchestrator = modules
    app_module = types.SimpleNamespace(
        os=os,
        config=sys.modules["config"],
        __file__=str(tmp_path / "app.py"),
        limiter=types.SimpleNamespace(limit=lambda *a, **k: lambda fn: fn),
        orchestrate=orchestrator.orchestrate,
    )
    monkeypatch.setattr(deps, "_app_module", app_module)
    monkeypatch.delitem(sys.modules, "routes.ai_write", raising=False)
    route = importlib.import_module("routes.ai_write")
    app = Flask("synthetic-boundary")
    app.register_blueprint(route.bp)
    yield app, app_module
    sys.modules.pop("routes.ai_write", None)


def test_unknown_provider_is_rejected(modules):
    agent, _ = modules
    with pytest.raises(ValueError):
        agent.normalize_provider("unknown-provider")


def test_per_call_provider_does_not_mutate_default(modules, monkeypatch):
    agent, _ = modules
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setattr(agent, "is_local_openai_available", lambda: True)
    monkeypatch.setattr(agent, "call_local_openai", lambda messages: "local")
    assert agent.call_llm([], provider="local_openai") == "local"
    assert os.environ["LLM_PROVIDER"] == "ollama"


def test_persistence_excludes_untrusted_values(modules, monkeypatch, tmp_path):
    _, orchestrator = modules
    monkeypatch.setattr(orchestrator, "__file__", str(tmp_path / "orchestrator.py"))
    marker = "SYNTHETIC_PRIVATE_MARKER"
    orchestrator._persist_job(
        {
            "job_id": marker,
            "prompt": marker,
            "log": [marker],
            "final_response": {"status": "planned", "tool_plan": [marker], "error": marker},
            "observability": {"detail": marker, "gpu_before": {"error": marker}},
        }
    )
    text = (tmp_path / "logs" / "ai_orchestrator_jobs.jsonl").read_text()
    assert marker not in text
    assert json.loads(text)["status"] == "planned"


@pytest.mark.parametrize(
    "url",
    [
        "https://untrusted.example/v1",
        "http://8.8.8.8/v1",
        "http://localhost@evil.example/v1",
        "http://user:pass@localhost/v1",
        "http://localhost\\@evil.example/v1",
        "http://localhost%2eevil.example/v1",
        "http://127.1/v1",
        "http://2130706433/v1",
        "http://0x7f000001/v1",
        "http://0177.0.0.1/v1",
        "http://localhost./v1",
        "http://localhost:0/v1",
        "http://localhost:99999/v1",
        "http://localhost:01234/v1",
        "http://localhost/v1?redirect=evil",
        "http://localhost/v1#fragment",
        " http://localhost/v1",
        "http://localhost/\nfoo",
        "http://localhost//evil",
        "http://localhost/a/../b",
        "ftp://localhost/v1",
        "http://localh\u043est/v1",
        "http://[::ffff:127.0.0.1]/v1",
        None,
        12,
    ],
)
def test_endpoint_representations_rejected(url):
    import ai_egress as egress

    with pytest.raises(egress.EgressPolicyError, match="^AI endpoint is not permitted.$"):
        egress.validate_url(url)


@pytest.mark.parametrize("rendering", ["dotted", "hex"])
@pytest.mark.parametrize("trusted", [False, True])
@pytest.mark.parametrize("host", ["::ffff:127.0.0.1", "::ffff:7f00:1", "::ffff:8.8.8.8", "::ffff:808:808"])
def test_mapped_ipv6_rejected_independent_of_rendering(monkeypatch, rendering, trusted, host):
    import ai_egress as egress

    original_str = egress.ipaddress.IPv6Address.__str__

    def render_address(address):
        mapped = address.ipv4_mapped
        if mapped is None:
            return original_str(address)
        if rendering == "dotted":
            return f"::ffff:{mapped}"
        return f"::ffff:{int(mapped) >> 16:x}:{int(mapped) & 0xFFFF:x}"

    # Exercise both Python rendering conventions without replacing address semantics.
    monkeypatch.setattr(egress.ipaddress.IPv6Address, "__str__", render_address)
    origin = f"https://[{host}]"
    if trusted:
        monkeypatch.setenv("AI_TRUSTED_ORIGINS", origin)
    url = origin + "/v1"
    calls = fake_pool(monkeypatch)
    for operation in (egress._parse, egress.validate_url, egress.validate_endpoint, egress.get):
        with pytest.raises(egress.EgressPolicyError, match="^AI endpoint is not permitted.$"):
            operation(url)
    assert calls == []


@pytest.mark.parametrize(
    "url",
    ["http://localhost:1234/v1", "http://127.0.0.1:11434/api/tags", "http://[::1]:1234/v1", "https://localhost/v1"],
)
def test_loopback_defaults_remain_allowed(url):
    from ai_egress import validate_url

    assert validate_url(url) == url


def test_remote_requires_exact_trusted_origin(monkeypatch):
    import ai_egress as egress

    monkeypatch.setenv("AI_TRUSTED_ORIGINS", "https://models.example:8443")
    assert egress.validate_url("https://models.example:8443/v1")
    for url in ("https://models.example/v1", "https://models.example.evil:8443/v1", "http://models.example:8443/v1"):
        with pytest.raises(egress.EgressPolicyError):
            egress.validate_url(url)


@pytest.mark.parametrize(
    "trusted", ["http://models.example", "https://models.example/path", "https://user@models.example", "*"]
)
def test_invalid_admin_trust_fails_closed(monkeypatch, trusted):
    import ai_egress as egress

    monkeypatch.setenv("AI_TRUSTED_ORIGINS", trusted)
    with pytest.raises(egress.EgressPolicyError):
        egress.validate_url("http://localhost:1234/v1")


def fake_pool(monkeypatch, *, status=200, body=None):
    import ai_egress as egress

    calls = []

    class Pool:
        def __init__(self, host, **kwargs):
            calls.append(("pool", host, kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def urlopen(self, method, path, **kwargs):
            calls.append((method, path, kwargs))
            return types.SimpleNamespace(
                status=status,
                data=json.dumps(body or {"data": []}).encode(),
                headers={"content-type": "application/json", "Location": "https://evil.example"},
            )

    monkeypatch.setattr(egress.urllib3, "HTTPConnectionPool", Pool)
    monkeypatch.setattr(egress.urllib3, "HTTPSConnectionPool", Pool)
    return calls


def test_transport_pins_dns_and_preserves_tls_identity(monkeypatch):
    import ai_egress as egress

    monkeypatch.setenv("AI_TRUSTED_ORIGINS", "https://models.example")
    monkeypatch.setenv("HTTPS_PROXY", "http://evil.example:8080")
    resolutions = []

    def resolve(*args, **kwargs):
        resolutions.append(args)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    calls = fake_pool(monkeypatch)
    response = egress.post(
        "https://models.example/v1/chat/completions",
        headers={"Authorization": "Bearer SYNTHETIC", "Host": "evil.example"},
        json={"x": 1},
    )
    assert response.json() == {"data": []}
    assert len(resolutions) == 1
    assert calls[0][1] == "8.8.8.8"
    assert calls[0][2]["server_hostname"] == calls[0][2]["assert_hostname"] == "models.example"
    assert calls[0][2]["cert_reqs"] == "CERT_REQUIRED"
    assert calls[1][2]["headers"]["Host"] == "models.example"
    assert calls[1][2]["redirect"] is False
    assert calls[1][2]["retries"] is False


@pytest.mark.parametrize(
    "addresses", [[], ["127.0.0.1"], ["10.0.0.1"], ["169.254.169.254"], ["8.8.8.8", "::1"], ["224.0.0.1"]]
)
def test_untrusted_dns_addresses_never_receive_credentials(monkeypatch, addresses):
    import ai_egress as egress

    monkeypatch.setenv("AI_TRUSTED_ORIGINS", "https://models.example")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in addresses],
    )
    calls = fake_pool(monkeypatch)
    with pytest.raises(egress.EgressPolicyError):
        egress.get("https://models.example/v1/models", headers={"Authorization": "SYNTHETIC"})
    assert calls == []


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirects_never_follow(monkeypatch, status):
    import ai_egress as egress

    calls = fake_pool(monkeypatch, status=status)
    with pytest.raises(egress.EgressPolicyError):
        egress.get("http://localhost:1234/v1/models", headers={"Authorization": "SYNTHETIC"})
    assert len(calls) == 2


@pytest.mark.parametrize(
    "body",
    [
        {"provider": "claude", "base_url": "https://evil.example/v1", "api_key": "SYNTHETIC"},
        {"provider": "unknown", "api_key": "SYNTHETIC"},
        {"provider": "local_openai", "model": {}},
        {"provider": "local_openai", "AI_TRUSTED_ORIGINS": "https://evil.example"},
        {"provider": "claude", "model": "SYNTHETIC\x00MODEL"},
        {"provider": "claude", "api_key": "SYNTHETIC\r\nINJECTION"},
        {"provider": "claude", "model": "\ud800"},
    ],
)
def test_config_rejection_is_atomic(api, monkeypatch, body):
    app, _ = api
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LOCAL_OPENAI_API_KEY", "OLD_SYNTHETIC")
    keys = ("LLM_PROVIDER", "LOCAL_OPENAI_API_KEY", "LOCAL_OPENAI_BASE_URL", "LOCAL_OPENAI_MODEL")
    before = {key: os.getenv(key) for key in keys}
    with app.test_client() as client:
        response = client.post("/api/config/llm", json=body)
    assert response.status_code == 400
    assert {key: os.getenv(key) for key in keys} == before
    assert b"SYNTHETIC" not in response.data


def test_explicit_global_config_operation_preserved(api):
    app, _ = api
    with app.test_client() as client:
        response = client.post("/api/config/llm", json={"provider": "lmstudio", "base_url": "http://localhost:1234/v1"})
    assert response.status_code == 200
    assert os.environ["LLM_PROVIDER"] == "local_openai"


def test_concurrent_summary_does_not_change_ask_dispatch(api, modules, monkeypatch):
    app, app_module = api
    agent, _ = modules
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    entered, release = threading.Event(), threading.Event()

    def summarize(messages):
        entered.set()
        assert release.wait(5)
        return "synthetic summary"

    monkeypatch.setattr(agent, "is_local_openai_available", lambda: True)
    monkeypatch.setattr(agent, "is_ollama_available", lambda: True)
    monkeypatch.setattr(agent, "call_local_openai", summarize)
    monkeypatch.setattr(
        agent, "call_ollama", lambda messages: '{"description":"default-local","mapping_request":{"synthetic":true}}'
    )
    monkeypatch.setattr(agent, "call_claude", lambda messages: pytest.fail("Cloud leak"))
    app_module.ask_ai = agent.ask_ai
    for name in (
        "_is_modeled_tables_question",
        "_is_llm_status_question",
        "_is_truncate_mapping_intent",
        "_is_remembered_unmapped_flow_mapping_request",
        "_is_unmapped_odi_flow_question",
        "_is_table_data_request",
    ):
        setattr(app_module, name, lambda *a: False)

    def request_summary():
        with app.test_client() as client:
            return client.post(
                "/api/local-analyst/summarize", json={"content": "synthetic", "provider": "local_openai"}
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(request_summary)
        try:
            assert entered.wait(5)
            with app.test_client() as client:
                ask = client.post("/api/ask", json={"message": "hello"})
            assert ask.status_code == 200
            assert ask.json["aciklama"] == "default-local"
            assert os.environ["LLM_PROVIDER"] == "ollama"
        finally:
            release.set()
        assert result.result().status_code == 200


def test_unknown_summary_provider_cannot_dispatch(api, modules, monkeypatch):
    app, _ = api
    agent, _ = modules
    monkeypatch.setattr(agent, "call_llm", lambda *a, **k: pytest.fail("Unexpected dispatch"))
    with app.test_client() as client:
        assert (
            client.post("/api/local-analyst/summarize", json={"content": "synthetic", "provider": "typo"}).status_code
            == 400
        )


def test_graph_failure_is_fixed_and_http_failure(api, modules, monkeypatch, tmp_path):
    app, _ = api
    _, graph = modules
    marker = "SYNTHETIC_PRIVATE_MARKER"

    def fail(*args, **kwargs):
        raise RuntimeError(marker)

    monkeypatch.setattr(graph, "get_orchestrator_graph", lambda: types.SimpleNamespace(invoke=fail))
    monkeypatch.setattr(graph, "_runtime_status", lambda: {})
    monkeypatch.setattr(graph, "_hardware_profile", lambda: {})
    monkeypatch.setattr(graph, "_latest_benchmark_summary", lambda: [])
    monkeypatch.setattr(graph, "_gpu_snapshot", lambda: {"error": marker, "name": marker})
    monkeypatch.setattr(graph, "__file__", str(tmp_path / "graph.py"))
    with app.test_client() as client:
        response = client.post("/api/ai/orchestrate", json={"prompt": marker})
    assert response.status_code == 500
    assert marker not in response.get_data(as_text=True)
    assert response.json["ok"] is False
    assert marker not in (tmp_path / "logs" / "ai_orchestrator_jobs.jsonl").read_text()


def test_node_and_gpu_failures_do_not_retain_stderr(modules, monkeypatch):
    _, graph = modules
    marker = "SYNTHETIC_PRIVATE_MARKER"
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr=marker)
    )
    assert marker not in json.dumps(graph._gpu_snapshot())
    observations = []
    monkeypatch.setattr(graph, "_append_node_observation", lambda *a: observations.append(a) or {})

    def fail(state):
        raise RuntimeError(marker)

    with pytest.raises(RuntimeError):
        graph._observable_node("planner", fail)({})
    assert marker not in str(observations)


def test_health_rejects_untrusted_before_credentials(modules, monkeypatch):
    agent, graph = modules
    monkeypatch.setenv("LOCAL_OPENAI_BASE_URL", "https://evil.example/v1")
    monkeypatch.setenv("LOCAL_OPENAI_API_KEY", "SYNTHETIC_PRIVATE_MARKER")
    calls = fake_pool(monkeypatch)
    assert agent.is_local_openai_available() is False
    assert graph._runtime_status()["local_openai_api"] is False
    # Ollama's separate loopback health request remains allowed.
    assert all(call[1] != "evil.example" for call in calls)


def test_local_cli_rejects_before_model_management(monkeypatch):
    import local_llm_analyst as analyst
    from ai_egress import EgressPolicyError

    monkeypatch.setattr(analyst, "is_model_loaded", lambda *a: pytest.fail("Model management attempted"))
    with pytest.raises(EgressPolicyError):
        analyst.analyze_text(
            "synthetic", base_url="https://evil.example/v1", http_post=lambda *a, **k: pytest.fail("HTTP attempted")
        )


def test_trusted_dns_failure_config_remains_atomic(api, monkeypatch):
    app, _ = api
    monkeypatch.setenv("AI_TRUSTED_ORIGINS", "https://models.example")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
    )
    keys = ("LLM_PROVIDER", "LOCAL_OPENAI_API_KEY", "LOCAL_OPENAI_BASE_URL", "LOCAL_OPENAI_MODEL")
    before = {key: os.getenv(key) for key in keys}
    with app.test_client() as client:
        response = client.post("/api/config/llm", json={"provider": "claude", "base_url": "https://models.example/v1"})
    assert response.status_code == 400
    assert {key: os.getenv(key) for key in keys} == before


def test_endpoint_rejected_before_key_lookup(monkeypatch):
    import ai_egress as egress

    real_getenv = os.getenv

    def getenv(name, *args):
        if name == "LOCAL_OPENAI_API_KEY":
            pytest.fail("Credential read before policy approval")
        if name == "LOCAL_OPENAI_BASE_URL":
            return "https://evil.example/v1"
        return real_getenv(name, *args)

    monkeypatch.setattr(os, "getenv", getenv)
    with pytest.raises(egress.EgressPolicyError):
        egress.local_openai_settings("http://localhost:1234/v1")


def test_real_anthropic_sdk_uses_pinned_transport(modules, monkeypatch):
    agent, _ = modules
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
    )
    calls = fake_pool(
        monkeypatch,
        body={
            "id": "synthetic",
            "type": "message",
            "role": "assistant",
            "model": "synthetic",
            "content": [{"type": "text", "text": "synthetic answer"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 2},
        },
    )
    monkeypatch.setenv("HTTPS_PROXY", "http://evil.example:80")
    assert agent.call_claude([{"role": "user", "content": "synthetic"}]) == "synthetic answer"
    assert calls[0][1] == "8.8.8.8"
    assert calls[1][1] == "/v1/messages"
    assert calls[1][2]["headers"]["Host"] == "api.anthropic.com"
    assert calls[1][2]["headers"]["x-api-key"] == "synthetic-key"


def test_cloud_environment_override_cannot_reuse_key(modules, monkeypatch):
    agent, _ = modules
    from ai_egress import EgressPolicyError

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://evil.example")
    calls = fake_pool(monkeypatch)
    with pytest.raises(EgressPolicyError):
        agent._get_claude_client()
    assert calls == []


@pytest.mark.parametrize("function", ["capture_predictions", "probe_runtime"])
def test_eval_cli_rejects_untrusted_before_fake_http(function):
    import local_model_eval as evaluation

    result = getattr(evaluation, function)(base_url="https://evil.example/v1", model="synthetic", api_key="SYNTHETIC")
    assert result["model_call_attempted"] is False
    if function == "probe_runtime":
        assert result["reachable"] is False and result["error_type"] == "EgressPolicyError"
    else:
        assert result["failed"] == result["total"] and not result["outputs"]
        assert all(item["error_type"] == "EgressPolicyError" for item in result["results"])


def test_benchmark_cli_rejects_untrusted_before_fake_http(modules):
    import local_ai_benchmark as benchmark
    from ai_egress import EgressPolicyError

    with pytest.raises(EgressPolicyError):
        benchmark.call_openai_compatible("https://evil.example/v1", "synthetic", "synthetic", "SYNTHETIC")


def test_embedding_and_health_callers_share_policy(modules, monkeypatch):
    monkeypatch.setenv("OLLAMA_EMBEDDINGS_URL", "https://evil.example/api/embeddings")
    monkeypatch.setenv("LOCAL_OPENAI_BASE_URL", "https://evil.example/v1")
    config = sys.modules["config"]
    config.OLLAMA_TAGS_URL = "https://evil.example/api/tags"
    db = types.ModuleType("db")
    db.test_connection = lambda: pytest.fail("DB forbidden")
    monkeypatch.setitem(sys.modules, "db", db)
    for name in ("rag_manager", "health_helpers"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    rag = importlib.import_module("rag_manager")
    health = importlib.import_module("health_helpers")
    calls = fake_pool(monkeypatch)
    assert rag.get_embedding("synthetic") == []
    assert health._chk_llm() is False
    assert health._chk_local_openai() is False
    assert calls == []


def test_success_plan_persists_only_allowlisted_metadata(modules, monkeypatch, tmp_path):
    _, graph = modules
    marker = "SYNTHETIC_PRIVATE_MARKER"
    monkeypatch.setattr(graph, "__file__", str(tmp_path / "graph.py"))
    graph._persist_job(
        {
            "prompt": marker,
            "job_id": marker,
            "final_response": {
                "status": "planned",
                "mode": "dry_run",
                "task_type": "ods_modeling",
                "tool_plan": [{"name": "oracle.describe_table", "detail": marker}, {"name": marker}],
                "warnings": [marker],
                "test_plan": [marker],
            },
            "observability": {
                "status": "SUCCEEDED",
                "duration_ms": 123,
                "started_at": "2026-11-05T01:02:03",
                "log": marker,
                "node_metrics": [{"node": "planner", "status": "SUCCESS", "duration_ms": 12, "detail": marker}],
                "gpu_after": {"name": marker, "stderr": marker, "memory_used_mib": 42},
            },
        }
    )
    text = (tmp_path / "logs" / "ai_orchestrator_jobs.jsonl").read_text()
    value = json.loads(text)
    assert marker not in text
    assert value["tools"] == ["oracle.describe_table"]
    assert value["observability"]["duration_ms"] == 123
    assert value["observability"]["node_metrics"] == [{"node": "planner", "status": "SUCCESS", "duration_ms": 12}]
    assert value["observability"]["gpu_after"]["memory_used_mib"] == 42


def test_gpu_exception_and_source_exception_are_fixed(modules, monkeypatch):
    _, graph = modules
    marker = "SYNTHETIC_PRIVATE_MARKER"

    def fail(*args, **kwargs):
        raise RuntimeError(marker)

    monkeypatch.setattr(subprocess, "run", fail)
    source = types.ModuleType("source_connector")
    source.get_source_connector = fail
    monkeypatch.setitem(sys.modules, "source_connector", source)
    assert marker not in json.dumps(graph._hardware_profile())
    assert marker not in json.dumps(graph._gpu_snapshot())
    assert marker not in json.dumps(graph._summarize_columns("SYNTHETIC.TABLE"))


def test_single_shot_graph_does_not_serialize_raw_state(modules):
    _, graph = modules
    assert graph.build_orchestrator_graph().checkpointer is None


def test_cloud_config_override_rejection_is_atomic(api, monkeypatch):
    app, _ = api
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://evil.example")
    with app.test_client() as client:
        response = client.post("/api/config/llm", json={"provider": "claude", "anthropic_api_key": "SYNTHETIC"})
    assert response.status_code == 400
    assert os.environ["LLM_PROVIDER"] == "ollama"
    assert "ANTHROPIC_API_KEY" not in os.environ


@pytest.mark.parametrize("key_source", ["environment", "fallback"])
def test_rework_origin_change_cannot_reuse_existing_key(api, modules, monkeypatch, key_source):
    app, _ = api
    agent, _ = modules
    hosted = "https://models.example/v1"
    monkeypatch.setattr(agent, "LOCAL_OPENAI_BASE_URL", hosted)
    monkeypatch.setattr(agent, "LOCAL_OPENAI_API_KEY", "PRIVATE_FALLBACK_KEY")
    monkeypatch.setenv("LOCAL_OPENAI_BASE_URL", hosted)
    monkeypatch.setenv("LLM_PROVIDER", "local_openai")
    if key_source == "environment":
        monkeypatch.setenv("LOCAL_OPENAI_API_KEY", "PRIVATE_ENV_KEY")
    before = {key: os.getenv(key) for key in ("LLM_PROVIDER", "LOCAL_OPENAI_BASE_URL", "LOCAL_OPENAI_API_KEY")}
    calls = fake_pool(monkeypatch)
    with app.test_client() as client:
        response = client.post(
            "/api/config/llm", json={"provider": "local_openai", "base_url": "http://127.0.0.1:8123/v1"}
        )
    assert response.status_code == 400
    assert {key: os.getenv(key) for key in before} == before
    assert calls == []


def test_rework_fallback_key_is_bound_to_its_default_origin(monkeypatch):
    import ai_egress as egress

    monkeypatch.setenv("LOCAL_OPENAI_BASE_URL", "http://127.0.0.1:8123/v1")
    with pytest.raises(egress.EgressPolicyError):
        egress.local_openai_settings("https://models.example/v1", "PRIVATE_FALLBACK_KEY")


class ReworkResponse:
    headers = {"content-type": "application/json"}
    text = "PRIVATE_PROVIDER_BODY"

    def __init__(self, status=500):
        self.status_code = status
        self.reads = 0

    def json(self):
        self.reads += 1
        return {
            "error": {"message": self.text},
            "choices": [{"message": {"content": self.text}}],
            "usage": {"completion_tokens": 7},
        }


@pytest.mark.parametrize("tool", ["benchmark", "analyst"])
def test_rework_error_response_body_is_never_exposed(modules, monkeypatch, tool):
    response = ReworkResponse()
    if tool == "benchmark":
        import local_ai_benchmark as target

        monkeypatch.setattr(target.requests, "post", lambda *a, **k: response)
        result = target.call_openai_compatible("http://localhost:1234/v1", "synthetic", "synthetic")
    else:
        import local_llm_analyst as target

        monkeypatch.setattr(target, "is_model_loaded", lambda model: True)
        result = target.analyze_text("synthetic", http_post=lambda *a, **k: response)
    assert response.text not in json.dumps(result)
    assert response.reads == 0


@pytest.mark.parametrize("tool", ["benchmark", "analyst"])
@pytest.mark.parametrize("status", [200, 500])
def test_rework_cli_diagnostics_do_not_persist_prompt_or_provider_text(
    modules, monkeypatch, tmp_path, capsys, tool, status
):
    response = ReworkResponse(status)
    out = tmp_path / "synthetic.jsonl"
    if tool == "benchmark":
        import local_ai_benchmark as target

        monkeypatch.setattr(target.requests, "post", lambda *a, **k: response)
        monkeypatch.setattr(target, "gpu_snapshot", lambda: {"available": False, "error": "PRIVATE_GPU_BODY"})
        argv = ["benchmark", "--prompt", "PRIVATE_INPUT_PROMPT"]
    else:
        import local_llm_analyst as target

        original = target.analyze_text
        monkeypatch.setattr(target, "is_model_loaded", lambda model: True)
        monkeypatch.setattr(
            target, "analyze_text", lambda *a, **k: original(*a, **k, http_post=lambda *a, **k: response)
        )
        argv = ["analyst", "--text", "PRIVATE_INPUT_PROMPT", "--task", "PRIVATE_TASK_TEXT"]
    monkeypatch.setattr(sys, "argv", argv + ["--base-url", "http://localhost:1234/v1", "--out", str(out)])
    assert target.main() == (0 if status == 200 else 1)
    serialized = out.read_text() + capsys.readouterr().out
    for marker in ("PRIVATE_PROVIDER_BODY", "PRIVATE_INPUT_PROMPT", "PRIVATE_GPU_BODY", "PRIVATE_TASK_TEXT"):
        assert marker not in serialized


@pytest.mark.parametrize("function", ["capture_predictions", "probe_runtime"])
def test_rework_dns_denial_is_structured_before_credentials(monkeypatch, function):
    import local_model_eval as evaluation

    monkeypatch.setenv("AI_TRUSTED_ORIGINS", "https://models.example")

    def bad_dns(*a, **k):
        raise socket.gaierror("PRIVATE_DNS_DETAIL")

    monkeypatch.setattr(socket, "getaddrinfo", bad_dns)

    class NoCredentialUse:
        def __bool__(self):
            pytest.fail("Credentials inspected before endpoint validation")

    kwargs = {"base_url": "https://models.example/v1", "model": "synthetic", "api_key": NoCredentialUse()}
    if function == "capture_predictions":
        kwargs.update(
            prompt_pack={"prompts": [{"id": "one", "task": "nl_to_sql", "prompt": "synthetic"}]},
            http_post=lambda *a, **k: pytest.fail("HTTP attempted"),
        )
    else:
        kwargs["http_get"] = lambda *a, **k: pytest.fail("HTTP attempted")
    result = getattr(evaluation, function)(**kwargs)
    assert result["model_call_attempted"] is False
    assert "PRIVATE_DNS_DETAIL" not in json.dumps(result)
    if function == "capture_predictions":
        assert result["failed"] == 1 and result["outputs"] == {}
        assert result["results"][0]["error_type"] == "EgressPolicyError"
    else:
        assert result["reachable"] is False and result["error_type"] == "EgressPolicyError"


@pytest.mark.parametrize("tool", ["benchmark", "analyst"])
def test_rework_cli_url_override_cannot_reuse_default_key(modules, monkeypatch, tmp_path, tool):
    from ai_egress import EgressPolicyError

    monkeypatch.setenv("LOCAL_OPENAI_BASE_URL", "https://models.example/v1")
    monkeypatch.setenv("LOCAL_OPENAI_API_KEY", "PRIVATE_CLI_KEY")
    response = ReworkResponse(200)
    if tool == "benchmark":
        import local_ai_benchmark as target

        monkeypatch.setattr(target, "LOCAL_OPENAI_BASE_URL", "https://models.example/v1")
        monkeypatch.setattr(target.requests, "post", lambda *a, **k: response)
        monkeypatch.setattr(target, "gpu_snapshot", lambda: {})
        argv = ["benchmark", "--prompt", "synthetic"]
    else:
        import local_llm_analyst as target

        original = target.analyze_text
        monkeypatch.setattr(target, "DEFAULT_BASE_URL", "https://models.example/v1")
        monkeypatch.setattr(target, "is_model_loaded", lambda model: True)
        monkeypatch.setattr(
            target, "analyze_text", lambda *a, **k: original(*a, **k, http_post=lambda *a, **k: response)
        )
        argv = ["analyst", "--text", "synthetic"]
    monkeypatch.setattr(
        sys, "argv", argv + ["--base-url", "http://127.0.0.1:8123/v1", "--out", str(tmp_path / "out.jsonl")]
    )
    with pytest.raises(EgressPolicyError):
        target.main()


@pytest.mark.parametrize("key_field", ["local_api_key", "api_key"])
@pytest.mark.parametrize("replacement", ["NEW_SYNTHETIC_KEY", ""])
@pytest.mark.parametrize("key_source", ["environment", "fallback"])
def test_rework_explicit_rebind_or_clear_never_sends_old_key(
    api, modules, monkeypatch, key_field, replacement, key_source
):
    app, _ = api
    agent, _ = modules
    monkeypatch.setattr(agent, "LOCAL_OPENAI_BASE_URL", "https://models.example/v1")
    monkeypatch.setattr(agent, "LOCAL_OPENAI_API_KEY", "OLD_PRIVATE_FALLBACK")
    monkeypatch.setenv("LOCAL_OPENAI_BASE_URL", "https://models.example/v1")
    if key_source == "environment":
        monkeypatch.setenv("LOCAL_OPENAI_API_KEY", "OLD_PRIVATE_ENV")
    calls = fake_pool(monkeypatch)
    with app.test_client() as client:
        response = client.post(
            "/api/config/llm",
            json={"provider": "local_openai", "base_url": "http://127.0.0.1:8123/v1", key_field: replacement},
        )
    assert response.status_code == 200
    assert os.environ["LOCAL_OPENAI_API_KEY"] == replacement
    assert agent.is_local_openai_available() is True
    headers = calls[1][2]["headers"]
    assert headers.get("Authorization") == (f"Bearer {replacement}" if replacement else None)
    assert "OLD_PRIVATE" not in str(calls)


@pytest.mark.parametrize("env_key", [None, ""])
def test_rework_no_credential_origin_change_remains_allowed(api, modules, monkeypatch, env_key):
    app, _ = api
    agent, _ = modules
    monkeypatch.setattr(agent, "LOCAL_OPENAI_API_KEY", "")
    if env_key is not None:
        monkeypatch.setenv("LOCAL_OPENAI_API_KEY", env_key)
    calls = fake_pool(monkeypatch)
    with app.test_client() as client:
        assert (
            client.post(
                "/api/config/llm", json={"provider": "local_openai", "base_url": "http://127.0.0.1:8123/v1"}
            ).status_code
            == 200
        )
    assert agent.is_local_openai_available() is True
    assert "Authorization" not in calls[1][2]["headers"]


def test_rework_same_origin_path_change_preserves_key(api, modules, monkeypatch):
    app, _ = api
    agent, _ = modules
    monkeypatch.setenv("LOCAL_OPENAI_API_KEY", "SAME_ORIGIN_KEY")
    calls = fake_pool(monkeypatch)
    with app.test_client() as client:
        assert (
            client.post(
                "/api/config/llm", json={"provider": "local_openai", "base_url": "http://localhost:1234/v2"}
            ).status_code
            == 200
        )
    assert agent.is_local_openai_available() is True
    assert calls[1][2]["headers"]["Authorization"] == "Bearer SAME_ORIGIN_KEY"


def test_rework_empty_environment_key_suppresses_fallback_on_changed_origin(monkeypatch):
    import ai_egress as egress

    monkeypatch.setenv("LOCAL_OPENAI_API_KEY", "")
    monkeypatch.setenv("LOCAL_OPENAI_BASE_URL", "http://127.0.0.1:8123/v1")
    _, headers = egress.local_openai_settings("https://models.example/v1", "PRIVATE_FALLBACK")
    assert headers == {}


@pytest.mark.parametrize("tool", ["benchmark", "analyst"])
def test_rework_success_analysis_is_still_returned_transiently(modules, monkeypatch, tool):
    response = ReworkResponse(200)
    if tool == "benchmark":
        import local_ai_benchmark as target

        monkeypatch.setattr(target.requests, "post", lambda *a, **k: response)
        result = target.call_openai_compatible("http://localhost:1234/v1", "synthetic", "synthetic")
        assert result["text_preview"] == response.text
    else:
        import local_llm_analyst as target

        monkeypatch.setattr(target, "is_model_loaded", lambda model: True)
        result = target.analyze_text("synthetic", http_post=lambda *a, **k: response)
        assert result["analysis"] == response.text
    assert result["status"] == "ready" and result["completion_tokens"] == 7


@pytest.mark.parametrize("tool", ["benchmark", "analyst"])
def test_rework_malformed_success_body_is_not_returned(modules, monkeypatch, tool):
    response = ReworkResponse(200)
    monkeypatch.setattr(response, "json", lambda: {"error": "PRIVATE_MALFORMED_BODY"})
    if tool == "benchmark":
        import local_ai_benchmark as target

        monkeypatch.setattr(target.requests, "post", lambda *a, **k: response)
        result = target.call_openai_compatible("http://localhost:1234/v1", "synthetic", "synthetic")
    else:
        import local_llm_analyst as target

        monkeypatch.setattr(target, "is_model_loaded", lambda model: True)
        result = target.analyze_text("synthetic", http_post=lambda *a, **k: response)
    assert result["status"] == "invalid_response"
    assert "PRIVATE_MALFORMED_BODY" not in json.dumps(result)


@pytest.mark.parametrize("function", ["capture_predictions", "probe_runtime"])
def test_rework_eval_implicit_cli_key_cannot_follow_origin_override(monkeypatch, function):
    import local_model_eval as evaluation

    monkeypatch.setenv("LOCAL_OPENAI_BASE_URL", "https://models.example/v1")
    monkeypatch.setenv("LOCAL_OPENAI_API_KEY", "PRIVATE_OLD_KEY")
    result = getattr(evaluation, function)(base_url="http://127.0.0.1:8123/v1", model="synthetic", api_key=None)
    assert result["model_call_attempted"] is False
    if function == "capture_predictions":
        assert result["failed"] == result["total"] and not result["outputs"]
    else:
        assert result["reachable"] is False


@pytest.mark.parametrize("function", ["capture_predictions", "probe_runtime"])
def test_rework_explicit_eval_output_contract_remains_intact(monkeypatch, tmp_path, function):
    import local_model_eval as evaluation

    response = ReworkResponse(200)
    monkeypatch.setattr(
        response,
        "json",
        lambda: {
            "data": [{"id": "synthetic"}],
            "choices": [{"message": {"content": "SELECT COUNT(*) FROM HR.EMPLOYEES"}}],
        },
    )
    calls = []

    def fake_http(*a, **k):
        calls.append(k)
        return response

    kwargs = {"base_url": "http://localhost:1234/v1", "model": "synthetic", "api_key": "EXPLICIT_KEY"}
    if function == "capture_predictions":
        path = tmp_path / "approved_capture.json"
        evaluation.export_predictions(
            path,
            **kwargs,
            http_post=fake_http,
            prompt_pack={"prompts": [{"id": "one", "task": "nl_to_sql", "prompt": "synthetic"}]},
        )
        result = json.loads(path.read_text())
        assert result["outputs"] == {"one": "SELECT COUNT(*) FROM HR.EMPLOYEES"}
    else:
        result = evaluation.probe_runtime(**kwargs, http_get=fake_http)
        assert result["reachable"] is True and result["model_available"] is True
    assert calls[0]["headers"]["Authorization"] == "Bearer EXPLICIT_KEY"
