"""
Environment based configuration management.

Usage:
  ENVIRONMENT=dev  → the .env.dev file is loaded  (default)
  ENVIRONMENT=test → the .env.test file is loaded
  ENVIRONMENT=prod → the .env.prod file is loaded

If the environment file is absent, the root .env is read as a fallback.
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

from secret_provider import get_secret, get_secret_list

# ── 1. Environment detection ──────────────────────────────────────────────────────────
# First read the ENVIRONMENT variable from the root .env
load_dotenv(override=False)
ENVIRONMENT = os.getenv("ENVIRONMENT", "dev").lower()

# ── 2. Load the environment file ─────────────────────────────────────────────────
_base_dir = Path(__file__).resolve().parent
_env_file = _base_dir / f".env.{ENVIRONMENT}"

if _env_file.exists():
    load_dotenv(_env_file, override=True)
else:
    # If the environment file is absent, continue with the root .env
    load_dotenv(override=True)

# ── 3. LLM Settings ──────────────────────────────────────────────────────────
APP_HOST          = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT          = int(os.getenv("APP_PORT", "5000"))
APP_PUBLIC_HOST   = os.getenv("APP_PUBLIC_HOST", "localhost")
APP_URL           = os.getenv("APP_URL", f"http://{APP_PUBLIC_HOST}:{APP_PORT}")
APP_DEBUG         = os.getenv("APP_DEBUG", "false").strip().lower() in {"1", "true", "yes", "on"}
APP_USE_RELOADER  = os.getenv("APP_USE_RELOADER", "false").strip().lower() in {"1", "true", "yes", "on"}
DEPLOYMENT_PROFILE = os.getenv("DEPLOYMENT_PROFILE", "local-only").strip().lower() or "local-only"
APP_TLS_ENABLED = os.getenv("APP_TLS_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
APP_REVERSE_PROXY_TLS_ENABLED = os.getenv("APP_REVERSE_PROXY_TLS_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
APP_TLS_CERT_FILE = os.getenv("APP_TLS_CERT_FILE", "")
APP_TLS_KEY_FILE = os.getenv("APP_TLS_KEY_FILE", "")
APP_ALLOWED_HOSTS = get_secret_list("APP_ALLOWED_HOSTS")
APP_IP_ALLOWLIST = get_secret_list("APP_IP_ALLOWLIST")
APP_ALLOWED_ORIGINS = get_secret_list("APP_ALLOWED_ORIGINS")
AIRFLOW_URL       = os.getenv("AIRFLOW_URL", "http://localhost:8080")
APP_AUTH_ENABLED  = os.getenv("APP_AUTH_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
APP_AUTH_USERNAME = os.getenv("APP_AUTH_USERNAME", "")
APP_AUTH_PASSWORD_HASH = get_secret("APP_AUTH_PASSWORD_HASH", "")
APP_AUTH_DEFAULT_ROLE = os.getenv("APP_AUTH_DEFAULT_ROLE", "operator").strip().lower() or "operator"
APP_SESSION_TIMEOUT_MINUTES = int(os.getenv("APP_SESSION_TIMEOUT_MINUTES", "480"))
SECURITY_AUDIT_LOG_FILE = os.getenv("SECURITY_AUDIT_LOG_FILE", "logs/security_audit.jsonl")
FLASK_SECRET_KEY = get_secret("FLASK_SECRET_KEY", "")

LLM_PROVIDER      = os.getenv("LLM_PROVIDER", "hybrid")
OLLAMA_MODEL      = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:14b")
OLLAMA_BASE_URL   = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
OLLAMA_CHAT_URL   = os.getenv("OLLAMA_CHAT_URL", f"{OLLAMA_BASE_URL}/api/chat")
OLLAMA_TAGS_URL   = os.getenv("OLLAMA_TAGS_URL", f"{OLLAMA_BASE_URL}/api/tags")
OLLAMA_EMBEDDINGS_URL = os.getenv("OLLAMA_EMBEDDINGS_URL", f"{OLLAMA_BASE_URL}/api/embeddings")
LOCAL_OPENAI_BASE_URL = os.getenv("LOCAL_OPENAI_BASE_URL", "http://localhost:1234/v1").rstrip("/")
LOCAL_OPENAI_MODEL = os.getenv("LOCAL_OPENAI_MODEL", "qwen/qwen2.5-coder-14b")
LOCAL_OPENAI_API_KEY = get_secret("LOCAL_OPENAI_API_KEY", "")
ANTHROPIC_API_KEY  = get_secret("ANTHROPIC_API_KEY")

# ── 4. Oracle Connection ───────────────────────────────────────────────────────
ORACLE_USER        = get_secret("ORACLE_USER")
ORACLE_PASSWORD    = get_secret("ORACLE_PASSWORD")
ORACLE_DSN         = get_secret("ORACLE_DSN")
ORACLE_DDL_USER     = get_secret("ORACLE_DDL_USER")
ORACLE_DDL_PASSWORD = get_secret("ORACLE_DDL_PASSWORD")
AUDIT_ORACLE_USER   = get_secret("AUDIT_ORACLE_USER", ORACLE_USER or "AUDIT_USER")

# ── 5. MS SQL Connection (legacy adapter; disabled by default) ─────────────────────
ENABLE_MSSQL_SOURCE = os.getenv("ENABLE_MSSQL_SOURCE", "false").lower() in {"1", "true", "yes", "on"}
MSSQL_DEFAULT_SERVER   = os.getenv("MSSQL_DEFAULT_SERVER", "")
MSSQL_DEFAULT_DATABASE = os.getenv("MSSQL_DEFAULT_DATABASE", "")
MSSQL_SERVER       = get_secret("MSSQL_SERVER")
MSSQL_DATABASE     = get_secret("MSSQL_DATABASE")
MSSQL_USER         = get_secret("MSSQL_USER")
MSSQL_PASSWORD     = get_secret("MSSQL_PASSWORD")

# ── 5b. SOURCE / TARGET separation (PHASE 1 — source connector) ────────────────────
# SOURCE_DB_TYPE: "oracle" (primary path) | "mssql" (legacy adapter; requires ENABLE_MSSQL_SOURCE)
SOURCE_DB_TYPE        = os.getenv("SOURCE_DB_TYPE", "oracle").lower()
# Source Oracle (schemas such as HR and SH)
SOURCE_ORACLE_USER     = get_secret("SOURCE_ORACLE_USER", ORACLE_USER)
SOURCE_ORACLE_PASSWORD = get_secret("SOURCE_ORACLE_PASSWORD", ORACLE_PASSWORD)
SOURCE_ORACLE_DSN      = get_secret("SOURCE_ORACLE_DSN", ORACLE_DSN)
# Comma-separated schema list; if empty, use all accessible schemas
SOURCE_ORACLE_SCHEMAS  = get_secret_list("SOURCE_ORACLE_SCHEMAS")
# Target Oracle (conceptually separate, even if it is the same DB)
TARGET_ORACLE_USER     = get_secret("TARGET_ORACLE_USER", ORACLE_DDL_USER)
TARGET_ORACLE_PASSWORD = get_secret("TARGET_ORACLE_PASSWORD", ORACLE_DDL_PASSWORD)
TARGET_ORACLE_DSN      = get_secret("TARGET_ORACLE_DSN", ORACLE_DSN)
# Business schemas in the project scope (UI table list and schema selector).
# Oracle system schemas and AUDIT_USER (internal infrastructure) are omitted from the list.
PROJECT_SCHEMAS        = [s.strip().upper() for s in os.getenv("PROJECT_SCHEMAS", "HR,SH,DWH").split(",") if s.strip()]

# ── 6. RAG Settings ──────────────────────────────────────────────────────────
RAG_DB_PATH        = os.getenv("RAG_DB_PATH", "./rag_database.json")
CHROMA_PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", "./chroma_db")

# ── 7. Logging ────────────────────────────────────────────────────────────────
# ... (logging code) ...

# ── 8. ODI Automation Settings (Phase 3) ──────────────────────────────────────────
ODI_HOME        = os.getenv("ODI_HOME", "C:\\Oracle\\Middleware\\Oracle_Home")
ODI_PROJECT     = os.getenv("ODI_PROJECT", "ORA_DWH")
ODI_FOLDER      = os.getenv("ODI_FOLDER", "FLD_CORE_MAPPINGS")
ODI_REPO_DSN      = get_secret("ODI_REPO_DSN", ORACLE_DSN)
ODI_REPO_SCHEMA   = os.getenv("ODI_REPO_SCHEMA", "REPO_ODI_REPO")
ODI_REPO_USER     = get_secret("ODI_REPO_USER", "REPO_ODI_REPO")
ODI_REPO_PASSWORD = get_secret("ODI_REPO_PASSWORD", "")
LOG_LEVEL          = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE           = os.getenv("LOG_FILE", "logs/app.log")

# ── 10. Productization — Environment-Independent Settings ──────────────────────────────
JAVA_HOME           = os.getenv("JAVA_HOME", "C:\\Program Files\\Java\\jdk1.8.0_211")
JAVA_FALLBACK_PATHS = [
    item.strip()
    for item in os.getenv(
        "JAVA_FALLBACK_PATHS",
        "C:\\Program Files\\Java\\jdk1.8.0_211\\bin\\java.exe;"
        "C:\\Program Files\\Java\\jre1.8.0_211\\bin\\java.exe;"
        "C:\\Oracle\\WINDOWS.X64_193000_db_home\\jdk\\bin\\java.exe;"
        "C:\\oracle\\WINDOWS.X64_193000_db_home\\jdk\\bin\\java.exe",
    ).split(";")
    if item.strip()
]
ODI_MODE            = os.getenv("ODI_MODE", "groovy")          # groovy | rest
ODI_REST_URL        = get_secret("ODI_REST_URL", "")            # ODI 14c REST endpoint
ODI_REST_USER       = get_secret("ODI_REST_USER", "")
ODI_REST_PASSWORD   = get_secret("ODI_REST_PASSWORD", "")
SOURCE_SYSTEM_NAME  = os.getenv("SOURCE_SYSTEM_NAME", "ORACLE_SAMPLE_SCHEMAS")
TABLE_NAME_MAX_LEN  = int(os.getenv("TABLE_NAME_MAX_LENGTH", "128"))  # Oracle 12.2+/19c and ODI 14c support 128-char identifiers
OJDBC_JAR_NAME      = os.getenv("OJDBC_JAR_NAME", "ojdbc8.jar")     # ojdbc8 | ojdbc11
GROOVY_JAR_NAME     = os.getenv("GROOVY_JAR_NAME", "groovy-all-2.4.17.jar")
# Oracle user that executes ODI loads and needs DML grants on SCD2 targets.
# Override when the database has no ODI_EXEC user (documented in dwh-scd2-generic-builder.md).
ODI_EXEC_USER       = (os.getenv("ODI_EXEC_USER", "ODI_EXEC") or "ODI_EXEC").strip().upper()
ODI_IKM_PRIORITY    = os.getenv("ODI_IKM_PRIORITY", "oracle merge,oracle update,oracle insert")
# Exact KM names an API caller may request (comma separated, case-insensitive exact match).
ODI_APPROVED_IKMS   = os.getenv(
    "ODI_APPROVED_IKMS",
    "IKM Oracle Incremental Update,IKM Oracle Incremental Update (MERGE),IKM Oracle Merge,"
    "IKM Oracle Insert,IKM Oracle Control Append,IKM Oracle Slowly Changing Dimension,"
    "IKM SQL Control Append",
)
ODI_LKM_PRIORITY    = os.getenv("ODI_LKM_PRIORITY", "sql to oracle,mssql to oracle,sql to sql")

# ── 9. Alert & Monitoring Settings ──────────────────────────────────────────
ALERT_FRESHNESS_DAYS = int(os.getenv("ALERT_FRESHNESS_DAYS", "2"))
ALERT_POLL_INTERVAL  = int(os.getenv("ALERT_POLL_INTERVAL", "60"))

# Create the log directory
_log_dir = Path(LOG_FILE).parent
_log_dir.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        # D-2: RotatingFileHandler — max 50MB, 5 backup files (log growth is bounded)
        RotatingFileHandler(
            LOG_FILE, maxBytes=50 * 1024 * 1024, backupCount=5, encoding="utf-8"
        ),
        logging.StreamHandler(),
    ],
)

# Keep app DEBUG logs useful without dumping outbound request bodies or prompt payloads
# from HTTP/LLM client libraries into local files.
for _noisy_logger in ("anthropic", "httpx", "httpcore", "urllib3"):
    logging.getLogger(_noisy_logger).setLevel(logging.WARNING)

logger = logging.getLogger("dwh_app")

logger.info(
    "Runtime configuration loaded: environment=%s oracle_configured=%s",
    ENVIRONMENT,
    bool(ORACLE_USER and ORACLE_DSN),
)
