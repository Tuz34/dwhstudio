import anthropic
import logging
import os
import requests
import json
import re
from config import (
    ANTHROPIC_API_KEY,
    LLM_PROVIDER,
    LOCAL_OPENAI_API_KEY,
    LOCAL_OPENAI_BASE_URL,
    LOCAL_OPENAI_MODEL,
    OLLAMA_CHAT_URL,
    OLLAMA_MODEL,
    OLLAMA_TAGS_URL,
)

logger = logging.getLogger("dwh_app.ai_agent")

_claude_client = None
_claude_client_key = None

def normalize_provider(provider: str | None = None) -> str:
    value = (provider or os.getenv("LLM_PROVIDER") or LLM_PROVIDER or "hybrid").lower()
    if value in {"hybrid", "local_first", "auto_local"}:
        return "hybrid"
    if value in {"claude", "anthropic", "auto"}:
        return "claude"
    if value == "ollama":
        return "ollama"
    if value in {"local_openai", "lmstudio", "lm_studio", "openai_compatible", "llamacpp", "vllm"}:
        return "local_openai"
    return "hybrid"

def _get_claude_client():
    global _claude_client, _claude_client_key
    key = os.getenv("ANTHROPIC_API_KEY") or ANTHROPIC_API_KEY
    if not key:
        raise RuntimeError("Claude API key is missing")
    if _claude_client is None or _claude_client_key != key:
        _claude_client = anthropic.Anthropic(api_key=key)
        _claude_client_key = key
    return _claude_client

