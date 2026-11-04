import sys
from concurrent.futures import ThreadPoolExecutor, wait
from threading import Event, Lock
from types import SimpleNamespace

from flask import Flask

from status_probe_cache import StatusProbeCache


def test_concurrent_cold_and_expired_cache_is_single_flight():
    now = [0.0]
    calls = []
    lock = Lock()
    entered, release = Event(), Event()

    def probe():
        with lock:
            calls.append(1)
        entered.set()
        assert release.wait(3)
        return True

    cache = StatusProbeCache(clock=lambda: now[0], wait_seconds=0.02)
    try:
        for expected in (1, 2):
            entered.clear()
            release.clear()
            with ThreadPoolExecutor(max_workers=8) as clients:
                pending = [clients.submit(cache.read, {"oracle": probe}) for _ in range(8)]
                assert entered.wait(1)
                results = [future.result(timeout=2) for future in pending]
            assert len(calls) == expected
            assert all(result == ({"oracle": False}, 0) for result in results)
            release.set()
            wait(cache._pending.values(), timeout=1)
            assert cache.read({"oracle": probe}) == ({"oracle": True}, 0)
            now[0] += 31
    finally:
        release.set()
        cache.close()


def test_hung_refresh_does_not_spawn_replacements_after_timeout_or_ttl():
    now = [0.0]
    entered, release = Event(), Event()
    calls = []

    def probe():
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return True

    cache = StatusProbeCache(clock=lambda: now[0], wait_seconds=0.001)
    try:
        for _ in range(20):
            assert cache.read({"oracle": probe}) == ({"oracle": False}, 0)
            now[0] += 61
        assert entered.is_set()
        assert calls == [1]
    finally:
        release.set()
        cache.close()


def test_errors_are_false_and_cached_without_message_disclosure():
    calls = []

    def broken():
        calls.append(1)
        raise RuntimeError("synthetic-private-diagnostic")

    cache = StatusProbeCache()
    try:
        assert cache.read({"oracle": broken}) == ({"oracle": False}, 0)
        assert cache.read({"oracle": broken}) == ({"oracle": False}, 0)
        assert calls == [1]
    finally:
        cache.close()


def test_hung_peer_cannot_keep_completed_success_healthy_past_ttl():
    now = [0.0]
    release = Event()
    calls = []

    def fast():
        calls.append(1)
        return True

    def hung():
        assert release.wait(3)
        return True

    cache = StatusProbeCache(clock=lambda: now[0], wait_seconds=0.001)
    try:
        checkers = {"fast": fast, "hung": hung}
        cache.read(checkers)
        cache._pending["fast"].result(timeout=1)
        assert cache.read(checkers) == ({"fast": True, "hung": False}, 0)
        now[0] = 3600
        assert cache.read(checkers) == ({"fast": False, "hung": False}, 0)
        assert calls == [1]
    finally:
        release.set()
        cache.close()


def test_delayed_collection_does_not_extend_completed_result_lifetime():
    now = [0.0]
    release = Event()
    calls = []

    def probe():
        calls.append(1)
        if len(calls) == 1:
            assert release.wait(3)
            return True
        return False

    cache = StatusProbeCache(clock=lambda: now[0], wait_seconds=0.001)
    try:
        assert cache.read({"oracle": probe}) == ({"oracle": False}, 0)
        now[0] = 3
        pending = list(cache._pending.values())
        release.set()
        wait(pending, timeout=1)
        assert all(future.done() for future in pending)
        now[0] = 3600
        assert cache.read({"oracle": probe}) == ({"oracle": False}, 0)
        assert calls == [1, 1]
    finally:
        release.set()
        cache.close()


def test_request_budget_precedes_probe_and_expires_without_client_key():
    now = [0.0]
    cache = StatusProbeCache(clock=lambda: now[0], requests_per_minute=2, ttl_seconds=1)
    calls = []

    def probe():
        calls.append(1)
        return True

    try:
        assert cache.read({"oracle": probe}) == ({"oracle": True}, 0)
        now[0] = 2
        assert cache.read({"oracle": probe}) == ({"oracle": True}, 0)
        now[0] = 4
        assert cache.read({"oracle": probe}) == (None, 56)
        assert calls == [1, 1]
        now[0] = 60
        assert cache.read({"oracle": probe}) == ({"oracle": True}, 0)
        assert len(calls) == 3
    finally:
        cache.close()


def test_cached_snapshot_cannot_be_mutated_by_caller():
    cache = StatusProbeCache()
    try:
        checks, _ = cache.read({"oracle": lambda: True})
        checks["oracle"] = False
        assert cache.read({"oracle": lambda: False}) == ({"oracle": True}, 0)
    finally:
        cache.close()


def test_http_budget_and_cache_cannot_be_busted_by_query_or_forwarded_ip(monkeypatch):
    from routes import deps, status

    now = [0.0]
    calls = []

    def probe():
        calls.append(1)
        return True

    names = ("oracle", "source", "mssql", "rag", "llm", "local_openai", "claude",
             "docker", "mcp", "monitoring", "dba", "graph_health", "ai_orchestrator")
    module = SimpleNamespace(**{f"_chk_{name}": probe for name in names})
    monkeypatch.setattr(deps, "_app_module", module)
    monkeypatch.setitem(sys.modules, "ai_agent", SimpleNamespace(normalize_provider=lambda _: "hybrid"))
    cache = StatusProbeCache(clock=lambda: now[0], requests_per_minute=2)
    monkeypatch.setattr(status, "_PROBE_CACHE", cache)
    app = Flask(__name__)
    app.register_blueprint(status.bp)
    try:
        with app.test_client() as client:
            first = client.get("/api/status?refresh=1")
            second = client.get("/api/status?refresh=2", headers={"X-Forwarded-For": "192.0.2.1"})
            blocked = client.get("/api/status?refresh=3", headers={"X-Forwarded-For": "192.0.2.2"})
            assert first.status_code == second.status_code == 200
            assert first.get_json() == second.get_json()
            assert set(first.get_json()) == {"ok", "checks", "count", "llm_provider"}
            assert first.get_json()["count"] == "13/13"
            assert len(calls) == 13
            assert blocked.status_code == 429
            assert blocked.headers["Retry-After"] == "60"
            assert blocked.headers["Cache-Control"] == "no-store"
            now[0] = 60
            assert client.get("/api/status").status_code == 200
            assert len(calls) == 26
    finally:
        cache.close()


def test_false_or_nonboolean_probe_results_do_not_report_healthy():
    cache = StatusProbeCache()
    try:
        assert cache.read({"false": lambda: False, "invalid": lambda: "private-text"}) == (
            {"false": False, "invalid": False}, 0,
        )
    finally:
        cache.close()
