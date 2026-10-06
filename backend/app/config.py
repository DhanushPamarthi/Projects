from pathlib import Path
import os
from urllib.parse import quote

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("TESTIQ_DATA_DIR", BASE_DIR / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = Path(os.getenv("TESTIQ_DB_PATH", DATA_DIR / "testiq.db"))

def _postgres_url_from_parts() -> str | None:
    host = os.getenv("TESTIQ_POSTGRES_HOST")
    if not host:
        return None
    user = quote(os.getenv("TESTIQ_POSTGRES_USER", "testiq"), safe="")
    password = os.getenv("TESTIQ_POSTGRES_PASSWORD")
    password_part = f":{quote(password, safe='')}" if password is not None else ""
    port = os.getenv("TESTIQ_POSTGRES_PORT", "5432")
    database = quote(os.getenv("TESTIQ_POSTGRES_DATABASE", "testiq"), safe="")
    sslmode = os.getenv("TESTIQ_POSTGRES_SSLMODE")
    query = f"?sslmode={quote(sslmode, safe='')}" if sslmode else ""
    return f"postgresql://{user}{password_part}@{host}:{port}/{database}{query}"

DATABASE_URL = os.getenv("DATABASE_URL") or _postgres_url_from_parts()
BRONZE_SCHEMA = os.getenv("BRONZE_SCHEMA", "bronzebox")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TIMEOUT_SECONDS = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "30"))
OPENAI_PROMPT_CACHING_MODE = os.getenv("OPENAI_PROMPT_CACHING_MODE", "disabled").strip().lower()
if OPENAI_PROMPT_CACHING_MODE not in {"disabled", "explicit", "implicit"}:
    raise ValueError("OPENAI_PROMPT_CACHING_MODE must be disabled, explicit, or implicit")
OPENAI_EMBEDDING_DIMENSIONS = int(os.getenv("OPENAI_EMBEDDING_DIMENSIONS", "1536"))
PGVECTOR_DIMENSIONS = int(os.getenv("PGVECTOR_DIMENSIONS", str(OPENAI_EMBEDDING_DIMENSIONS)))
PGVECTOR_HNSW_EF_SEARCH = int(os.getenv("PGVECTOR_HNSW_EF_SEARCH", "100"))
PGVECTOR_MODE = os.getenv("TESTIQ_PGVECTOR_MODE", "auto").strip().lower()
if PGVECTOR_MODE not in {"auto", "disabled", "required"}:
    raise ValueError("TESTIQ_PGVECTOR_MODE must be auto, disabled, or required")
POSTGRES_LOCK_TIMEOUT_MS = int(os.getenv("TESTIQ_POSTGRES_LOCK_TIMEOUT_MS", "5000"))
POSTGRES_STATEMENT_TIMEOUT_MS = int(os.getenv("TESTIQ_POSTGRES_STATEMENT_TIMEOUT_MS", "90000"))
POSTGRES_POOL_MIN_SIZE = int(os.getenv("TESTIQ_POSTGRES_POOL_MIN_SIZE", "1"))
POSTGRES_POOL_MAX_SIZE = int(os.getenv("TESTIQ_POSTGRES_POOL_MAX_SIZE", "10"))
POSTGRES_POOL_TIMEOUT_SECONDS = float(os.getenv("TESTIQ_POSTGRES_POOL_TIMEOUT_SECONDS", "15"))
EXTRACTION_PROMPT_VERSION = os.getenv("EXTRACTION_PROMPT_VERSION", "brd-extraction-v1")
REQUIREMENTS_ANALYSIS_PROMPT_VERSION = os.getenv("REQUIREMENTS_ANALYSIS_PROMPT_VERSION", "requirements-analysis-v2")
TEST_CASE_GENERATION_PROMPT_VERSION = os.getenv("TEST_CASE_GENERATION_PROMPT_VERSION", "test-case-generation-v7")
EXTRACTION_CONFIG_VERSION = os.getenv("EXTRACTION_CONFIG_VERSION", "1.0.0")
LOCAL_EXTRACTOR_VERSION = os.getenv("LOCAL_EXTRACTOR_VERSION", "local-parser-v1")
REQUIRED_BRD_SECTIONS = [item.strip() for item in os.getenv("REQUIRED_BRD_SECTIONS", "").split(",") if item.strip()]
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".json", ".yaml", ".yml", ".sql", ".csv"}
