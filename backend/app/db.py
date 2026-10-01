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
  source_id TEXT PRIMARY KEY, logical_source_id TEXT NOT NULL, project_id TEXT NOT NULL REFERENCES testiq.project(project_id), source_type TEXT NOT NULL CHECK (source_type IN ('brd','campaign_rules','api_spec','database_schema','message_template','ui_description')),
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
  source_id TEXT NOT NULL REFERENCES testiq.source_document(source_id),
  fragment_index INTEGER NOT NULL, heading TEXT, source_reference TEXT NOT NULL,
  page_number INTEGER, fragment_type TEXT NOT NULL DEFAULT 'paragraph',
  paragraph_index INTEGER, table_index INTEGER, row_index INTEGER, cell_index INTEGER,
  heading_path TEXT, paragraph_start INTEGER, paragraph_end INTEGER, character_start INTEGER, character_end INTEGER,
  content_hash TEXT, extractor_version TEXT, fragment_text TEXT NOT NULL,
  UNIQUE(revision_id, fragment_index)
);
CREATE TABLE IF NOT EXISTS testiq.source_extraction (
  extraction_id TEXT PRIMARY KEY, revision_id TEXT NOT NULL REFERENCES testiq.source_revision(revision_id),
  mode TEXT NOT NULL, status TEXT NOT NULL, extracted_text TEXT, error TEXT,
  extractor_version TEXT NOT NULL, model_name TEXT, prompt_version TEXT,
  config_version TEXT, prompt_hash TEXT, config_hash TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS testiq.source_package_manifest (
  package_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES testiq.project(project_id), package_version INTEGER NOT NULL,
  package_name TEXT, scope TEXT, manifest_hash TEXT, lifecycle_status TEXT NOT NULL DEFAULT 'draft', run_id TEXT, created_at TEXT NOT NULL, UNIQUE(project_id, package_version)
);
CREATE TABLE IF NOT EXISTS testiq.source_package_item (
  package_id TEXT NOT NULL REFERENCES testiq.source_package_manifest(package_id),
  source_id TEXT NOT NULL REFERENCES testiq.source_document(source_id),
  revision_id TEXT NOT NULL REFERENCES testiq.source_revision(revision_id),
  source_version TEXT, ingestion_status TEXT NOT NULL DEFAULT 'unknown', ingestion_version TEXT,
  content_hash TEXT, model_name TEXT, prompt_version TEXT, config_version TEXT, extractor_version TEXT, prompt_hash TEXT, config_hash TEXT, duplicate_of_revision_id TEXT,
  PRIMARY KEY(package_id, source_id)
);
CREATE TABLE IF NOT EXISTS testiq.source_ingestion_run (
  run_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES testiq.project(project_id), package_id TEXT, status TEXT NOT NULL,
  configuration_snapshot TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS testiq.source_relationship (
  relationship_id TEXT PRIMARY KEY, brd_source_id TEXT NOT NULL REFERENCES testiq.source_document(source_id),
  supporting_source_id TEXT NOT NULL REFERENCES testiq.source_document(source_id),
  relationship_type TEXT NOT NULL DEFAULT 'supports', created_at TEXT NOT NULL,
  UNIQUE(brd_source_id, supporting_source_id)
);
CREATE TABLE IF NOT EXISTS testiq.test_run (
  run_id TEXT PRIMARY KEY, package_id TEXT NOT NULL REFERENCES testiq.source_package_manifest(package_id),
  project_id TEXT NOT NULL REFERENCES testiq.project(project_id), status TEXT NOT NULL, configuration_snapshot TEXT,
  generation_id TEXT,
  selected_test_case_ids_json TEXT,
  frozen_contract_json TEXT,
  baseline_hash TEXT,
  reviewer TEXT,
  review_comments TEXT,
  lifecycle_state TEXT,
  approved_at TEXT,
  started_at TEXT,
  completed_at TEXT,
  cancelled_at TEXT,
  error TEXT,
  immutable_observations_json TEXT,
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
CREATE TABLE IF NOT EXISTS testiq.test_case_generation (
  generation_id TEXT PRIMARY KEY,
  analysis_id TEXT NOT NULL REFERENCES testiq.requirement_analysis(analysis_id),
  package_id TEXT NOT NULL REFERENCES testiq.source_package_manifest(package_id),
  project_id TEXT NOT NULL,
  status TEXT NOT NULL,
  contract_json TEXT NOT NULL,
  model_name TEXT,
  prompt_version TEXT,
  prompt_hash TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS testiq.discovery_context (
  context_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES testiq.project(project_id),
  package_id TEXT REFERENCES testiq.source_package_manifest(package_id),
  context_type TEXT NOT NULL CHECK (context_type IN ('web_ui','api','schema')),
  name TEXT NOT NULL,
  description TEXT,
  details_json TEXT NOT NULL,
  created_by TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS testiq.requirement_review_audit (
  audit_id TEXT PRIMARY KEY,
  analysis_id TEXT NOT NULL REFERENCES testiq.requirement_analysis(analysis_id),
  package_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  reviewer TEXT NOT NULL,
  decision TEXT NOT NULL,
  comments TEXT,
  analysis_version INTEGER NOT NULL,
  contract_version TEXT,
  reviewed_contract_hash TEXT NOT NULL,
  approved_items_json TEXT NOT NULL,
  rejected_items_json TEXT NOT NULL,
  pending_items_json TEXT NOT NULL,
  accepted_assumptions_json TEXT NOT NULL,
  resolved_gaps_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS testiq.test_case_review_audit (
  audit_id TEXT PRIMARY KEY,
  generation_id TEXT NOT NULL REFERENCES testiq.test_case_generation(generation_id),
  analysis_id TEXT NOT NULL,
  package_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  reviewer TEXT NOT NULL,
  comments TEXT,
  reviewed_contract_hash TEXT NOT NULL,
  approved_test_case_ids_json TEXT NOT NULL,
  rejected_test_case_ids_json TEXT NOT NULL,
  pending_test_case_ids_json TEXT NOT NULL,
  reviewed_contract_json TEXT,
  reviewed_items_json TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS testiq.test_data_record (
  record_id TEXT PRIMARY KEY,
  generation_id TEXT NOT NULL REFERENCES testiq.test_case_generation(generation_id),
  test_case_id TEXT NOT NULL,
  template_id TEXT NOT NULL,
  template_name TEXT NOT NULL,
  entity_name TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  pre_steps_json TEXT NOT NULL,
  schema_context_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'draft',
  bronze_raw_file_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(generation_id, test_case_id, template_id)
);
"""
SQLITE_SCHEMA = SCHEMA.replace("CREATE SCHEMA IF NOT EXISTS testiq;\n", "").replace("testiq.", "")

MIGRATIONS = [
    "ALTER TABLE test_data_record ADD COLUMN schema_context_json TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE test_data_record ADD COLUMN template_name TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE source_document ADD COLUMN logical_source_id TEXT",
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
    "ALTER TABLE test_run ADD COLUMN generation_id TEXT",
    "ALTER TABLE test_run ADD COLUMN selected_test_case_ids_json TEXT",
    "ALTER TABLE test_run ADD COLUMN frozen_contract_json TEXT",
    "ALTER TABLE test_run ADD COLUMN baseline_hash TEXT",
    "ALTER TABLE test_run ADD COLUMN reviewer TEXT",
    "ALTER TABLE test_run ADD COLUMN review_comments TEXT",
    "ALTER TABLE test_run ADD COLUMN lifecycle_state TEXT",
    "ALTER TABLE test_run ADD COLUMN approved_at TEXT",
    "ALTER TABLE test_run ADD COLUMN started_at TEXT",
    "ALTER TABLE test_run ADD COLUMN completed_at TEXT",
    "ALTER TABLE test_run ADD COLUMN cancelled_at TEXT",
    "ALTER TABLE test_run ADD COLUMN error TEXT",
    "ALTER TABLE test_run ADD COLUMN immutable_observations_json TEXT",
    "ALTER TABLE source_package_manifest ADD COLUMN package_name TEXT",
    "ALTER TABLE source_extraction ADD COLUMN prompt_hash TEXT",
    "ALTER TABLE source_extraction ADD COLUMN config_hash TEXT",
    "ALTER TABLE source_package_manifest ADD COLUMN manifest_hash TEXT",
    "ALTER TABLE source_package_manifest ADD COLUMN lifecycle_status TEXT NOT NULL DEFAULT 'draft'",
    "ALTER TABLE source_package_manifest ADD COLUMN run_id TEXT",
    "ALTER TABLE source_package_item ADD COLUMN extractor_version TEXT",
    "ALTER TABLE source_package_item ADD COLUMN prompt_hash TEXT",
    "ALTER TABLE source_package_item ADD COLUMN config_hash TEXT",
    "ALTER TABLE source_package_item ADD COLUMN duplicate_of_revision_id TEXT",
    "ALTER TABLE source_fragment ADD COLUMN heading_path TEXT",
    "ALTER TABLE source_fragment ADD COLUMN paragraph_start INTEGER",
    "ALTER TABLE source_fragment ADD COLUMN paragraph_end INTEGER",
    "ALTER TABLE source_fragment ADD COLUMN character_start INTEGER",
    "ALTER TABLE source_fragment ADD COLUMN character_end INTEGER",
    "ALTER TABLE source_fragment ADD COLUMN content_hash TEXT",
    "ALTER TABLE source_fragment ADD COLUMN extractor_version TEXT",
    "ALTER TABLE source_fragment ADD COLUMN provenance_mode TEXT",
    "ALTER TABLE test_case_review_audit ADD COLUMN reviewed_contract_json TEXT",
    "ALTER TABLE test_case_review_audit ADD COLUMN reviewed_items_json TEXT",
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
            conn.execute("UPDATE source_document SET logical_source_id=source_id WHERE logical_source_id IS NULL")
            conn.execute("UPDATE source_fragment f SET source_id=r.source_id FROM source_revision r WHERE f.revision_id=r.revision_id AND f.source_id IS NULL")
            conn.execute("UPDATE source_fragment SET provenance_mode='local' WHERE provenance_mode IS NULL")
            if conn.execute("SELECT to_regclass('testiq.test_data_template')").fetchone()[0]:
                conn.execute("UPDATE test_data_record r SET template_name=t.template_name FROM test_data_template t WHERE r.template_id=t.template_id AND r.template_name=''")
                conn.execute("ALTER TABLE test_data_record DROP CONSTRAINT IF EXISTS test_data_record_template_id_fkey")
                conn.execute("DROP TABLE test_data_template")
            conn.commit()
    else:
        with sqlite3.connect(DB_PATH) as conn:
            conn.executescript(SQLITE_SCHEMA)
            for statement in MIGRATIONS:
                try: conn.execute(statement)
                except sqlite3.OperationalError:
                    pass
            conn.execute("UPDATE source_document SET logical_source_id=source_id WHERE logical_source_id IS NULL")
            conn.execute("UPDATE source_fragment SET source_id=(SELECT source_id FROM source_revision WHERE source_revision.revision_id=source_fragment.revision_id) WHERE source_id IS NULL")
            conn.execute("UPDATE source_fragment SET provenance_mode='local' WHERE provenance_mode IS NULL")
            conn.commit()
            _migrate_sqlite_test_data_templates(conn)

def _migrate_sqlite_test_data_templates(conn):
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "test_data_record" not in tables:
        return
    columns = {row[1] for row in conn.execute("PRAGMA table_info(test_data_record)").fetchall()}
    if "template_name" not in columns:
        conn.execute("ALTER TABLE test_data_record ADD COLUMN template_name TEXT NOT NULL DEFAULT ''")
        columns.add("template_name")
    if "test_data_template" in tables:
        conn.execute("UPDATE test_data_record SET template_name=(SELECT template_name FROM test_data_template WHERE test_data_template.template_id=test_data_record.template_id) WHERE template_name='' AND EXISTS (SELECT 1 FROM test_data_template WHERE test_data_template.template_id=test_data_record.template_id)")
    foreign_keys = conn.execute("PRAGMA foreign_key_list(test_data_record)").fetchall()
    references_templates = any(row[2] == "test_data_template" for row in foreign_keys)
    if references_templates:
        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("""CREATE TABLE test_data_record_new (
          record_id TEXT PRIMARY KEY,
          generation_id TEXT NOT NULL REFERENCES test_case_generation(generation_id),
          test_case_id TEXT NOT NULL,
          template_id TEXT NOT NULL,
          template_name TEXT NOT NULL DEFAULT '',
          entity_name TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          pre_steps_json TEXT NOT NULL,
          schema_context_json TEXT NOT NULL DEFAULT '[]',
          status TEXT NOT NULL DEFAULT 'draft',
          bronze_raw_file_id TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(generation_id, test_case_id, template_id)
        )""")
        column_list = "record_id,generation_id,test_case_id,template_id,template_name,entity_name,payload_json,pre_steps_json,schema_context_json,status,bronze_raw_file_id,created_at,updated_at"
        conn.execute(f"INSERT INTO test_data_record_new ({column_list}) SELECT {column_list} FROM test_data_record")
        conn.execute("DROP TABLE test_data_record")
        conn.execute("ALTER TABLE test_data_record_new RENAME TO test_data_record")
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")
    if "test_data_template" in tables:
        conn.execute("DROP TABLE test_data_template")
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
