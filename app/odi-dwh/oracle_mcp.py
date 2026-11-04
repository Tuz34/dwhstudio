r"""Dormant MCP artifact kept only for backwards lookup.

This file is not imported or launched by the active Flask app. Do not wire it
back into runtime without an explicit external MCP-host decision.
"""

from mcp.server.fastmcp import FastMCP
import re
import db

# CALL and double-space bypass paths are also blocked
_FORBIDDEN_SQL = re.compile(
    r'\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|GRANT|REVOKE|TRUNCATE|EXECUTE|EXEC|MERGE|CALL)\b',
    re.IGNORECASE
)

# Create a new MCP server using FastMCP
mcp = FastMCP("Oracle_19c_MCP_Server")

@mcp.tool()
def oracle_get_tables() -> list[str]:
    """Lists DWH and ODS tables in the Oracle database."""
    try:
        return db.get_user_tables()
    except Exception as e:
        return [f"Hata: {str(e)}"]

@mcp.tool()
def oracle_get_table_schema(table_name: str) -> str:
    """Return the columns and data types of a specified Oracle table (e.g. DWH.DIM_PRODUCT)."""
    try:
        df = db.get_table_schema(table_name)
        return df.to_json(orient='records', force_ascii=False)
    except Exception as e:
        return f"Could not retrieve schema: {str(e)}"

@mcp.tool()
def oracle_get_table_constraints(table_name: str) -> str:
    """Returns constraints such as Primary Key and Foreign Key for the specified Oracle table."""
    try:
        df = db.get_table_constraints(table_name)
        return df.to_json(orient='records', force_ascii=False)
    except Exception as e:
        return f"Constraint error: {str(e)}"

@mcp.tool()
def oracle_execute_readonly_query(sql: str) -> str:
    """Run READ-ONLY SELECT queries in Oracle. (Used for testing and analysis)"""
    if _FORBIDDEN_SQL.search(sql):
        return "SECURITY ERROR: Only SELECT queries are allowed."
    if not re.search(r'\bSELECT\b', sql, re.IGNORECASE):
        return "SECURITY ERROR: Query must start with SELECT."

    try:
        limited_sql = f"SELECT * FROM ({sql}) WHERE ROWNUM <= 100"
        df = db.execute_select(limited_sql)
        return df.to_json(orient='records', force_ascii=False)
    except Exception as e:
        return f"Query error: {str(e)}"

if __name__ == "__main__":
    # Run the MCP server over STDIO (invoked as a subprocess by Claude/LangChain)
    raise SystemExit(
        "oracle_mcp.py is archived/dormant. Use the archived copy only after "
        "an explicit external MCP-host decision."
    )
