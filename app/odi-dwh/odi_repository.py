"""Read-only helpers for ODI repository metadata.

This module reads only ODI repository tables. It never writes, commits, removes,
or executes ODI SDK operations.
"""

from __future__ import annotations

import re
from typing import Any

import oracledb

from config import ODI_REPO_DSN, ODI_REPO_PASSWORD, ODI_REPO_SCHEMA, ODI_REPO_USER


_IDENTIFIER_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


class OdiRepositoryReader:
    """Small read-only repository metadata reader."""

    def __init__(self) -> None:
        schema = (ODI_REPO_SCHEMA or "").strip().upper()
        if not _IDENTIFIER_RE.match(schema):
            raise ValueError("Invalid ODI_REPO_SCHEMA.")
        self.schema = schema

    def _table(self, table_name: str) -> str:
        clean = table_name.strip().upper()
        if not _IDENTIFIER_RE.match(clean):
            raise ValueError("Invalid ODI repository table name.")
        return f"{self.schema}.{clean}"

    def _connect(self):
        return oracledb.connect(
            user=ODI_REPO_USER,
            password=ODI_REPO_PASSWORD,
            dsn=ODI_REPO_DSN,
        )

    @staticmethod
    def short_table_name(table_name: str | None) -> str:
        text = (table_name or "").strip().upper()
        return text.split(".", 1)[-1] if "." in text else text

    @staticmethod
    def schema_hint_from_table(table_name: str | None) -> str | None:
        text = (table_name or "").strip().upper()
        if "." not in text:
            return None
        return text.split(".", 1)[0]

    @staticmethod
    def schema_hint_from_model(model_name: str | None) -> str | None:
        text = (model_name or "").strip().upper()
        for prefix in ("ORACLE_", "SRC_", "TGT_", "MDL_SRC_ORACLE_", "MDL_TGT_ORACLE_"):
            if text.startswith(prefix) and len(text) > len(prefix):
                return text[len(prefix):]
        return None

    @staticmethod
    def _tokens(*values: Any) -> set[str]:
        tokens: set[str] = set()
        for value in values:
            for token in re.split(r"[^A-Z0-9]+", str(value or "").upper()):
                if token:
                    tokens.add(token)
        return tokens

    @staticmethod
    def _score_model(row: dict[str, Any], *, role: str | None, schema_hint: str | None,
                     preferred_model: str | None) -> int:
        code = str(row.get("model_code") or "").upper()
        name = str(row.get("model_name") or "").upper()
        logical_schema = str(row.get("logical_schema") or "").upper()
        tokens = OdiRepositoryReader._tokens(code, name, logical_schema)
        score = 0

        preferred = (preferred_model or "").strip().upper()
        if preferred and preferred in {code, name, logical_schema}:
            score += 40

        hint = (schema_hint or "").strip().upper()
        if hint and hint in tokens:
            score += 30

        role_key = (role or "").strip().lower()
        if role_key == "source":
            if "SRC" in tokens or code.startswith("SRC_") or name.startswith("MDL_SRC_"):
                score += 15
            if "TGT" in tokens or code.startswith("TGT_") or name.startswith("MDL_TGT_"):
                score -= 20
        elif role_key == "target":
            if "TGT" in tokens or code.startswith("TGT_") or name.startswith("MDL_TGT_"):
                score += 15
            if "DWH" in tokens or "ODS" in tokens:
                score += 5
            if "SRC" in tokens or code.startswith("SRC_") or name.startswith("MDL_SRC_"):
                score -= 20

        return score

    def list_models(self) -> list[dict[str, Any]]:
        sql = f"""
            SELECT
                m.I_MOD AS model_id,
                m.COD_MOD AS model_code,
                m.MOD_NAME AS model_name,
                m.TECH_INT_NAME AS technology,
                m.LSCHEMA_NAME AS logical_schema,
                COUNT(t.I_TABLE) AS datastore_count
            FROM {self._table("SNP_MODEL")} m
            LEFT JOIN {self._table("SNP_TABLE")} t ON t.I_MOD = m.I_MOD
            GROUP BY m.I_MOD, m.COD_MOD, m.MOD_NAME, m.TECH_INT_NAME, m.LSCHEMA_NAME
            ORDER BY m.MOD_NAME
        """
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(sql)
            cols = [d[0].lower() for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def resolve_model(self, *, schema_hint: str | None = None, role: str | None = None,
                      preferred_model: str | None = None) -> dict[str, Any] | None:
        models = self.list_models()
        if not models:
            return None
        ranked = sorted(
            (
                {
                    **model,
                    "score": self._score_model(
                        model,
                        role=role,
                        schema_hint=schema_hint,
                        preferred_model=preferred_model,
                    ),
                }
                for model in models
            ),
            key=lambda item: (item["score"], str(item.get("model_code") or "")),
            reverse=True,
        )
        best = ranked[0]
        if best["score"] <= 0:
            return None
        best["ambiguous"] = len(ranked) > 1 and ranked[1]["score"] == best["score"]
        return best

    def resolve_datastore(self, table_name: str, *, schema_hint: str | None = None,
                          role: str | None = None,
                          preferred_model: str | None = None) -> dict[str, Any] | None:
        short_name = self.short_table_name(table_name)
        if not short_name:
            return None

        sql = f"""
            SELECT
                m.I_MOD AS model_id,
                m.COD_MOD AS model_code,
                m.MOD_NAME AS model_name,
                m.TECH_INT_NAME AS technology,
                m.LSCHEMA_NAME AS logical_schema,
                t.I_TABLE AS datastore_id,
                t.TABLE_NAME AS datastore_name,
                t.RES_NAME AS resource_name
            FROM {self._table("SNP_TABLE")} t
            JOIN {self._table("SNP_MODEL")} m ON m.I_MOD = t.I_MOD
            WHERE UPPER(t.TABLE_NAME) = :table_name
               OR UPPER(t.RES_NAME) = :table_name
            ORDER BY m.MOD_NAME, t.TABLE_NAME
        """
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(sql, {"table_name": short_name.upper()})
            cols = [d[0].lower() for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]

        if not rows:
            return None

        ranked = sorted(
            (
                {
                    **row,
                    "score": self._score_model(
                        row,
                        role=role,
                        schema_hint=schema_hint,
                        preferred_model=preferred_model,
                    ),
                }
                for row in rows
            ),
            key=lambda item: (item["score"], str(item.get("model_code") or "")),
            reverse=True,
        )
        best = ranked[0]
        best["ambiguous"] = len(ranked) > 1 and ranked[1]["score"] == best["score"]
        best["candidate_count"] = len(ranked)
        return best

    def list_datastores_by_names(self, table_names: list[str]) -> list[dict[str, Any]]:
        """Return ODI datastores whose table/resource name matches any name.

        This is read-only and intentionally bulk-oriented so metadata inventory
        screens do not have to issue one repository query per source table.
        """
        clean_names = sorted({
            self.short_table_name(name)
            for name in (table_names or [])
            if self.short_table_name(name)
        })
        if not clean_names:
            return []

        binds = {f"n{i}": name.upper() for i, name in enumerate(clean_names)}
        placeholders = ", ".join(f":n{i}" for i in range(len(clean_names)))
        sql = f"""
            SELECT
                m.I_MOD AS model_id,
                m.COD_MOD AS model_code,
                m.MOD_NAME AS model_name,
                m.TECH_INT_NAME AS technology,
                m.LSCHEMA_NAME AS logical_schema,
                t.I_TABLE AS datastore_id,
                t.TABLE_NAME AS datastore_name,
                t.RES_NAME AS resource_name
            FROM {self._table("SNP_TABLE")} t
            JOIN {self._table("SNP_MODEL")} m ON m.I_MOD = t.I_MOD
            WHERE UPPER(t.TABLE_NAME) IN ({placeholders})
               OR UPPER(t.RES_NAME) IN ({placeholders})
            ORDER BY m.MOD_NAME, t.TABLE_NAME
        """
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(sql, binds)
            cols = [d[0].lower() for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def list_mapping_table_refs(self) -> list[dict[str, Any]]:
        """Return mapping datastore components and their source/target role.

        A datastore component is treated as target when an output attribute has
        an expression, matching the repository pattern used by LineageManager.
        """
        sql = f"""
            SELECT
                m.NAME AS mapping_name,
                p.PROJECT_CODE AS project_code,
                p.PROJECT_NAME AS project_name,
                f.FOLDER_NAME AS folder_name,
                c.NAME AS comp_name,
                c.TYPE_NAME AS comp_type,
                r.QUALIFIED_NAME AS table_qualified_name,
                CASE WHEN EXISTS (
                    SELECT 1
                    FROM {self._table("SNP_MAP_CP")} cp
                    JOIN {self._table("SNP_MAP_ATTR")} a
                      ON a.I_OWNER_MAP_CP = cp.I_MAP_CP
                    JOIN {self._table("SNP_MAP_EXPR")} e
                      ON e.I_OWNER_MAP_ATTR = a.I_MAP_ATTR
                    WHERE cp.I_OWNER_MAP_COMP = c.I_MAP_COMP
                      AND cp.DIRECTION = 'O'
                ) THEN 'Y' ELSE 'N' END AS is_target
            FROM {self._table("SNP_MAPPING")} m
            LEFT JOIN {self._table("SNP_PROJECT")} p ON p.I_PROJECT = m.I_PROJECT
            LEFT JOIN {self._table("SNP_FOLDER")} f ON f.I_FOLDER = m.I_FOLDER
            JOIN {self._table("SNP_MAP_COMP")} c ON m.I_MAPPING = c.I_OWNER_MAPPING
            LEFT JOIN {self._table("SNP_MAP_REF")} r ON c.I_MAP_REF = r.I_MAP_REF
            WHERE c.TYPE_NAME = 'DATASTORE'
            ORDER BY m.NAME, c.NAME
        """
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(sql)
            cols = [d[0].lower() for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def mapping_exists(self, mapping_name: str, *, project: str | None = None,
                       folder: str | None = None) -> dict[str, Any] | None:
        clean_mapping = (mapping_name or "").strip().upper()
        if not clean_mapping:
            return None

        sql = f"""
            SELECT
                m.NAME AS mapping_name,
                p.PROJECT_CODE AS project_code,
                p.PROJECT_NAME AS project_name,
                f.FOLDER_NAME AS folder_name
            FROM {self._table("SNP_MAPPING")} m
            LEFT JOIN {self._table("SNP_PROJECT")} p ON p.I_PROJECT = m.I_PROJECT
            LEFT JOIN {self._table("SNP_FOLDER")} f ON f.I_FOLDER = m.I_FOLDER
            WHERE UPPER(m.NAME) = :mapping_name
        """
        binds = {"mapping_name": clean_mapping}
        project_hint = (project or "").strip().upper()
        if project_hint:
            sql += " AND (UPPER(p.PROJECT_CODE) = :project OR UPPER(p.PROJECT_NAME) = :project)"
            binds["project"] = project_hint
        folder_hint = (folder or "").strip().upper()
        if folder_hint:
            sql += " AND UPPER(f.FOLDER_NAME) = :folder"
            binds["folder"] = folder_hint
        sql += " FETCH FIRST 1 ROWS ONLY"

        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(sql, binds)
            row = cur.fetchone()
            if row is None:
                return None
            cols = [d[0].lower() for d in cur.description]
            return dict(zip(cols, row))
