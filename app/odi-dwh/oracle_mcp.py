r"""Dormant MCP artifact kept only for backwards lookup.

This file is not imported or launched by the active Flask app. Do not wire it
back into runtime without an explicit external MCP-host decision.
"""

from mcp.server.fastmcp import FastMCP
import re
import db

# CALL ve çift-boşluklu bypass yolları da engellendi
_FORBIDDEN_SQL = re.compile(
    r'\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|GRANT|REVOKE|TRUNCATE|EXECUTE|EXEC|MERGE|CALL)\b',
    re.IGNORECASE
)

# FastMCP kullanarak yeni bir MCP sunucusu olusturuyoruz
mcp = FastMCP("Oracle_19c_MCP_Server")

@mcp.tool()
def oracle_get_tables() -> list[str]:
    """Oracle veritabanindaki DWH ve ODS tablolarini listeler."""
    try:
        return db.get_user_tables()
    except Exception as e:
        return [f"Hata: {str(e)}"]

@mcp.tool()
def oracle_get_table_schema(table_name: str) -> str:
    """Belirtilen bir Oracle tablosunun (ÖRN: DWH.DIM_PRODUCT) kolonlarini ve veri tiplerini dondurur."""
    try:
        df = db.get_table_schema(table_name)
        return df.to_json(orient='records', force_ascii=False)
    except Exception as e:
        return f"Şema Alinamadi: {str(e)}"

@mcp.tool()
def oracle_get_table_constraints(table_name: str) -> str:
    """Belirtilen Oracle tablosunun Primary Key, Foreign Key gibi kisitlamalarini dondurur."""
    try:
        df = db.get_table_constraints(table_name)
        return df.to_json(orient='records', force_ascii=False)
    except Exception as e:
        return f"Constraint Hatasi: {str(e)}"

@mcp.tool()
def oracle_execute_readonly_query(sql: str) -> str:
    """Oracle veritabaninda READ-ONLY SELECT sorgulari calistirir. (Test ve analiz icon kullanilir)"""
    if _FORBIDDEN_SQL.search(sql):
        return "GUVENLIK HATASI: Sadece SELECT sorgularina izin verilir."
    if not re.search(r'\bSELECT\b', sql, re.IGNORECASE):
        return "GUVENLIK HATASI: Sorgu SELECT ile baslamalidir."

    try:
        limited_sql = f"SELECT * FROM ({sql}) WHERE ROWNUM <= 100"
        df = db.execute_select(limited_sql)
        return df.to_json(orient='records', force_ascii=False)
    except Exception as e:
        return f"Sorgu Hatasi: {str(e)}"

if __name__ == "__main__":
    # MCP server'i STDIO uzerinden calistir (Claude/LangChain tarafindan subprocess olarak cagrilir)
    raise SystemExit(
        "oracle_mcp.py is archived/dormant. Use the archived copy only after "
        "an explicit external MCP-host decision."
    )
