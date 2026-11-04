"""
Mapping Graph — LangGraph Tabanlı Mapping Orkestratörü
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Mevcut MappingGenerator pipeline'ını LangGraph durum makinesi olarak
yeniden yapılandırır. Avantajlar:
  - Checkpoint: Çökerse kaldığı adımdan devam
  - Dallanma: SKEY gerekli mi? → koşullu yol
  - Hata yönetimi: Her node'da hata → error_handler'a dallanma
  - Human-in-the-loop: Onay adımı (UI Wizard ile entegre)

Mevcut MappingGenerator'daki iş mantığı (metotlar) aynen kullanılır.
Bu modül sadece orkestrasyon akışını yönetir.

Kullanım:
    from mapping_graph import run_mapping_graph, get_graph

    # Tek seferlik çalıştırma
    result = run_mapping_graph(user_prompt="Customer tablosunu ODS'ye aktar")

    # Veya parametrelerle
    result = run_mapping_graph(params={
        "mapping_name": "MAP_SALES_CUSTOMER_TO_ODS",
        "src_model": "SQL_SERVER",
        "src_table": "Sales.Customer",
        "tgt_model": "ORACLE_ODS",
        "tgt_table": "ODS_SALES_CUSTOMER",
        "tgt_owner": "DWH",
        "needs_skey": False,
    })

    # Human-in-the-loop ile (UI Wizard entegrasyonu)
    result = run_mapping_graph(
        params={...},
        require_approval=True,   # preview adımında durur
    )
"""

import logging
import uuid
from datetime import datetime
from typing import Any, Literal

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from typing_extensions import TypedDict

logger = logging.getLogger("mapping_graph")


def _persist_graph_state(graph, config: dict, thread_id: str | None = None) -> None:
    """Persist latest LangGraph state for other modules.

    LangGraph's MemorySaver keeps checkpoints only in process memory. This
    writes the current state to Oracle so DQ, Impact, ODI sync and UI can read
    a durable lifecycle record.
    """
    try:
        from mapping_state_manager import MappingStateManager

        raw_values = graph.get_state(config).values
        state = dict(raw_values) if raw_values else {}
        if not state:
            logger.warning("Graph state bos doniyor — checkpoint okunamadi (thread=%s).", thread_id)
            return
        with MappingStateManager() as msm:
            msm.upsert_state(state, thread_id=thread_id)
    except Exception as exc:
        # Tani: yetki, baglanti, sema, vs. gibi sorunlar gorunsun
        logger.error("Graph state persist edilemedi (thread=%s): %s", thread_id, exc, exc_info=True)

# ═══════════════════════════════════════════════════════════════════════════════
# STATE SCHEMA — Tüm adımlar arasında paylaşılan durum
# ═══════════════════════════════════════════════════════════════════════════════

class MappingState(TypedDict, total=False):
    """LangGraph durum nesnesi. Her node bu state'i okur/yazar."""
    # ── Giriş ──
    user_prompt: str               # Doğal dil isteği (opsiyonel)
    raw_params: dict               # Kullanıcının verdiği parametreler
    require_approval: bool         # True ise preview'da durur

    # ── Pipeline durumu ──
    batch_id: str                  # İşlem batch ID'si
    current_step: str              # Aktif adım adı
    validated_params: dict         # Doğrulanmış parametreler

    # ── Adım sonuçları ──
    parse_result: dict             # Prompt parse sonucu
    validate_result: dict          # Doğrulama sonucu
    target_table_result: dict      # Hedef tablo oluşturma sonucu
    datastore_result: dict         # ODI datastore kayıt sonucu
    mapping_result: dict           # ODI mapping oluşturma sonucu
    skey_result: dict              # SKEY oluşturma sonucu
    lineage_result: dict           # Lineage kayıt sonucu
    test_result: dict              # DQ test üretim sonucu
    snapshot_result: dict          # Snapshot sonucu

    # ── Kontrol ──
    approval_status: str           # "pending" / "approved" / "rejected"
    overall_status: str            # "running" / "success" / "failed"
    error: str | None              # Son hata mesajı
    log: list[dict]                # Tüm adım logları
    completed_steps: list[str]     # Tamamlanan adım isimleri