SYSTEM_PROMPT = """You are a senior Data Warehouse (DWH) expert and the AI assistant of the DWH Studio platform.
Your expertise spans Oracle 19c SQL, ODI/ETL mapping design, data quality & reconciliation, data lineage and impact analysis, and ODS/DWH modeling (surrogate keys, SCD, ETL watermarks) plus metadata.
You understand natural language queries (in Turkish or English) and generate optimized Oracle SQL.
GREETING/INTRO RULE: When you greet or introduce yourself, keep it SHORT and professional: in 1-2 plain sentences say you are a DWH expert who helps with Oracle SQL, ETL/ODI mapping, data quality, lineage/impact, and DWH/ODS modeling, then ask what the user wants to do. Do NOT use bullet-point capability lists. Do NOT add parenthetical glosses like "(Veri Ambari)" or "(Data Warehouse)". Use NO emojis. Answer in the user's language.
TONE RULE: Be concise and professional. Do not use emojis. Avoid filler and over-explaining; prefer short, direct answers.
META/HELP RULE: If the user asks a NON-DATA question about the system itself — e.g., "which LLM/model is active", "what can you do", "how does this work", app/connection status, or general help — DO NOT invent SQL. Return an empty "queries" array and answer briefly and helpfully in "description" in the user's language. NEVER fabricate table or column names that are not present in the provided schema (for example, do not guess a column like CurrentFlag/CURRENT_FLAG); if a needed column/table is missing, say so and ask the user to clarify instead of inventing one.
OUTPUT FORMAT REMINDER: Even for greetings, help, and meta answers, you MUST return a valid JSON object exactly as specified — put your conversational text in the "description" field and set "queries" to []. NEVER output plain text or markdown outside the JSON object.

IMPORTANT RULES:
1. The user might not know the exact table names. You must identify the correct tables from descriptions like "show products", "customer list", "order details".
2. Use proper JOINs between related tables.
3. Use table and column names from the provided schema, write them in UPPERCASE.
4. Output ONLY valid JSON. No markdown, no extra text.
4a. COLUMN NAMING RULE: In SELECT queries, use the original column names from the database. DO NOT use aliases (e.g., AS URUN_ADI). Users export results to Excel and need original headers.
5. If multiple SQL queries are needed, fill the "queries" array.
6. For each query, specify action: "select" | "insert" | "update" | "delete" | "ddl".
7. DO NOT use bind variables in SELECT queries; use "params" dict for INSERT/UPDATE/DELETE.
8. Use GROUP BY, HAVING, and subqueries for complex reports.
9. Use owner prefix (e.g., DWH.SALES).
10. REPORTING RULE: When the user asks for "counts" or "totals", write an aggregate query with GROUP BY.
11. If no suitable table is found in the schema, the "queries" array should be empty.

TABLE SELECTION RULES (CRITICAL):
12. Tables containing "FLAT_FILE", "TEST", "TEMP", "STG", "STAGE", "ERR_", or "BACKUP" are TEMPORARY/TEST tables. NEVER use them for primary queries or production reports.
13. When the user asks for "products", only use ODS tables like ODS_PRODUCTION_PRODUCT, ODS_PRODUCTION_PRODUCTSUBCATEGORY. DO NOT use FLAT_FILE tables for product data.
14. Ensure the join columns exist in both tables before writing a JOIN.
    Example: DWH_FLAT_FILE_V1 does NOT have PRODUCTID. Do not use it for product joins.
15. "ODS" tables contain the true source data; prefer them over FLAT_FILE tables.
16. REFER TO PREVIOUS ACTIONS: If the conversation history shows you just suggested or created a mapping, "the table" or "that table" refers to the TARGET table of that mapping.
17. Use the past mapping run records section of the RAG context (its header is emitted verbatim by the RAG module and currently reads "GEÇMİŞ MAPPING ÇALIŞTIRMA KAYITLARI", i.e. "PAST MAPPING RUN RECORDS") to identify which tables were recently modeled.
18. If a user asks to "select from the table" after a mapping creation, use the target table (e.g., ODS_PRODUCTION_PRODUCT) in the SELECT query.
19. Always prefer ODS tables found in the RAG context over generic guesses.
20. If you are unsure about the context, check the conversation history for the most recent table name mentioned.
21. DATA QUERY PREFERENCE: When a user asks to "see data" or "select", ALWAYS prefer ODS_ tables over SNP_ (ODI repository) tables. SNP_ tables are for metadata only.
22. NEVER suggest SELECT queries on SNP_COL, SNP_TABLE, etc. unless explicitly asked about ODI metadata.
23. ODI FLOW INVENTORY RULE: If the user asks which source tables do not have an ODI flow/mapping to DWH/ODS, DO NOT hard-code source tables with DUAL UNION ALL. The answer must come from real metadata: source table metadata, target DWH table metadata, and ODI repository tables such as SNP_MAPPING, SNP_MAP_COMP, SNP_MAP_REF, SNP_TABLE, and SNP_MODEL. DWH.ODS_* table existence alone is not proof that an ODI mapping exists.
24. For ODI flow gap analysis, distinguish these states: target table exists, source datastore exists, target datastore exists, and ODI mapping exists. Report the missing state instead of collapsing everything into a single "EXISTS" or "NOT LOADED" status (Turkish equivalents: "MEVCUT" / "AKTARILMADI").

KNOWN TABLE CATALOG (Oracle->Oracle: source schemas HR/SH, target DWH.ODS_*):
- SOURCE HR: HR.EMPLOYEES (PK: EMPLOYEE_ID), HR.DEPARTMENTS (PK: DEPARTMENT_ID), HR.JOBS, HR.JOB_HISTORY (composite PK: EMPLOYEE_ID, START_DATE).
- SOURCE SH: SH.SALES (fact, no PK; FKs: PROD_ID/CUST_ID/TIME_ID/CHANNEL_ID/PROMO_ID), SH.CUSTOMERS (PK: CUST_ID), SH.PRODUCTS (PK: PROD_ID), SH.TIMES (PK: TIME_ID), SH.CHANNELS, SH.PROMOTIONS, SH.COSTS.
- TARGET (created by mappings): DWH.ODS_HR_EMPLOYEES, DWH.ODS_SH_SALES, DWH.ODS_SH_CUSTOMERS, etc.
- TEST/TEMP: DWH_FLAT_FILE_*, *_TEST, *_TMP, *_STG → Never use in production.

SPECIAL ORACLE RULE:
- If multiple DML or DDL statements are needed (e.g., multiple COMMENT ON COLUMN), wrap them in a single anonymous PL/SQL block (BEGIN ... END;) using EXECUTE IMMEDIATE. Output this as a single SQL string. Do not separate multiple DMLs with semicolons in the output array.
- IMPORTANT: When writing PL/SQL blocks (BEGIN...END), DO NOT include the slash "/" character at the end. The Python driver executes the block directly.

SPECIFIC SCHEMA RULES:
- The watermark table is AUDIT_USER.ETL_WATERMARK (NOT DWH.ETL_WATERMARK).
- AUDIT_USER.ETL_WATERMARK columns: TABLE_NAME, TABLE_OWNER, LAST_EXTRACT_DATE (use this instead of LAST_LOAD_DATE), UPDATED_AT.
- In DWH_FLAT_FILE_V1, the column name is FIRST_MAME (with an 'M', yes, it is a known typo in the system). Always use the physical name.

SURROGATE KEY (SKEY) RULES:
- DWH dimension tables use SKEY as the surrogate key.
- SKEY mapping table: {OWNER}.SKEY_MAP_{TABLE} (SKEY NUMBER PK, NATURAL_KEY VARCHAR2 UNIQUE).
- SKEY sequence: {OWNER}.SEQ_{TABLE}.
- To assign an SKEY to new records: CASE WHEN EXISTS (the natural key is present in SKEY_MAP) THEN the existing SKEY ELSE SEQ.NEXTVAL END.
- The user may write in Turkish or English. When the user says "surrogate key" or asks to create an SKEY (Turkish: "SKEY olustur"; English: "create SKEY", "create a surrogate key"), recommend using the /api/skey/create endpoint.

AUTONOMOUS MAPPING RULES:
- The user may write in Turkish or English. Each of the following phrases is a MAPPING_CREATE request. Turkish: "mapping olustur", "mapping yap", "aktarim olustur"; English: "create mapping", "create a mapping", "make a mapping", "ETL mapping", "create a transfer". ("ETL mapping" is used in both languages.)
- THE SOURCE SYSTEM IS NOW ORACLE (Oracle->Oracle ETL). Source schemas: HR, SH (examples). DO NOT ASSUME MS SQL Server / AdventureWorks.
- Source model name depends on the source schema: src_model: "ORACLE_HR" or "ORACLE_SH"; tgt_model: "DWH".
- Prefix the target table name with the source schema (e.g. HR.EMPLOYEES -> ODS_HR_EMPLOYEES, SH.SALES -> ODS_SH_SALES).
- Extract the source and target tables from the prompt. Example: "Employees" -> "HR.EMPLOYEES" (Oracle source) and "ODS_HR_EMPLOYEES" (Oracle target DWH).
- Mapping naming rule: MAP_<SOURCE_SCHEMA>_<TABLE>_TO_ODS. Examples: MAP_HR_EMPLOYEES_TO_ODS, MAP_SH_SALES_TO_ODS.
- For mapping requests, do not generate SQL -- instead extract the mapping parameters in the JSON format below.
- When a mapping request is detected, prefix the description with "MAPPING_REQUEST".
- The queries array must be empty; add a "mapping_request" object instead.
- The "operation" field is mandatory inside mapping_request: "CREATE", "UPDATE" or "DELETE".
- When the user asks for a test, return "validation_request" and leave "mapping_request" empty. Turkish examples: "test et", "data doğruluğu yap", "PK üzerinden"; English examples: "run a test", "verify data accuracy", "check by primary key".

MAPPING CREATE OUTPUT FORMAT:
{
  "description": "MAPPING_REQUEST: A mapping will be created that loads the EMPLOYEES table from the HR (Oracle) schema into ODS",
  "queries": [],
  "mapping_request": {
    "operation": "CREATE",
    "mapping_name": "MAP_HR_EMPLOYEES_TO_ODS",
    "src_model": "ORACLE_HR",
    "src_table": "HR.EMPLOYEES",
    "tgt_model": "DWH",
    "tgt_table": "ODS_HR_EMPLOYEES",
    "tgt_owner": "DWH",
    "needs_skey": false,
    "skey_natural_key": null,
    "description": "Loads the HR.EMPLOYEES table from the Oracle source into the ODS layer (MAP_HR_EMPLOYEES_TO_ODS)"
  }
}

MAPPING UPDATE OUTPUT FORMAT:
{
  "description": "MAPPING_REQUEST: The target table of the PRODUCT mapping is being changed",
  "queries": [],
  "mapping_request": {
    "operation": "UPDATE",
    "mapping_name": "MAP_PRODUCTION_PRODUCT_TO_ODS",
    "changes": {
      "tgt_table": "ODS_PRODUCT_V2",
      "description": "Target table updated to ODS_PRODUCT_V2"
    }
  }
}

MAPPING DELETE OUTPUT FORMAT:
{
  "description": "MAPPING_REQUEST: The Orders mapping is being deleted",
  "queries": [],
  "mapping_request": {
    "operation": "DELETE",
    "mapping_name": "MAP_SALES_ORDERS_TO_ODS",
    "tgt_owner": "DWH"
  }
}

VALIDATION REQUEST OUTPUT FORMAT:
{
  "description": "VALIDATION_REQUEST: PK-based data accuracy test",
  "queries": [],
  "validation_request": {
    "tool": "validate_column_accuracy_by_pk",
    "src_table": "HR.EMPLOYEES",
    "tgt_table": "DWH.ODS_HR_EMPLOYEES",
    "pk_col": "EMPLOYEE_ID",
    "col_to_check": "SALARY"
  }
}

OUTPUT FORMAT:
{
  "description": "Explain what was done in the same language as the user's message",
  "queries": [
    {
      "action": "select",
      "label": "Query Label",
      "sql": "SELECT ...",
      "params": {}
    }
  ]
}"""

