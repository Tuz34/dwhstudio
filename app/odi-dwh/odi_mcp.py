import logging
import threading
import time

from mcp.server.fastmcp import FastMCP
import oracledb
import pandas as pd
from config import ODI_REPO_SCHEMA, ODI_REPO_USER, ODI_REPO_PASSWORD, ODI_REPO_DSN, ORACLE_DSN
from diagnostic_errors import safe_error_json
from odi_orchestrator import OdiOrchestrator

# Create a new MCP server using FastMCP
mcp = FastMCP("ODI_Repository_MCP_Server")
odi_orch = OdiOrchestrator()
logger = logging.getLogger(__name__)

def execute_odi_select(sql: str) -> pd.DataFrame:
    """Execute the given SQL as the configured ODI repository user and return a DataFrame."""
    conn = oracledb.connect(user=ODI_REPO_USER, password=ODI_REPO_PASSWORD, dsn=ODI_REPO_DSN or ORACLE_DSN)
    try:
        df = pd.read_sql(sql, conn)
        # Convert datetime values to strings
        for col in df.select_dtypes(include=['datetime64', 'datetimetz']).columns:
            df[col] = df[col].dt.strftime('%Y-%m-%d %H:%M:%S')
        return df
    finally:
        conn.close()


def execute_odi_lob_query(sql: str, params: dict = None) -> list:
    """Execute CLOB/LOB queries with a cursor and convert LOB values to strings."""
    conn = oracledb.connect(user=ODI_REPO_USER, password=ODI_REPO_PASSWORD, dsn=ODI_REPO_DSN or ORACLE_DSN)
    try:
        cur = conn.cursor()
        if params:
            cur.execute(sql, params)
        else:
            cur.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = []
        for r in cur.fetchall():
            row = {}
            for i, col in enumerate(cols):
                val = r[i]
                if hasattr(val, 'read'):   # LOB (CLOB/BLOB) object
                    val = val.read()
                row[col] = val
            rows.append(row)
        return rows
    finally:
        conn.close()

@mcp.tool()
def odi_generate_mapping(mapping_name: str, src_model: str, src_table: str, tgt_model: str, tgt_table: str, sequence_name: str = "NONE") -> str:
    """
    Prepare the Groovy command needed to create an ODI mapping between the specified source and target tables and package a scenario.
    If SKEY is used, pass it through `sequence_name` (e.g. SEQ_ODS_CUST_SKEY); otherwise leave it empty.
    Example: odi_generate_mapping("MAP_CUSTOMER", "MSSQL", "CUSTOMER", "ORACLE_ODS", "ODS_CUSTOMER", "SEQ_CUSTOMER_ID")
    """
    try:
        return odi_orch.generate_mapping(mapping_name, src_model, src_table, tgt_model, tgt_table, sequence_name)
    except Exception as e:
        return safe_error_json("ODI mapping preparation", e)

@mcp.tool()
def odi_get_recent_sessions(limit: int = 20) -> str:
    """Return the status of the latest scenario and mapping sessions running on ODI (Oracle Data Integrator)."""
    sql = f"""
        SELECT 
            SESS_NO AS SESSION_NO,
            SESS_NAME AS SESSION_NAME,
            SESS_BEG AS START_TIME,
            SESS_END AS END_TIME,
            SESS_STATUS AS STATUS,      -- 'D' (Done), 'E' (Error), 'R' (Running), 'W' (Waiting)
            SESS_MESS AS ERROR_MESSAGE,
            NB_ROW AS PROCESSED_ROWS,
            NB_INS AS INSERTED_ROWS,
            NB_UPD AS UPDATED_ROWS,
            NB_ERR AS ERROR_ROWS
        FROM {ODI_REPO_SCHEMA}.SNP_SESSION
        ORDER BY SESS_BEG DESC
        FETCH FIRST {limit} ROWS ONLY
    """
    try:
        df = execute_odi_select(sql)
        return df.to_json(orient='records', force_ascii=False)
    except Exception as e:
        return safe_error_json("ODI sessions read", e)