# ═══════════════════════════════════════════════════════════════════════════════
# NODE FONKSİYONLARI — Her biri bir graph node'u
# ═══════════════════════════════════════════════════════════════════════════════

def _add_log(state: MappingState, step: str, status: str, detail: str = "") -> list[dict]:
    """Log listesine yeni giriş ekler."""
    logs = list(state.get("log", []))
    logs.append({
        "step": step,
        "status": status,
        "detail": detail,
        "timestamp": datetime.now().isoformat(),
    })
    return logs


def _target_table_detail(result: dict) -> str:
    """Build a user-facing summary for the target table step."""
    table = result.get("table", "target table")
    status = result.get("status", "SUCCESS")
    columns_count = result.get("columns_count")
    defaults_added = result.get("default_columns_added") or []
    added_columns = result.get("added_columns") or []

    parts = [f"{table}: {status}"]
    if columns_count is not None:
        parts.append(f"{columns_count} source columns")
    if defaults_added:
        parts.append(f"{len(defaults_added)} default columns")
    if status in ("RECONCILED", "ALREADY_IN_SYNC"):
        parts.append(f"{len(added_columns)} missing columns added")
    if status == "CREATED":
        parts.append("DDL executed")
        parts.append("grants processed")
    return "; ".join(parts)


def _repository_step_status(result: dict) -> tuple[str, str]:
    """Summarize ODI repository registration output for the graph log."""
    src_result = str(result.get("src_result") or "")
    tgt_result = str(result.get("tgt_result") or "")
    combined = " | ".join(part for part in (src_result, tgt_result) if part)
    lowered = combined.lower()

    if "warning:" in lowered or ("hata:" in lowered and "zaten mevcut" not in lowered):
        return "WARNING", combined[:240]
    return "SUCCESS", combined[:240] or "ODI datastores checked"


def node_init(state: MappingState) -> dict:
    """Pipeline'ı başlatır, batch_id atar."""
    batch_id = state.get("batch_id") or str(uuid.uuid4())[:8]
    logger.info("[%s] Pipeline başlatıldı", batch_id)
    return {
        "batch_id": batch_id,
        "current_step": "init",
        "overall_status": "running",
        "completed_steps": [],
        "log": _add_log(state, "init", "SUCCESS", f"batch_id={batch_id}"),
    }


def node_parse_prompt(state: MappingState) -> dict:
    """Doğal dil prompt'unu parametrelere dönüştürür."""
    from mapping_generator import MappingGenerator

    prompt = state.get("user_prompt", "")
    if not prompt:
        # Prompt yok, direkt raw_params kullan
        return {
            "current_step": "parse_prompt",
            "parse_result": {"skipped": True, "reason": "No prompt, using raw_params"},
            "log": _add_log(state, "parse_prompt", "SKIPPED", "raw_params kullanılıyor"),
        }

    try:
        mg = MappingGenerator()
        parsed = mg.parse_prompt(prompt)
        return {
            "current_step": "parse_prompt",
            "raw_params": parsed,
            "parse_result": {"parsed": True, "mapping_name": parsed.get("mapping_name")},
            "log": _add_log(state, "parse_prompt", "SUCCESS",
                           f"mapping={parsed.get('mapping_name')}"),
        }
    except Exception as e:
        return {
            "current_step": "parse_prompt",
            "overall_status": "failed",
            "error": f"Prompt parse hatası: {e}",
            "log": _add_log(state, "parse_prompt", "FAILED", str(e)),
        }


def node_validate(state: MappingState) -> dict:
    """Parametreleri doğrular ve normalize eder."""
    from mapping_generator import MappingGenerator

    if state.get("overall_status") == "failed":
        return {"overall_status": "failed"}

    raw = state.get("raw_params", {})
    if not raw:
        return {
            "overall_status": "failed",
            "error": "Parametre bulunamadı",
            "log": _add_log(state, "validate", "FAILED", "raw_params boş"),
        }

    try:
        mg = MappingGenerator()
        validated = mg.validate_params(raw)
        return {
            "current_step": "validate",
            "validated_params": validated,
            "validate_result": {"valid": True},
            "log": _add_log(state, "validate", "SUCCESS",
                           f"{validated['src_table']} → {validated['tgt_table']}"),
            "completed_steps": list(state.get("completed_steps", [])) + ["validate"],
        }
    except ValueError as e:
        return {
            "current_step": "validate",
            "overall_status": "failed",
            "error": f"Doğrulama hatası: {e}",
            "log": _add_log(state, "validate", "FAILED", str(e)),
        }


