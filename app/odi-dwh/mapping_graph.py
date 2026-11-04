"""
Mapping Graph — LangGraph-Based Mapping Orchestrator
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Structures the existing MappingGenerator pipeline as a LangGraph state machine
Benefits:
  - Checkpoint: resume from the last step after a crash
  - Branching: conditional path based on whether SKEY is needed
  - Error handling: each node branches to error_handler on failure
  - Human-in-the-loop: approval step (integrated with UI Wizard)

The existing MappingGenerator business logic (methods) is reused unchanged.
This module manages only the orchestration flow.

Usage:
    from mapping_graph import run_mapping_graph, get_graph

    # One-off run
    result = run_mapping_graph(user_prompt="Transfer the Customer table to ODS")

    # Or with parameters
    result = run_mapping_graph(params={
        "mapping_name": "MAP_SALES_CUSTOMER_TO_ODS",
        "src_model": "SQL_SERVER",
        "src_table": "Sales.Customer",
        "tgt_model": "ORACLE_ODS",
        "tgt_table": "ODS_SALES_CUSTOMER",
        "tgt_owner": "DWH",
        "needs_skey": False,
    })

    # With human-in-the-loop (UI Wizard integration)
    result = run_mapping_graph(
        params={...},
        require_approval=True,   # stop at the preview step
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
            logger.warning("Graph state is empty — checkpoint could not be read (thread=%s).", thread_id)
            return
        with MappingStateManager() as msm:
            msm.upsert_state(state, thread_id=thread_id)
    except Exception as exc:
        # Diagnosis: expose issues such as permissions, connections, and schemas
        logger.error("Graph state could not be persisted (thread=%s): %s", thread_id, exc, exc_info=True)

# ═══════════════════════════════════════════════════════════════════════════════
# STATE SCHEMA — State shared across all steps
# ═══════════════════════════════════════════════════════════════════════════════

class MappingState(TypedDict, total=False):
    """LangGraph state object. Every node reads and writes this state."""
    # ── Input ──
    user_prompt: str               # Natural-language request (optional)
    raw_params: dict               # User-provided parameters
    require_approval: bool         # Stops at preview when True

    # ── Pipeline state ──
    batch_id: str                  # Operation batch ID
    current_step: str              # Active step name
    validated_params: dict         # Validated parameters

    # ── Step results ──
    parse_result: dict             # Prompt parsing result
    validate_result: dict          # Validation result
    target_table_result: dict      # Target table creation result
    datastore_result: dict         # ODI datastore registration result
    mapping_result: dict           # ODI mapping creation result
    skey_result: dict              # SKEY creation result
    lineage_result: dict           # Lineage registration result
    test_result: dict              # DQ test generation result
    snapshot_result: dict          # Snapshot result

    # ── Control ──
    approval_status: str           # "pending" / "approved" / "rejected"
    overall_status: str            # "running" / "success" / "failed"
    error: str | None              # Last error message
    log: list[dict]                # All step logs
    completed_steps: list[str]     # Completed step names


# ═══════════════════════════════════════════════════════════════════════════════
# NODE FUNCTIONS — Each is a graph node
# ═══════════════════════════════════════════════════════════════════════════════

def _add_log(state: MappingState, step: str, status: str, detail: str = "") -> list[dict]:
    """Add a new entry to the log list."""
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
    """Start the pipeline and assign batch_id."""
    batch_id = state.get("batch_id") or str(uuid.uuid4())[:8]
    logger.info("[%s] Pipeline started", batch_id)
    return {
        "batch_id": batch_id,
        "current_step": "init",
        "overall_status": "running",
        "completed_steps": [],
        "log": _add_log(state, "init", "SUCCESS", f"batch_id={batch_id}"),
    }


def node_parse_prompt(state: MappingState) -> dict:
    """Convert a natural-language prompt into parameters."""
    from mapping_generator import MappingGenerator

    prompt = state.get("user_prompt", "")
    if not prompt:
        # No prompt; use raw_params directly
        return {
            "current_step": "parse_prompt",
            "parse_result": {"skipped": True, "reason": "No prompt, using raw_params"},
            "log": _add_log(state, "parse_prompt", "SKIPPED", "Using raw_params"),
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
            "error": f"Prompt parsing failed: {e}",
            "log": _add_log(state, "parse_prompt", "FAILED", str(e)),
        }


def node_validate(state: MappingState) -> dict:
    """Validate and normalize parameters."""
    from mapping_generator import MappingGenerator

    if state.get("overall_status") == "failed":
        return {"overall_status": "failed"}

    raw = state.get("raw_params", {})
    if not raw:
        return {
            "overall_status": "failed",
            "error": "No parameters found",
            "log": _add_log(state, "validate", "FAILED", "raw_params is empty"),
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
            "error": f"Validation failed: {e}",
            "log": _add_log(state, "validate", "FAILED", str(e)),
        }


def node_preview(state: MappingState) -> dict:
    """Preview step awaiting approval. Stops if require_approval=True."""
    if state.get("overall_status") == "failed":
        return {"overall_status": "failed"}

    params = state.get("validated_params", {})
    if state.get("require_approval", False):
        return {
            "current_step": "preview",
            "approval_status": "pending",
            "log": _add_log(state, "preview", "WAITING",
                           f"Waiting for user approval: {params.get('mapping_name')}"),
        }

    # Automatic approval
    return {
        "current_step": "preview",
        "approval_status": "approved",
        "log": _add_log(state, "preview", "AUTO_APPROVED", "require_approval=False"),
        "completed_steps": list(state.get("completed_steps", [])) + ["preview"],
    }


def node_create_table(state: MappingState) -> dict:
    """Create the target Oracle table."""
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
            # If create_table updates src_table (full_src_name detection)
            "validated_params": params,
            "log": _add_log(state, "create_table", result.get("status", "SUCCESS"),
                           _target_table_detail(result)),
            "completed_steps": list(state.get("completed_steps", [])) + ["create_table"],
        }
    except Exception as e:
        return {
            "current_step": "create_table",
            "overall_status": "failed",
            "error": f"Table creation failed: {e}",
            "log": _add_log(state, "create_table", "FAILED", str(e)),
        }


def node_register_datastores(state: MappingState) -> dict:
    """Register source and target datastores in ODI."""
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
            "error": f"Datastore registration failed: {e}",
            "log": _add_log(state, "register_datastores", "FAILED", str(e)),
        }


def node_create_mapping(state: MappingState) -> dict:
    """Create the ODI mapping."""
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
            "error": f"Mapping creation failed: {e}",
            "log": _add_log(state, "create_mapping", "FAILED", str(e)),
        }


def node_create_skey(state: MappingState) -> dict:
    """Create surrogate key objects."""
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
            "log": _add_log(state, "create_skey", "SUCCESS", "SKEY created"),
            "completed_steps": list(state.get("completed_steps", [])) + ["create_skey"],
        }
    except Exception as e:
        # SKEY failure is noncritical
        return {
            "current_step": "create_skey",
            "skey_result": {"error": str(e)},
            "log": _add_log(state, "create_skey", "WARNING", str(e)),
            "completed_steps": list(state.get("completed_steps", [])) + ["create_skey"],
        }


def node_skip_skey(state: MappingState) -> dict:
    """Skip if SKEY is not needed."""
    return {
        "current_step": "skip_skey",
        "skey_result": {"skipped": True},
        "log": _add_log(state, "create_skey", "SKIPPED", "SKEY not requested"),
        "completed_steps": list(state.get("completed_steps", [])) + ["skip_skey"],
    }


def node_post_steps(state: MappingState) -> dict:
    """Run lineage, DQ test, and snapshot steps."""
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
    """Complete the pipeline and write to MAPPING_LOG."""
    from mapping_generator import MappingGenerator

    params = state.get("validated_params", {})
    status = state.get("overall_status", "running")

    if status == "failed":
        final_status = "failed"
    else:
        final_status = "success"

    # Save to MAPPING_LOG
    try:
        mg = MappingGenerator()
        mg._batch_id = state.get("batch_id", "unknown")
        mg._log_to_mapping_log(params, final_status.upper(), state.get("error"))
    except Exception as e:
        logger.warning("Could not write MAPPING_LOG: %s", e)

    logger.info("[%s] Pipeline completed: %s", state.get("batch_id"), final_status)

    return {
        "current_step": "finalize",
        "overall_status": final_status,
        "log": _add_log(state, "finalize", final_status.upper(),
                       f"batch_id={state.get('batch_id')}"),
        "completed_steps": list(state.get("completed_steps", [])) + ["finalize"],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# CONDITIONAL BRANCH FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def should_continue_after_validate(state: MappingState) -> str:
    """After validation: go to finalize on error; otherwise go to preview."""
    if state.get("overall_status") == "failed":
        return "finalize"
    return "preview"


def should_continue_after_preview(state: MappingState) -> str:
    """After preview: branch based on approval status."""
    approval = state.get("approval_status", "approved")
    if approval == "rejected":
        return "finalize"
    if approval == "pending":
        return "finalize"  # Pause until UI approval arrives (interrupt)
    return "create_table"


def needs_skey(state: MappingState) -> str:
    """Is SKEY needed?"""
    params = state.get("validated_params", {})
    if state.get("overall_status") == "failed":
        return "skip_skey"
    if params.get("needs_skey", False):
        return "create_skey"
    return "skip_skey"


def should_continue_after_mapping(state: MappingState) -> str:
    """After mapping: finalize on error; otherwise check SKEY."""
    if state.get("overall_status") == "failed":
        return "finalize"
    return "skey_check"


# ═══════════════════════════════════════════════════════════════════════════════
# GRAPH CREATION
# ═══════════════════════════════════════════════════════════════════════════════

def build_mapping_graph() -> StateGraph:
    """Create the LangGraph state machine.

    Graph structure:
    ```
    START → init → parse_prompt → validate ─┬→ preview ─┬→ create_table
                                             │           │   → register_ds
           finalize ←──── (error) ←───────────┘           │   → create_mapping
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

    # ── Add nodes ──
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

    # ── Define edges ──

    # Start
    graph.add_edge(START, "init")
    graph.add_edge("init", "parse_prompt")
    graph.add_edge("parse_prompt", "validate")

    # After validation: error → finalize; success → preview
    graph.add_conditional_edges("validate", should_continue_after_validate,
                                {"finalize": "finalize", "preview": "preview"})

    # After preview: approval → create_table; rejection → finalize
    graph.add_conditional_edges("preview", should_continue_after_preview,
                                {"create_table": "create_table", "finalize": "finalize"})

    # Sequential steps
    graph.add_edge("create_table", "register_datastores")
    graph.add_edge("register_datastores", "create_mapping")

    # After mapping: check SKEY
    graph.add_conditional_edges("create_mapping", should_continue_after_mapping,
                                {"skey_check": "skey_check", "finalize": "finalize"})

        # SKEY branching (virtual node — as a conditional edge)
    graph.add_conditional_edges("skey_check", needs_skey,
                                {"create_skey": "create_skey", "skip_skey": "skip_skey"})

    # SKEY join → post_steps
    graph.add_edge("create_skey", "post_steps")
    graph.add_edge("skip_skey", "post_steps")

    # Post-steps → Finalize → END
    graph.add_edge("post_steps", "finalize")
    graph.add_edge("finalize", END)

    return graph


# "skey_check" virtual node — routing only
def _skey_check_passthrough(state: MappingState) -> dict:
    """SKEY check — branch point only."""
    return {"current_step": "skey_check"}


def get_graph(use_memory_checkpoint: bool = True):
    """Return the compiled graph, with or without checkpointing.

    Args:
        use_memory_checkpoint: If True, checkpointing is enabled with MemorySaver.
            SQLiteSaver can be used in production.

    Returns:
        Compiled LangGraph CompiledGraph object.
    """
    builder = build_mapping_graph()

    # Add the virtual skey_check node
    builder.add_node("skey_check", _skey_check_passthrough)

    checkpointer = MemorySaver() if use_memory_checkpoint else None
    return builder.compile(checkpointer=checkpointer)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN RUN FUNCTION
# ═══════════════════════════════════════════════════════════════════════════════

def run_mapping_graph(
    user_prompt: str | None = None,
    params: dict | None = None,
    require_approval: bool = False,
    thread_id: str | None = None,
) -> dict:
    """Run the LangGraph mapping pipeline.

    Args:
        user_prompt: Natural-language request (optional).
        params: Structured parameters (optional). Used if user_prompt is not provided.
        require_approval: If True, stop at the preview step.
        thread_id: Checkpoint thread ID. A UUID is generated if omitted.

    Returns:
        Copy of the final state (all step results, logs, status).
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

    # Run the graph
    final_state = None
    for step_output in graph.stream(initial_state, config=config):
        # Log the output of each step
        for node_name, node_output in step_output.items():
            logger.info("Graph step: %s → %s",
                       node_name,
                       node_output.get("current_step", "?"))
        _persist_graph_state(graph, config, resolved_thread_id)
        final_state = step_output

    # Flatten the final state
    if final_state:
        # The stream returns {node_name: state_update} as its final output
        last_node = list(final_state.keys())[0]
        result = graph.get_state(config).values
        _persist_graph_state(graph, config, resolved_thread_id)
        return dict(result)

    return {"overall_status": "failed", "error": "Graph did not run"}


# ═══════════════════════════════════════════════════════════════════════════════
# HELPER: Graph visualization (ASCII)
# ═══════════════════════════════════════════════════════════════════════════════

def print_graph_ascii():
    """Print the graph as ASCII."""
    graph = get_graph(use_memory_checkpoint=False)
    try:
        print(graph.get_graph().draw_ascii())
    except Exception:
        # Show the node list if ASCII drawing is unavailable
        nodes = graph.get_graph().nodes
        print("=== Mapping Graph Nodes ===")
        for name in nodes:
            print(f"  [{name}]")


if __name__ == "__main__":
    # Test: create the graph and display it as ASCII
    import sys
    sys.stdout.reconfigure(encoding='utf-8')

    print("=== LangGraph Mapping Pipeline ===\n")
    print_graph_ascii()

    print("\n=== Graph compilation succeeded ===")
    print("Usage: run_mapping_graph(params={...})")
