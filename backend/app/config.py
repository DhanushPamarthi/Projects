from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("TESTIQ_DATA_DIR", BASE_DIR / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = Path(os.getenv("TESTIQ_DB_PATH", DATA_DIR / "testiq.db"))
DATABASE_URL = os.getenv("DATABASE_URL")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TIMEOUT_SECONDS = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "30"))
OPENAI_EMBEDDING_DIMENSIONS = int(os.getenv("OPENAI_EMBEDDING_DIMENSIONS", "1536"))
PGVECTOR_DIMENSIONS = int(os.getenv("PGVECTOR_DIMENSIONS", str(OPENAI_EMBEDDING_DIMENSIONS)))
PGVECTOR_HNSW_EF_SEARCH = int(os.getenv("PGVECTOR_HNSW_EF_SEARCH", "100"))
EXTRACTION_PROMPT_VERSION = os.getenv("EXTRACTION_PROMPT_VERSION", "brd-extraction-v1")
REQUIREMENTS_ANALYSIS_PROMPT_VERSION = os.getenv("REQUIREMENTS_ANALYSIS_PROMPT_VERSION", "requirements-analysis-v2")
TEST_CASE_GENERATION_PROMPT_VERSION = os.getenv("TEST_CASE_GENERATION_PROMPT_VERSION", "test-case-generation-v1")
EXTRACTION_CONFIG_VERSION = os.getenv("EXTRACTION_CONFIG_VERSION", "1.0.0")
LOCAL_EXTRACTOR_VERSION = os.getenv("LOCAL_EXTRACTOR_VERSION", "local-parser-v1")
REQUIRED_BRD_SECTIONS = [item.strip() for item in os.getenv("REQUIRED_BRD_SECTIONS", "").split(",") if item.strip()]
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".json", ".yaml", ".yml", ".sql", ".csv"}
