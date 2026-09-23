import io
import sys
import types
import uuid
from io import BytesIO
from pathlib import Path
from fastapi.testclient import TestClient
import pytest

def _client(monkeypatch):
    data = Path(".test-data") / uuid.uuid4().hex
    monkeypatch.setenv("TESTIQ_DATA_DIR", str(data))
    monkeypatch.setenv("TESTIQ_DB_PATH", str(data / "test.db"))
    import importlib
    import app.config, app.db, app.main
    importlib.reload(app.config); importlib.reload(app.db); importlib.reload(app.main)
    return TestClient(app.main.app)

def test_upload_revision_duplicate_manifest(monkeypatch):
    client = _client(monkeypatch)
    body = b"# Login\nThe user must sign in with email and password."
    first = client.post("/api/v1/projects/p1/sources", files={"file": ("requirements.md", io.BytesIO(body), "text/markdown")}, data={"source_type": "brd", "owner": "product", "owner_version": "1.0"})
    assert first.status_code == 202, first.text
    first_data = first.json()
    assert first_data["active"] is True
    duplicate = client.post("/api/v1/projects/p1/sources", files={"file": ("requirements.md", io.BytesIO(body), "text/markdown")}, data={"source_type": "brd"})
    assert duplicate.status_code == 201
    assert duplicate.json()["status"] == "duplicate"
    duplicate_revision = client.get("/api/v1/revisions/" + duplicate.json()["revision_id"])
    assert duplicate_revision.json()["extraction_summary"]["status"] == "skipped"
    assert duplicate_revision.json()["extraction_summary"]["extraction_mode"] == "reused"
    assert duplicate_revision.json()["extraction_summary"]["reused_revision_id"] == first_data["revision_id"]
    changed = client.post("/api/v1/projects/p1/sources", files={"file": ("requirements.md", io.BytesIO(body + b"\n# Logout"), "text/markdown")}, data={"source_type": "brd", "owner_version": "2.0"})
    assert changed.status_code == 202
    assert changed.json()["revision"] == 3
    manifest = client.post("/api/v1/projects/p1/manifests", json={"source_ids": [first_data["source_id"]], "scope": "MVP"})
    assert manifest.status_code == 201
    assert manifest.json()["items"][0]["revision_id"] == changed.json()["revision_id"]
    fetched = client.get("/api/v1/manifests/" + manifest.json()["package_id"])
    assert fetched.status_code == 200
    assert fetched.json()["items"][0]["sha256"] == changed.json()["sha256"]