def node_preview(state: MappingState) -> dict:
    """Onay bekleyen preview adımı. require_approval=True ise durur."""
    if state.get("overall_status") == "failed":
        return {"overall_status": "failed"}

    params = state.get("validated_params", {})
    if state.get("require_approval", False):
        return {
            "current_step": "preview",
            "approval_status": "pending",
            "log": _add_log(state, "preview", "WAITING",
                           f"Kullanıcı onayı bekleniyor: {params.get('mapping_name')}"),
        }

    # Otomatik onay
    return {
        "current_step": "preview",
        "approval_status": "approved",
        "log": _add_log(state, "preview", "AUTO_APPROVED", "require_approval=False"),
        "completed_steps": list(state.get("completed_steps", [])) + ["preview"],
    }


def node_create_table(state: MappingState) -> dict:
    """Hedef Oracle tablosunu oluşturur."""
    from mapping_generator import MappingGenerator

    if state.get("overall_status") == "failed":
        return {"overall_status": "failed"}

    params = state.get("validated_params", {})
    try:
        mg = MappingGenerator()
        mg._batch_id = state.get("batch_id", "unknown")
        result = mg._create_target_table(params)
        return {
            "current_step": "create_table",
            "target_table_result": result,
            # Eğer create_table src_table'ı güncellerse (full_src_name tespiti)
            "validated_params": params,
            "log": _add_log(state, "create_table", result.get("status", "SUCCESS"),
                           _target_table_detail(result)),
            "completed_steps": list(state.get("completed_steps", [])) + ["create_table"],
        }
    except Exception as e:
        return {
            "current_step": "create_table",
            "overall_status": "failed",
            "error": f"Tablo oluşturma hatası: {e}",
            "log": _add_log(state, "create_table", "FAILED", str(e)),
        }


def node_register_datastores(state: MappingState) -> dict:
    """ODI'da kaynak ve hedef datastore'ları kaydeder."""
    from mapping_generator import MappingGenerator

    if state.get("overall_status") == "failed":
        return {"overall_status": "failed"}

    params = state.get("validated_params", {})
    try:
        mg = MappingGenerator()
        mg._batch_id = state.get("batch_id", "unknown")
        result = mg._register_odi_datastores(params)
        repo_status, repo_detail = _repository_step_status(result)
        return {
            "current_step": "register_datastores",
            "datastore_result": result,
            "log": _add_log(state, "register_datastores", repo_status, repo_detail),
            "completed_steps": list(state.get("completed_steps", [])) + ["register_datastores"],
        }
    except Exception as e:
        return {
            "current_step": "register_datastores",
            "overall_status": "failed",
            "error": f"Datastore kayıt hatası: {e}",
            "log": _add_log(state, "register_datastores", "FAILED", str(e)),
        }


def node_create_mapping(state: MappingState) -> dict:
    """ODI mapping'i oluşturur."""
    from mapping_generator import MappingGenerator

    if state.get("overall_status") == "failed":
        return {"overall_status": "failed"}

    params = state.get("validated_params", {})
    try:
        mg = MappingGenerator()
        mg._batch_id = state.get("batch_id", "unknown")
        result = mg._create_odi_mapping(params)
        return {
            "current_step": "create_mapping",
            "mapping_result": result,
            "log": _add_log(state, "create_mapping", "SUCCESS",
                           result.get("odi_result", "")[:80]),
            "completed_steps": list(state.get("completed_steps", [])) + ["create_mapping"],
        }
    except Exception as e:
        return {
            "current_step": "create_mapping",
            "overall_status": "failed",
            "error": f"Mapping oluşturma hatası: {e}",
            "log": _add_log(state, "create_mapping", "FAILED", str(e)),
        }


