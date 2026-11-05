"""Complement the row model with pinned SQL and unexpected-failure boundaries."""

import json
from pathlib import Path

import oracledb
import pytest

import dim_product_scd2_stock_migration as migration
from dim_product_scd2_stock_contract import stock_invalid_effective_sql
from tests.test_dim_product_scd2_stock_migration_snapshot import (
    OPEN_2400,
    Conn,
    Cursor,
    Factory,
    World,
    clone_rows,
    make_row,
    plain_rows,
    run_executor,
    stock_with_legacy_backup,
    work,
)


class UnexpectedAdapterError(Exception):
    pass


def test_executable_sql_matches_reviewed_contract():
    # Deliberately frozen: a SQL edit requires review, never automatic refresh.
    expected = json.loads(
        (Path(__file__).parent / "fixtures" / "odi87_stock_sql_contract.json").read_text(encoding="utf-8")
    )
    actual = {
        "read_committed": migration._READ_COMMITTED_SQL,
        "lock_target": migration.LOCK_TARGET_SQL,
        "lock_backup": migration.LOCK_BACKUP_SQL,
        "guards": migration._GUARD_SQL,
        "counts": migration._COUNT_SQL,
        "target_metadata": migration._TARGET_METADATA_SQL,
        "backup_metadata": migration._BACKUP_METADATA_SQL,
        "create_backup": migration.CREATE_MIGRATION_BACKUP_SQL,
        "migrate": migration.MIGRATE_UPDATE_SQL,
        "restore": migration.RESTORE_UPDATE_SQL,
        "stock_invalid_effective": stock_invalid_effective_sql(),
    }
    assert actual == expected


def _world(mode):
    if mode == "restore":
        return World(*stock_with_legacy_backup())
    rows = plain_rows()
    return World(rows, clone_rows(rows))


@pytest.mark.parametrize("mode", ["migrate", "restore"])
@pytest.mark.parametrize("error", [oracledb.DatabaseError, OSError, UnexpectedAdapterError])
def test_failed_rollback_requires_manual_review(mode, error, monkeypatch):
    world = _world(mode)
    original = Conn.rollback

    def failing_rollback(conn):
        if conn.role == "work" and conn.rollback_calls:
            conn.rollback_calls += 1
            raise error("SYNTHETIC_ROLLBACK_DETAIL")
        return original(conn)

    monkeypatch.setattr(Conn, "rollback", failing_rollback)
    world.once(
        "after",
        lambda sql, conn: f"odi87:{mode}_update" in sql and conn.role == "work",
        lambda _store, conn: conn.dirty_target[0].__setitem__("PROD_NAME", "changed"),
    )
    report, _, _ = run_executor(world, mode)
    assert report["status"] == "failed"
    assert report["rolled_back"] is False
    assert report["manual_recovery_review_required"] is True
    assert work(world).rollback_calls == 2
    assert work(world).commits == 0
    assert "SYNTHETIC_ROLLBACK_DETAIL" not in str(report)


@pytest.mark.parametrize("mode", ["migrate", "restore"])
@pytest.mark.parametrize("error", [OSError, RuntimeError, UnexpectedAdapterError])
@pytest.mark.parametrize("applied", [False, True])
def test_unexpected_commit_ack_loss_keeps_unknown_outcome(mode, error, applied, monkeypatch):
    world = _world(mode)
    original = Conn.commit

    def applied_then_lost(conn):
        if applied:
            original(conn)
        if conn.role == "work":
            raise error("SYNTHETIC_COMMIT_DETAIL")

    monkeypatch.setattr(Conn, "commit", applied_then_lost)
    report, _, _ = run_executor(world, mode)
    assert report["status"] == "outcome_unknown"
    assert report["committed"] is None and report["rolled_back"] is None
    assert report["manual_recovery_review_required"] is True
    assert report["post_commit_verification"] == ("proven_post_state" if applied else "unavailable")
    assert work(world).commits == int(applied)
    assert "SYNTHETIC_COMMIT_DETAIL" not in str(report)


@pytest.mark.parametrize("mode", ["migrate", "restore"])
@pytest.mark.parametrize("error", [OSError, RuntimeError, UnexpectedAdapterError])
@pytest.mark.parametrize("commit_mode", ["ok", "apply_then_raise"])
def test_fresh_factory_failure_cannot_erase_write_outcome(mode, error, commit_mode, monkeypatch):
    world = _world(mode)
    world.commit_mode = commit_mode
    original = Factory.__call__

    def proof_unavailable(factory):
        if factory.calls:
            factory.calls += 1
            raise error("SYNTHETIC_PROOF_DETAIL")
        return original(factory)

    monkeypatch.setattr(Factory, "__call__", proof_unavailable)
    report, _, _ = run_executor(world, mode)
    assert report["status"] == ("committed_unverified" if commit_mode == "ok" else "outcome_unknown")
    assert report["committed"] is (True if commit_mode == "ok" else None)
    assert report["post_commit_verification"] == "unavailable"
    assert report["manual_recovery_review_required"] is True
    assert work(world).commits == 1
    assert "SYNTHETIC_PROOF_DETAIL" not in str(report)


@pytest.mark.parametrize("mode", ["migrate", "restore"])
@pytest.mark.parametrize("error", [OSError, RuntimeError, UnexpectedAdapterError])
def test_unexpected_open_failure_has_redacted_report(mode, error, monkeypatch):
    world = _world(mode)

    def unavailable(factory):
        factory.calls += 1
        raise error("SYNTHETIC_OPEN_DETAIL")

    monkeypatch.setattr(Factory, "__call__", unavailable)
    report, factory, _ = run_executor(world, mode)
    assert report["status"] == "failed" and report["connection_attempted"] is True
    assert report["error_type"] == error.__name__
    assert factory.calls == 1 and not world.conns
    assert "SYNTHETIC_OPEN_DETAIL" not in str(report)