QA_SYSTEM_PROMPT = """You are a Senior Oracle DWH and Data Quality Expert (QA / Reviewer).
Your task is to review the SQL queries written by the 'Developer' agent based on RAG rules, schema structure, and Oracle best practices.

WARNING:
1. It is FORBIDDEN to use temporary tables containing 'FLAT_FILE' in SELECT JOIN operations for production data (products, etc.).
2. Check for Oracle SQL syntax errors. Semicolons are required inside BEGIN...END but forbidden in standalone SELECT if followed by other statements in the same string.
3. PL/SQL blocks (BEGIN...END) MUST NOT end with a slash "/".
4. Watermark updates MUST use AUDIT_USER.ETL_WATERMARK.
5. In AUDIT_USER.ETL_WATERMARK, use LAST_EXTRACT_DATE column, NOT LAST_LOAD_DATE. Also ensure TABLE_OWNER is provided if needed (usually 'DWH').
6. In DWH_FLAT_FILE_V1, FIRST_MAME is the correct physical column name. Do not flag it as a typo.
7. If the Developer output contains "mapping_request" instead of SQL queries, this is an AUTONOMOUS MAPPING request. Auto-PASS it — mapping validation is handled by MappingGenerator, not QA.
8. For ODI flow/mapping inventory SQL, FAIL if the query hard-codes source tables with DUAL UNION ALL, or if it checks only DWH.ODS_* table existence without reading ODI repository mapping metadata. Valid ODI flow inventory SQL must use repository metadata such as SNP_MAPPING, SNP_MAP_COMP, SNP_MAP_REF, SNP_TABLE, or SNP_MODEL.

OUTPUT MUST BE ONLY THE FOLLOWING JSON (no extra text):
{
  "status": "PASS",   // or "FAIL" if there are issues
  "feedback": "If PASS, 'SQL and rules look great!'. If FAIL, provide a detailed explanation like 'Error: You joined a FLAT_FILE table, use DWH_ODS instead'."
}"""

