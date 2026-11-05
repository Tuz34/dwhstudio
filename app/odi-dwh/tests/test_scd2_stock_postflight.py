"""Execute aggregate stock-DQ SQL against synthetic SQLite rows, never Oracle."""

import re
import sqlite3

import pytest

import scd2_postflight as postflight


class Connection:
    def __init__(self, rows=(), *, autocommit=False, secondary_keys=None, source_rows=None):
        self.autocommit = autocommit
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE DIM_TEST (NK INTEGER, CURRENT_FLAG TEXT, EFFECTIVE_FROM TEXT, EFFECTIVE_TO TEXT, NK2 INTEGER DEFAULT 1)")
        self.db.executemany("INSERT INTO DIM_TEST(NK,CURRENT_FLAG,EFFECTIVE_FROM,EFFECTIVE_TO) VALUES (?,?,?,?)", rows)
        for rowid, value in enumerate(secondary_keys or [], 1):
            self.db.execute("UPDATE DIM_TEST SET NK2=? WHERE rowid=?", (value, rowid))
        if source_rows is not None:
            self.db.execute("CREATE TABLE SRC_TEST (NK INTEGER)")
            self.db.executemany("INSERT INTO SRC_TEST VALUES (?)", [(i,) for i in range(source_rows)])
        self.db.commit()
        self.db.execute("PRAGMA query_only=ON")
        self.statements = []
        self.rollbacks = self.closes = 0
        self.cursor_closed = False
        self.fail_read = self.fail_rollback = self.fail_close = False
        self.interrupt = False
        self.interrupt_stage = None
        self.interrupt_error = KeyboardInterrupt
        self.override_row = False
        self.row_value = None
        self.bad_count = None
        self.override_count = False

    def cursor(self):
        return Cursor(self)

    def rollback(self):
        self.rollbacks += 1
        if self.interrupt_stage == "rollback":
            raise self.interrupt_error
        if self.fail_rollback:
            raise RuntimeError("SYNTHETIC_PRIVATE_ROLLBACK")
        self.db.rollback()

    def close(self):
        self.closes += 1
        self.db.close()
        if self.interrupt_stage == "connection_close":
            raise self.interrupt_error
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
        if connection.override_row:
            self.rows = [connection.row_value]

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def close(self):
        self.connection.cursor_closed = True
        if self.connection.interrupt_stage == "cursor_close":
            raise self.connection.interrupt_error


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
    assert result["connection_attempted"] is True and result["read_only_transaction"] is True
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
    assert result["connection_attempted"] is True and result["read_only_transaction"] is False
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
    assert result["connection_attempted"] is False and result["read_only_transaction"] is False


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


@pytest.mark.parametrize("value", ["2026-W01-1", "20260101  ", "2026-02-30", "2026-01-01 ", "２０２６-01-01"])
def test_maximum_date_requires_exact_ascii_calendar_format(value):
    result = collect(Connection([(1, "1", value, "2400-01-01")]))
    assert result["status"] == "error" and result["counts"] == {}


@pytest.mark.parametrize("row", [None, (), (0, 0), (0, "EXTRA_PRIVATE")])
def test_aggregate_row_width_is_pinned(row):
    connection = Connection()
    connection.override_row, connection.row_value = True, row
    result = collect(connection)
    assert result["status"] == "error" and result["counts"] == {}


@pytest.mark.parametrize("aggregate", ["count", "date"])
def test_only_one_aggregate_kind_is_widened(monkeypatch, aggregate):
    original = Cursor.execute

    def execute(cursor, sql):
        original(cursor, sql)
        if aggregate == "count" and "COUNT(*)" in sql:
            cursor.rows = [(0, 99)]
        elif aggregate == "date" and "TO_CHAR(MAX(" in sql:
            cursor.rows = [("2026-01-01", "EXTRA")]

    monkeypatch.setattr(Cursor, "execute", execute)
    result = collect(Connection())
    assert result["status"] == "error" and result["counts"] == {}
    assert result["stock_contract_valid"] is False


@pytest.mark.parametrize("stage", ["rollback", "cursor_close", "connection_close"])
@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_process_control_during_cleanup_still_attempts_all_resources(stage, error):
    connection = Connection()
    connection.interrupt_stage, connection.interrupt_error = stage, error
    with pytest.raises(error):
        collect(connection)
    assert connection.rollbacks == connection.closes == 1 and connection.cursor_closed


def test_all_natural_key_columns_participate_in_grouping_and_null_check():
    rows = [(1, "1", "2026-01-01", "2400-01-01")] * 2
    connection = Connection(rows, secondary_keys=[1, 2])
    result = postflight.collect_stock_postflight("DWH", "DIM_TEST", ["NK", "NK2"], lambda: connection)
    assert result["status"] == "ok" and result["counts"]["duplicate_current_groups"] == 0
    connection = Connection(rows[:1], secondary_keys=[None])
    result = postflight.collect_stock_postflight("DWH", "DIM_TEST", ["NK", "NK2"], lambda: connection)
    assert result["status"] == "invalid_stock_contract" and result["counts"]["null_natural_key_rows"] == 1


def test_source_count_is_metadata_not_a_business_parity_proof():
    connection = Connection(source_rows=3)
    result = postflight.collect_stock_postflight("DWH", "DIM_TEST", ["NK"], lambda: connection, source=("DWH", "SRC_TEST"))
    assert result["status"] == "ok" and result["counts"]["source_count"] == 3
    assert result["source_current_count_equal"] is False
    assert result["business_parity_proven"] is False and result["physical_noop_proven"] is False
    assert result["history_overlap_checked"] is False


@pytest.mark.parametrize("source", [[], ["DWH", "SRC_TEST"], (), ("DWH",), ("DWH", None), ("SYS", "SRC_TEST"), ("DWH", "X;DELETE")])
def test_source_shape_and_identifiers_are_validated_before_connection(source):
    calls = []
    result = postflight.collect_stock_postflight("DWH", "DIM_TEST", ["NK"], lambda: calls.append(True), source=source)
    assert result["status"] == "invalid_request" and calls == []


def test_custom_python_types_cannot_supply_formatted_sql():
    class Label(str):
        def __format__(self, spec):
            return "MALFORMED_SQL_LABEL"
    class Keys(list):
        pass
    for owner, table, keys in [(Label("DWH"), "DIM_TEST", ["NK"]), ("DWH", Label("DIM_TEST"), ["NK"]),
                               ("DWH", "DIM_TEST", [Label("NK")]), ("DWH", "DIM_TEST", Keys(["NK"]))]:
        calls = []
        result = postflight.collect_stock_postflight(owner, table, keys, lambda: calls.append(True))
        assert result["status"] == "invalid_request" and calls == []


def test_source_string_subclasses_are_rejected_before_connection():
    class Label(str):
        def __format__(self, spec):
            return "MALFORMED_SQL_LABEL"

    for source in [(Label("DWH"), "SRC_TEST"), ("DWH", Label("SRC_TEST"))]:
        calls = []
        result = postflight.collect_stock_postflight(
            "DWH", "DIM_TEST", ["NK"], lambda: calls.append(True), source=source
        )
        assert result["status"] == "invalid_request" and calls == []
