from mcp.server.fastmcp import FastMCP
import db
import pandas as pd
import logging
import json
from source_connector import get_source_connector

# Logger ayari
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
    """Aktif kaynak connector ve hedef Oracle tablolarinin satir sayilarini karsilastirir."""
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
    """Sayisal bir kolonun toplamlarinin (Source vs Target) esitligini kontrol eder."""
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
    """Kaynak tabloda olup hedef tabloda olmayan (eksik) kayitlari anahtar kolon uzerinden bulur."""
    try:
        # Bu islem icin MSSQL'deki tum anahtarları cekiyoruz (buyuk tablolarda limitli olmali)
        # Ancak burada basitlik icin direkt query atiyoruz
        source_sql = _source_limit_clause(
            f"SELECT {_q_oracle(key_column)} FROM {_q_source_table(src_table)}",
            limit * 100
        )
        source_keys = _execute_source(source_sql)
        
        # Oracle anahtarlari
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
    """Belirtilen kolon(lar) bazinda mukerrer (duplicate) kayit kontrolu yapar. (Oracle uzerinde)"""
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
    """Belirtilen kolondaki verilerin (ozellikle NULL durumunun) kaynak ve hedefte satır satır PK uzerinden ayni olup olmadigini kontrol eder."""
    try:
        # Hedef (Oracle) tabloda ilgili kolonun bos (NULL) oldugu ornek PK'lari cek:
        oracle_df = db.execute_select(
            f"SELECT {_q_oracle(pk_col)} FROM {tgt_table} "
            f"WHERE {_q_oracle(col_to_check)} IS NULL AND ROWNUM <= 100"
        )
        if oracle_df.empty:
            return json.dumps({
                "status": "PASS",
                "message": f"Hedef tabloda ({tgt_table}) {col_to_check} kolonu bos hicbir kayit bulunamadi. (Limitli test)"
            }, indent=2)
            
        # PK listesini cikar
        pks = oracle_df[pk_col.upper()].astype(str).tolist()
        pk_list_str = ",".join(f"'{p}'" for p in pks)
        
        # Bu PK'lar kaynak (MSSQL) uzerinde ne alemde? Orada da bos mu?
        source_df = _execute_source(
            f"SELECT {_q_oracle(pk_col)}, {_q_oracle(col_to_check)} "
            f"FROM {_q_source_table(src_table)} WHERE {_q_oracle(pk_col)} IN ({pk_list_str})"
        )
        
        # MSSQL'de dolu olanlari (NULL olmayanlari) bul
        mismatched_pks = []
        for index, row in source_df.iterrows():
            val = row[col_to_check.upper()]
            if pd.notna(val) and val != '':
                mismatched_pks.append(row[pk_col.upper()])
                
        if mismatched_pks:
            return json.dumps({
                "status": "FAIL (MISMATCH)",
                "message": f"Hedefte NULL olan ama kaynakta DOLU olan kayitlar tespit edildi!",
                "mismatched_pk_samples": mismatched_pks[:10],
                "total_mismatch_in_sample": len(mismatched_pks)
            }, indent=2)
        else:
             return json.dumps({
                "status": "PASS",
                "message": f"Hedef tabloda NULL olan kayitlarin tamami kaynakta da NULL. Veri tutarli.",
                "verified_null_count": len(pks)
            }, indent=2)           
    except Exception as e:
        return f"Hata: {str(e)}"

if __name__ == "__main__":
    mcp.run(transport="stdio")
