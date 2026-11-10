from __future__ import annotations

from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, request

from ai_egress import CLAUDE_ORIGIN, CONFIG_LOCK, endpoint_origin, validate_endpoint
from chat_mapping_proposal import normalize_mapping_request
from routes.deps import get_app_module
from routes.error_responses import redacted_write_error

bp = Blueprint("ai_write", __name__)
limiter = get_app_module().limiter


@bp.route("/api/config/llm", methods=["POST"])
@limiter.limit("10 per minute")
def api_config_llm():
    app_module = get_app_module()
    body = request.get_json() or {}
    import ai_agent
    try:
        if not isinstance(body, dict) or any(not isinstance(v, str) for v in body.values()):
            raise ValueError()
        if any(len(v) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in v) for v in body.values()):
            raise ValueError()
        for value in body.values():
            value.encode("utf-8")
        if set(body) - {"provider", "base_url", "model", "local_api_key", "api_key",
                        "anthropic_api_key", "claude_api_key", "anthropicKey"}:
            raise ValueError()
        provider = ai_agent.normalize_provider(body.get("provider", "hybrid"))
        updates = {"LLM_PROVIDER": provider}
        if body.get("base_url"):
            updates["LOCAL_OPENAI_BASE_URL"] = validate_endpoint(body["base_url"])
        if body.get("model"):
            updates["LOCAL_OPENAI_MODEL"] = body["model"].strip()
        local_key_supplied = "local_api_key" in body or "api_key" in body
        local_api_key = body.get("local_api_key", body.get("api_key", "")).strip()
        anthropic_key = (body.get("anthropic_api_key") or body.get("claude_api_key")
                         or body.get("anthropicKey") or "").strip()
        with CONFIG_LOCK:
            # Validate the effective endpoint even when only a key is submitted.
            old_url = app_module.os.getenv("LOCAL_OPENAI_BASE_URL") or ai_agent.LOCAL_OPENAI_BASE_URL
            new_url = validate_endpoint(updates.get("LOCAL_OPENAI_BASE_URL") or old_url)
            old_key = app_module.os.getenv("LOCAL_OPENAI_API_KEY")
            if old_key is None:
                old_key = ai_agent.LOCAL_OPENAI_API_KEY
            if old_key and endpoint_origin(new_url) != endpoint_origin(old_url) and not local_key_supplied:
                raise ValueError()
            if provider in {"claude", "hybrid"}:
                validate_endpoint(app_module.os.getenv("ANTHROPIC_BASE_URL") or CLAUDE_ORIGIN, cloud=True)
            if local_key_supplied:
                updates["LOCAL_OPENAI_API_KEY"] = local_api_key
                updates["LOCAL_OPENAI_API_KEY_ORIGIN"] = endpoint_origin(new_url)
            if anthropic_key:
                updates["ANTHROPIC_API_KEY"] = anthropic_key
            app_module.os.environ.update(updates)
            ai_agent.LLM_PROVIDER = provider
            if anthropic_key:
                ai_agent.ANTHROPIC_API_KEY = anthropic_key
                app_module.config.ANTHROPIC_API_KEY = anthropic_key
                ai_agent._claude_client = None
                ai_agent._claude_client_key = None
    except ValueError:
        return jsonify({"error": "Invalid LLM configuration"}), 400

    claude_key = app_module.os.getenv("ANTHROPIC_API_KEY") or getattr(app_module.config, "ANTHROPIC_API_KEY", "")
    return jsonify({"ok": True, "provider": provider, "claude_configured": bool(claude_key and len(claude_key) > 10)})


@bp.route("/api/local-analyst/summarize", methods=["POST"])
@limiter.limit("20 per minute")
def api_local_analyst_summarize():
    app_module = get_app_module()
    body = request.get_json() or {}
    title = (body.get("title") or "Local Analyst Note").strip()[:120]
    content = (body.get("content") or "").strip()
    provider = body.get("provider", "local_openai")
    if not content:
        return jsonify({"error": "content is required."}), 400
    if len(content) > 60000:
        content = content[:60000]

    messages = [
        {
            "role": "system",
            "content": (
                "You are a concise local code analyst. Summarize the provided text "
                "as English bullet points focused on actions. Use only these headings: "
                "Summary, Risks, Next Steps."
            ),
        },
        {"role": "user", "content": f"Title: {title}\n\nText:\n{content}"},
    ]

    import ai_agent

    try:
        if not isinstance(provider, str):
            raise ValueError("Invalid provider")
        provider = ai_agent.normalize_provider(provider)
    except ValueError:
        return jsonify({"error": "Invalid provider"}), 400
    try:
        summary = ai_agent.call_llm(messages, provider=provider)
    except Exception as exc:
        return redacted_write_error("Local analyst", exc)

    note_path = Path(app_module.__file__).resolve().parent / "LOCAL_NOTES.md"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with note_path.open("a", encoding="utf-8") as f:
        f.write(f"\n## {timestamp} - {title}\n\n{summary.strip()}\n")

    return jsonify({"ok": True, "summary": summary, "note_file": "LOCAL_NOTES.md"})


