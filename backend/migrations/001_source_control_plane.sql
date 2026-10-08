-- TestIQ source-ingestion control plane. Run with a PostgreSQL migration tool.
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
  scope TEXT, automation_stage TEXT NOT NULL DEFAULT 'not_started', automation_error TEXT,
  created_at TEXT NOT NULL, UNIQUE(project_id, package_version)
);
ALTER TABLE testiq.source_package_manifest ADD COLUMN IF NOT EXISTS automation_stage TEXT NOT NULL DEFAULT 'not_started';
ALTER TABLE testiq.source_package_manifest ADD COLUMN IF NOT EXISTS automation_error TEXT;

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

ALTER TABLE testiq.source_fragment ADD COLUMN IF NOT EXISTS source_id TEXT;
ALTER TABLE testiq.source_package_item ADD COLUMN IF NOT EXISTS source_version TEXT;
ALTER TABLE testiq.source_package_item ADD COLUMN IF NOT EXISTS ingestion_status TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE testiq.source_package_item ADD COLUMN IF NOT EXISTS ingestion_version TEXT;
ALTER TABLE testiq.source_fragment ADD COLUMN IF NOT EXISTS fragment_type TEXT NOT NULL DEFAULT 'paragraph';
ALTER TABLE testiq.source_fragment ADD COLUMN IF NOT EXISTS paragraph_index INTEGER;
ALTER TABLE testiq.source_fragment ADD COLUMN IF NOT EXISTS table_index INTEGER;
ALTER TABLE testiq.source_fragment ADD COLUMN IF NOT EXISTS row_index INTEGER;
ALTER TABLE testiq.source_fragment ADD COLUMN IF NOT EXISTS cell_index INTEGER;
ALTER TABLE testiq.source_extraction ADD COLUMN IF NOT EXISTS model_name TEXT;
ALTER TABLE testiq.source_extraction ADD COLUMN IF NOT EXISTS prompt_version TEXT;
ALTER TABLE testiq.source_extraction ADD COLUMN IF NOT EXISTS config_version TEXT;
ALTER TABLE testiq.source_package_item ADD COLUMN IF NOT EXISTS content_hash TEXT;
ALTER TABLE testiq.source_package_item ADD COLUMN IF NOT EXISTS model_name TEXT;
ALTER TABLE testiq.source_package_item ADD COLUMN IF NOT EXISTS prompt_version TEXT;
ALTER TABLE testiq.source_package_item ADD COLUMN IF NOT EXISTS config_version TEXT;
ALTER TABLE testiq.test_run ADD COLUMN IF NOT EXISTS configuration_snapshot TEXT;
