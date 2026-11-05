from __future__ import annotations

from pathlib import Path

from scd2_manager import PRODUCTS_SOURCE_METADATA, build_dim_product_scd2_plan


ODS_AUDIT_METADATA = (
    ("ODS_CREATED_AT", "TIMESTAMP", False),
    ("ODS_UPDATED_AT", "TIMESTAMP", True),
)


def _rows(metadata):
    rows = []
    for name, full_type, nullable in metadata:
        if full_type.startswith("VARCHAR2("):
            data_type = "VARCHAR2"
            length = int(full_type.removeprefix("VARCHAR2(").removesuffix(")"))
            precision = scale = None
        elif full_type.startswith("NUMBER("):
            data_type = "NUMBER"
            body = full_type.removeprefix("NUMBER(").removesuffix(")")
            parts = [int(part) for part in body.split(",")]
            precision = parts[0]
            scale = parts[1] if len(parts) == 2 else 0
            length = 22
        elif full_type == "TIMESTAMP":
            data_type = "TIMESTAMP(6)"
            length = 11
            precision = scale = None
        else:
            data_type = full_type
            length = 22 if data_type == "NUMBER" else 7
            precision = scale = None
        rows.append((name, data_type, length, precision, scale, "Y" if nullable else "N"))
    return rows


def _source_metadata():
    return (*PRODUCTS_SOURCE_METADATA, *ODS_AUDIT_METADATA)


def _target_metadata():
    plan = build_dim_product_scd2_plan(
        {
            "name": name,
            "data_type": data_type,
            "nullable": nullable,
        }
        for name, data_type, nullable in _source_metadata()
    )
    return tuple(
        (column["name"], column["data_type"], column["nullable"])
        for column in plan["target_columns"]
    )


class _Cursor:
    def __init__(
        self,
        *,
        source_metadata,
        target_metadata,
        source_count=72,
        target_count=0,
        source_duplicates=0,
        product_sk_duplicates=0,
        current_duplicates=0,
        invalid_flags=0,
        invalid_dates=0,
        sequence_exists=False,
    ):
        self.source_metadata = source_metadata
        self.target_metadata = target_metadata
        self.source_count = source_count
        self.target_count = target_count
        self.source_duplicates = source_duplicates
        self.product_sk_duplicates = product_sk_duplicates
        self.current_duplicates = current_duplicates
        self.invalid_flags = invalid_flags
        self.invalid_dates = invalid_dates
        self.sequence_exists = sequence_exists
        self.result = []
        self.statements = []

    def execute(self, sql, params=None):
        compact = " ".join(sql.split())
        upper = compact.upper()
        self.statements.append((compact, params))
        if upper.startswith("SET TRANSACTION READ ONLY"):
            self.result = []
        elif "FROM ALL_TAB_COLUMNS" in upper:
            table = params["table_name"]
            self.result = list(
                self.source_metadata if table == "ODS_SH_PRODUCTS" else self.target_metadata
            )
        elif "FROM ALL_SEQUENCES" in upper:
            self.result = [(1 if self.sequence_exists else 0,)]
        elif "HAVING COUNT(*) > 1" in upper and '"PRODUCT_SK"' in upper:
            self.result = [(self.product_sk_duplicates,)]
        elif "HAVING COUNT(*) > 1" in upper and '"CURRENT_FLAG" = \'1\'' in upper:
            self.result = [(self.current_duplicates,)]
        elif "HAVING COUNT(*) > 1" in upper:
            self.result = [(self.source_duplicates,)]
        elif '"CURRENT_FLAG" NOT IN' in upper:
            self.result = [(self.invalid_flags,)]
        elif '"EFFECTIVE_FROM" > "EFFECTIVE_TO"' in upper:
            self.result = [(self.invalid_dates,)]
        elif upper.startswith('SELECT COUNT(*) FROM "DWH"."ODS_SH_PRODUCTS"'):
            self.result = [(self.source_count,)]
        elif upper.startswith('SELECT COUNT(*) FROM "DWH"."DIM_PRODUCT"'):
            self.result = [(self.target_count,)]
        else:
            raise AssertionError(f"Unexpected statement: {sql}")

    def fetchall(self):
        return list(self.result)

    def fetchone(self):
        return self.result[0]

    def close(self):
        return None


class _Connection:
    def __init__(self, **kwargs):
        self.cursor_instance = _Cursor(**kwargs)
        self.rollback_count = 0

    def cursor(self):
        return self.cursor_instance

    def rollback(self):
        self.rollback_count += 1


def _connection(**overrides):
    values = {
        "source_metadata": _rows(_source_metadata()),
        "target_metadata": [],
    }
    values.update(overrides)
    return _Connection(**values)