@bp.route("/api/ai/orchestrate", methods=["POST"])
@limiter.limit("20 per minute")
def api_ai_orchestrate():
    app_module = get_app_module()
    body = request.get_json() or {}
    prompt = (body.get("prompt") or body.get("message") or "").strip()
    if not prompt:
        return jsonify({"error": "prompt is required."}), 400

    try:
        result = app_module.orchestrate(
            prompt,
            dry_run=body.get("dry_run") is not False,
            allow_cloud_review=body.get("allow_cloud_review") is True,
            job_id=body.get("job_id"),
        )
        if result.get("status") == "failed":
            return jsonify({"ok": False, "error": "AI orchestration failed.",
                            "error_type": "OrchestrationError"}), 500
        return jsonify({"ok": True, "result": result})
    except Exception as exc:
        return redacted_write_error("AI orchestration", exc)


@bp.route("/api/rag_knowledge", methods=["GET", "POST", "DELETE"])
@limiter.limit("10 per minute")
def api_rag_knowledge():
    app_module = get_app_module()
    try:
        if request.method == "POST":
            data = request.get_json() or {}
            approval_error = app_module._approval_error(data, "RAG knowledge write")
            if approval_error:
                return approval_error
            if not data:
                return jsonify({"error": "No data provided"}), 400

            from rag_manager import add_document

            collection = data.get("collection")
            text = data.get("text")
            metadata = data.get("metadata", {})
            doc_id = data.get("id")

            if not collection or not text:
                return jsonify({"error": "collection and text are required"}), 400

            new_id = add_document(collection, text, metadata, doc_id)
            return jsonify({"success": True, "id": new_id})

        if request.method == "DELETE":
            data = request.get_json() or {}
            approval_error = app_module._approval_error(data, "RAG knowledge write")
            if approval_error:
                return approval_error
            if not data:
                return jsonify({"error": "No data provided"}), 400

            from rag_manager import delete_document

            collection = data.get("collection")
            doc_id = data.get("id")

            if not collection or not doc_id:
                return jsonify({"error": "collection and id are required"}), 400

            deleted = delete_document(collection, doc_id)
            if deleted:
                return jsonify({"success": True})
            return jsonify({"error": "Document not found or delete failed"}), 404

        from rag_manager import _load_db

        db = _load_db()
        clean_db = {}
        for coll, items in db.items():
            clean_db[coll] = [
                {
                    "id": doc.get("id"),
                    "text": doc.get("text"),
                    "metadata": doc.get("metadata", {}),
                }
                for doc in items
            ]
        return jsonify(clean_db)
    except Exception as exc:
        return redacted_write_error("RAG knowledge operation", exc)


