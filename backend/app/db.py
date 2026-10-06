"""Database access for the TestIQ PostgreSQL control plane."""
import sqlite3
import threading
import re
from contextlib import contextmanager
from .config import (DATABASE_URL, DB_PATH, DATA_DIR, PGVECTOR_DIMENSIONS, PGVECTOR_MODE,
                     POSTGRES_LOCK_TIMEOUT_MS, POSTGRES_POOL_MAX_SIZE, POSTGRES_POOL_MIN_SIZE,
                     POSTGRES_POOL_TIMEOUT_SECONDS, POSTGRES_STATEMENT_TIMEOUT_MS, UPLOAD_DIR)
PGVECTOR_AVAILABLE = False
_POSTGRES_POOL = None
_POSTGRES_POOL_LOCK = threading.Lock()
_INIT_DONE = False
_INIT_LOCK = threading.Lock()

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
  package_name TEXT, scope TEXT, manifest_hash TEXT, lifecycle_status TEXT NOT NULL DEFAULT 'draft',
  rule_review_status TEXT NOT NULL DEFAULT 'pending', rule_set_hash TEXT, rules_reviewed_at TEXT,
  run_id TEXT, created_at TEXT NOT NULL, UNIQUE(project_id, package_version)
);
CREATE TABLE IF NOT EXISTS testiq.package_business_rule (
  rule_id TEXT PRIMARY KEY,
  package_id TEXT NOT NULL REFERENCES testiq.source_package_manifest(package_id),
  ordinal INTEGER NOT NULL,
  statement TEXT NOT NULL,
  source_fragment_id TEXT REFERENCES testiq.source_fragment(fragment_id),
  source_reference TEXT,
  impact_if_missing TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
  reviewer TEXT,
  review_comment TEXT,
  reviewed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(package_id, ordinal)
);
CREATE INDEX IF NOT EXISTS testiq_package_business_rule_scope_idx ON testiq.package_business_rule(package_id, status, ordinal);
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
  analysis_kind TEXT NOT NULL DEFAULT 'legacy',
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
CREATE TABLE IF NOT EXISTS testiq.test_data_bronze_ref (
  bronze_ref_id TEXT PRIMARY KEY,
  record_id TEXT NOT NULL REFERENCES testiq.test_data_record(record_id),
  generation_id TEXT NOT NULL REFERENCES testiq.test_case_generation(generation_id),
  test_case_id TEXT NOT NULL,
  bronze_schema TEXT NOT NULL,
  bronze_table TEXT NOT NULL,
  bronze_id TEXT NOT NULL,
  bronze_load_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(record_id, bronze_schema, bronze_table, bronze_id)
);
CREATE INDEX IF NOT EXISTS testiq_test_data_bronze_ref_record_idx ON testiq.test_data_bronze_ref(record_id);
CREATE INDEX IF NOT EXISTS testiq_test_data_bronze_ref_case_idx ON testiq.test_data_bronze_ref(generation_id,test_case_id);
CREATE TABLE IF NOT EXISTS testiq.test_execution_result (
  result_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES testiq.test_run(run_id),
  generation_id TEXT NOT NULL REFERENCES testiq.test_case_generation(generation_id),
  test_case_id TEXT NOT NULL,
  status TEXT NOT NULL,
  expected_json TEXT NOT NULL,
  actual_json TEXT NOT NULL,
  assertions_json TEXT NOT NULL,
  bronze_refs_json TEXT NOT NULL,
  started_at TEXT NOT NULL,
  completed_at TEXT NOT NULL,
  error TEXT,
  UNIQUE(run_id, test_case_id)
);
CREATE INDEX IF NOT EXISTS testiq_test_execution_result_run_idx ON testiq.test_execution_result(run_id);
CREATE INDEX IF NOT EXISTS testiq_test_execution_result_case_idx ON testiq.test_execution_result(generation_id,test_case_id);
CREATE TABLE IF NOT EXISTS testiq.knowledge_index (
  index_id TEXT PRIMARY KEY, package_id TEXT NOT NULL REFERENCES testiq.source_package_manifest(package_id),
  project_id TEXT NOT NULL REFERENCES testiq.project(project_id), manifest_hash TEXT NOT NULL,
  knowledge_version INTEGER NOT NULL DEFAULT 1, parent_index_id TEXT,
  build_mode TEXT NOT NULL DEFAULT 'full', reused_chunk_count INTEGER NOT NULL DEFAULT 0,
  new_chunk_count INTEGER NOT NULL DEFAULT 0, retired_chunk_count INTEGER NOT NULL DEFAULT 0,
  config_hash TEXT NOT NULL, provider TEXT NOT NULL, embedding_model TEXT NOT NULL,
  graph_model TEXT NOT NULL, dimensions INTEGER NOT NULL, status TEXT NOT NULL,
  configuration_json TEXT NOT NULL, chunker_version TEXT NOT NULL, graph_prompt_version TEXT NOT NULL,
  graph_prompt_hash TEXT NOT NULL, chunk_count INTEGER NOT NULL DEFAULT 0, node_count INTEGER NOT NULL DEFAULT 0,
  edge_count INTEGER NOT NULL DEFAULT 0, progress_percent INTEGER NOT NULL DEFAULT 0,
  progress_stage TEXT NOT NULL DEFAULT 'pending', progress_message TEXT,
  error TEXT, created_at TEXT NOT NULL, UNIQUE(package_id, config_hash)
);
CREATE TABLE IF NOT EXISTS testiq.knowledge_chunk (
  chunk_id TEXT PRIMARY KEY, index_id TEXT NOT NULL REFERENCES testiq.knowledge_index(index_id),
  package_id TEXT NOT NULL REFERENCES testiq.source_package_manifest(package_id),
  source_id TEXT NOT NULL REFERENCES testiq.source_document(source_id),
  revision_id TEXT NOT NULL REFERENCES testiq.source_revision(revision_id),
  fragment_id TEXT NOT NULL REFERENCES testiq.source_fragment(fragment_id), source_type TEXT NOT NULL,
  source_reference TEXT NOT NULL, heading_path TEXT, chunk_text TEXT NOT NULL, content_hash TEXT NOT NULL,
  embedding_json TEXT NOT NULL, metadata_json TEXT NOT NULL, UNIQUE(index_id, fragment_id, chunk_id)
);
CREATE INDEX IF NOT EXISTS testiq_knowledge_chunk_scope_idx ON testiq.knowledge_chunk(package_id,index_id,source_type);
CREATE TABLE IF NOT EXISTS testiq.knowledge_node (
  node_id TEXT PRIMARY KEY, index_id TEXT NOT NULL REFERENCES testiq.knowledge_index(index_id),
  package_id TEXT NOT NULL REFERENCES testiq.source_package_manifest(package_id), canonical_id TEXT NOT NULL,
  label TEXT NOT NULL, entity_type TEXT NOT NULL, aliases_json TEXT NOT NULL, evidence_chunk_ids_json TEXT NOT NULL,
  UNIQUE(index_id,canonical_id)
);
CREATE TABLE IF NOT EXISTS testiq.knowledge_edge (
  edge_id TEXT PRIMARY KEY, index_id TEXT NOT NULL REFERENCES testiq.knowledge_index(index_id),
  package_id TEXT NOT NULL REFERENCES testiq.source_package_manifest(package_id),
  source_node_id TEXT NOT NULL REFERENCES testiq.knowledge_node(node_id),
  target_node_id TEXT NOT NULL REFERENCES testiq.knowledge_node(node_id), relationship_type TEXT NOT NULL,
  evidence_chunk_ids_json TEXT NOT NULL, confidence REAL NOT NULL,
  UNIQUE(index_id,source_node_id,target_node_id,relationship_type)
);
CREATE TABLE IF NOT EXISTS testiq.knowledge_index_review (
  review_id TEXT PRIMARY KEY, index_id TEXT NOT NULL REFERENCES testiq.knowledge_index(index_id),
  reviewer TEXT NOT NULL, decision TEXT NOT NULL CHECK(decision IN ('approved','needs_changes','rejected')),
  comments TEXT, object_hash TEXT NOT NULL, reviewed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS testiq.knowledge_retrieval_run (
  retrieval_id TEXT PRIMARY KEY, index_id TEXT NOT NULL REFERENCES testiq.knowledge_index(index_id),
  package_id TEXT NOT NULL REFERENCES testiq.source_package_manifest(package_id), query_text TEXT NOT NULL,
  query_hash TEXT NOT NULL, requested_by TEXT NOT NULL, purpose TEXT NOT NULL,
  retrieval_config_json TEXT NOT NULL, result_json TEXT NOT NULL, context_hash TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS testiq_knowledge_retrieval_scope_idx ON testiq.knowledge_retrieval_run(package_id,index_id,created_at);
CREATE TABLE IF NOT EXISTS testiq.knowledge_evaluation_run (
  evaluation_id TEXT PRIMARY KEY, index_id TEXT NOT NULL REFERENCES testiq.knowledge_index(index_id),
  package_id TEXT NOT NULL REFERENCES testiq.source_package_manifest(package_id), requested_by TEXT NOT NULL,
  questions_json TEXT NOT NULL, result_json TEXT NOT NULL, passed INTEGER NOT NULL, created_at TEXT NOT NULL
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
    "ALTER TABLE source_package_manifest ADD COLUMN rule_review_status TEXT NOT NULL DEFAULT 'pending'",
    "ALTER TABLE source_package_manifest ADD COLUMN rule_set_hash TEXT",
    "ALTER TABLE source_package_manifest ADD COLUMN rules_reviewed_at TEXT",
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
    "ALTER TABLE knowledge_index ADD COLUMN progress_percent INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE knowledge_index ADD COLUMN progress_stage TEXT NOT NULL DEFAULT 'pending'",
    "ALTER TABLE knowledge_index ADD COLUMN progress_message TEXT",
    "ALTER TABLE knowledge_index ADD COLUMN knowledge_version INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE knowledge_index ADD COLUMN parent_index_id TEXT",
    "ALTER TABLE requirement_analysis ADD COLUMN analysis_kind TEXT NOT NULL DEFAULT 'legacy'",
    "ALTER TABLE knowledge_index ADD COLUMN build_mode TEXT NOT NULL DEFAULT 'full'",
    "ALTER TABLE knowledge_index ADD COLUMN reused_chunk_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE knowledge_index ADD COLUMN new_chunk_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE knowledge_index ADD COLUMN retired_chunk_count INTEGER NOT NULL DEFAULT 0",
    "UPDATE knowledge_index SET knowledge_version=(SELECT package_version FROM source_package_manifest WHERE source_package_manifest.package_id=knowledge_index.package_id) WHERE knowledge_version=1 AND EXISTS (SELECT 1 FROM source_package_manifest WHERE source_package_manifest.package_id=knowledge_index.package_id)",
    "CREATE INDEX IF NOT EXISTS testiq_knowledge_index_project_version_idx ON knowledge_index(project_id,knowledge_version)",
    "CREATE TABLE IF NOT EXISTS test_data_bronze_ref (bronze_ref_id TEXT PRIMARY KEY, record_id TEXT NOT NULL REFERENCES test_data_record(record_id), generation_id TEXT NOT NULL REFERENCES test_case_generation(generation_id), test_case_id TEXT NOT NULL, bronze_schema TEXT NOT NULL, bronze_table TEXT NOT NULL, bronze_id TEXT NOT NULL, bronze_load_id TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(record_id, bronze_schema, bronze_table, bronze_id))",
    "CREATE INDEX IF NOT EXISTS testiq_test_data_bronze_ref_record_idx ON test_data_bronze_ref(record_id)",
    "CREATE INDEX IF NOT EXISTS testiq_test_data_bronze_ref_case_idx ON test_data_bronze_ref(generation_id,test_case_id)",
    "CREATE TABLE IF NOT EXISTS test_execution_result (result_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES test_run(run_id), generation_id TEXT NOT NULL REFERENCES test_case_generation(generation_id), test_case_id TEXT NOT NULL, status TEXT NOT NULL, expected_json TEXT NOT NULL, actual_json TEXT NOT NULL, assertions_json TEXT NOT NULL, bronze_refs_json TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT NOT NULL, error TEXT, UNIQUE(run_id, test_case_id))",
    "CREATE INDEX IF NOT EXISTS testiq_test_execution_result_run_idx ON test_execution_result(run_id)",
    "CREATE INDEX IF NOT EXISTS testiq_test_execution_result_case_idx ON test_execution_result(generation_id,test_case_id)",
]

class _SQLiteConnection:
    def __init__(self, conn): self._conn = conn
    def execute(self, sql, params=()): return self._conn.execute(sql.replace("%s", "?"), params)
    def executemany(self, sql, params): return self._conn.executemany(sql.replace("%s", "?"), params)
    def executescript(self, sql): return self._conn.executescript(sql)
    def commit(self): self._conn.commit()
    def rollback(self): self._conn.rollback()
    def close(self): self._conn.close()

def init_db():
    global _INIT_DONE
    if _INIT_DONE:
        return
    with _INIT_LOCK:
        if _INIT_DONE:
            return
        _init_db_uncached()
        _INIT_DONE = True


def _init_db_uncached():
    # Original source files remain on the application filesystem even when
    # PostgreSQL is used for the control-plane metadata.
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if DATABASE_URL:
        import psycopg
        with psycopg.connect(DATABASE_URL, options=_postgres_session_options()) as conn:
            global PGVECTOR_AVAILABLE
            with conn.pipeline():
                catalog_cursor = conn.execute(
                    "SELECT n.nspname,c.relname FROM pg_namespace n "
                    "LEFT JOIN pg_class c ON c.relnamespace=n.oid WHERE n.nspname='testiq'"
                )
                columns_cursor = conn.execute(
                    "SELECT table_name,column_name FROM information_schema.columns WHERE table_schema='testiq'"
                )
                extension_cursor = conn.execute(
                    "SELECT installed_version FROM pg_available_extensions WHERE name='vector'"
                ) if PGVECTOR_MODE != "disabled" else None
            catalog_rows = catalog_cursor.fetchall()
            schema_exists = bool(catalog_rows)
            existing_relations = {row[1] for row in catalog_rows if row[1]}
            existing_columns = {
                (row[0], row[1]) for row in columns_cursor.fetchall()
            }
            for statement in SCHEMA.split(";"):
                statement = statement.strip()
                if not statement:
                    continue
                # PostgreSQL still takes relation locks for CREATE ... IF NOT
                # EXISTS. Avoid touching objects that already exist so app
                # startup is not blocked by unrelated long-running sessions.
                if re.match(r"^CREATE\s+SCHEMA\s+IF\s+NOT\s+EXISTS\s+testiq$", statement, re.IGNORECASE):
                    if schema_exists:
                        continue
                table_match = re.match(r"^CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(?:testiq\.)?([A-Za-z_][A-Za-z0-9_]*)", statement, re.IGNORECASE)
                if table_match and table_match.group(1) in existing_relations:
                    continue
                index_match = re.match(r"^CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)", statement, re.IGNORECASE)
                if index_match and index_match.group(1) in existing_relations:
                    continue
                conn.execute(statement)
                if table_match:
                    existing_relations.add(table_match.group(1))
                if index_match:
                    existing_relations.add(index_match.group(1))
                if re.match(r"^CREATE\s+SCHEMA", statement, re.IGNORECASE):
                    schema_exists = True
            PGVECTOR_AVAILABLE = False
            if PGVECTOR_MODE != "disabled":
                available = extension_cursor.fetchone()
                if available:
                    vector_index = "testiq_knowledge_chunk_embedding_hnsw_idx"
                    vector_is_ready = bool(
                        available[0]
                        and ("knowledge_chunk", "embedding_vector") in existing_columns
                        and vector_index in existing_relations
                    )
                    if vector_is_ready:
                        PGVECTOR_AVAILABLE = True
                    else:
                        conn.execute("SAVEPOINT pgvector_setup")
                        try:
                            if not available[0]:
                                conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
                            if ("knowledge_chunk", "embedding_vector") not in existing_columns:
                                conn.execute(f"ALTER TABLE testiq.knowledge_chunk ADD COLUMN IF NOT EXISTS embedding_vector vector({PGVECTOR_DIMENSIONS})")
                                existing_columns.add(("knowledge_chunk", "embedding_vector"))
                            if vector_index not in existing_relations:
                                conn.execute("CREATE INDEX IF NOT EXISTS testiq_knowledge_chunk_embedding_hnsw_idx ON testiq.knowledge_chunk USING hnsw (embedding_vector vector_cosine_ops) WITH (m=16,ef_construction=64)")
                                existing_relations.add(vector_index)
                            conn.execute("RELEASE SAVEPOINT pgvector_setup")
                            PGVECTOR_AVAILABLE = True
                        except Exception:
                            conn.execute("ROLLBACK TO SAVEPOINT pgvector_setup")
                            conn.execute("RELEASE SAVEPOINT pgvector_setup")
                if PGVECTOR_MODE == "required" and not PGVECTOR_AVAILABLE:
                    raise RuntimeError("pgvector is required but is not installed or could not be enabled")
            maintenance_statements = []
            for index, statement in enumerate(MIGRATIONS):
                add_column = re.match(
                    r"^ALTER TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+ADD COLUMN\s+([A-Za-z_][A-Za-z0-9_]*)\s+",
                    statement,
                    re.IGNORECASE,
                )
                if add_column and (add_column.group(1), add_column.group(2)) in existing_columns:
                    continue
                create_table = re.match(r"^CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)", statement, re.IGNORECASE)
                if create_table and create_table.group(1) in existing_relations:
                    continue
                create_index = re.match(r"^CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)", statement, re.IGNORECASE)
                if create_index and create_index.group(1) in existing_relations:
                    continue
                if re.match(r"^UPDATE\s+", statement, re.IGNORECASE):
                    maintenance_statements.append(statement)
                    continue
                savepoint = f"migration_{index}"
                conn.execute(f"SAVEPOINT {savepoint}")
                try:
                    conn.execute(statement)
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                    if add_column:
                        existing_columns.add((add_column.group(1), add_column.group(2)))
                    if create_table:
                        existing_relations.add(create_table.group(1))
                    if create_index:
                        existing_relations.add(create_index.group(1))
                except Exception:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            maintenance_statements.extend([
                "UPDATE source_document SET logical_source_id=source_id WHERE logical_source_id IS NULL",
                "UPDATE source_fragment f SET source_id=r.source_id FROM source_revision r WHERE f.revision_id=r.revision_id AND f.source_id IS NULL",
                "UPDATE source_fragment SET provenance_mode='local' WHERE provenance_mode IS NULL",
                "UPDATE requirement_analysis SET analysis_kind=CASE "
                "WHEN contract_json LIKE '%\"functional_requirements_generation\"%true%' THEN 'functional' "
                "WHEN contract_json LIKE '%\"business_requirements_generation\"%true%' THEN 'business' "
                "ELSE analysis_kind END WHERE analysis_kind='legacy'",
            ])
            with conn.pipeline():
                for statement in maintenance_statements:
                    conn.execute(statement)
                conn.execute("COMMIT")
            if "test_data_template" in existing_relations:
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
            conn.execute("UPDATE requirement_analysis SET analysis_kind=CASE "
                         "WHEN contract_json LIKE '%\"functional_requirements_generation\"%true%' THEN 'functional' "
                         "WHEN contract_json LIKE '%\"business_requirements_generation\"%true%' THEN 'business' "
                         "ELSE analysis_kind END WHERE analysis_kind='legacy'")
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
        pool = _get_postgres_pool()
        with pool.connection(timeout=POSTGRES_POOL_TIMEOUT_SECONDS) as conn:
            yield conn
        return
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


def _postgres_session_options():
    return (
        f"-c lock_timeout={POSTGRES_LOCK_TIMEOUT_MS}ms "
        f"-c statement_timeout={POSTGRES_STATEMENT_TIMEOUT_MS}ms "
        "-c search_path=testiq"
    )


def _get_postgres_pool():
    global _POSTGRES_POOL
    if _POSTGRES_POOL is not None:
        return _POSTGRES_POOL
    with _POSTGRES_POOL_LOCK:
        if _POSTGRES_POOL is None:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
            _POSTGRES_POOL = ConnectionPool(
                conninfo=DATABASE_URL,
                min_size=max(1, POSTGRES_POOL_MIN_SIZE),
                max_size=max(POSTGRES_POOL_MIN_SIZE, POSTGRES_POOL_MAX_SIZE),
                timeout=POSTGRES_POOL_TIMEOUT_SECONDS,
                kwargs={"row_factory": dict_row, "options": _postgres_session_options()},
                open=True,
                name="testiq-postgres",
            )
    return _POSTGRES_POOL


def close_connection_pool():
    global _POSTGRES_POOL
    with _POSTGRES_POOL_LOCK:
        if _POSTGRES_POOL is not None:
            _POSTGRES_POOL.close()
            _POSTGRES_POOL = None


def warm_connection_pool():
    init_db()
    if DATABASE_URL:
        _get_postgres_pool().wait(timeout=POSTGRES_POOL_TIMEOUT_SECONDS)
