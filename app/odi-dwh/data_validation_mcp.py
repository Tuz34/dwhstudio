from mcp.server.fastmcp import FastMCP
import db
import pandas as pd
import logging
import json
from source_connector import get_source_connector

# Logger setup
logger = logging.getLogger("data_validation_mcp")

mcp = FastMCP("Data_Validation_MCP_Server")

def _split_table(table_name: str) -> tuple[str | None, str]:
    text = str(table_name or "").strip()
    if "." in text:
        schema, table = text.split(".", 1)
        return schema.strip(), table.strip()
    return None, text

def _q_oracle(name: str) -> str:
    return '"' + str(name).upper().replace('"', '') + '"'

def _q_source_table(table_name: str) -> str:
    connector = get_source_connector()
    schema, table = _split_table(table_name)
    if connector.db_type == "mssql":
        return f"[{schema}].[{table}]" if schema else table
    return f"{_q_oracle(schema)}.{_q_oracle(table)}" if schema else _q_oracle(table)

def _source_limit_clause(sql: str, limit: int) -> str:
    connector = get_source_connector()
    if connector.db_type == "mssql":
        return sql.replace("SELECT ", f"SELECT TOP {int(limit)} ", 1)
    return f"{sql} FETCH FIRST {int(limit)} ROWS ONLY"

def _execute_source(sql: str):
    return get_source_connector().execute_select(sql)

@mcp.tool()
def compare_row_counts(src_table: str, tgt_table: str) -> str:
    """Compares row counts of the active source connector and target Oracle tables."""
    try:
        src_df = _execute_source(f"SELECT COUNT(*) as CNT FROM {_q_source_table(src_table)}")
        src_count = int(src_df.iloc[0]['CNT'])
        
        # Oracle Count
        oracle_df = db.execute_select(f"SELECT COUNT(*) as CNT FROM {tgt_table}")
        tgt_count = int(oracle_df.iloc[0]['CNT'])
        
        diff = src_count - tgt_count
        status = "MATCH" if diff == 0 else "MISMATCH"
        
        return json.dumps({
            "src_table": src_table,
            "src_count": src_count,
            "tgt_table": tgt_table,
            "tgt_count": tgt_count,
            "difference": diff,
            "status": status
        }, indent=2)
    except Exception as e:
        return f"Hata: {str(e)}"

@mcp.tool()
def validate_column_sum(src_table: str, tgt_table: str, col_name: str) -> str:
    """Checks whether sums of a numeric column match (Source vs Target)."""
    try:
        src_df = _execute_source(f"SELECT SUM({_q_oracle(col_name)}) as TOTAL FROM {_q_source_table(src_table)}")
        src_sum = float(src_df.iloc[0]['TOTAL'] or 0)
        
        # Oracle Sum
        oracle_df = db.execute_select(f"SELECT SUM({_q_oracle(col_name)}) as TOTAL FROM {tgt_table}")
        tgt_sum = float(oracle_df.iloc[0]['TOTAL'] or 0)
        
        diff = round(src_sum - tgt_sum, 4)
        status = "MATCH" if abs(diff) < 0.01 else "MISMATCH"
        
        return json.dumps({
            "column": col_name,
            "src_sum": src_sum,
            "tgt_sum": tgt_sum,
            "difference": diff,
            "status": status
        }, indent=2)
    except Exception as e:
        return f"Hata: {str(e)}"

@mcp.tool()
def find_missing_rows(src_table: str, tgt_table: str, key_column: str, limit: int = 50) -> str:
    """Finds records present in the source table but missing from the target table by key column."""
    try:
        # For this operation, fetch all keys from MSSQL (should be limited for large tables)
        # For simplicity, query directly here
        source_sql = _source_limit_clause(
            f"SELECT {_q_oracle(key_column)} FROM {_q_source_table(src_table)}",
            limit * 100
        )
        source_keys = _execute_source(source_sql)
        
        # Oracle keys
        oracle_keys = db.execute_select(f"SELECT {_q_oracle(key_column)} FROM {tgt_table} WHERE ROWNUM <= {limit*100}")
        
        src_set = set(source_keys[key_column.upper()].astype(str))
        tgt_set = set(oracle_keys[key_column.upper()].astype(str))
        
        missing = list(src_set - tgt_set)[:limit]
        
        return json.dumps({
            "key_column": key_column,
            "missing_count_in_sample": len(missing),
            "sample_missing_keys": missing,
            "note": f"Sample size limited to top {limit*100} keys"
        }, indent=2)
    except Exception as e:
        return f"Hata: {str(e)}"

@mcp.tool()
def check_duplicate_records(table_name: str, key_columns: str) -> str:
    """Checks for duplicate records by the specified column(s) (on Oracle)."""
    try:
        sql = f"""
            SELECT {key_columns}, COUNT(*) as CNT 
            FROM {table_name} 
            GROUP BY {key_columns} 
            HAVING COUNT(*) > 1
        """
        df = db.execute_select(sql)
        return df.to_json(orient='records', force_ascii=False)
    except Exception as e:
        return f"Hata: {str(e)}"

@mcp.tool()
def validate_column_accuracy_by_pk(src_table: str, tgt_table: str, pk_col: str, col_to_check: str) -> str:
    """Checks row by row by PK whether values in the specified column (especially NULLs) match in source and target."""
    try:
        # Fetch sample PKs where the relevant column in the target (Oracle) table is empty (NULL):
        oracle_df = db.execute_select(
            f"SELECT {_q_oracle(pk_col)} FROM {tgt_table} "
            f"WHERE {_q_oracle(col_to_check)} IS NULL AND ROWNUM <= 100"
        )
        if oracle_df.empty:
            return json.dumps({
                "status": "PASS",
                "message": f"No records were found in the target table ({tgt_table}) with an empty {col_to_check} column. (Limited test)"
            }, indent=2)
            
        # Extract the PK list
        pks = oracle_df[pk_col.upper()].astype(str).tolist()
        pk_list_str = ",".join(f"'{p}'" for p in pks)
        
        # What is the state of these PKs in the source (MSSQL)? Are they empty there too?
        source_df = _execute_source(
            f"SELECT {_q_oracle(pk_col)}, {_q_oracle(col_to_check)} "
            f"FROM {_q_source_table(src_table)} WHERE {_q_oracle(pk_col)} IN ({pk_list_str})"
        )
        
        # Find nonempty (non-NULL) values in MSSQL
        mismatched_pks = []
        for index, row in source_df.iterrows():
            val = row[col_to_check.upper()]
            if pd.notna(val) and val != '':
                mismatched_pks.append(row[pk_col.upper()])
                
        if mismatched_pks:
            return json.dumps({
                "status": "FAIL (MISMATCH)",
                "message": f"Records were found that are NULL in the target but NONEMPTY in the source!",
                "mismatched_pk_samples": mismatched_pks[:10],
                "total_mismatch_in_sample": len(mismatched_pks)
            }, indent=2)
        else:
             return json.dumps({
                "status": "PASS",
                "message": f"All records that are NULL in the target are also NULL in the source. Data is consistent.",
                "verified_null_count": len(pks)
            }, indent=2)           
    except Exception as e:
        return f"Hata: {str(e)}"

if __name__ == "__main__":
    mcp.run(transport="stdio")
