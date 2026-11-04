import sys
from rag_manager import add_document, search_documents, _load_db, _save_db
from db import get_user_tables, get_table_schema, test_connection, get_table_constraints, get_table_comment

def get_table_description(table_name: str) -> str:
    db_comment = get_table_comment(table_name)
    if db_comment:
        return db_comment
        
    name = table_name.lower()
    if 'product' in name or 'urun' in name:
        return 'DWH Ürün boyut (dimension) tablosudur. Ürün ana verilerini, kategorilerini ve fiyat bilgilerini içerir. Ürün analizlerinde kullanılır.'
    elif 'customer' in name or 'musteri' in name:
        return 'DWH Müşteri boyut (dimension) tablosudur. Müşteri demografik bilgilerini içerir. Müşteri analizleri ve segmentasyon için kullanılır.'
    elif 'sales' in name or 'satis' in name:
        return 'DWH Satış gerçek (fact) tablosudur. Müşteri ve ürün boyutlarıyla JOIN yapılabilecek satış metrikleri (tutar, miktar, tarih) içerir.'
    elif 'order' in name or 'siparis' in name:
        return 'Sipariş (fact/detay) tablosudur. Verilen siparişlerin zaman, miktar ve tutar bilgilerini detaylı olarak barındırır.'
    elif 'emp' in name or 'calisan' in name:
        return 'Çalışan (personel) bilgilerini, maaşlarını ve departman ilişkilerini içerir.'
    elif 'dept' in name or 'departman' in name:
        return 'Şirket departman (bölüm) bilgilerini içerir.'
    elif 'dim' in name:
        return 'DWH Boyut (dimension) tablosudur. İlgili analizin detay ve kırılım (master data) bilgilerini içerir.'
    elif 'fact' in name:
        return 'DWH Gerçek (fact) tablosudur. Ölçüm (measure), metrik, tutar ve miktar gibi analitik/toplamsal verileri içerir.'
    else:
        return 'Kurumsal DWH tablosudur.'

import re
import pandas as pd

def extract_and_store_schema():
    print("Veritabanı bağlantısı kontrol ediliyor...")
    if test_connection():
        print("Veritabanına bağlanıldı. Şema bilgileri çekiliyor...")
        tables = get_user_tables()
        
        # Eski (duplicate) kayıtları engellemek için mevcut şema bilgisini temizle
        db = _load_db()
        db["schema_metadata"] = []
        _save_db(db)
        
        for tbl in tables:
            try:
                df = get_table_schema(tbl)
                # Sınırlamaları (Constraints) çek
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
                    
                    # Handle comments safely - sadece DB'den oku
                    comment = r.get('COMMENTS')
                    if pd.isna(comment) or not comment or str(comment).lower().strip() == 'nan':
                        comment_val = "N / A"
                    else:
                        comment_val = str(comment).strip()
                    
                    comment_str = f" {{{comment_val}}}"
                    cols_list.append(f"{col} {dtype}{prop_str}{comment_str}")

                cols_str = " | ".join(cols_list)
                
                doc_text = f"Tablo Adı: {tbl}\nKolonlar: {cols_str}\nAçıklama: {description}"
                
                # RAG'a ekle
                doc_id = add_document(
                    collection_name="schema_metadata",
                    document_text=doc_text,
                    metadata={"table_name": tbl, "source": "oracle_db"}
                )
                print(f"Eklendi: {tbl} (ID: {doc_id})")
            except Exception as e:
                print(f"Hata ({tbl}): {e}")
    else:
        print("UYARI: Gerçek Oracle veritabanına bağlanılamadı!")
        print("Demonstrasyon (Mock) DWH şeması SimpleRAG'a yükleniyor...")
        
        mock_tables = [
            {
                "name": "DWH.DIM_CUSTOMER", 
                "text": "Tablo Adı: DWH.DIM_CUSTOMER\nKolonlar: CUSTOMER_ID NUMBER [PK,IDX] {N / A}, FIRST_NAME VARCHAR2 {N / A}, LAST_NAME VARCHAR2 {N / A}, EMAIL VARCHAR2 [IDX] {N / A}, REGION VARCHAR2 {N / A}, CREATED_AT DATE {N / A}\nAçıklama: DWH Müşteri Boyut (Dimension) Tablosu. Müşterilerin demografik bilgilerini içerir."
            },
            {
                "name": "DWH.DIM_PRODUCT", 
                "text": "Tablo Adı: DWH.DIM_PRODUCT\nKolonlar: PRODUCT_ID NUMBER [PK] {N / A}, PRODUCT_NAME VARCHAR2 {N / A}, CATEGORY VARCHAR2 {N / A}, PRICE NUMBER {N / A}, STOCK_QUANTITY NUMBER {N / A}, FULL_DESCRIPTION VARCHAR2 [COMPRESS] {N / A}\nAçıklama: DWH Ürün Boyut (Dimension) Tablosu. Ürünlerin temel bilgilerini ve fiyatlarını içerir."
            },
            {
                "name": "DWH.FACT_SALES", 
                "text": "Tablo Adı: DWH.FACT_SALES\nKolonlar: SALE_ID NUMBER [PK] {N / A}, CUSTOMER_ID NUMBER [FK,IDX] {N / A}, PRODUCT_ID NUMBER [FK,IDX] {N / A}, SALE_DATE DATE {N / A}, QUANTITY NUMBER {N / A}, TOTAL_AMOUNT NUMBER {N / A}\nAçıklama: DWH Satış Gerçek (Fact) Tablosu. Tüm satış işlemlerini tutar. Müşteri (DIM_CUSTOMER) ve Ürün (DIM_PRODUCT) ile JOIN yapılabilir."
            }
        ]
        
        # Koleksiyonu temizle (yeni baştan)
        db = _load_db()
        db["schema_metadata"] = []
        _save_db(db)
        
        for t in mock_tables:
            doc_id = add_document(
                collection_name="schema_metadata",
                document_text=t["text"],
                metadata={"table_name": t["name"], "source": "mock_data"}
            )
            print(f"Mock Eklendi: {t['name']} (ID: {doc_id})")

if __name__ == "__main__":
    extract_and_store_schema()
    print("\nİşlem Tamamlandı. SimpleRAG 'schema_metadata' güncellendi.")
