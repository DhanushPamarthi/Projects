import importlib
import json
import sys
import types
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException


def _modules(monkeypatch):
    data = Path(".test-data") / uuid.uuid4().hex
    monkeypatch.setenv("TESTIQ_DATA_DIR", str(data))
    monkeypatch.setenv("TESTIQ_DB_PATH", str(data / "test.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for name in ["app.knowledge_fabric", "app.db", "app.config"]:
        sys.modules.pop(name, None)
    import app.config, app.db, app.knowledge_fabric
    importlib.reload(app.config)
    importlib.reload(app.db)
    importlib.reload(app.knowledge_fabric)
    return app.db, app.knowledge_fabric


def _seed_package(db, package_id, version, revision_id, fragment_id, review_status="approved", text="Eligible members must receive the campaign reminder."):
    timestamp = "2026-01-01T00:00:00+00:00"
    with db.connection() as conn:
        if not conn.execute("SELECT 1 FROM project WHERE project_id='p1'").fetchone():
            conn.execute("INSERT INTO project (project_id,project_name,created_at) VALUES (%s,%s,%s)", ("p1", "Project", timestamp))
        if not conn.execute("SELECT 1 FROM source_document WHERE source_id='source-1'").fetchone():
            conn.execute("INSERT INTO source_document (source_id,logical_source_id,project_id,source_type,logical_name,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                ("source-1", "source-1", "p1", "brd", "Campaign BRD", timestamp, timestamp))
        conn.execute("INSERT INTO source_revision (revision_id,source_id,revision,filename,size_bytes,sha256,storage_path,ingestion_version,status,validation_messages,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (revision_id, "source-1", version, "campaign.md", 100, f"sha-{version}", "unused", "1", "accepted", "[]", timestamp))
        conn.execute("INSERT INTO source_fragment (fragment_id,revision_id,source_id,fragment_index,heading,heading_path,source_reference,fragment_type,content_hash,extractor_version,fragment_text) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (fragment_id, revision_id, "source-1", 1, "Requirements", "Requirements", "paragraph 1", "paragraph", f"fragment-{version}", "test", text))
        conn.execute("INSERT INTO source_package_manifest (package_id,project_id,package_version,package_name,manifest_hash,lifecycle_status,rule_review_status,rule_set_hash,rules_reviewed_at,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (package_id, "p1", version, f"Package {version}", f"manifest-{version}", "ready", review_status,
             f"rules-{version}" if review_status == "approved" else None, timestamp if review_status == "approved" else None, timestamp))
        conn.execute("INSERT INTO source_package_item (package_id,source_id,revision_id,ingestion_status,content_hash) VALUES (%s,%s,%s,%s,%s)",
            (package_id, "source-1", revision_id, "succeeded", f"sha-{version}"))
        conn.execute("INSERT INTO package_business_rule (rule_id,package_id,ordinal,statement,source_fragment_id,source_reference,impact_if_missing,status,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (f"rule-{version}", package_id, 1, text, fragment_id, "Campaign BRD | paragraph 1", "Requirement would be incomplete.",
             "approved" if review_status == "approved" else "pending", timestamp, timestamp))


class _FakeOpenAI:
    def __init__(self):
        self.embedding_inputs = []
        self.embeddings = types.SimpleNamespace(create=self._embed)
        self.responses = types.SimpleNamespace(create=self._respond)

    def _embed(self, **kwargs):
        values = kwargs["input"]
        self.embedding_inputs.extend(values)
        return types.SimpleNamespace(data=[types.SimpleNamespace(index=index, embedding=[0.1] * 1536) for index, _ in enumerate(values)])

    @staticmethod
    def _respond(**_kwargs):
        return types.SimpleNamespace(output_text=json.dumps({"entities": [], "relationships": []}))


def test_build_requires_completed_business_rule_review(monkeypatch):
    db, knowledge = _modules(monkeypatch)
    _seed_package(db, "pkg-1", 1, "rev-1", "frag-1", review_status="pending")
    with pytest.raises(HTTPException) as failure:
        knowledge.build_knowledge_index("pkg-1", knowledge.BuildIndexRequest())
    assert failure.value.status_code == 409
    assert "Business Use Case" in str(failure.value.detail)


def test_project_knowledge_versions_reuse_unchanged_embeddings_and_graph(monkeypatch):
    db, knowledge = _modules(monkeypatch)
    fake = _FakeOpenAI()
    monkeypatch.setattr(knowledge, "_create_client", lambda: fake)
    _seed_package(db, "pkg-1", 1, "rev-1", "frag-1")
    first = knowledge.build_knowledge_index("pkg-1", knowledge.BuildIndexRequest())
    _seed_package(db, "pkg-2", 2, "rev-2", "frag-2")
    second = knowledge.build_knowledge_index("pkg-2", knowledge.BuildIndexRequest())
    _seed_package(db, "pkg-3", 3, "rev-3", "frag-3", text="Eligible members must receive the updated campaign reminder.")
    third = knowledge.build_knowledge_index("pkg-3", knowledge.BuildIndexRequest())

    assert first["knowledge_version"] == 1
    assert first["build_mode"] == "full"
    assert second["knowledge_version"] == 2
    assert second["parent_index_id"] == first["index_id"]
    assert second["build_mode"] == "incremental"
    assert second["reused_chunk_count"] == 1
    assert second["new_chunk_count"] == 0
    assert second["retired_chunk_count"] == 0
    assert third["knowledge_version"] == 3
    assert third["parent_index_id"] == second["index_id"]
    assert third["build_mode"] == "incremental"
    assert third["reused_chunk_count"] == 0
    assert third["new_chunk_count"] == 1
    assert third["retired_chunk_count"] == 1
    assert len(fake.embedding_inputs) == 2
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM knowledge_index WHERE project_id='p1' AND status='ready'").fetchone()["count"] == 3
        assert conn.execute("SELECT COUNT(*) AS count FROM knowledge_node WHERE index_id=%s", (second["index_id"],)).fetchone()["count"] > 0