@bp.route("/api/ask", methods=["POST"])
@limiter.limit("20 per minute")
def api_ask():
    app_module = get_app_module()
    body = request.get_json() or {}
    user_msg = body.get("message", "").strip()
    history = body.get("history", [])
    modeled_tables_question = app_module._is_modeled_tables_question(user_msg)

    if not user_msg:
        return jsonify({"error": "message is required."}), 400

    try:
        if app_module._is_llm_status_question(user_msg):
            return jsonify(app_module._llm_status_answer())

        if app_module._is_truncate_mapping_intent(user_msg):
            return jsonify(app_module._truncate_mapping_intent_answer(user_msg))

        if app_module._is_remembered_unmapped_flow_mapping_request(user_msg, history):
            return jsonify(app_module._remembered_unmapped_flow_mapping_answer(user_msg, history))

        if app_module._is_unmapped_odi_flow_question(user_msg):
            return jsonify(app_module._unmapped_odi_flow_answer(user_msg))

        if modeled_tables_question:
            answer = app_module._modeled_tables_answer(user_msg)
            app_module._remember_modeled_target(answer)
            return jsonify(answer)

        if app_module._is_table_data_request(user_msg):
            table_data = app_module._table_data_answer(user_msg, history)
            if table_data:
                return jsonify(table_data)
            return jsonify(app_module._table_data_clarification_answer())

        schema = ""
        result, raw = app_module.ask_ai(user_msg, schema, history)

        if "mapping_request" in result and result["mapping_request"]:
            proposal = normalize_mapping_request(user_msg, result["mapping_request"])
            if proposal is None:
                return jsonify({
                    "aciklama": "The mapping proposal could not be validated. Please restate the request.",
                    "queries": [],
                    "raw": "",
                })
            return jsonify({
                "aciklama": (
                    "MAPPING_REQUEST: Review the proposed mapping in the wizard."
                    if proposal.get("target_layer")
                    else result.get("aciklama", result.get("description", ""))
                ),
                "queries": [],
                "mapping_request": proposal,
                "raw": "",
            })

        if "validation_request" in result and result["validation_request"]:
            import json as _json

            from data_validation_mcp import validate_column_accuracy_by_pk

            vr = result["validation_request"]
            try:
                res_str = validate_column_accuracy_by_pk(
                    src_table=vr.get("src_table", ""),
                    tgt_table=vr.get("tgt_table", ""),
                    pk_col=vr.get("pk_col", ""),
                    col_to_check=vr.get("col_to_check", ""),
                )
                parsed = _json.loads(res_str)
                status_text = "PASS" if "PASS" in parsed.get("status", "") else "FAIL"
                aciklama = f"**Data Validation Result: {status_text}**\n\n"
                aciklama += f"- **Source Table:** `{vr.get('src_table')}`\n"
                aciklama += f"- **Target Table:** `{vr.get('tgt_table')}`\n"
                aciklama += f"- **Join Column (PK):** `{vr.get('pk_col')}`\n"
                aciklama += f"- **Check Column:** `{vr.get('col_to_check')}`\n\n"
                aciklama += f"> {parsed.get('message', '')}\n\n"

                if parsed.get("mismatched_pk_samples"):
                    aciklama += (
                        "**Mismatched PK Samples:**\n```json\n"
                        + _json.dumps(parsed.get("mismatched_pk_samples"), indent=2)
                        + "\n```"
                    )

                return jsonify({"aciklama": aciklama, "queries": [], "raw": raw})
            except Exception as exc:
                app_module.logger.error(
                    "AI validation request failed: %s",
                    type(exc).__name__,
                )
                return jsonify({
                    "aciklama": "Validation failed. See server logs for details.",
                    "queries": [],
                    "raw": raw,
                })

        queries_out = []
        for q in result.get("queries", []):
            action = q.get("action", "select").lower()
            q_out = {
                "action": action,
                "label": q.get("label", "Operation"),
                "sql": q.get("sql", ""),
                "params": q.get("params", {}),
            }
            if action == "select":
                try:
                    clean_sql = q.get("sql", "").strip()
                    if clean_sql.endswith(";"):
                        clean_sql = clean_sql[:-1].strip()
                    preview_rows = 3
                    try:
                        df = app_module.execute_select(
                            f"SELECT * FROM (\n{clean_sql}\n) WHERE ROWNUM <= {preview_rows}"
                        )
                    except Exception:
                        df = app_module.execute_select(clean_sql).head(preview_rows)
                    q_out["result"] = {
                        "columns": list(df.columns),
                        "rows": app_module.df_to_jsonable(df),
                    }
                    q_out["preview_limited"] = True
                    q_out["preview_rows"] = preview_rows
                except Exception as exc:
                    app_module.logger.error(
                        "AI SQL preview failed: %s",
                        type(exc).__name__,
                    )
                    retry_msg = (
                        "Oracle returned an execution error while running the SQL.\n"
                        "Fix the SQL using only the query context and return a new valid SQL query. "
                        "Use the error_patterns remediation rules if relevant."
                    )
                    retry_history = history + [
                        {"role": "user", "content": user_msg},
                        {"role": "assistant", "content": raw},
                    ]
                    try:
                        retry_result, _retry_raw = app_module.ask_ai(retry_msg, schema, retry_history)
                        if "queries" in retry_result and len(retry_result["queries"]) > 0:
                            retry_q = retry_result["queries"][0]
                            clean_sql_retry = retry_q.get("sql", "").strip()
                            if clean_sql_retry.endswith(";"):
                                clean_sql_retry = clean_sql_retry[:-1].strip()
                            df_retry = app_module.execute_select(clean_sql_retry)
                            q_out["sql"] = clean_sql_retry
                            q_out["label"] += " (AI auto-corrected)"
                            q_out["result"] = {
                                "columns": list(df_retry.columns),
                                "rows": app_module.df_to_jsonable(df_retry),
                            }
                        else:
                            q_out["error"] = "SQL preview failed. See server logs for details."
                    except Exception as retry_exc:
                        app_module.logger.error(
                            "AI SQL preview retry failed: %s",
                            type(retry_exc).__name__,
                        )
                        q_out["error"] = "SQL preview failed. See server logs for details."
            queries_out.append(q_out)

        return jsonify({
            "aciklama": result.get("description", result.get("aciklama", "")),
            "queries": queries_out,
            "raw": raw,
        })
    except Exception as exc:
        return redacted_write_error("AI ask", exc)
