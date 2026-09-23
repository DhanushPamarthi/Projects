"""Database access for the TestIQ PostgreSQL control plane."""
import os
import sqlite3
from contextlib import contextmanager
from .config import DB_PATH, DATA_DIR, UPLOAD_DIR

DATABASE_URL = os.getenv("DATABASE_URL")

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS testiq;
CREATE TABLE IF NOT EXISTS testiq.project (
  project_id TEXT PRIMARY KEY, project_name TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS testiq.source_document (
  source_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, source_type TEXT NOT NULL,
  logical_name TEXT NOT NULL, owner_name TEXT, active_revision_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(project_id, source_type, logical_name)
);
CREATE TABLE IF NOT EXISTS testiq.source_revision (
  revision_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES testiq.source_document(source_id),
  revision INTEGER NOT NULL, filename TEXT NOT NULL, content_type TEXT,
  size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, storage_path TEXT NOT NULL,
  owner_version TEXT, ingestion_version TEXT NOT NULL, status TEXT NOT NULL,
  validation_messages TEXT NOT NULL, duplicate_of_revision_id TEXT, created_at TEXT NOT NULL,
  UNIQUE(source_id, revision)
);
CREATE TABLE IF NOT EXISTS testiq.source_fragment (
  fragment_id TEXT PRIMARY KEY, revision_id TEXT NOT NULL REFERENCES testiq.source_revision(revision_id),
  source_id TEXT,
  fragment_index INTEGER NOT NULL, heading TEXT, source_reference TEXT NOT NULL,
  page_number INTEGER, fragment_type TEXT NOT NULL DEFAULT 'paragraph',
  paragraph_index INTEGER, table_index INTEGER, row_index INTEGER, cell_index INTEGER,
  fragment_text TEXT NOT NULL,
  UNIQUE(revision_id, fragment_index)
);
CREATE TABLE IF NOT EXISTS testiq.source_extraction (
  extraction_id TEXT PRIMARY KEY, revision_id TEXT NOT NULL REFERENCES testiq.source_revision(revision_id),
  mode TEXT NOT NULL, status TEXT NOT NULL, extracted_text TEXT, error TEXT,
  extractor_version TEXT NOT NULL, model_name TEXT, prompt_version TEXT,
  config_version TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS testiq.source_package_manifest (
  package_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, package_version INTEGER NOT NULL,
  scope TEXT, created_at TEXT NOT NULL, UNIQUE(project_id, package_version)
);
CREATE TABLE IF NOT EXISTS testiq.source_package_item (
  package_id TEXT NOT NULL REFERENCES testiq.source_package_manifest(package_id),
  source_id TEXT NOT NULL REFERENCES testiq.source_document(source_id),
  revision_id TEXT NOT NULL REFERENCES testiq.source_revision(revision_id),
  source_version TEXT, ingestion_status TEXT NOT NULL DEFAULT 'unknown', ingestion_version TEXT,
  content_hash TEXT, model_name TEXT, prompt_version TEXT, config_version TEXT,
  PRIMARY KEY(package_id, source_id)
);
CREATE TABLE IF NOT EXISTS testiq.source_relationship (
  relationship_id TEXT PRIMARY KEY, brd_source_id TEXT NOT NULL REFERENCES testiq.source_document(source_id),
  supporting_source_id TEXT NOT NULL REFERENCES testiq.source_document(source_id),
  relationship_type TEXT NOT NULL DEFAULT 'supports', created_at TEXT NOT NULL,
  UNIQUE(brd_source_id, supporting_source_id)
);
CREATE TABLE IF NOT EXISTS testiq.test_run (
  run_id TEXT PRIMARY KEY, package_id TEXT NOT NULL REFERENCES testiq.source_package_manifest(package_id),
  project_id TEXT NOT NULL, status TEXT NOT NULL, configuration_snapshot TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS testiq.requirement_analysis (
  analysis_id TEXT PRIMARY KEY,
  package_id TEXT NOT NULL REFERENCES testiq.source_package_manifest(package_id),
  project_id TEXT NOT NULL,
  analysis_version INTEGER NOT NULL,
  status TEXT NOT NULL,
  contract_json TEXT NOT NULL,
  reviewer TEXT,
  review_decision TEXT,
  review_comments TEXT,
  reviewed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(package_id, analysis_version)
);
"""
SQLITE_SCHEMA = SCHEMA.replace("CREATE SCHEMA IF NOT EXISTS testiq;\n", "").replace("testiq.", "")

MIGRATIONS = [
    "ALTER TABLE source_fragment ADD COLUMN source_id TEXT",
    "ALTER TABLE source_package_item ADD COLUMN source_version TEXT",
    "ALTER TABLE source_package_item ADD COLUMN ingestion_status TEXT NOT NULL DEFAULT 'unknown'",
    "ALTER TABLE source_package_item ADD COLUMN ingestion_version TEXT",
    "ALTER TABLE source_fragment ADD COLUMN fragment_type TEXT NOT NULL DEFAULT 'paragraph'",
    "ALTER TABLE source_fragment ADD COLUMN paragraph_index INTEGER",
    "ALTER TABLE source_fragment ADD COLUMN table_index INTEGER",
    "ALTER TABLE source_fragment ADD COLUMN row_index INTEGER",
    "ALTER TABLE source_fragment ADD COLUMN cell_index INTEGER",
    "ALTER TABLE source_extraction ADD COLUMN model_name TEXT",
    "ALTER TABLE source_extraction ADD COLUMN prompt_version TEXT",
    "ALTER TABLE source_extraction ADD COLUMN config_version TEXT",
    "ALTER TABLE source_package_item ADD COLUMN content_hash TEXT",
    "ALTER TABLE source_package_item ADD COLUMN model_name TEXT",
    "ALTER TABLE source_package_item ADD COLUMN prompt_version TEXT",
    "ALTER TABLE source_package_item ADD COLUMN config_version TEXT",
    "ALTER TABLE test_run ADD COLUMN configuration_snapshot TEXT",
]

class _SQLiteConnection:
    def __init__(self, conn): self._conn = conn
    def execute(self, sql, params=()): return self._conn.execute(sql.replace("%s", "?"), params)
    def executescript(self, sql): return self._conn.executescript(sql)
    def commit(self): self._conn.commit()
    def rollback(self): self._conn.rollback()
    def close(self): self._conn.close()

def init_db():
    # Original source files remain on the application filesystem even when
    # PostgreSQL is used for the control-plane metadata.
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if DATABASE_URL:
        import psycopg
        with psycopg.connect(DATABASE_URL) as conn:
            for statement in SCHEMA.split(";"):
                if statement.strip(): conn.execute(statement)
            conn.execute("SET search_path TO testiq")
            for index, statement in enumerate(MIGRATIONS):
                savepoint = f"migration_{index}"
                conn.execute(f"SAVEPOINT {savepoint}")
                try:
                    conn.execute(statement)
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                except Exception:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            conn.commit()
    else:
        with sqlite3.connect(DB_PATH) as conn:
            conn.executescript(SQLITE_SCHEMA)
            for statement in MIGRATIONS:
                try: conn.execute(statement)
                except sqlite3.OperationalError:
                    pass
            conn.commit()

@contextmanager
def connection():
    init_db()
    if DATABASE_URL:
        import psycopg
        from psycopg.rows import dict_row
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        conn.execute("SET search_path TO testiq")
    else:
        conn = _SQLiteConnection(sqlite3.connect(DB_PATH))
        conn._conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