@pytest.mark.parametrize("error", [OSError, RuntimeError, UnexpectedAdapterError])
def test_ctas_ack_loss_and_unexpected_proof_failure_keep_backup_unknown(error, monkeypatch):
    world = World(plain_rows())
    world.ctas_mode = "ack_lost"
    original = Factory.__call__

    def proof_unavailable(factory):
        if factory.calls:
            factory.calls += 1
            raise error("SYNTHETIC_CTAS_PROOF_DETAIL")
        return original(factory)

    monkeypatch.setattr(Factory, "__call__", proof_unavailable)
    report, _, _ = run_executor(world, "migrate")
    assert report["status"] == "failed"
    assert report["backup_create_attempted"] is True and report["backup_may_exist"] is True
    assert report["backup_verified"] is False and report["migration_backup_retained"] is False
    assert report["manual_recovery_review_required"] is True
    assert world.backup is not None
    assert "SYNTHETIC_CTAS_PROOF_DETAIL" not in str(report)


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_process_control_exceptions_are_not_swallowed(error):
    with pytest.raises(error):
        with migration._capture_failure():
            raise error()


def test_autocommit_property_failure_blocks_before_statements():
    class BrokenAdapter:
        @property
        def autocommit(self):
            raise UnexpectedAdapterError("SYNTHETIC_AUTOCOMMIT_DETAIL")

    assert migration._autocommit_refused(BrokenAdapter()) is True


@pytest.mark.parametrize("mode", ["migrate", "restore"])
@pytest.mark.parametrize("path", ["guard", "stale", "noop", "lock"])
def test_every_locked_refusal_requires_review_when_rollback_fails(mode, path, monkeypatch):
    world = _world(mode)
    if path == "noop":
        world = _world("restore" if mode == "migrate" else "migrate")
    if path == "lock":
        world.fail_next_lock = "target"
    original = Conn.rollback

    def fail_later(conn):
        if conn.role == "work" and conn.rollback_calls:
            conn.rollback_calls += 1
            raise OSError("SYNTHETIC_LOCKED_ROLLBACK")
        return original(conn)

    monkeypatch.setattr(Conn, "rollback", fail_later)
    if path in {"guard", "stale"}:
        def alter(store, _conn):
            if path == "guard":
                store.guards["enabled_target_trigger_count"] = 1
            else:
                store.side_insert(make_row(
                    migration.EXPECTED_ROW_COUNT + 1,
                    **({"flag": "1", "end": OPEN_2400} if mode == "restore" else {}),
                ))
        world.once("before", lambda sql, conn: sql.startswith("SET TRANSACTION")
                   and conn.role == "work" and conn.set_txn_count == 2, alter)
    report, _, _ = run_executor(world, mode)
    expected = {"guard": "blocked_enabled_triggers", "stale": "blocked_stale_state",
                "noop": "rerun_noop", "lock": "blocked_lock_contention"}
    assert report["status"] == expected[path]
    assert report["rolled_back"] is False
    assert report["manual_recovery_review_required"] is True
    assert work(world).commits == 0


@pytest.mark.parametrize("marker", ["lock_target", "lock_backup"])
@pytest.mark.parametrize("error", [OSError, RuntimeError, TypeError])
@pytest.mark.parametrize("proof_available", [True, False])
def test_post_ctas_error_never_reports_a_created_backup_absent(marker, error, proof_available, monkeypatch):
    world = World(plain_rows())
    original = Factory.__call__

    def factory(factory):
        if factory.calls and not proof_available:
            factory.calls += 1
            raise OSError("SYNTHETIC_FRESH_PROOF_UNAVAILABLE")
        return original(factory)

    monkeypatch.setattr(Factory, "__call__", factory)

    def fail(_store, _conn):
        raise error("SYNTHETIC_POST_CTAS_LOCK")

    world.once("before", lambda sql, conn: f"odi87:{marker}" in sql and conn.role == "work", fail)
    report, _, _ = run_executor(world, "migrate")
    assert report["status"] == "failed" and world.backup is not None
    assert report["backup_create_attempted"] is True
    assert report["backup_may_exist"] is True
    assert report["backup_verified"] is proof_available
    assert report["manual_recovery_review_required"] is True
    assert work(world).commits == 0


def test_reread_exception_reports_successful_rollback():
    world = _world("migrate")

    def fail(_store, _conn):
        raise RuntimeError("SYNTHETIC_REREAD")

    world.once("before", lambda sql, conn: "odi87:check_constraint_count" in sql
               and conn.role == "work" and conn.set_txn_count == 2, fail)
    report, _, _ = run_executor(world, "migrate")
    assert report["status"] == "failed" and report["rolled_back"] is True
    assert work(world).rollback_calls == 2


@pytest.mark.parametrize("resource", [Conn, Cursor])
def test_unexpected_cleanup_error_cannot_replace_committed_outcome(resource, monkeypatch):
    original = resource.close

    def close_then_raise(obj):
        original(obj)
        conn = obj.conn if resource is Cursor else obj
        if conn.role == "work":
            raise OSError("SYNTHETIC_CLOSE_DETAIL")

    monkeypatch.setattr(resource, "close", close_then_raise)
    report, _, _ = run_executor(_world("migrate"), "migrate")
    assert report["committed"] is True
    assert "SYNTHETIC_CLOSE_DETAIL" not in str(report)