def node_create_skey(state: MappingState) -> dict:
    """Surrogate Key nesnelerini oluşturur."""
    from mapping_generator import MappingGenerator

    if state.get("overall_status") == "failed":
        return {"overall_status": "failed"}

    params = state.get("validated_params", {})
    try:
        mg = MappingGenerator()
        mg._batch_id = state.get("batch_id", "unknown")
        result = mg._create_skey(params)
        return {
            "current_step": "create_skey",
            "skey_result": result,
            "log": _add_log(state, "create_skey", "SUCCESS", "SKEY oluşturuldu"),
            "completed_steps": list(state.get("completed_steps", [])) + ["create_skey"],
        }
    except Exception as e:
        # SKEY hatası kritik değil
        return {
            "current_step": "create_skey",
            "skey_result": {"error": str(e)},
            "log": _add_log(state, "create_skey", "WARNING", str(e)),
            "completed_steps": list(state.get("completed_steps", [])) + ["create_skey"],
        }


def node_skip_skey(state: MappingState) -> dict:
    """SKEY gerekmiyorsa atla."""
    return {
        "current_step": "skip_skey",
        "skey_result": {"skipped": True},
        "log": _add_log(state, "create_skey", "SKIPPED", "SKEY istenmedi"),
        "completed_steps": list(state.get("completed_steps", [])) + ["skip_skey"],
    }


def node_post_steps(state: MappingState) -> dict:
    """Lineage, DQ test ve snapshot adımlarını çalıştırır."""
    from mapping_generator import MappingGenerator

    if state.get("overall_status") == "failed":
        return {"overall_status": "failed"}

    params = state.get("validated_params", {})
    mg = MappingGenerator()
    mg._batch_id = state.get("batch_id", "unknown")

    results = {}
    logs = list(state.get("log", []))
    completed = list(state.get("completed_steps", []))

    # Lineage
    try:
        lr = mg._populate_lineage(params)
        results["lineage_result"] = lr
        logs.append({"step": "lineage", "status": "SUCCESS",
                     "detail": f"inserted={lr.get('lineage_result', {}).get('inserted', 0)}",
                     "timestamp": datetime.now().isoformat()})
        completed.append("lineage")
    except Exception as e:
        results["lineage_result"] = {"error": str(e)}
        logs.append({"step": "lineage", "status": "WARNING", "detail": str(e),
                     "timestamp": datetime.now().isoformat()})

    # DQ Tests
    try:
        tr = mg._generate_tests(params)
        results["test_result"] = tr
        logs.append({"step": "tests", "status": "SUCCESS",
                     "detail": f"{len(tr.get('tests', []))} test",
                     "timestamp": datetime.now().isoformat()})
        completed.append("tests")
    except Exception as e:
        results["test_result"] = {"error": str(e)}
        logs.append({"step": "tests", "status": "WARNING", "detail": str(e),
                     "timestamp": datetime.now().isoformat()})

    # Snapshot
    try:
        sr = mg._take_snapshot(params)
        results["snapshot_result"] = sr
        logs.append({"step": "snapshot", "status": "SUCCESS",
                     "detail": f"snap_id={sr.get('snap_id')}",
                     "timestamp": datetime.now().isoformat()})
        completed.append("snapshot")
    except Exception as e:
        results["snapshot_result"] = {"error": str(e)}
        logs.append({"step": "snapshot", "status": "WARNING", "detail": str(e),
                     "timestamp": datetime.now().isoformat()})

    results["current_step"] = "post_steps"
    results["log"] = logs
    results["completed_steps"] = completed
    return results


