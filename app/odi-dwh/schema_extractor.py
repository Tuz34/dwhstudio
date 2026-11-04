import sys
from rag_manager import add_document, search_documents, _load_db, _save_db
from db import get_user_tables, get_table_schema, test_connection, get_table_constraints, get_table_comment

def get_table_description(table_name: str) -> str:
    db_comment = get_table_comment(table_name)
    if db_comment:
        return db_comment
        
    name = table_name.lower()
    if 'product' in name or 'urun' in name:
        return 'DWH product dimension table. It contains product master data, categories, and pricing information. Used in product analysis.'
    elif 'customer' in name or 'musteri' in name:
        return 'DWH customer dimension table. It contains customer demographic information. Used in customer analysis and segmentation.'
    elif 'sales' in name or 'satis' in name:
        return 'DWH sales fact table. It contains sales metrics (amount, quantity, date) that can be JOINed with customer and product dimensions.'
    elif 'order' in name or 'siparis' in name:
        return 'Order fact/detail table. It contains detailed timing, quantity, and amount information for placed orders.'
    elif 'emp' in name or 'employee' in name or 'calisan' in name:
        return 'Contains employee (personnel) information, salaries, and department relationships.'
    elif 'dept' in name or 'department' in name or 'departman' in name:
        return 'Contains company department information.'
    elif 'dim' in name:
        return 'DWH dimension table. It contains detail and breakdown (master data) information for the relevant analysis.'
    elif 'fact' in name:
        return 'DWH fact table. It contains analytical/aggregate data such as measures, metrics, amounts, and quantities.'
    else:
        return 'Enterprise DWH table.'

import re
import pandas as pd

def extract_and_store_schema():
    print("Checking the database connection...")
    if test_connection():
        print("Connected to the database. Fetching schema information...")
        tables = get_user_tables()
        
        # Clear existing schema information to prevent old duplicate records.
        db = _load_db()
        db["schema_metadata"] = []
        _save_db(db)
        
        for tbl in tables:
            try:
                df = get_table_schema(tbl)
                # Fetch constraints.
                cons_df = get_table_constraints(tbl)
                pk_cols = cons_df[cons_df['CONSTRAINT_TYPE'] == 'P']['COLUMN_NAME'].tolist() if not cons_df.empty else []
                fk_cols = cons_df[cons_df['CONSTRAINT_TYPE'] == 'R']['COLUMN_NAME'].tolist() if not cons_df.empty else []
                u_cols = cons_df[cons_df['CONSTRAINT_TYPE'] == 'U']['COLUMN_NAME'].tolist() if not cons_df.empty else []
                
                cols_list = []
                description = get_table_description(tbl)

                for _, r in df.iterrows():
                    props = []
                    col = r['COLUMN_NAME']
                    # Use FULL_DATA_TYPE from db.py
                    dtype = r.get('FULL_DATA_TYPE', r['DATA_TYPE'])
                    
                    if col in pk_cols:
                        props.append("PK")
                    if col in fk_cols:
                        props.append("FK")
                    if col in u_cols:
                        props.append("IDX")
                        
                    if str(dtype).startswith('LOB') or (str(dtype).startswith('VARCHAR2') and int(r.get('DATA_LENGTH', 0)) >= 4000):
                        props.append("COMPRESS")
                        
                    prop_str = f" [{','.join(props)}]" if props else ""
                    
                    # Handle comments safely — read only from the database.
                    comment = r.get('COMMENTS')
                    if pd.isna(comment) or not comment or str(comment).lower().strip() == 'nan':
                        comment_val = "N / A"
                    else:
                        comment_val = str(comment).strip()
                    
                    comment_str = f" {{{comment_val}}}"
                    cols_list.append(f"{col} {dtype}{prop_str}{comment_str}")

                cols_str = " | ".join(cols_list)
                
                doc_text = f"Table Name: {tbl}\nColumns: {cols_str}\nDescription: {description}"
                
                # Add to RAG.
                doc_id = add_document(
                    collection_name="schema_metadata",
                    document_text=doc_text,
                    metadata={"table_name": tbl, "source": "oracle_db"}
                )
                print(f"Added: {tbl} (ID: {doc_id})")
            except Exception as e:
                print(f"Error ({tbl}): {e}")
    else:
        print("WARNING: Could not connect to the real Oracle database!")
        print("Loading the demonstration (mock) DWH schema into SimpleRAG...")
        
        mock_tables = [
            {
                "name": "DWH.DIM_CUSTOMER", 
                "text": "Table Name: DWH.DIM_CUSTOMER\nColumns: CUSTOMER_ID NUMBER [PK,IDX] {N / A}, FIRST_NAME VARCHAR2 {N / A}, LAST_NAME VARCHAR2 {N / A}, EMAIL VARCHAR2 [IDX] {N / A}, REGION VARCHAR2 {N / A}, CREATED_AT DATE {N / A}\nDescription: DWH Customer Dimension Table. Contains customer demographic information."
            },
            {
                "name": "DWH.DIM_PRODUCT", 
                "text": "Table Name: DWH.DIM_PRODUCT\nColumns: PRODUCT_ID NUMBER [PK] {N / A}, PRODUCT_NAME VARCHAR2 {N / A}, CATEGORY VARCHAR2 {N / A}, PRICE NUMBER {N / A}, STOCK_QUANTITY NUMBER {N / A}, FULL_DESCRIPTION VARCHAR2 [COMPRESS] {N / A}\nDescription: DWH Product Dimension Table. Contains basic product information and prices."
            },
            {
                "name": "DWH.FACT_SALES", 
                "text": "Table Name: DWH.FACT_SALES\nColumns: SALE_ID NUMBER [PK] {N / A}, CUSTOMER_ID NUMBER [FK,IDX] {N / A}, PRODUCT_ID NUMBER [FK,IDX] {N / A}, SALE_DATE DATE {N / A}, QUANTITY NUMBER {N / A}, TOTAL_AMOUNT NUMBER {N / A}\nDescription: DWH Sales Fact Table. Stores all sales transactions. Can be JOINed with Customer (DIM_CUSTOMER) and Product (DIM_PRODUCT)."
            }
        ]
        
        # Clear the collection to start fresh.
        db = _load_db()
        db["schema_metadata"] = []
        _save_db(db)
        
        for t in mock_tables:
            doc_id = add_document(
                collection_name="schema_metadata",
                document_text=t["text"],
                metadata={"table_name": t["name"], "source": "mock_data"}
            )
            print(f"Mock added: {t['name']} (ID: {doc_id})")

if __name__ == "__main__":
    extract_and_store_schema()
    print("\nOperation completed. SimpleRAG 'schema_metadata' updated.")
