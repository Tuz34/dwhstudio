import json
import logging
import threading
import time
import uuid
import os
from typing import Any

from config import OLLAMA_EMBEDDINGS_URL

logger = logging.getLogger("rag_manager")

# Python 3.14 üzerinde Pydantic V1 (ChromaDB) uyumsuzluk hatası aldığımız için,
# çok daha hafif, hatasız ve yerel çalışan bir JSON tabanlı SimpleRAG (Vektör Motoru Alternatifi) kullanıyoruz.
CHROMA_DB_FILE = os.path.join(os.path.dirname(__file__), "rag_database.json")

# O1: Eş zamanlı yazma işlemlerinden kaynaklanan race condition'ı önlemek için kilit
_db_lock = threading.Lock()

# M-1: In-memory cache — her sorguda 8.3MB JSON yeniden okunmasın
_CACHE_TTL = 60  # saniye
_cache: dict = {"db": None, "ts": 0.0}

COLLECTIONS = [
    "schema_metadata",
    "odi_patterns",
    "dwh_modeling",
    "business_rules",
    "error_patterns",
    "mapping_runs"
]


class RagStoreError(RuntimeError):
    """Base error for local RAG store availability and integrity failures."""


class RagStoreCorruptionError(RagStoreError):
    """Raised when an existing RAG store cannot be decoded safely."""

def _load_db() -> dict[str, list]:
    now = time.time()
    if _cache["db"] is not None and (now - _cache["ts"]) < _CACHE_TTL:
        return _cache["db"]
    if os.path.exists(CHROMA_DB_FILE):
        with open(CHROMA_DB_FILE, "r", encoding="utf-8") as f:
            try:
                db = json.load(f)
                _cache["db"] = db
                _cache["ts"] = now
                return db
            except json.JSONDecodeError as exc:
                logger.error("RAG store is corrupt: %s", type(exc).__name__)
                raise RagStoreCorruptionError("Local RAG store is corrupt; existing content was preserved.") from exc
            except OSError as exc:
                logger.error("RAG store is unavailable: %s", type(exc).__name__)
                raise RagStoreError("Local RAG store is unavailable.") from exc
    db = {c: [] for c in COLLECTIONS}
    _cache["db"] = db
    _cache["ts"] = now
    return db

def _save_db(db: dict[str, list]) -> None:
    target_path = os.path.abspath(CHROMA_DB_FILE)
    parent = os.path.dirname(target_path)
    os.makedirs(parent, exist_ok=True)
    temp_path = f"{target_path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(db, handle, ensure_ascii=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target_path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    # Cache only data that is durably visible at the destination path.
    _cache["db"] = db
    _cache["ts"] = time.time()

def init_collections():
    _save_db(_load_db())

def get_embedding(text: str) -> list[float]:
    """N-1: Timeout 5s, 3 deneme — Ollama idle/restart sonrası düşen embedding isteklerini tolere eder."""
    import requests
    payload = {"model": "nomic-embed-text:latest", "prompt": text}
    try:
        r = requests.post(os.getenv("OLLAMA_EMBEDDINGS_URL") or OLLAMA_EMBEDDINGS_URL, json=payload, timeout=(0.5, 1.0))
        emb = r.json().get("embedding", [])
        if emb:
            return emb
    except Exception as e:
        logger.debug("Embedding istegi basarisiz; lexical RAG fallback kullanilacak: %s", e)
    return []

def cosine_similarity(v1: list[float], v2: list[float]) -> float:
    import math
    if not v1 or not v2: return 0.0
    dot_product = sum(a * b for a, b in zip(v1, v2))
    norm_v1 = math.sqrt(sum(a * a for a in v1))
    norm_v2 = math.sqrt(sum(b * b for b in v2))
    if norm_v1 == 0 or norm_v2 == 0: return 0.0
    return dot_product / (norm_v1 * norm_v2)

def add_document(collection_name: str, document_text: str, metadata: dict[str, Any] | None = None, doc_id: str | None = None) -> str:
    # Embedding hesaplaması lock dışında yapılır (ağ çağrısı, kilitli tutmaya gerek yok)
    embedding = get_embedding(document_text)
    doc = {
        "id": doc_id or str(uuid.uuid4()),
        "text": document_text,
        "metadata": metadata or {},
        "embedding": embedding
    }

    # O1: Yazma işlemi kilit altında — aynı anda iki istek JSON'u bozmasın
    with _db_lock:
        db = _load_db()
        if collection_name not in db:
            db[collection_name] = []

        # ID varsa güncelle, yoksa ekle
        for i, existing in enumerate(db[collection_name]):
            if existing["id"] == doc["id"]:
                db[collection_name][i] = doc
                _save_db(db)
                return doc["id"]

        db[collection_name].append(doc)
        _save_db(db)
    return doc["id"]