def test_upload_validation(monkeypatch):
    client = _client(monkeypatch)
    empty = client.post("/api/v1/projects/p1/sources", files={"file": ("empty.md", io.BytesIO(b""), "text/markdown")}, data={"source_type": "brd"})
    assert empty.status_code == 422
    assert empty.json()["status"] == "rejected"
    assert empty.json()["original_preserved"] is True
    assert client.get("/api/v1/revisions/" + empty.json()["revision_id"]).json()["status"] == "rejected"
    unsupported = client.post("/api/v1/projects/p1/sources", files={"file": ("bad.exe", io.BytesIO(b"x"), "application/octet-stream")}, data={"source_type": "brd"})
    assert unsupported.status_code == 415
    assert unsupported.json()["original_preserved"] is True
    bad_docx = client.post("/api/v1/projects/p1/sources", files={"file": ("bad.docx", io.BytesIO(b"not-a-docx"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}, data={"source_type": "brd"})
    assert bad_docx.status_code == 422
    assert "readable DOCX" in bad_docx.json()["validation_messages"][0]
    mismatch = client.post("/api/v1/projects/p1/sources", files={"file": ("valid.md", io.BytesIO(b"# Scope\nReadable text"), "application/pdf")}, data={"source_type": "brd"})
    assert mismatch.status_code == 202
    assert "detected type was used" in mismatch.json()["validation_messages"][0]

def test_openai_first_falls_back_to_local_when_key_missing(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = _client(monkeypatch)
    response = client.post("/api/v1/projects/p1/sources", files={"file": ("brd.txt", io.BytesIO(b"# Scope\nBuild a portal."), "text/plain")}, data={"source_type": "brd"})
    assert response.status_code == 202
    assert response.json()["extraction"]["status"] == "pending"
    revision = client.get("/api/v1/revisions/" + response.json()["revision_id"])
    assert revision.status_code == 200
    assert revision.json()["extraction_summary"]["extraction_mode"] == "local"
    assert any("OpenAI extraction failed" in message for message in revision.json()["validation_messages"])

def test_supporting_source_manifest_is_traceable(monkeypatch):
    client = _client(monkeypatch)
    brd = client.post("/api/v1/projects/p1/sources", files={"file": ("brd.md", io.BytesIO(b"# Scope\n# Requirements\nBuild it."), "text/markdown")}, data={"source_type": "brd", "owner_version": "3.0"})
    assert brd.status_code == 202
    brd_id = brd.json()["source_id"]
    api = client.post("/api/v1/projects/p1/sources", files={"file": ("api.md", io.BytesIO(b"# Endpoints\nGET /members"), "text/markdown")}, data={"source_type": "api_spec", "brd_source_id": brd_id, "owner_version": "2.1"})
    assert api.status_code == 202, api.text
    listed = client.get("/api/v1/projects/p1/sources").json()
    brd_row = next(item for item in listed if item["source_id"] == brd_id)
    assert brd_row["supporting_sources"][0]["source_type"] == "api_spec"
    manifest = client.post("/api/v1/projects/p1/manifests", json={"source_ids": [brd_id, api.json()["source_id"]], "scope": "release-1"})
    assert manifest.status_code == 201
    assert all(item["ingestion_status"] == "uploaded" for item in manifest.json()["items"])
    brd_item = next(item for item in manifest.json()["items"] if item["source_id"] == brd_id)
    assert brd_item["content_hash"] == brd.json()["sha256"]
    assert brd_item["model_name"]
    assert brd_item["prompt_version"] == "brd-extraction-v1"
    assert brd_item["config_version"] == "1.0.0"
    fetched_manifest = client.get("/api/v1/manifests/" + manifest.json()["package_id"])
    assert fetched_manifest.status_code == 200
    assert fetched_manifest.json()["package_id"] == manifest.json()["package_id"]
    assert all(item["content_hash"] for item in fetched_manifest.json()["items"])

def test_requirement_analysis_contract_can_be_created_and_reviewed_without_graphrag(monkeypatch):
    client = _client(monkeypatch)
    brd = client.post(
        "/api/v1/projects/p1/sources",
        files={"file": ("brd.md", io.BytesIO(b"# Eligibility\nEligible members must have a refill due.\n# Acceptance Criteria\nExpected outcome: send an SMS reminder."), "text/markdown")},
        data={"source_type": "brd"},
    )
    assert brd.status_code == 202
    manifest = client.post("/api/v1/projects/p1/manifests", json={"source_ids": [brd.json()["source_id"]], "scope": "MVP"})
    assert manifest.status_code == 201
    analysis = client.post(f"/api/v1/packages/{manifest.json()['package_id']}/requirements/analyze")
    assert analysis.status_code == 201, analysis.text
    payload = analysis.json()
    assert payload["status"] == "needs_review"
    assert payload["contract"]["generation_mode"] == "source_package_fallback"
    assert payload["contract"]["requirements"][0]["source_references"]
    reviewed = client.post(
        f"/api/v1/requirement-analyses/{payload['analysis_id']}/review",
        json={"reviewer": "product-owner", "decision": "approved", "comments": "POC scope accepted", "approved_items": ["REQ-001", "RULE-1"]},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "approved"
    assert reviewed.json()["contract"]["approved_items"] == ["REQ-001", "RULE-1"]


def test_all_supporting_source_types_require_and_retain_brd_association(monkeypatch):
    client = _client(monkeypatch)
    brd = client.post("/api/v1/projects/p1/sources", files={"file": ("brd.md", io.BytesIO(b"# Scope\nCampaign scope"), "text/markdown")}, data={"source_type": "brd", "owner": "Product", "owner_version": "1.0"})
    assert brd.status_code == 202
    brd_id = brd.json()["source_id"]
    supporting_types = ["campaign_rules", "api_spec", "database_schema", "message_template", "ui_description"]
    for index, source_type in enumerate(supporting_types, 1):
        response = client.post(
            "/api/v1/projects/p1/sources",
            files={"file": (f"{source_type}.md", io.BytesIO(f"# Details\n{source_type} content".encode()), "text/markdown")},
            data={"source_type": source_type, "brd_source_id": brd_id, "owner": f"Team {index}", "owner_version": f"{index}.0"},
        )
        assert response.status_code == 202, response.text
    missing_association = client.post("/api/v1/projects/p1/sources", files={"file": ("orphan.md", io.BytesIO(b"# Details\nNo BRD"), "text/markdown")}, data={"source_type": "api_spec"})
    assert missing_association.status_code == 422
    listed = client.get("/api/v1/projects/p1/sources").json()
    brd_row = next(source for source in listed if source["source_id"] == brd_id)
    assert {source["source_type"] for source in brd_row["supporting_sources"]} == set(supporting_types)
    assert all(source["active_revision"]["owner_version"] for source in listed if source["source_type"] in supporting_types)

def test_unreadable_source_and_missing_sections_are_visible(monkeypatch):
    client = _client(monkeypatch)
    unreadable = client.post("/api/v1/projects/p1/sources", files={"file": ("broken.docx", io.BytesIO(b"not-a-docx"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}, data={"source_type": "brd"})
    assert unreadable.status_code == 422
    unreadable_revision = client.get("/api/v1/revisions/" + unreadable.json()["revision_id"]).json()
    assert unreadable_revision["status"] == "rejected"
    assert unreadable_revision["validation_messages"]
    missing = client.post("/api/v1/projects/p2/sources", files={"file": ("short.md", io.BytesIO(b"# Scope\nOnly scope"), "text/markdown")}, data={"source_type": "brd", "required_sections": "Scope,Requirements"})
    assert missing.status_code == 202

    revision = client.get("/api/v1/revisions/" + missing.json()["revision_id"])
    assert revision.status_code == 200
    data = revision.json()
    assert data["source"]["source_id"] == missing.json()["source_id"]
    assert data["source"]["source_type"] == "brd"
    assert data["extraction_summary"]["text_available"] is True
    assert data["extraction_summary"]["section_count"] == 1
    assert data["extraction_summary"]["missing_sections"] == ["Requirements"]
    assert data["fragments"][0]["heading"] == "Scope"
    assert data["fragments"][0]["source_reference"].startswith("line ")


def test_docx_fragments_preserve_paragraph_and_table_locations(monkeypatch):
    from docx import Document

    client = _client(monkeypatch)
    document = Document()
    document.add_heading("Scope", level=1)
    document.add_paragraph("The campaign must support refill reminders.")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Scenario"
    table.cell(0, 1).text = "Expected result"
    buffer = BytesIO()
    document.save(buffer)
    response = client.post("/api/v1/projects/p1/sources", files={"file": ("requirements.docx", buffer.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}, data={"source_type": "brd"})
    assert response.status_code == 202
    revision = client.get("/api/v1/revisions/" + response.json()["revision_id"]).json()
    assert any(fragment["fragment_type"] == "paragraph" and fragment["source_reference"] == "paragraph 1" for fragment in revision["fragments"])
    assert any(fragment["fragment_type"] == "table" and fragment["source_reference"] == "table 1, row 1" and fragment["heading"] == "Scope" for fragment in revision["fragments"])


@pytest.mark.skip(reason="GraphRAG is owned by another team")
def test_graphrag_index_builds_cited_chunks_and_graph(monkeypatch):
    pytest.skip("GraphRAG implementation is owned by another team")
    class FakeCollection:
        def __init__(self):
            self.upserts = []

        def upsert(self, **kwargs):
            self.upserts.append(kwargs)

        def query(self, query_embeddings, n_results):
            ids = self.upserts[-1]["ids"][:n_results] if self.upserts else []
            return {"ids": [ids], "distances": [[0.1 for _ in ids]]}

    class FakeClient:
        collections = {}

        def __init__(self, path):
            self.path = path

        def get_or_create_collection(self, name, metadata):
            self.name = name
            self.metadata = metadata
            self.collections.setdefault((self.path, name), FakeCollection())
            return self.collections[(self.path, name)]

        def get_collection(self, name):
            return self.collections[(self.path, name)]

    class FakeGraph:
        def __init__(self):
            self.nodes = {}
            self.edges = []

        def add_node(self, node_id, **attrs):
            self.nodes[node_id] = attrs

        def add_edge(self, source, target, **attrs):
            self.edges.append((source, target, attrs))

        def number_of_nodes(self):
            return len(self.nodes)

        def number_of_edges(self):
            return len(self.edges)

    def fake_node_link_data(graph):
        return {
            "nodes": [{"id": node_id, **attrs} for node_id, attrs in graph.nodes.items()],
            "links": [{"source": source, "target": target, **attrs} for source, target, attrs in graph.edges],
        }

    monkeypatch.setitem(sys.modules, "networkx", types.SimpleNamespace(MultiDiGraph=FakeGraph, node_link_data=fake_node_link_data))
    monkeypatch.setitem(sys.modules, "chromadb", types.SimpleNamespace(PersistentClient=FakeClient))

    client = _client(monkeypatch)
    brd = client.post(
        "/api/v1/projects/p1/sources",
        files={"file": ("brd.md", io.BytesIO(b"# Scope\nSend refill reminders.\n# Requirements\nMembers need opt-out support."), "text/markdown")},
        data={"source_type": "brd", "owner": "Product", "owner_version": "1.0"},
    )
    assert brd.status_code == 202
    api = client.post(
        "/api/v1/projects/p1/sources",
        files={"file": ("api.md", io.BytesIO(b"# API\nPOST /reminders\nField: memberId"), "text/markdown")},
        data={"source_type": "api_spec", "brd_source_id": brd.json()["source_id"], "owner": "Integration", "owner_version": "1.0"},
    )
    assert api.status_code == 202
    manifest = client.post(
        "/api/v1/projects/p1/manifests",
        json={"source_ids": [brd.json()["source_id"], api.json()["source_id"]], "scope": "PBM GraphRAG POC"},
    )
    assert manifest.status_code == 201

    index = client.post(f"/api/v1/packages/{manifest.json()['package_id']}/graphrag/index", json={})
    assert index.status_code == 202, index.text
    index_data = index.json()
    assert index_data["status"] == "queued"
    index_data = client.get(f"/api/v1/graphrag/indexes/{index_data['index_id']}").json()
    assert index_data["status"] == "completed"
    assert index_data["chunk_count"] >= 2
    assert index_data["vector_collection"] == index_data["index_id"]

    chunks = client.get(f"/api/v1/graphrag/indexes/{index_data['index_id']}/chunks").json()
    assert chunks
    assert chunks[0]["source_references"][0]["fragment_id"].startswith("frag_")
    assert chunks[0]["package_id"] == manifest.json()["package_id"]

    graph = client.get(f"/api/v1/graphrag/indexes/{index_data['index_id']}/graph").json()
    assert any(node.get("node_type") == "Package" for node in graph["nodes"])
    assert any(edge.get("relationship") == "CITES" for edge in graph["links"])

    retrieval = client.post(
        f"/api/v1/graphrag/indexes/{index_data['index_id']}/retrieve",
        json={"query": "Which requirements mention reminders and opt-out support?", "top_k": 2, "max_graph_hops": 1, "token_budget": 1200},
    )
    assert retrieval.status_code == 200, retrieval.text
    retrieval_data = retrieval.json()
    assert retrieval_data["retrieval_mode"] == "scoped_vector_graph"
    assert retrieval_data["vector_backend"] == "chromadb"
    assert retrieval_data["candidate_count"] >= 1
    assert retrieval_data["chunks"]
    assert retrieval_data["chunks"][0]["package_id"] == manifest.json()["package_id"]
    assert retrieval_data["citations"]
    assert retrieval_data["graph_context"]["nodes"]
    assert retrieval_data["graph_context"]["edges"]
    assert retrieval_data["agent_context"].startswith("Question:")
    assert retrieval_data["reviewer_evidence"]
    assert retrieval_data["reviewer_evidence"][0]["provenance_paths"]
    assert retrieval_data["graph_evidence"]["edges"]
    assert retrieval_data["evidence_summary"]["evidence_count"] == len(retrieval_data["reviewer_evidence"])

    evaluation = client.post(f"/api/v1/graphrag/indexes/{index_data['index_id']}/evaluate", json={})
    assert evaluation.status_code == 200, evaluation.text
    evaluation_data = evaluation.json()
    assert evaluation_data["question_count"] == 3
    assert evaluation_data["passed_count"] >= 1
    assert 0 <= evaluation_data["pass_rate"] <= 1
    assert evaluation_data["items"][0]["checks"]["has_evidence"] is True
    assert evaluation_data["items"][0]["retrieval"]["reviewer_evidence"]

    reused = client.post(f"/api/v1/packages/{manifest.json()['package_id']}/graphrag/index", json={})
    assert reused.status_code == 200
    assert reused.json()["reused"] is True