# EVIDENCE_READ_BUDGET_S is the caller's total wait on time.monotonic, from
# connect through close. The driver caps below are only the worker's own
# per-round-trip limits (oracledb 2.4.1: tcp_connect_timeout is seconds,
# call_timeout is milliseconds for one round trip).
EVIDENCE_READ_BUDGET_S = 5
EVIDENCE_CONNECT_TIMEOUT_S = 1.0
EVIDENCE_CALL_TIMEOUT_MS = 2000
_EVIDENCE_MESSAGE_CHARS = 4000
_EVIDENCE_MAX_SAFE_INTEGER = 2**53 - 1
_EVIDENCE_READ_SLOTS = threading.BoundedSemaphore(2)


def _wait_evidence(done: threading.Event, budget_s: float) -> bool:
    """Wait on the monotonic clock. A late set after this returns is ignored."""
    deadline = time.monotonic() + float(budget_s)
    remaining = deadline - time.monotonic()
    if remaining < 0:
        remaining = 0.0
    return bool(done.wait(remaining))


def _take_evidence_row(cursor: object, session_no: int) -> dict | None:
    names = [item[0] for item in cursor.description]
    fetched = cursor.fetchmany(2)
    if len(fetched) != 1:
        return None
    row = {names[index]: fetched[0][index] for index in range(len(names))}
    found = row.get("SESS_NO")
    if type(found) is not int or isinstance(found, bool) or found != session_no:
        return None
    return row


def _finish_evidence_row(row: dict | None, *, include_error_text: bool) -> dict | None:
    if row is None:
        return None
    text = row.get("ERROR_MESSAGE")
    if (
        include_error_text
        and isinstance(text, str)
        and len(text) <= _EVIDENCE_MESSAGE_CHARS
    ):
        return row
    row.pop("ERROR_MESSAGE", None)
    return row


def _evidence_worker(
    session_no: int,
    sql: str,
    include_error_text: bool,
    box: dict,
    done: threading.Event,
) -> None:
    """Own the connection. No Flask context. Log only an exception class name."""
    conn = None
    try:
        conn = oracledb.connect(
            user=ODI_REPO_USER,
            password=ODI_REPO_PASSWORD,
            dsn=ODI_REPO_DSN or ORACLE_DSN,
            tcp_connect_timeout=EVIDENCE_CONNECT_TIMEOUT_S,
            retry_count=0,
        )
        conn.call_timeout = EVIDENCE_CALL_TIMEOUT_MS
        cursor = conn.cursor()
        cursor.prefetchrows = 2
        cursor.execute(sql, {"sess_no": session_no})
        box["result"] = _finish_evidence_row(
            _take_evidence_row(cursor, session_no),
            include_error_text=include_error_text,
        )
    except (oracledb.Error, OSError) as exc:
        box["result"] = None
        logger.warning("%s", type(exc).__name__)
    finally:
        try:
            if conn is not None:
                conn.close()
        except (oracledb.Error, OSError) as exc:
            logger.warning("%s", type(exc).__name__)
        finally:
            done.set()
            _EVIDENCE_READ_SLOTS.release()


def read_session_run_evidence(session_no: int, *, include_error_text: bool) -> dict | None:
    """One SNP_SESSION statement. The caller waits at most EVIDENCE_READ_BUDGET_S."""
    if (
        type(session_no) is not int
        or isinstance(session_no, bool)
        or session_no <= 0
        or session_no > _EVIDENCE_MAX_SAFE_INTEGER
    ):
        return None
    if not _EVIDENCE_READ_SLOTS.acquire(blocking=False):
        return None
    message_column = ""
    if include_error_text:
        message_column = (
            f",\n               SUBSTR(SESS_MESS, 1, {_EVIDENCE_MESSAGE_CHARS}) AS ERROR_MESSAGE"
        )
    sql = f"""
        SELECT SESS_NO,
               NB_ROW AS PROCESSED_ROWS,
               NB_INS AS INSERTED_ROWS,
               NB_UPD AS UPDATED_ROWS,
               NB_ERR AS ERROR_ROWS{message_column}
          FROM {ODI_REPO_SCHEMA}.SNP_SESSION
         WHERE SESS_NO = :sess_no
         FETCH FIRST 2 ROWS ONLY
    """
    done = threading.Event()
    box: dict = {}
    worker = threading.Thread(
        target=_evidence_worker,
        args=(session_no, sql, include_error_text, box, done),
        daemon=True,
        name="odi-evidence-read",
    )
    started = False
    try:
        worker.start()
        started = True
        if not _wait_evidence(done, EVIDENCE_READ_BUDGET_S):
            return None
        result = box.get("result")
        return result if isinstance(result, dict) else None
    finally:
        if not started:
            _EVIDENCE_READ_SLOTS.release()