def delete_document(collection_name: str, doc_id: str) -> bool:
    # O1: Yazma işlemi kilit altında
    with _db_lock:
        db = _load_db()
        if collection_name not in db:
            return False

        original_length = len(db[collection_name])
        db[collection_name] = [doc for doc in db[collection_name] if doc.get("id") != doc_id]

        if len(db[collection_name]) < original_length:
            _save_db(db)
            return True
    return False

def search_documents(collection_name: str, query: str, n_results: int = 3, query_emb: list[float] | None = None) -> list[str]:
    db = _load_db()
    items = db.get(collection_name, [])
    if not items:
        return []
    
    # Gerçek Semantik Arama (Mock search devreden çıkarıldı)
    if query_emb is None:
        query_emb = get_embedding(query)
    scored = []
    
    if query_emb:
        for item in items:
            item_emb = item.get("embedding", [])
            # Cosine similarity ile uzaklığı tespit et
            score = cosine_similarity(query_emb, item_emb) if item_emb else 0.0
            
            # "Tablo" veya "Table" kelimesi geciyorsa ve bu bir mapping run kaydiysa skoru artir
            if ("tablo" in query.lower() or "table" in query.lower()) and collection_name == "mapping_runs":
                score += 0.2

            # Tablo adi birebir cumlede geciyorsa skorunu ucur
            m = item.get("metadata", {})
            table_keys = ["table_name", "src_table", "tgt_table", "mapping_name"]
            for k in table_keys:
                if k in m and str(m[k]).lower() in query.lower():
                    score += 0.4
                    break
                    
            scored.append((score, item["text"]))
    else:
        # Fallback (Embedding servisi çökerse kelime bazlı ilkel arama)
        query_words = set(query.lower().split())
        for item in items:
            text_lower = item["text"].lower()
            score = sum(1 for w in query_words if w in text_lower)
            if "table_name" in item["metadata"] and item["metadata"]["table_name"].lower() in query_words:
                score += 10
            scored.append((score, item["text"]))
        
    scored.sort(key=lambda x: x[0], reverse=True)
    
    if query_emb:
        # 0.35 threshold (ilkisiz sonuçlar RAG'ı kirletmesin diye filtreliyoruz)
        return [s[1] for s in scored[:n_results] if s[0] > 0.35]
    else:
        return [s[1] for s in scored[:n_results] if s[0] > 0]

def get_rag_context(user_query: str) -> str:
    context_parts = []
    try:
        query_emb = get_embedding(user_query)
        # İş kuralları (en kritik — tablo seçimi, JOIN kuralları, güvenlik)
        business = search_documents("business_rules", user_query, n_results=3, query_emb=query_emb)
        if business:
            context_parts.append("--- İŞ KURALLARI ---\n" + "\n".join(business))

        # Şema metadata (tablo yapısı, kolon bilgisi)
        schema = search_documents("schema_metadata", user_query, n_results=3, query_emb=query_emb)
        if schema:
            context_parts.append("--- İLGİLİ TABLO ŞEMALARI ---\n" + "\n".join(schema))

        # DWH modelleme kuralları
        modeling_rules = search_documents("dwh_modeling", user_query, n_results=2, query_emb=query_emb)
        if modeling_rules:
            context_parts.append("--- DWH MODELLEME KURALLARI ---\n" + "\n".join(modeling_rules))

        # Hata pattern'ları (benzer hata daha önce yaşandıysa çözümü getir)
        errors = search_documents("error_patterns", user_query, n_results=1, query_emb=query_emb)
        if errors:
            context_parts.append("--- BİLİNEN HATA ÇÖZÜMLERİ ---\n" + "\n".join(errors))

        # ODI pattern'ları (mapping soruları için)
        patterns = search_documents("odi_patterns", user_query, n_results=1, query_emb=query_emb)
        if patterns:
            context_parts.append("--- ODI MAPPING ÖRNEKLERİ ---\n" + "\n".join(patterns))

        # Gecmis Mapping Runlari (bu mapping nasil yapildi sorulari icin)
        runs = search_documents("mapping_runs", user_query, n_results=2, query_emb=query_emb)
        if runs:
            context_parts.append("--- GEÇMİŞ MAPPING ÇALIŞTIRMA KAYITLARI ---\n" + "\n".join(runs))

    except Exception as e:
        logger.error("RAG arama hatasi: %s", e)

    return "\n\n".join(context_parts)

if __name__ == "__main__":
    init_collections()
    print("SimpleRAG altyapısı hazır!")
    print(f"Veri Dosyası: {CHROMA_DB_FILE}")