def test_preflight_reports_live_ods_count_and_blocks_missing_dim_target():
    from dim_product_scd2_preflight import run_dim_product_scd2_read_only_preflight

    connection = _connection(source_count=72, source_duplicates=0, sequence_exists=False)

    report = run_dim_product_scd2_read_only_preflight(connection)

    assert report["status"] == "blocked_target_missing"
    assert report["source"]["row_count"] == 72
    assert report["source"]["duplicate_key_group_count"] == 0
    assert report["source"]["metadata_status"] == "metadata_ready_for_review"
    assert report["target"]["exists"] is False
    assert report["sequence"]["exists"] is False
    assert report["required_next"] == ["review_static_target_and_sequence_contract"]
    assert report["write_attempted"] is False
    assert report["raw_rows_included"] is False
    assert report["pk_values_included"] is False
    assert connection.cursor_instance.statements[0][0] == "SET TRANSACTION READ ONLY"
    assert connection.rollback_count == 1


def test_preflight_accepts_aligned_target_and_sequence():
    from dim_product_scd2_preflight import run_dim_product_scd2_read_only_preflight

    connection = _connection(
        target_metadata=_rows(_target_metadata()),
        target_count=72,
        sequence_exists=True,
    )

    report = run_dim_product_scd2_read_only_preflight(connection)

    assert report["status"] == "ready_for_scd2_candidate_preflight"
    assert report["target"]["metadata_status"] == "metadata_ready_for_review"
    assert report["target"]["column_count"] == 26
    assert report["target"]["row_count"] == 72
    assert report["target"]["duplicate_product_sk_group_count"] == 0
    assert report["target"]["duplicate_current_natural_key_group_count"] == 0
    assert report["target"]["invalid_current_flag_count"] == 0
    assert report["target"]["invalid_effective_date_order_count"] == 0
    assert report["sequence"]["exists"] is True
    combined = "\n".join(sql for sql, _params in connection.cursor_instance.statements)
    assert '"CURRENT_FLAG" = \'1\'' in combined
    assert "NOT IN ('1', '0')" in combined
    assert "DATE '2400-01-01'" in combined
    assert "DATE '9999-12-31'" in combined
    assert '"CURRENT_FLAG" = \'Y\'' not in combined
    assert '"CURRENT_FLAG" NOT IN (\'Y\', \'N\')' not in combined


def test_preflight_fails_closed_on_ods_metadata_drift():
    from dim_product_scd2_preflight import run_dim_product_scd2_read_only_preflight

    drifted = tuple(item for item in _source_metadata() if item[0] != "PROD_STATUS")
    connection = _connection(source_metadata=_rows(drifted))

    report = run_dim_product_scd2_read_only_preflight(connection)

    assert report["status"] == "blocked_source_metadata_drift"
    assert report["source"]["metadata_drift"]["missing_columns"] == ["PROD_STATUS"]


def test_preflight_blocks_target_metadata_drift_before_target_dq_queries():
    from dim_product_scd2_preflight import run_dim_product_scd2_read_only_preflight

    drifted = tuple(item for item in _target_metadata() if item[0] != "CURRENT_FLAG")
    connection = _connection(target_metadata=_rows(drifted), sequence_exists=True)

    report = run_dim_product_scd2_read_only_preflight(connection)

    assert report["status"] == "blocked_target_metadata_drift"
    assert report["target"]["metadata_drift"]["missing_columns"] == ["CURRENT_FLAG"]
    assert report["target"]["duplicate_current_natural_key_group_count"] is None


def test_preflight_blocks_multiple_current_rows_for_a_natural_key():
    from dim_product_scd2_preflight import run_dim_product_scd2_read_only_preflight

    connection = _connection(
        target_metadata=_rows(_target_metadata()),
        sequence_exists=True,
        current_duplicates=1,
    )

    report = run_dim_product_scd2_read_only_preflight(connection)

    assert report["status"] == "blocked_target_multiple_current_rows"
    assert report["target"]["duplicate_current_natural_key_group_count"] == 1


def test_preflight_module_has_no_persistent_sql_or_evidence_leak_surface():
    source = (
        Path(__file__).parents[1] / "dim_product_scd2_preflight.py"
    ).read_text(encoding="utf-8")
    lowered = source.lower()

    assert "set transaction read only" in lowered
    for forbidden in (
        "create table",
        "create sequence",
        "insert into",
        "merge into",
        "update dwh",
        "delete from",
        "truncate table",
        "drop table",
        "drop sequence",
    ):
        assert forbidden not in lowered