def node_finalize(state: MappingState) -> dict:
    """Pipeline'ı tamamlar, MAPPING_LOG'a yazar."""
    from mapping_generator import MappingGenerator

    params = state.get("validated_params", {})
    status = state.get("overall_status", "running")

    if status == "failed":
        final_status = "failed"
    else:
        final_status = "success"

    # MAPPING_LOG'a kaydet
    try:
        mg = MappingGenerator()
        mg._batch_id = state.get("batch_id", "unknown")
        mg._log_to_mapping_log(params, final_status.upper(), state.get("error"))
    except Exception as e:
        logger.warning("MAPPING_LOG yazılamadı: %s", e)

    logger.info("[%s] Pipeline tamamlandı: %s", state.get("batch_id"), final_status)

    return {
        "current_step": "finalize",
        "overall_status": final_status,
        "log": _add_log(state, "finalize", final_status.upper(),
                       f"batch_id={state.get('batch_id')}"),
        "completed_steps": list(state.get("completed_steps", [])) + ["finalize"],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# KOŞULLU DALLANMA FONKSİYONLARI
# ═══════════════════════════════════════════════════════════════════════════════

def should_continue_after_validate(state: MappingState) -> str:
    """Doğrulama sonrası: hata varsa finalize'a git, yoksa preview'a."""
    if state.get("overall_status") == "failed":
        return "finalize"
    return "preview"


def should_continue_after_preview(state: MappingState) -> str:
    """Preview sonrası: onay durumuna göre dallan."""
    approval = state.get("approval_status", "approved")
    if approval == "rejected":
        return "finalize"
    if approval == "pending":
        return "finalize"  # UI'dan onay gelene kadar durur (interrupt)
    return "create_table"


def needs_skey(state: MappingState) -> str:
    """SKEY gerekli mi?"""
    params = state.get("validated_params", {})
    if state.get("overall_status") == "failed":
        return "skip_skey"
    if params.get("needs_skey", False):
        return "create_skey"
    return "skip_skey"


def should_continue_after_mapping(state: MappingState) -> str:
    """Mapping sonrası: hata varsa finalize, yoksa SKEY kontrolü."""
    if state.get("overall_status") == "failed":
        return "finalize"
    return "skey_check"


# ═══════════════════════════════════════════════════════════════════════════════
# GRAPH OLUŞTURMA
# ═══════════════════════════════════════════════════════════════════════════════

def build_mapping_graph() -> StateGraph:
    """LangGraph state machine'i oluşturur.

    Graph yapısı:
    ```
    START → init → parse_prompt → validate ─┬→ preview ─┬→ create_table
                                             │           │   → register_ds
           finalize ←──── (hata) ←───────────┘           │   → create_mapping
              ↑                              (red) ──────┘        │
              │                                              skey_check
              │                                             ┌─────┴─────┐
              │                                        create_skey  skip_skey
              │                                             └─────┬─────┘
              │                                              post_steps
              └──────────────────────────────────────────── finalize → END
    ```
    """
    graph = StateGraph(MappingState)

    # ── Node'ları ekle ──
    graph.add_node("init", node_init)
    graph.add_node("parse_prompt", node_parse_prompt)
    graph.add_node("validate", node_validate)
    graph.add_node("preview", node_preview)
    graph.add_node("create_table", node_create_table)
    graph.add_node("register_datastores", node_register_datastores)
    graph.add_node("create_mapping", node_create_mapping)
    graph.add_node("create_skey", node_create_skey)
    graph.add_node("skip_skey", node_skip_skey)
    graph.add_node("post_steps", node_post_steps)
    graph.add_node("finalize", node_finalize)

    # ── Edge'leri tanımla ──

    # Başlangıç
    graph.add_edge(START, "init")
    graph.add_edge("init", "parse_prompt")
    graph.add_edge("parse_prompt", "validate")

    # Validate sonrası: hata → finalize, başarı → preview
    graph.add_conditional_edges("validate", should_continue_after_validate,
                                {"finalize": "finalize", "preview": "preview"})

    # Preview sonrası: onay → create_table, red → finalize
    graph.add_conditional_edges("preview", should_continue_after_preview,
                                {"create_table": "create_table", "finalize": "finalize"})

    # Sıralı adımlar
    graph.add_edge("create_table", "register_datastores")
    graph.add_edge("register_datastores", "create_mapping")

    # Mapping sonrası: SKEY kontrolü
    graph.add_conditional_edges("create_mapping", should_continue_after_mapping,
                                {"skey_check": "skey_check", "finalize": "finalize"})

    # SKEY dallanma (sanal node — conditional edge olarak)
    graph.add_conditional_edges("skey_check", needs_skey,
                                {"create_skey": "create_skey", "skip_skey": "skip_skey"})

    # SKEY birleşme → post_steps
    graph.add_edge("create_skey", "post_steps")
    graph.add_edge("skip_skey", "post_steps")

    # Post-steps → Finalize → END
    graph.add_edge("post_steps", "finalize")
    graph.add_edge("finalize", END)

    return graph


# "skey_check" sanal node — sadece routing için
def _skey_check_passthrough(state: MappingState) -> dict:
    """SKEY kontrolü — sadece dallanma noktası."""
    return {"current_step": "skey_check"}


def get_graph(use_memory_checkpoint: bool = True):
    """Derlenmiş graph döner. Checkpoint ile veya olmadan.

    Args:
        use_memory_checkpoint: True ise MemorySaver ile checkpoint aktif.
            Production'da SQLiteSaver kullanılabilir.

    Returns:
        Derlenmiş LangGraph CompiledGraph nesnesi.
    """
    builder = build_mapping_graph()

    # skey_check sanal node'u ekle
    builder.add_node("skey_check", _skey_check_passthrough)

    checkpointer = MemorySaver() if use_memory_checkpoint else None
    return builder.compile(checkpointer=checkpointer)


# ═══════════════════════════════════════════════════════════════════════════════
# ANA ÇALIŞTIRMA FONKSİYONU
# ═══════════════════════════════════════════════════════════════════════════════

def run_mapping_graph(
    user_prompt: str | None = None,
    params: dict | None = None,
    require_approval: bool = False,
    thread_id: str | None = None,
) -> dict:
    """LangGraph mapping pipeline'ını çalıştırır.

    Args:
        user_prompt: Doğal dil isteği (opsiyonel).
        params: Yapısal parametreler (opsiyonel). user_prompt verilmezse bu kullanılır.
        require_approval: True ise preview adımında durur.
        thread_id: Checkpoint thread ID. Belirtilmezse UUID üretilir.

    Returns:
        Son state'in kopyası (tüm adım sonuçları, loglar, status).
    """
    graph = get_graph(use_memory_checkpoint=True)
    resolved_thread_id = thread_id or str(uuid.uuid4())

    initial_state: MappingState = {
        "user_prompt": user_prompt or "",
        "raw_params": params or {},
        "require_approval": require_approval,
    }

    config = {
        "configurable": {
            "thread_id": resolved_thread_id,
        }
    }

    # Graph'ı çalıştır
    final_state = None
    for step_output in graph.stream(initial_state, config=config):
        # Her adımın çıktısını logla
        for node_name, node_output in step_output.items():
            logger.info("Graph step: %s → %s",
                       node_name,
                       node_output.get("current_step", "?"))
        _persist_graph_state(graph, config, resolved_thread_id)
        final_state = step_output

    # Son state'i düzleştir
    if final_state:
        # stream son çıktıda {node_name: state_update} döner
        last_node = list(final_state.keys())[0]
        result = graph.get_state(config).values
        _persist_graph_state(graph, config, resolved_thread_id)
        return dict(result)

    return {"overall_status": "failed", "error": "Graph hiç çalışmadı"}


# ═══════════════════════════════════════════════════════════════════════════════
# YARDIMCI: Graph görselleştirme (ASCII)
# ═══════════════════════════════════════════════════════════════════════════════

def print_graph_ascii():
    """Graph'ın ASCII temsilini yazdırır."""
    graph = get_graph(use_memory_checkpoint=False)
    try:
        print(graph.get_graph().draw_ascii())
    except Exception:
        # ASCII draw mevcut değilse node listesini göster
        nodes = graph.get_graph().nodes
        print("=== Mapping Graph Nodes ===")
        for name in nodes:
            print(f"  [{name}]")


if __name__ == "__main__":
    # Test: Graph'ı oluştur ve ASCII olarak göster
    import sys
    sys.stdout.reconfigure(encoding='utf-8')

    print("=== LangGraph Mapping Pipeline ===\n")
    print_graph_ascii()

    print("\n=== Graph compile başarılı ===")
    print("Kullanım: run_mapping_graph(params={...})")