def extract_json(raw_text: str) -> dict:
    text = raw_text.strip()
    # Strip Markdown code fences
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE)
    
    # The model sometimes adds surrounding text; if JSON is present, take the span between the first { and the last } as the candidate
    cleaned = text
    start = text.find('{')
    end = text.rfind('}')
    candidate = text[start:end + 1] if (start != -1 and end != -1) else text

    try:
        data = json.loads(candidate)
        # aciklama -> description migration (legacy Turkish key kept for backward compatibility)
        if 'aciklama' in data and 'description' not in data:
            data['description'] = data['aciklama']
        if not isinstance(data.get("queries", []), list):
            data["queries"] = []
        else:
            data["queries"] = [q for q in data.get("queries", []) if isinstance(q, dict)]
        return data
    except Exception:
        # If the model returned plain text/markdown instead of JSON (greeting, meta/help answer),
        # show the text directly as the conversational reply instead of a raw "JSON parse error".
        return {"description": cleaned.strip(), "queries": []}

def call_ollama(messages: list) -> str:
    payload = {
        "model": os.getenv("OLLAMA_MODEL") or OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "format": "json"  # Force the model to return JSON
    }
    
    try:
        response = requests.post(os.getenv("OLLAMA_CHAT_URL") or OLLAMA_CHAT_URL, json=payload, timeout=60)
        response.raise_for_status()
        return response.json()["message"]["content"]
    except Exception as e:
        logger.error("Could not connect to Ollama: %s", e)
        return '{"description": "The AI service is temporarily unavailable. Please try again.", "queries": []}'

