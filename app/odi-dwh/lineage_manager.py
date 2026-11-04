"""
Lineage Manager — Data Lineage Management
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Fetches source/target column mappings from the ODI repository
and writes them to the AUDIT_USER.DATA_LINEAGE table.

Queries:
  - get_table_lineage(table_name) → table based lineage
  - get_impact_analysis(source_table) → change impact analysis
"""

import logging
from datetime import datetime

import oracledb
from config import (
    ORACLE_USER, ORACLE_PASSWORD, ORACLE_DSN,
    ODI_REPO_USER, ODI_REPO_PASSWORD, ODI_REPO_SCHEMA,
)

logger = logging.getLogger("lineage_manager")


class LineageManager:
    """Data lineage management."""

    def __init__(self):
        self.conn = None
        self.odi_conn = None

    def _get_conn(self):
        if self.conn is None or not self.conn.is_healthy():
            self.conn = oracledb.connect(
                user=ORACLE_USER,
                password=ORACLE_PASSWORD,
                dsn=ORACLE_DSN,
            )
        return self.conn

    def _get_odi_conn(self):
        if self.odi_conn is None or not self.odi_conn.is_healthy():
            self.odi_conn = oracledb.connect(
                user=ODI_REPO_USER,
                password=ODI_REPO_PASSWORD,
                dsn=ORACLE_DSN,
            )
        return self.odi_conn

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None
        if self.odi_conn:
            self.odi_conn.close()
            self.odi_conn = None

    def __enter__(self) -> "LineageManager":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False

    def populate_from_odi(self) -> dict:
        """Fetches source-target table mappings from the ODI repository and updates DATA_LINEAGE.

        Correct approach (2 steps):
          1. Target components: DATASTORE with at least one SNP_MAP_EXPR row on the OUTPUT port.
          2. Source components: DATASTORE with no SNP_MAP_EXPR rows on the OUTPUT port.
          Column names are not matched (source/target names may differ in ODI).

        Returns:
            Dictionary: inserted (number of rows added) and
            mappings_processed (number of mappings processed).

        Raises:
            Exception: When an ODI or audit connection error occurs.
        """
        odi_conn = self._get_odi_conn()
        audit_conn = self._get_conn()
        c_odi = odi_conn.cursor()
        c_audit = audit_conn.cursor()

        inserted = 0
        mappings_processed = set()

        # TARGET components: DATASTORE with an expression on the OUTPUT port
        _tgt_sql = f"""
            SELECT DISTINCT m.NAME AS MAPPING_NAME, tgt.NAME AS TARGET_TABLE
            FROM {ODI_REPO_SCHEMA}.SNP_MAPPING m
            JOIN {ODI_REPO_SCHEMA}.SNP_MAP_COMP tgt
              ON tgt.I_OWNER_MAPPING = m.I_MAPPING
             AND tgt.TYPE_NAME = 'DATASTORE'
            WHERE EXISTS (
                SELECT 1
                FROM {ODI_REPO_SCHEMA}.SNP_MAP_CP  cp
                JOIN {ODI_REPO_SCHEMA}.SNP_MAP_ATTR a  ON a.I_OWNER_MAP_CP   = cp.I_MAP_CP
                JOIN {ODI_REPO_SCHEMA}.SNP_MAP_EXPR e  ON e.I_OWNER_MAP_ATTR = a.I_MAP_ATTR
                WHERE cp.I_OWNER_MAP_COMP = tgt.I_MAP_COMP
                  AND cp.DIRECTION = 'O'
            )
        """

        # SOURCE components: DATASTORE without an expression on the OUTPUT port
        _src_sql = f"""
            SELECT DISTINCT m.NAME AS MAPPING_NAME, src.NAME AS SOURCE_TABLE
            FROM {ODI_REPO_SCHEMA}.SNP_MAPPING m
            JOIN {ODI_REPO_SCHEMA}.SNP_MAP_COMP src
              ON src.I_OWNER_MAPPING = m.I_MAPPING
             AND src.TYPE_NAME = 'DATASTORE'
            WHERE NOT EXISTS (
                SELECT 1
                FROM {ODI_REPO_SCHEMA}.SNP_MAP_CP  cp
                JOIN {ODI_REPO_SCHEMA}.SNP_MAP_ATTR a  ON a.I_OWNER_MAP_CP   = cp.I_MAP_CP
                JOIN {ODI_REPO_SCHEMA}.SNP_MAP_EXPR e  ON e.I_OWNER_MAP_ATTR = a.I_MAP_ATTR
                WHERE cp.I_OWNER_MAP_COMP = src.I_MAP_COMP
                  AND cp.DIRECTION = 'O'
            )
        """

        try:
            c_odi.execute(_tgt_sql)
            targets_by_mapping: dict[str, list[str]] = {}
            for mname, tgt_table in c_odi.fetchall():
                targets_by_mapping.setdefault(mname, []).append(tgt_table)

            c_odi.execute(_src_sql)
            sources_by_mapping: dict[str, list[str]] = {}
            for mname, src_table in c_odi.fetchall():
                sources_by_mapping.setdefault(mname, []).append(src_table)

            # Full refresh
            c_audit.execute("DELETE FROM AUDIT_USER.DATA_LINEAGE")

            # One row for each (mapping, source, target) pair
            rows_to_insert = []
            for mname, tgt_tables in targets_by_mapping.items():
                for src_table in sources_by_mapping.get(mname, []):
                    for tgt_table in tgt_tables:
                        rows_to_insert.append({
                            "mname": mname,
                            "stbl": src_table,
                            "ttbl": tgt_table,
                        })

            if rows_to_insert:
                c_audit.executemany("""
                    INSERT INTO AUDIT_USER.DATA_LINEAGE
                        (MAPPING_NAME, SOURCE_TABLE, TARGET_TABLE)
                    VALUES (:mname, :stbl, :ttbl)
                """, rows_to_insert)
                inserted = len(rows_to_insert)
                mappings_processed = {r["mname"] for r in rows_to_insert}

            audit_conn.commit()

        except Exception as e:
            try:
                audit_conn.rollback()
            except Exception as rollback_exc:
                logger.error("Lineage rollback failed: %s", type(rollback_exc).__name__)
            logger.error("Lineage populate failed: %s", type(e).__name__)
            raise
        finally:
            c_odi.close()
            c_audit.close()

        logger.info(
            "Lineage updated: %d records, %d mappings",
            inserted, len(mappings_processed)
        )
        return {
            "inserted": inserted,
            "mappings_processed": len(mappings_processed),
        }

    def get_table_lineage(self, table_name: str) -> list[dict]:
        """Returns lineage records for the specified table.

        Searches the table as both source (SOURCE_TABLE) and target (TARGET_TABLE).

        Args:
            table_name: Table name to query for lineage (case insensitive).

        Returns:
            LINEAGE_ID, MAPPING_NAME, SOURCE/TARGET table and column, EXPRESSION
            fields as a list of dictionaries.
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT LINEAGE_ID, MAPPING_NAME, SOURCE_OWNER, SOURCE_TABLE,
                       SOURCE_COLUMN, TARGET_OWNER, TARGET_TABLE,
                       TARGET_COLUMN, EXPRESSION, UPDATED_AT
                FROM AUDIT_USER.DATA_LINEAGE
                WHERE UPPER(SOURCE_TABLE) = UPPER(:tname)
                   OR UPPER(TARGET_TABLE) = UPPER(:tname)
                ORDER BY MAPPING_NAME, TARGET_COLUMN
            """, {"tname": table_name.upper()})

            cols = [d[0] for d in cursor.description]
            rows = []
            for row in cursor.fetchall():
                d = dict(zip(cols, row))
                for k, v in d.items():
                    if hasattr(v, "isoformat"):
                        d[k] = v.isoformat()
                rows.append(d)
            return rows

        except oracledb.DatabaseError as e:
            if "ORA-00942" in str(e):
                return []
            raise
        finally:
            cursor.close()

    def get_impact_analysis(self,
                            table_name: str,
                            direction: str = "both") -> list[dict]:
        """Two way impact analysis by table.

        First analyzes mapping tables (SNP_MAPPING and
        SNP_MAP_COMP) directly from the ODI repository. At this layer, the OUTPUT
        port is no longer filtered by whether an expression exists;
        therefore both classic source→target mappings and complex EXP/FILTER
        mappings (e.g. PRODUCTSUBCATEGORY) remain in scope.

        If ODI returns no records or the query fails,
        uses AUDIT_USER.DATA_LINEAGE as the fallback source for the
        downstream direction.

        Args:
            table_name: Table name to analyze (case insensitive).
            direction: 'downstream' (this table is the source; find targets),
                       'upstream'   (this table is the target; find sources),
                       'both'       (both directions).

        Returns:
            Each row has mapping_name, related_table, and
            direction ('downstream'/'upstream') fields.
        """
        results: list[dict] = []

        try:
            odi_conn = self._get_odi_conn()
            cursor = odi_conn.cursor()
            try:
                # ── DOWNSTREAM: table is a SOURCE; find affected targets ──
                if direction in ("downstream", "both"):
                    ds_sql = f"""
                        SELECT DISTINCT
                            m.NAME   AS MAPPING_NAME,
                            tgt.NAME AS RELATED_TABLE
                        FROM {ODI_REPO_SCHEMA}.SNP_MAPPING m
                        JOIN {ODI_REPO_SCHEMA}.SNP_MAP_COMP src
                          ON src.I_OWNER_MAPPING = m.I_MAPPING
                         AND UPPER(src.NAME)     = UPPER(:tbl)
                        JOIN {ODI_REPO_SCHEMA}.SNP_MAP_COMP tgt
                          ON tgt.I_OWNER_MAPPING = m.I_MAPPING
                         AND tgt.I_MAP_COMP     != src.I_MAP_COMP
                        ORDER BY m.NAME, tgt.NAME
                    """
                    cursor.execute(ds_sql, {"tbl": table_name.upper()})
                    for mname, rel_table in cursor.fetchall():
                        results.append({
                            "direction":     "downstream",
                            "mapping_name":  mname,
                            "related_table": rel_table,
                            "label":         f"→ {rel_table}",
                        })

                # ── UPSTREAM: table is a TARGET; find feeding sources ──
                if direction in ("upstream", "both"):
                    us_sql = f"""
                        SELECT DISTINCT
                            m.NAME   AS MAPPING_NAME,
                            src.NAME AS RELATED_TABLE
                        FROM {ODI_REPO_SCHEMA}.SNP_MAPPING m
                        JOIN {ODI_REPO_SCHEMA}.SNP_MAP_COMP tgt
                          ON tgt.I_OWNER_MAPPING = m.I_MAPPING
                         AND UPPER(tgt.NAME)     = UPPER(:tbl)
                        JOIN {ODI_REPO_SCHEMA}.SNP_MAP_COMP src
                          ON src.I_OWNER_MAPPING = m.I_MAPPING
                         AND src.I_MAP_COMP     != tgt.I_MAP_COMP
                        ORDER BY m.NAME, src.NAME
                    """
                    cursor.execute(us_sql, {"tbl": table_name.upper()})
                    for mname, rel_table in cursor.fetchall():
                        results.append({
                            "direction":     "upstream",
                            "mapping_name":  mname,
                            "related_table": rel_table,
                            "label":         f"← {rel_table}",
                        })

            finally:
                cursor.close()

        except Exception as e:
            logger.error("ODI impact query failed [%s / %s]: %s",
                         table_name, direction, e, exc_info=True)
            # Propagate the actual error to the API layer so the UI can
            # distinguish "no relationships" from "query error".
            raise

        if results:
            logger.info("Impact (%s): %s → %d rows", direction, table_name, len(results))
            return results

        # ── Fallback: DATA_LINEAGE cache (supports only downstream) ────────
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    SELECT MAPPING_NAME, TARGET_TABLE
                    FROM AUDIT_USER.DATA_LINEAGE
                    WHERE UPPER(SOURCE_TABLE) = UPPER(:tbl)
                    ORDER BY TARGET_TABLE
                """, {"tbl": table_name.upper()})
                seen: set[str] = set()
                for mname, tgt_table in cursor.fetchall():
                    key = f"{mname}|{tgt_table}"
                    if key not in seen:
                        seen.add(key)
                        results.append({
                            "direction":    "downstream",
                            "mapping_name": mname,
                            "related_table": tgt_table,
                            "label":        f"→ {tgt_table}",
                        })
            finally:
                cursor.close()
        except oracledb.DatabaseError:
            pass  # table or column absent — return empty silently

        # ── Final fallback: MAPPING_RUN_STATE (persistent LangGraph state) ──────────
        if not results:
            state_rows = self._impact_from_state(table_name, direction)
            if state_rows:
                logger.info("Impact (%s): %s -> %d rows (state fallback)",
                            direction, table_name, len(state_rows))
                results.extend(state_rows)

        return results

    def _impact_from_state(self, table_name: str, direction: str) -> list[dict]:
        """Derives relationships from LangGraph state if ODI/DATA_LINEAGE returns empty.

        If the target table matches:
          - upstream: state.validated_params.src_table -> this table
        If the source table matches (state's src_table is this table):
          - downstream: this table -> state.validated_params.tgt_table
        """
        try:
            from mapping_state_manager import MappingStateManager
        except Exception:
            return []
        try:
            with MappingStateManager() as msm:
                target_state = msm.get_by_target_table(table_name)
        except Exception as e:
            logger.debug("MAPPING_RUN_STATE could not be read (%s): %s", table_name, e)
            return []
        if not target_state:
            return []
        params = target_state.get("validated_params") or target_state.get("raw_params") or {}
        mname = (params.get("mapping_name") or "").upper()
        src_table = str(params.get("src_table") or "").strip()
        src_schema = str(params.get("src_schema") or "").strip()
        tgt_table = str(params.get("tgt_table") or "").strip()
        out: list[dict] = []
        target_short = (table_name or "").upper().split(".")[-1]
        tgt_short = tgt_table.upper().split(".")[-1]

        if direction in ("upstream", "both") and tgt_short == target_short and src_table:
            related = f"{src_schema}.{src_table}" if src_schema else src_table
            out.append({
                "direction":    "upstream",
                "mapping_name": mname or "(state)",
                "related_table": related,
                "label":        f"<- {related}",
                "source":       "MAPPING_RUN_STATE",
            })
        return out

    def get_table_usage_details(self, table_name: str, mapping_name: str) -> list[dict]:
        """Returns detailed usage of a table within a specific mapping.

        Scans the mapping's components (Filter, Join, Expression, Datastore)
        and finds where the table (or its columns) occurs.

        Args:
            table_name: Table name to search for (e.g. ODS_PRODUCT).
            mapping_name: Mapping name to analyze.

        Returns:
            List containing component name, type, column, and expression details.
        """
        results = []
        cursor = None
        try:
            odi_conn = self._get_odi_conn()
            cursor = odi_conn.cursor()

            # 1. Fetch all components and expressions in this mapping
            # (using a SNP_MAP_ATTR and SNP_MAP_EXPR join)
            sql = """
                SELECT
                    c.NAME AS COMP_NAME,
                    c.TYPE_NAME AS COMP_TYPE,
                    a.NAME AS ATTR_NAME,
                    e.TXT AS EXPRESSION
                FROM {schema}.SNP_MAPPING m
                JOIN {schema}.SNP_MAP_COMP c ON c.I_OWNER_MAPPING = m.I_MAPPING
                JOIN {schema}.SNP_MAP_CP cp ON cp.I_OWNER_MAP_COMP = c.I_MAP_COMP
                JOIN {schema}.SNP_MAP_ATTR a ON a.I_OWNER_MAP_CP = cp.I_MAP_CP
                LEFT JOIN {schema}.SNP_MAP_EXPR e ON e.I_OWNER_MAP_ATTR = a.I_MAP_ATTR
                WHERE UPPER(m.NAME) = UPPER(:mname)
                  AND (
                    UPPER(CAST(e.TXT AS VARCHAR2(4000))) LIKE '%' || UPPER(:tbl) || '%'
                    OR UPPER(c.NAME) = UPPER(:tbl)
                  )
                ORDER BY c.TYPE_NAME, c.NAME, a.NAME
            """.format(schema=ODI_REPO_SCHEMA)

            cursor.execute(sql, {"mname": mapping_name, "tbl": table_name})

            for c_name, c_type, a_name, expr in cursor.fetchall():
                # Convert CLOB (LOB) data to a string
                expr_str = ""
                if expr is not None:
                    if hasattr(expr, "read"):
                        expr_str = expr.read()
                    else:
                        expr_str = str(expr)

                results.append({
                    "comp_name": c_name,
                    "comp_type": c_type,
                    "attr_name": a_name,
                    "expression": expr_str
                })

            # 2. FILTER components are special (their conditions are in SNP_MAP_PROP)
            filter_sql = """
                SELECT
                    c.NAME AS COMP_NAME,
                    'FILTER' AS COMP_TYPE,
                    'FILTER_CONDITION' AS ATTR_NAME,
                    e.TXT AS EXPRESSION
                FROM {schema}.SNP_MAPPING m
                JOIN {schema}.SNP_MAP_COMP c ON c.I_OWNER_MAPPING = m.I_MAPPING
                JOIN {schema}.SNP_MAP_PROP p ON p.I_MAP_COMP = c.I_MAP_COMP
                JOIN {schema}.SNP_MAP_EXPR e ON e.I_OWNER_MAP_PROP = p.I_MAP_PROP
                WHERE UPPER(m.NAME) = UPPER(:mname)
                  AND c.TYPE_NAME = 'FILTER'
                  AND UPPER(CAST(e.TXT AS VARCHAR2(4000))) LIKE '%' || UPPER(:tbl) || '%'
            """.format(schema=ODI_REPO_SCHEMA)
            cursor.execute(filter_sql, {"mname": mapping_name, "tbl": table_name})
            for c_name, c_type, a_name, expr in cursor.fetchall():
                expr_str = expr.read() if hasattr(expr, "read") else str(expr)
                results.append({
                    "comp_name": c_name,
                    "comp_type": c_type,
                    "attr_name": a_name,
                    "expression": expr_str
                })

        except Exception as e:
            logger.error("Usage detail query error: %s", e, exc_info=True)
            raise
        finally:
            if cursor is not None:
                cursor.close()

        return results
