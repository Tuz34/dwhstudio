"""Current stock-IKM literals for the fixed DWH.DIM_PRODUCT path.

Pre-migration Y/N and 9999-12-31 values stay named here so callers can tell the
two contracts apart. Load, candidate, read-only preflight, and attribute proof
accept only the stock literals. The migration preflight is the only reader that
accepts the pre-migration literals, and only before a reviewed cutover.
"""

from __future__ import annotations

STOCK_CONTRACT_ERA = "stock_ikm_1_0_2400"
PRE_MIGRATION_CONTRACT_ERA = "pre_migration_yn_9999"

STOCK_CURRENT_FLAG = "1"
STOCK_HISTORICAL_FLAG = "0"
PRE_MIGRATION_CURRENT_FLAG = "Y"
PRE_MIGRATION_HISTORICAL_FLAG = "N"

STOCK_OPEN_ENDED_ISO = "2400-01-01"
PRE_MIGRATION_OPEN_ENDED_ISO = "9999-12-31"
STOCK_OPEN_ENDED_DATE_SQL = "DATE '2400-01-01'"
PRE_MIGRATION_OPEN_ENDED_DATE_SQL = "DATE '9999-12-31'"

STOCK_CURRENT_MATCH_SQL = "\"CURRENT_FLAG\" = '1'"
STOCK_HISTORICAL_MATCH_SQL = "\"CURRENT_FLAG\" = '0'"
STOCK_INVALID_FLAG_SQL = "\"CURRENT_FLAG\" NOT IN ('1', '0') OR \"CURRENT_FLAG\" IS NULL"

# Dedicated mapping expressions in odi_automation/dim_product_scd2_mapping_create.groovy.
# SQL uses SYSTIMESTAMP as the statement-time equivalent of CURRENT_TIMESTAMP.
MAPPING_EFFECTIVE_TO_EXPRESSION = "TO_DATE('24000101','YYYYMMDD')"
MAPPING_CURRENT_FLAG_EXPRESSION = "1"
MAPPING_INSERT_DATE_EXPRESSION = "CURRENT_TIMESTAMP"
MAPPING_UPDATE_DATE_EXPRESSION = "CURRENT_TIMESTAMP"
MAPPING_SOURCE_SYSTEM_EXPRESSION = "'SH'"
MAPPING_ETL_BATCH_ID_EXPRESSION = "<%=odiRef.getSession(\"SESS_NO\")%>"

SQL_INSERT_AUDIT = "SYSTIMESTAMP"
SQL_UPDATE_AUDIT = "SYSTIMESTAMP"
SQL_SOURCE_SYSTEM = "'SH'"

EXPECTED_COLUMN_COUNT = 26
EXPECTED_ROW_COUNT = 72
FLAG_DATA_TYPE = "VARCHAR2(1)"
EFFECTIVE_TO_DATA_TYPE = "DATE"


def stock_invalid_effective_sql() -> str:
    """Stock DQ: 9999 is gone, current rows end at 2400, history is not open-ended."""
    return (
        '"EFFECTIVE_FROM" > "EFFECTIVE_TO"'
        f" OR \"EFFECTIVE_TO\" = {PRE_MIGRATION_OPEN_ENDED_DATE_SQL}"
        f" OR ({STOCK_CURRENT_MATCH_SQL} AND \"EFFECTIVE_TO\" <> {STOCK_OPEN_ENDED_DATE_SQL})"
        f" OR ({STOCK_HISTORICAL_MATCH_SQL} AND \"EFFECTIVE_TO\" = {STOCK_OPEN_ENDED_DATE_SQL})"
    )


def oracle_column_type(
    data_type: object,
    data_length: object,
    precision: object,
    scale: object,
) -> str:
    """Render one ALL_TAB_COLUMNS row the same way the fixed preflight does."""
    base = str(data_type or "").upper()
    if base == "TIMESTAMP(6)":
        return "TIMESTAMP"
    if base in {"VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR"}:
        return f"{base}({int(data_length)})"
    if base == "NUMBER" and precision is not None:
        if scale not in (None, 0):
            return f"NUMBER({int(precision)},{int(scale)})"
        return f"NUMBER({int(precision)})"
    return base