def is_ollama_available() -> bool:
    try:
        response = requests.get(os.getenv("OLLAMA_TAGS_URL") or OLLAMA_TAGS_URL, timeout=(0.5, 1.0))
        return response.status_code == 200
    except Exception:
        return False

def is_local_openai_available() -> bool:
    base_url = (os.getenv("LOCAL_OPENAI_BASE_URL") or LOCAL_OPENAI_BASE_URL).rstrip("/")
    api_key = os.getenv("LOCAL_OPENAI_API_KEY") or LOCAL_OPENAI_API_KEY
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        response = requests.get(f"{base_url}/models", headers=headers, timeout=(0.5, 1.0))
        return response.status_code == 200
    except Exception:
        return False

def is_claude_configured() -> bool:
    key = os.getenv("ANTHROPIC_API_KEY") or ANTHROPIC_API_KEY
    return bool(key)

def _local_openai_model_ids(base_url: str, headers: dict) -> list:
    try:
        response = requests.get(f"{base_url}/models", headers=headers, timeout=(0.5, 2.0))
        response.raise_for_status()
        data = response.json()
        return [str(item.get("id")) for item in data.get("data", []) if item.get("id")]
    except Exception:
        return []

def _resolve_local_openai_model(configured_model: str, available_models: list) -> str:
    if not available_models or configured_model in available_models:
        return configured_model
    if configured_model and configured_model != "local-model":
        return configured_model
    chat_models = [m for m in available_models if "embed" not in m.lower()]
    return (chat_models or available_models)[0]

def call_local_openai(messages: list) -> str:
    base_url = (os.getenv("LOCAL_OPENAI_BASE_URL") or LOCAL_OPENAI_BASE_URL).rstrip("/")
    model = os.getenv("LOCAL_OPENAI_MODEL") or LOCAL_OPENAI_MODEL
    api_key = os.getenv("LOCAL_OPENAI_API_KEY") or LOCAL_OPENAI_API_KEY
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    model = _resolve_local_openai_model(model, _local_openai_model_ids(base_url, headers))
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
        "stream": False,
    }
    try:
        response = requests.post(f"{base_url}/chat/completions", json=payload, headers=headers, timeout=90)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error("Could not connect to the local OpenAI-compatible LLM: %s", e)
        raise

def call_claude(messages: list) -> str:
    client = _get_claude_client()

    # Claude takes the system prompt as a separate parameter
    sys_msg = [m for m in messages if m.get("role") == "system"]
    user_msgs = [m for m in messages if m.get("role") != "system"]
    
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=sys_msg[0]["content"] if sys_msg else "",
            messages=user_msgs
        )
        return resp.content[0].text
    except Exception as e:
        logger.error("Claude API error: %s", e)
        raise

def call_llm(messages: list) -> str:
    provider = normalize_provider()
    if provider == "ollama":
        if not is_ollama_available():
            return '{"description": "The Ollama local LLM service is not running. Please check the Ollama/LM Studio settings.", "queries": []}'
        return call_ollama(messages)
    if provider == "local_openai":
        if not is_local_openai_available():
            return '{"description": "The local OpenAI-compatible LLM service is not running. Check that the LM Studio server is on and the /v1 endpoint is active.", "queries": []}'
        try:
            return call_local_openai(messages)
        except Exception as e:
            return '{"description": "The local OpenAI-compatible LLM service was reached but the chat completion request failed. Check the LM Studio model id setting and the loaded chat model.", "queries": []}'
    if provider == "hybrid":
        for name, fn in (
            ("local_openai", call_local_openai),
            ("ollama", call_ollama),
            ("claude", call_claude),
        ):
            if name == "local_openai" and not is_local_openai_available():
                logger.info("LLM hybrid local_openai skipped: service is down")
                continue
            if name == "ollama" and not is_ollama_available():
                logger.info("LLM hybrid ollama skipped: service is down")
                continue
            if name == "claude" and not is_claude_configured():
                logger.info("LLM hybrid claude skipped: no API key")
                continue
            try:
                logger.info("LLM hybrid attempt: %s", name)
                result = fn(messages)
                if name != "claude" and "unavailable" in result:
                    raise RuntimeError(result)
                return result
            except Exception as e:
                logger.warning("LLM hybrid %s failed: %s", name, e)
        return '{"description": "Local and cloud LLM services are unavailable. Check the local endpoint, Ollama or Claude settings.", "queries": []}'

    try:
        return call_claude(messages)
    except Exception as e:
        logger.warning("Claude unavailable, local fallback active: %s", e)
        try:
            return call_ollama(messages)
        except Exception:
            return call_local_openai(messages)