@mcp.tool()
def odi_get_scenario_list() -> str:
    """List available scenarios (Execution Packages) in ODI."""
    sql = f"""
        SELECT 
            SCEN_NAME AS SCENARIO_NAME,
            SCEN_VERSION AS VERSION
        FROM {ODI_REPO_SCHEMA}.SNP_SCEN
        ORDER BY SCEN_NAME
    """
    try:
        df = execute_odi_select(sql)
        return df.to_json(orient='records', force_ascii=False)
    except Exception as e:
        return safe_error_json("ODI scenarios read", e)

@mcp.tool()
def odi_get_knowledge_modules() -> str:
    """List all knowledge modules in the ODI repository. TRT_TYPE codes: KI=IKM, KL=LKM, CK=CKM, EK=EKM."""
    sql = f"""
        SELECT 
            I_TRT AS KM_ID,
            TRT_NAME AS KM_NAME,
            TRT_TYPE AS KM_TYPE,
            KM_VERSION AS VERSION
        FROM {ODI_REPO_SCHEMA}.SNP_TRT
        ORDER BY TRT_TYPE, TRT_NAME
    """
    try:
        df = execute_odi_select(sql)
        return df.to_json(orient='records', force_ascii=False)
    except Exception as e:
        return safe_error_json("ODI knowledge modules read", e)

@mcp.tool()
def odi_get_mapping_lineage(limit: int = 50) -> str:
    """Retrieve ODI mapping components (Data Lineage), including filters, distinct, and join objects."""
    sql = f"""
        SELECT
            m.NAME as MAPPING_NAME,
            p.PROJECT_CODE as PROJECT_CODE,
            p.PROJECT_NAME as PROJECT_NAME,
            f.FOLDER_NAME as FOLDER_NAME,
            c.NAME as COMP_NAME,
            c.TYPE_NAME as COMP_TYPE,
            r.QUALIFIED_NAME as TABLE_QUALIFIED_NAME
        FROM {ODI_REPO_SCHEMA}.SNP_MAPPING m
        LEFT JOIN {ODI_REPO_SCHEMA}.SNP_PROJECT p ON p.I_PROJECT = m.I_PROJECT
        LEFT JOIN {ODI_REPO_SCHEMA}.SNP_FOLDER f ON f.I_FOLDER = m.I_FOLDER
        JOIN {ODI_REPO_SCHEMA}.SNP_MAP_COMP c ON m.I_MAPPING = c.I_OWNER_MAPPING
        LEFT JOIN {ODI_REPO_SCHEMA}.SNP_MAP_REF r ON c.I_MAP_REF = r.I_MAP_REF
        ORDER BY m.NAME, c.TYPE_NAME
        FETCH FIRST {limit} ROWS ONLY
    """
    try:
        df = execute_odi_select(sql)
        return df.to_json(orient='records', force_ascii=False)
    except Exception as e:
        return safe_error_json("ODI mapping lineage read", e)

