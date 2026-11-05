"""Execute aggregate stock-DQ SQL against synthetic SQLite rows, never Oracle."""

import re
import sqlite3

import pytest

import scd2_postflight as postflight


class Connection:
    def __init__(self, rows=(), *, autocommit=False):
        self.autocommit = autocommit
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE DIM_TEST (NK INTEGER, CURRENT_FLAG TEXT, EFFECTIVE_FROM TEXT, EFFECTIVE_TO TEXT)")
        self.db.executemany("INSERT INTO DIM_TEST VALUES (?,?,?,?)", rows)
        self.db.commit()
        self.db.execute("PRAGMA query_only=ON")
        self.statements = []
        self.rollbacks = self.closes = 0
        self.cursor_closed = False
        self.fail_read = self.fail_rollback = self.fail_close = False
        self.interrupt = False
        self.bad_count = None
        self.override_count = False

    def cursor(self):
        return Cursor(self)

    def rollback(self):
        self.rollbacks += 1
        if self.fail_rollback:
            raise RuntimeError("SYNTHETIC_PRIVATE_ROLLBACK")
        self.db.rollback()

    def close(self):
        self.closes += 1
        self.db.close()
        if self.fail_close:
            raise OSError("SYNTHETIC_PRIVATE_CLOSE")


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def execute(self, sql):
        connection = self.connection
        connection.statements.append(sql)
        if sql == "SET TRANSACTION READ ONLY":
            return
        assert sql.startswith("SELECT ")
        assert re.search(r"COUNT\(\*\)|MAX\(", sql)
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE)\b", sql)
        if connection.fail_read:
            raise RuntimeError("SYNTHETIC_PRIVATE_READ")
        if connection.interrupt:
            raise KeyboardInterrupt
        sql = sql.replace('"DWH".', '').replace("DATE '2400-01-01'", "'2400-01-01'")
        sql = sql.replace('TO_CHAR(MAX("EFFECTIVE_FROM"), \'YYYY-MM-DD\')', 'MAX("EFFECTIVE_FROM")')
        self.rows = connection.db.execute(sql).fetchall()
        if connection.override_count and "COUNT(*)" in sql:
            self.rows = [(connection.bad_count,)]

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def close(self):
        self.connection.cursor_closed = True


def collect(connection):
    return postflight.collect_stock_postflight("DWH", "DIM_TEST", ["NK"], lambda: connection)


def test_stock_sql_and_transaction_execute_real_synthetic_counts():
    connection = Connection([(1, "0", "2026-01-01", "2026-06-01"), (1, "1", "2026-06-01", "2400-01-01")])
    result = collect(connection)
    assert result["status"] == "ok"
    assert result["contract_profile"] == "stock_1_0_2400"
    assert result["counts"]["target_total"] == 2
    assert result["counts"]["target_current"] == 1
    assert result["counts"]["rows_with_effective_to_set"] == 1
    assert result["stock_contract_valid"] is True
    assert connection.statements[0] == "SET TRANSACTION READ ONLY"
    assert connection.rollbacks == connection.closes == 1 and connection.cursor_closed
    assert result["raw_rows_included"] is False and result["pk_values_included"] is False


@pytest.mark.parametrize("rows,field", [
    ([(1, "Y", "2026-01-01", "9999-12-31")], "invalid_current_flag_rows"),
    ([(1, None, "2026-01-01", "2400-01-01")], "invalid_current_flag_rows"),
    ([(1, "1", "2026-01-01", "9999-12-31")], "invalid_open_end_rows"),
    ([(1, "1", "2026-01-01", None)], "invalid_open_end_rows"),
    ([(1, "0", "2026-01-01", "2400-01-01")], "invalid_closed_end_rows"),
    ([(1, "1", "2026-06-01", "2026-01-01")], "invalid_date_order_rows"),
    ([(1, "1", None, "2400-01-01")], "invalid_date_order_rows"),
    ([(None, "1", "2026-01-01", "2400-01-01")], "null_natural_key_rows"),
    ([(1, "1", "2026-01-01", "2400-01-01")] * 2, "duplicate_current_groups"),
])
def test_invalid_stock_state_is_not_a_successful_witness(rows, field):
    result = collect(Connection(rows))
    assert result["status"] == "invalid_stock_contract"
    assert result["stock_contract_valid"] is False and result["counts"][field] > 0


def test_empty_target_is_valid_and_has_no_maximum_date():
    result = collect(Connection())
    assert result["status"] == "ok" and result["counts"]["max_effective_from"] is None


@pytest.mark.parametrize("value", [None, -1, True, 1.5, "0"])
def test_invalid_counter_never_becomes_zero_or_a_pass(value):
    connection = Connection()
    connection.override_count = True
    connection.bad_count = value
    result = collect(connection)
    assert result["status"] == "error" and result["counts"] == {}
    assert result["stock_contract_valid"] is False


@pytest.mark.parametrize("autocommit", [True, None, 0])
def test_autocommit_refusal_precedes_all_statements(autocommit):
    connection = Connection(autocommit=autocommit)
    result = collect(connection)
    assert result["status"] == "error" and connection.statements == []
    assert connection.closes == 1


@pytest.mark.parametrize("failure", ["fail_read", "fail_rollback", "fail_close"])
def test_read_or_cleanup_failure_cannot_return_partial_success(failure):
    connection = Connection()
    setattr(connection, failure, True)
    result = collect(connection)
    assert result["status"] == "error" and result["counts"] == {}
    assert "SYNTHETIC_PRIVATE" not in str(result)
    assert connection.closes == 1 and connection.cursor_closed


def test_bad_identifiers_never_call_the_factory():
    called = []
    result = postflight.collect_stock_postflight("SYS", "DIM_TEST", ["NK"], lambda: called.append(True))
    assert result["status"] == "invalid_request" and called == []


def test_factory_failure_is_fixed_text_not_exception_body():
    def fail():
        raise OSError("SYNTHETIC_PRIVATE_FACTORY")
    result = postflight.collect_stock_postflight("DWH", "DIM_TEST", ["NK"], fail)
    assert result["status"] == "error" and result["counts"] == {}
    assert "SYNTHETIC_PRIVATE" not in str(result)


def test_process_interrupt_propagates_but_read_resources_are_closed():
    connection = Connection()
    connection.interrupt = True
    with pytest.raises(KeyboardInterrupt):
        collect(connection)
    assert connection.rollbacks == connection.closes == 1 and connection.cursor_closed


def test_invalid_maximum_date_is_not_echoed_or_accepted():
    result = collect(Connection([(1, "1", "SYNTHETIC_PRIVATE_DATE", "2400-01-01")]))
    assert result["status"] == "error" and result["counts"] == {}
    assert "SYNTHETIC_PRIVATE" not in str(result)


@pytest.mark.parametrize("keys", [None, "NK", [None], [], ["NK;DELETE"]])
def test_invalid_key_shapes_fail_before_connection(keys):
    calls = []
    result = postflight.collect_stock_postflight("DWH", "DIM_TEST", keys, lambda: calls.append(True))
    assert result["status"] == "invalid_request" and not calls