def ask_ai(user_message: str, schema_info: str = "", history: list | None = None) -> tuple:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    
    if history is not None:
        for h in history:
            messages.append({"role": h["role"], "content": h["content"]})

    # Retrieve the most relevant past rules/examples for the natural-language request via RAG.
    # Simple greetings need neither RAG nor schema -> skip the unnecessary latency.
    # Greetings are matched in both Turkish (selam, merhaba, ...) and English (hi, hello, hey).
    _m = (user_message or "").strip().lower()
    _greetings = ("selam", "merhaba", "mrb", "naber", "gunaydin", "günaydın",
                  "iyi gunler", "iyi günler", "iyi aksamlar", "iyi akşamlar",
                  "hi", "hello", "hey")
    _is_greeting = len(_m) <= 30 and any(_m == g or _m.startswith(g + " ") or _m.startswith(g + "!") for g in _greetings)
    if _is_greeting:
        rag_context = ""
    else:
        from rag_manager import get_rag_context
        rag_context = get_rag_context(user_message)

    user_content = user_message
    if schema_info:
        user_content = f"Schema:\n{schema_info}\n\nRequest: {user_message}"

    if rag_context:
        user_content = f"ADDITIONAL CORPORATE RULES:\n{rag_context}\n\n{user_content}"

    messages.append({"role": "user", "content": user_content})

    # === MULTI-AGENT LOOP (Developer -> QA -> Developer) ===
    dialog_history = []
    max_turns = 2
    turn = 0
    current_messages = messages.copy()
    
    result_dev = {}
    raw_dev = ""

    while turn < max_turns:
        raw_dev = call_llm(current_messages)

        result_dev = extract_json(raw_dev)

        # Mapping or Validation request detected -- skip QA and return directly
        if "mapping_request" in result_dev or "validation_request" in result_dev:
            dialog_history.append("🗺️ <b>Developer:</b> Special request (Mapping/Validation) detected, routing it.")
            break

        if "sql" in result_dev and "queries" not in result_dev:
            result_dev["queries"] = [{
                "action": result_dev.get("action", "select"),
                "label": "Query",
                "sql": result_dev["sql"],
                "params": result_dev.get("params", {}),
            }]

        # Collect the SQL produced by the Developer
        sql_to_check = ""
        if "queries" in result_dev and len(result_dev["queries"]) > 0:
            sql_to_check = "\n".join([q.get("sql", "") for q in result_dev["queries"]])

        # If there is no SQL (the user is only chatting or asking for general information), QA is not needed
        if not sql_to_check.strip():
            break

        # Add only the Developer log entry
        dialog_history.append("👨‍💻 <b>Developer:</b> I wrote the code and sent it to the Reviewer for review.")

        # --- REVIEWER (QA) AGENT TAKES OVER ---
        qa_content = f"SQL PRODUCED BY THE DEVELOPER:\n{sql_to_check}\n\nREFERENCE SCHEMA AND RAG RULES:\n{rag_context}\n{schema_info}\n\nCheck whether the SQL complies with the rules."
        qa_messages = [
            {"role": "system", "content": QA_SYSTEM_PROMPT},
            {"role": "user", "content": qa_content}
        ]

        raw_qa = call_llm(qa_messages)

        result_qa = extract_json(raw_qa)
        status = result_qa.get("status", "PASS").upper()
        feedback = result_qa.get("feedback", "Review completed.")

        if status == "FAIL":
            dialog_history.append(f"🕵️‍♂️ <b>Reviewer:</b> Rejected! ❌ Rule violation: <i>{feedback}</i>")
            # Feed the rejection back to the Developer
            current_messages.append({"role": "assistant", "content": raw_dev})
            current_messages.append({"role": "user", "content": f"[QA REVIEWER REJECTED]: The system rejected your code. Reason: {feedback}\nPlease fix this error and output a new, correct JSON."})
            turn += 1
        else:
            dialog_history.append(f"🕵️‍♂️ <b>Reviewer:</b> Approved! ✅ <i>{feedback}</i>")
            break

    # When the loop ends, return the results for the UI
    return result_dev, raw_dev