@mcp.tool()
def odi_get_mapping_details(mapping_name: str) -> str:
    """
    Return all components of a given mapping (DATASTORE, FILE, FILTER, etc.).
    Include column-level mappings (lineage) and FILTER conditions for each component.

    Returned JSON fields: MAPPING_NAME, COMP_NAME, COMP_TYPE, ATTR_NAME, EXPRESSION,
                         DATA_TYPE, COL_LENGTH, COL_PREC, COL_SCALE
    - DATASTORE/FILE + EXPRESSION populated → target component column mapping
    - DATASTORE/FILE + EXPRESSION null    → source component column
    - FILTER + ATTR_NAME=FILTER_CONDITION → filter condition expression
    """
    # S-1: Use bind variable (:mname) instead of string concatenation — prevents SQL injection
    # Fetch the actual table name (QUALIFIED_NAME) with the SNP_MAP_REF join
    sql = f"""
        SELECT
            m.NAME              AS MAPPING_NAME,
            c.NAME              AS COMP_NAME,
            c.TYPE_NAME         AS COMP_TYPE,
            r.QUALIFIED_NAME    AS TABLE_QUALIFIED_NAME,
            a.NAME              AS ATTR_NAME,
            e.TXT               AS EXPRESSION,
            a.DATA_TYPE_NAME    AS DATA_TYPE,
            a.LENGTH            AS COL_LENGTH,
            a.PREC              AS COL_PREC,
            a.SCALE             AS COL_SCALE
        FROM {ODI_REPO_SCHEMA}.SNP_MAPPING m
        JOIN {ODI_REPO_SCHEMA}.SNP_MAP_COMP c
          ON m.I_MAPPING = c.I_OWNER_MAPPING
        LEFT JOIN {ODI_REPO_SCHEMA}.SNP_MAP_REF r
          ON c.I_MAP_REF = r.I_MAP_REF
        JOIN {ODI_REPO_SCHEMA}.SNP_MAP_CP cp
          ON cp.I_OWNER_MAP_COMP = c.I_MAP_COMP AND cp.DIRECTION = 'O'
        JOIN {ODI_REPO_SCHEMA}.SNP_MAP_ATTR a
          ON a.I_OWNER_MAP_CP = cp.I_MAP_CP
        LEFT JOIN {ODI_REPO_SCHEMA}.SNP_MAP_EXPR e
          ON e.I_OWNER_MAP_ATTR = a.I_MAP_ATTR
        WHERE m.NAME = :mname
        UNION ALL
        SELECT
            m.NAME              AS MAPPING_NAME,
            c.NAME              AS COMP_NAME,
            c.TYPE_NAME         AS COMP_TYPE,
            NULL                AS TABLE_QUALIFIED_NAME,
            p.NAME              AS ATTR_NAME,
            e2.TXT              AS EXPRESSION,
            NULL                AS DATA_TYPE,
            NULL                AS COL_LENGTH,
            NULL                AS COL_PREC,
            NULL                AS COL_SCALE
        FROM {ODI_REPO_SCHEMA}.SNP_MAPPING m
        JOIN {ODI_REPO_SCHEMA}.SNP_MAP_COMP c
          ON m.I_MAPPING = c.I_OWNER_MAPPING
        JOIN {ODI_REPO_SCHEMA}.SNP_MAP_PROP p
          ON p.I_MAP_COMP = c.I_MAP_COMP
        JOIN {ODI_REPO_SCHEMA}.SNP_MAP_EXPR e2
          ON e2.I_OWNER_MAP_PROP = p.I_MAP_PROP
        WHERE m.NAME = :mname
          AND p.NAME IN ('FILTER_CONDITION', 'JOIN_CONDITION', 'LOOKUP_CONDITION', 'EXPRESSION')
        ORDER BY COMP_TYPE, COMP_NAME, ATTR_NAME
    """
    try:
        import json as _json
        rows = execute_odi_lob_query(sql, {"mname": mapping_name})
        return _json.dumps(rows, ensure_ascii=False, default=str)
    except Exception as e:
        return safe_error_json("ODI mapping details read", e)


# ── Scenario Generation & Execution ─────────────────────────────────────────────

def odi_create_scenario(name: str, mapping: str, version: str = "001") -> str:
    """Generates a scenario from an ODI mapping. Returns: command string or result."""
    import json as _json
    try:
        result = odi_orch.generate_scenario(name, mapping, version)
        return _json.dumps({"result": result}, ensure_ascii=False)
    except Exception as e:
        return safe_error_json("ODI scenario generation", e)


def odi_run_scenario(name: str, version: str = "001", context: str = "GLOBAL") -> str:
    """Run an existing ODI scenario. Returns: command string or result."""
    import json as _json
    try:
        result = odi_orch.run_scenario(name, version, context)
        return _json.dumps({"result": result}, ensure_ascii=False)
    except Exception as e:
        return safe_error_json("ODI scenario run", e)
