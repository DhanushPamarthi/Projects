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
    monkeypatch.setenv("TESTIQ_AUTH_REQUIRED", "false")
    import importlib
    import app.config, app.db, app.auth, app.main
    importlib.reload(app.config); importlib.reload(app.db); importlib.reload(app.auth); importlib.reload(app.main)
    # Uploads now reject unknown projects, so seed the stable IDs used by the
    # API-focused tests instead of weakening the production validation.
    with app.db.connection() as conn:
        for project_id in ("p1", "p2"):
            conn.execute(
                "INSERT INTO project (project_id,project_name,created_at) VALUES (%s,%s,%s)",
                (project_id, project_id, "2026-01-01T00:00:00Z"),
            )
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


def test_distinct_brds_remain_separate_and_can_share_one_package(monkeypatch):
    client = _client(monkeypatch)
    first = client.post("/api/v1/projects/p1/sources", files={"file": ("eligibility.md", io.BytesIO(b"# Eligibility\nEligible members receive a reminder."), "text/markdown")}, data={"source_type": "brd"})
    second = client.post("/api/v1/projects/p1/sources", files={"file": ("delivery.md", io.BytesIO(b"# Delivery\nSend the reminder by SMS."), "text/markdown")}, data={"source_type": "brd"})
    assert first.status_code == second.status_code == 202
    assert first.json()["source_id"] != second.json()["source_id"]
    package = client.post("/api/v1/projects/p1/manifests", json={"source_ids": [first.json()["source_id"], second.json()["source_id"]], "scope": "MVP"})
    assert package.status_code == 201, package.text
    assert {item["source_id"] for item in package.json()["items"]} == {first.json()["source_id"], second.json()["source_id"]}


def test_explicit_brd_version_upload_keeps_lineage_and_workspace_lists_all_brds(monkeypatch):
    client = _client(monkeypatch)
    first = client.post("/api/v1/projects/p1/sources", files={"file": ("requirements-v1.md", io.BytesIO(b"# Scope\nInitial requirements."), "text/markdown")}, data={"source_type": "brd", "owner_version": "1.0"})
    second = client.post("/api/v1/projects/p1/sources", files={"file": ("delivery.md", io.BytesIO(b"# Scope\nDelivery requirements."), "text/markdown")}, data={"source_type": "brd", "owner_version": "1.0"})
    assert first.status_code == second.status_code == 202

    updated = client.post(
        "/api/v1/projects/p1/sources",
        files={"file": ("requirements-renamed-v2.md", io.BytesIO(b"# Scope\nUpdated requirements."), "text/markdown")},
        data={"source_type": "brd", "owner_version": "2.0", "version_of_source_id": first.json()["source_id"]},
    )
    assert updated.status_code == 202, updated.text
    assert updated.json()["source_id"] == first.json()["source_id"]
    assert updated.json()["revision"] == 2

    sources = client.get("/api/v1/projects/p1/sources").json()
    assert len([source for source in sources if source["source_type"] == "brd"]) == 2
    revisions = client.get(f"/api/v1/sources/{first.json()['source_id']}/revisions").json()["revisions"]
    assert [revision["revision"] for revision in revisions] == [2, 1]

    summary = next(item for item in client.get("/api/v1/projects/workspace-summary").json() if item["project_id"] == "p1")
    assert len(summary["brds"]) == 2
    versioned = next(brd for brd in summary["brds"] if brd["source_id"] == first.json()["source_id"])
    assert versioned["revision_count"] == 2
    assert versioned["active_revision"]["filename"] == "requirements-renamed-v2.md"


def test_brd_version_upload_keeps_package_lineage_and_preserves_previous_snapshot(monkeypatch):
    client = _client(monkeypatch)
    import app.main
    monkeypatch.setattr(app.main, "_run_package_automation", lambda package_id: None)
    first = client.post(
        "/api/v1/projects/p1/sources",
        files={"file": ("requirements-v1.md", io.BytesIO(b"# Scope\nInitial requirements."), "text/markdown")},
        data={"source_type": "brd", "owner_version": "1.0"},
    )
    assert first.status_code == 202, first.text

    packages_after_v1 = client.get("/api/v1/projects/p1/manifests").json()
    assert len(packages_after_v1) == 1
    package_v1 = client.get(f"/api/v1/manifests/{packages_after_v1[0]['package_id']}").json()
    brd_v1 = next(item for item in package_v1["items"] if item["source_type"] == "brd")
    assert brd_v1["revision_id"] == first.json()["revision_id"]

    second = client.post(
        "/api/v1/projects/p1/sources",
        files={"file": ("requirements-renamed-v2.md", io.BytesIO(b"# Scope\nUpdated requirements."), "text/markdown")},
        data={
            "source_type": "brd",
            "owner_version": "2.0",
            "version_of_source_id": first.json()["source_id"],
        },
    )
    assert second.status_code == 202, second.text

    packages_after_v2 = client.get("/api/v1/projects/p1/manifests").json()
    assert [package["package_version"] for package in packages_after_v2] == [2, 1]
    package_v2 = client.get(f"/api/v1/manifests/{packages_after_v2[0]['package_id']}").json()
    preserved_package_v1 = client.get(f"/api/v1/manifests/{packages_after_v2[1]['package_id']}").json()
    brd_v2 = next(item for item in package_v2["items"] if item["source_type"] == "brd")
    preserved_brd_v1 = next(item for item in preserved_package_v1["items"] if item["source_type"] == "brd")

    assert package_v2["package_name"] == preserved_package_v1["package_name"] == "p1 Resource Package"
    assert brd_v2["source_id"] == preserved_brd_v1["source_id"] == first.json()["source_id"]
    assert brd_v2["revision_id"] == second.json()["revision_id"]
    assert preserved_brd_v1["revision_id"] == first.json()["revision_id"]
    assert brd_v2["revision"] == 2
    assert preserved_brd_v1["revision"] == 1

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
    assert "unreadable or corrupted" in bad_docx.json()["validation_messages"][0]
    mismatch = client.post("/api/v1/projects/p1/sources", files={"file": ("valid.md", io.BytesIO(b"# Scope\nReadable text"), "application/pdf")}, data={"source_type": "brd"})
    assert mismatch.status_code == 202
    assert "detected type was used" in mismatch.json()["validation_messages"][0]

def test_machine_readable_brd_uses_fast_local_extraction(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = _client(monkeypatch)
    response = client.post("/api/v1/projects/p1/sources", files={"file": ("brd.txt", io.BytesIO(b"# Scope\nBuild a portal."), "text/plain")}, data={"source_type": "brd"})
    assert response.status_code == 202
    assert response.json()["extraction"]["status"] == "pending"
    revision = client.get("/api/v1/revisions/" + response.json()["revision_id"])
    assert revision.status_code == 200
    assert revision.json()["extraction_summary"]["extraction_mode"] == "local"
    assert not any("OpenAI extraction" in message for message in revision.json()["validation_messages"])

def test_supporting_source_manifest_is_traceable(monkeypatch):
    client = _client(monkeypatch)
    brd = client.post("/api/v1/projects/p1/sources", files={"file": ("brd.md", io.BytesIO(b"# Scope\n# Requirements\nBuild it."), "text/markdown")}, data={"source_type": "brd", "owner_version": "3.0"})
    assert brd.status_code == 202
    brd_id = brd.json()["source_id"]
    api = client.post("/api/v1/projects/p1/sources", files={"file": ("api.md", io.BytesIO(b"# Endpoints\nGET /members"), "text/markdown")}, data={"source_type": "api_spec", "brd_source_id": brd_id, "owner_version": "2.1"})
    assert api.status_code == 202, api.text
    listed = client.get("/api/v1/projects/p1/sources").json()
    brd_row = next(item for item in listed if item["source_id"] == brd_id)
    assert any(source["source_id"] == api.json()["source_id"] and source["source_type"] == "api_spec"
               for source in brd_row["supporting_sources"])
    manifest = client.post("/api/v1/projects/p1/manifests", json={"source_ids": [brd_id, api.json()["source_id"]], "scope": "release-1"})
    assert manifest.status_code == 201
    assert all(item["ingestion_status"] == "succeeded" for item in manifest.json()["items"])
    brd_item = next(item for item in manifest.json()["items"] if item["source_id"] == brd_id)
    assert brd_item["content_hash"] == brd.json()["sha256"]
    assert brd_item["model_name"]
    assert brd_item["prompt_version"] == "brd-extraction-v1"
    assert brd_item["config_version"] == "1.0.0"
    fetched_manifest = client.get("/api/v1/manifests/" + manifest.json()["package_id"])
    assert fetched_manifest.status_code == 200
    assert fetched_manifest.json()["package_id"] == manifest.json()["package_id"]
    assert all(item["content_hash"] for item in fetched_manifest.json()["items"])

def test_business_use_cases_are_auto_approved_and_feed_knowledge_base(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    client = _client(monkeypatch)
    import app.main as main_module
    monkeypatch.setattr(main_module, "_run_package_automation", lambda *_args, **_kwargs: None)

    brd = client.post(
        "/api/v1/projects/p1/sources",
        files={"file": ("brd.md", io.BytesIO(b"# Rules\nEligible members must receive a reminder."), "text/markdown")},
        data={"source_type": "brd"},
    )
    assert brd.status_code == 202, brd.text
    revision = client.get(f"/api/v1/revisions/{brd.json()['revision_id']}").json()
    fragment = revision["fragments"][0]

    monkeypatch.setattr(main_module, "_business_requirements_with_timeout", lambda *args, **kwargs: [{
        "business_requirement_id": "BR-001",
        "description": "Eligible members must receive a reminder.",
        "priority": "Must have",
        "business_use_case": "Notify eligible members.",
        "data_entities": ["member"],
        "source_references": [{"fragment_id": fragment["fragment_id"], "source_reference": fragment["source_reference"]}],
        "supporting_source_references": [],
    }])
    manifest = client.post("/api/v1/projects/p1/manifests", json={"source_ids": [brd.json()["source_id"]], "scope": "MVP"})
    assert manifest.status_code == 201, manifest.text
    package_id = manifest.json()["package_id"]

    generated = client.post(f"/api/v1/packages/{package_id}/business-requirements/generate")
    assert generated.status_code == 201, generated.text
    assert generated.json()["contract"]["rag_status"]["used"] is False
    package_rules = client.get(f"/api/v1/packages/{package_id}/business-rules").json()
    assert package_rules["counts"] == {"pending": 0, "approved": 1, "rejected": 0}
    assert package_rules["rules"][0]["statement"] == "Eligible members must receive a reminder."
    assert package_rules["rules"][0]["status"] == "approved"
    assert package_rules["review_status"] == "approved"

    analysis = generated.json()
    review = client.post(
        f"/api/v1/requirement-analyses/{analysis['analysis_id']}/review",
        json={
            "reviewer": "product-owner",
            "decision": "needs_changes",
            "items": [
                {"item_id": "BRD-001", "approved": False, "review_status": "pending"},
                {"item_id": "BRD-001:BR-001", "approved": True, "review_status": "approved", "text": "Eligible members must receive a reminder."},
                {"item_id": "BRD-001:AC-001", "approved": False, "review_status": "pending"},
            ],
        },
    )
    assert review.status_code == 200, review.text
    assert review.json()["contract"]["knowledge_base_rule_status"]["review_status"] == "approved"
    verified_rules = client.get(f"/api/v1/packages/{package_id}/business-rules").json()
    assert verified_rules["counts"] == {"pending": 0, "approved": 1, "rejected": 0}
    assert verified_rules["review_status"] == "approved"

    import app.db as db_module
    import app.knowledge_fabric as knowledge_module

    class FakeKnowledgeClient:
        def __init__(self):
            self.embeddings = types.SimpleNamespace(create=self.embed)
            self.responses = types.SimpleNamespace(create=self.respond)

        @staticmethod
        def embed(**kwargs):
            return types.SimpleNamespace(data=[
                types.SimpleNamespace(index=index, embedding=[0.1] * 1536)
                for index, _ in enumerate(kwargs["input"])
            ])

        @staticmethod
        def respond(**_kwargs):
            return types.SimpleNamespace(output_text='{"entities": [], "relationships": []}')

    monkeypatch.setattr(knowledge_module, "_create_client", lambda: FakeKnowledgeClient())
    built = client.post(f"/api/v1/packages/{package_id}/knowledge-index", json={})
    assert built.status_code == 201, built.text
    assert built.json()["status"] == "ready"
    readiness = client.get(f"/api/v1/packages/{package_id}/rag-readiness")
    assert readiness.status_code == 200, readiness.text
    assert readiness.json()["ready"] is True
    summary = client.get(f"/api/v1/packages/{package_id}/knowledge-summary")
    assert summary.status_code == 200, summary.text
    assert summary.json()["readiness"]["ready"] is True
    assert summary.json()["indexes"][0]["index_id"] == built.json()["index_id"]
    assert summary.json()["business_rules"]["counts"]["approved"] == 1

    index_id = built.json()["index_id"]
    rag = {
        "index_id": index_id,
        "approved_rules": verified_rules["rules"],
        "brd_fragments": [{
            "fragment_id": fragment["fragment_id"],
            "source_reference": fragment["source_reference"],
            "fragment_text": "Eligible members must receive a reminder.",
        }],
        "technical_fragments": [],
        "rag_metadata": {
            "index_id": index_id,
            "context_hash": "context-hash",
            "retrieval_ids": ["ret-1"],
        },
    }

    def ready_context(requested_package_id, requested_by):
        assert requested_package_id == package_id
        assert requested_by == "Functional Requirements"
        with db_module.connection() as conn:
            assert conn.execute("SELECT status FROM knowledge_index WHERE index_id=%s", (index_id,)).fetchone()["status"] == "ready"
        return rag

    def functional_contract(package, _items, _fragments, approved, context, _previous=None):
        assert package["package_id"] == package_id
        assert approved[0]["business_requirement_id"] == "BR-001"
        assert context["index_id"] == index_id
        return {
            "contract_version": "1.0",
            "package_id": package_id,
            "project_id": package["project_id"],
            "requirements": [{
                "requirement_id": "FR-001",
                "title": "Send the reminder",
                "type": "Functional",
                "statement": "The system shall send a reminder to an eligible member.",
                "priority": "Must have",
                "actors": ["member"],
                "entities": ["member"],
                "business_rules": [{"rule_id": "BR-001", "text": approved[0]["text"], "source_references": approved[0]["source_references"]}],
                "acceptance_criteria": [{
                    "acceptance_id": "AC-001",
                    "scenario": "Eligible member reminder",
                    "given": "An eligible member exists.",
                    "when": "The reminder process runs.",
                    "then": "A reminder is sent.",
                    "source_references": approved[0]["source_references"],
                }],
                "expected_outcomes": [{
                    "outcome_id": "EO-001",
                    "text": "The eligible member receives a reminder.",
                    "source_references": approved[0]["source_references"],
                }],
                "flow_steps": ["Identify the member", "Send the reminder"],
                "ambiguities": [],
                "assumptions": [],
                "source_references": approved[0]["source_references"],
            }],
            "approved_business_requirements": approved,
            "global_gaps": [],
            "global_assumptions": [],
            "technical_gaps": [],
            "conflicting_requirements": [],
            "conflicts": [],
            "accepted_assumptions": [],
            "functional_requirements_generation": True,
            "generation_model": "test-model",
        }

    monkeypatch.setattr(main_module, "_require_package_context", ready_context)
    monkeypatch.setattr(main_module, "_generate_functional_contract", functional_contract)
    functional = client.post(f"/api/v1/packages/{package_id}/functional-requirements/generate")
    assert functional.status_code == 201, functional.text
    assert functional.json()["contract"]["requirements"][0]["requirement_id"] == "FR-001"
    assert functional.json()["contract"]["requirements"][0]["expected_outcomes"][0]["outcome_id"] == "EO-001"
    assert functional.json()["contract"]["requirements"][0]["business_rules"][0]["rule_id"] == "BR-001"
    assert functional.json()["contract"]["rag_status"]["used"] is True
    assert functional.json()["contract"]["rag_status"]["index_id"] == index_id
    latest = client.get(f"/api/v1/packages/{package_id}/requirements/latest-set")
    assert latest.status_code == 200, latest.text
    assert latest.json()["business"]["analysis_id"] == analysis["analysis_id"]
    assert latest.json()["functional"]["analysis_id"] == functional.json()["analysis_id"]


def test_functional_requirement_contract_keeps_outcomes_and_business_use_case_ids(monkeypatch):
    _client(monkeypatch)
    import app.main as main_module

    schema = main_module._functional_requirement_schema()
    requirement_schema = schema["properties"]["requirements"]["items"]
    assert "expected_outcomes" in requirement_schema["required"]
    assert "technical_gaps" in schema["required"]

    contract = {
        "requirements": [{
            "requirement_id": "FR-001",
            "business_rules": [{"rule_id": "BR-021", "text": "Approved rule", "source_references": [{"fragment_id": "frag-1", "source_reference": "BRD paragraph 1"}]}],
            "expected_outcomes": [{"outcome_id": "EO-001", "text": "The result is observable.", "source_references": [{"fragment_id": "frag-1", "source_reference": "BRD paragraph 1"}]}],
        }],
    }
    cleaned = main_module._clean_requirement_contract(contract)
    assert cleaned["requirements"][0]["business_rules"][0]["rule_id"] == "BR-021"
    assert cleaned["requirements"][0]["expected_outcomes"][0]["outcome_id"] == "EO-001"

def test_requirement_analysis_requires_openai_and_knowledge_base(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = _client(monkeypatch)
    body = (
        b"# Eligibility\n"
        b"Eligible members must have a refill due. Ineligible members are excluded.\n"
        b"# Acceptance Criteria\n"
        b"Expected outcome: send an SMS reminder. Email may be listed as a future channel.\n"
    )
    brd = client.post(
        "/api/v1/projects/p1/sources",
        files={"file": ("brd.md", io.BytesIO(body), "text/markdown")},
        data={"source_type": "brd"},
    )
    assert brd.status_code == 202
    manifest = client.post("/api/v1/projects/p1/manifests", json={"source_ids": [brd.json()["source_id"]], "scope": "MVP"})
    assert manifest.status_code == 201
    analysis = client.post(f"/api/v1/packages/{manifest.json()['package_id']}/requirements/analyze")
    assert analysis.status_code == 503, analysis.text
    assert "requires OpenAI and an approved Knowledge Base" in analysis.json()["detail"]


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
    assert set(supporting_types) <= {source["source_type"] for source in brd_row["supporting_sources"]}
    assert all(source["active_revision"]["owner_version"] for source in listed
               if source["logical_name"] in {f"{source_type}.md" for source_type in supporting_types})

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
    assert any(fragment["fragment_type"] == "paragraph" and fragment["source_reference"] == "paragraph 2" for fragment in revision["fragments"])
    assert any(fragment["fragment_type"] == "table" and fragment["source_reference"] == "table 1, row 1, cell 1" and fragment["cell_index"] == 1 and fragment["heading"] == "Scope" for fragment in revision["fragments"])


@pytest.mark.skip(reason="Legacy no-key fallback scenario is incompatible with the required RAG + OpenAI generation flow")
def test_test_case_generation_review_and_execution_lifecycle(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = _client(monkeypatch)
    body = (
        b"# PBM Refill Reminder\n"
        b"Active members with eligible prescriptions should receive an SMS refill reminder.\n"
        b"Inactive members and members without eligible prescriptions are excluded.\n"
        b"Expected outcome: an eligible member receives the refill reminder SMS and communication status is recorded.\n"
        b"If SMS fails, trigger the alternate phone call and record the call outcome.\n"
    )
    brd = client.post(
        "/api/v1/projects/p1/sources",
        files={"file": ("pbm-refill.md", io.BytesIO(body), "text/markdown")},
        data={"source_type": "brd"},
    )
    assert brd.status_code == 202, brd.text
    manifest = client.post("/api/v1/projects/p1/manifests", json={"source_ids": [brd.json()["source_id"]], "scope": "MVP"})
    assert manifest.status_code == 201, manifest.text

    analysis = client.post(f"/api/v1/packages/{manifest.json()['package_id']}/requirements/analyze")
    assert analysis.status_code == 201, analysis.text
    analysis_payload = analysis.json()
    requirement = analysis_payload["contract"]["requirements"][0]
    approved_items = [requirement["requirement_id"]]
    approved_items.extend(f"{requirement['requirement_id']}:{rule['rule_id']}" for rule in requirement["business_rules"][:1])
    approved_items.extend(f"{requirement['requirement_id']}:{criterion['acceptance_id']}" for criterion in requirement["acceptance_criteria"][:1])

    reviewed_requirements = client.post(
        f"/api/v1/requirement-analyses/{analysis_payload['analysis_id']}/review",
        json={
            "reviewer": "product-owner",
            "decision": "approved",
            "comments": "Approved for test design.",
            "approved_items": approved_items,
        },
    )
    assert reviewed_requirements.status_code == 200, reviewed_requirements.text

    generated = client.post(f"/api/v1/requirement-analyses/{analysis_payload['analysis_id']}/test-cases", json={"count": 2})
    assert generated.status_code == 201, generated.text
    generation_payload = generated.json()
    generation_id = generation_payload["generation_id"]
    test_cases = generation_payload["test_cases"]
    assert len(test_cases) == 2
    assert generation_payload["generation_mode"] == "local_fallback"

    coverage = client.get(f"/api/v1/test-case-generations/{generation_id}/coverage-review")
    assert coverage.status_code == 200, coverage.text
    coverage_payload = coverage.json()
    assert coverage_payload["summary"]["requirements_total"] == 1
    assert coverage_payload["matrix"][0]["requirement_id"] == requirement["requirement_id"]
    assert coverage_payload["quality"]["summary"]["total"] >= 0

    approved_case_id = test_cases[0]["test_case_id"]
    rejected_case_id = test_cases[1]["test_case_id"]
    reviewed_cases = client.post(
        f"/api/v1/test-case-generations/{generation_id}/review",
        json={
            "reviewer": "qa-reviewer",
            "comments": "Approved the happy path and rejected the duplicate for regeneration.",
            "items": [
                {
                    "test_case_id": approved_case_id,
                    "review_status": "approved",
                    "title": test_cases[0]["title"] + " - reviewed",
                    "steps": test_cases[0]["steps"] + ["Reviewer confirms deterministic cleanup ownership."],
                    "expected_results": test_cases[0]["expected_results"],
                },
                {
                    "test_case_id": rejected_case_id,
                    "review_status": "rejected",
                    "title": test_cases[1]["title"],
                    "steps": test_cases[1]["steps"],
                    "expected_results": test_cases[1]["expected_results"],
                },
            ],
        },
    )
    assert reviewed_cases.status_code == 200, reviewed_cases.text
    reviewed_contract = reviewed_cases.json()["contract"]
    assert reviewed_contract["approved_test_case_ids"] == [approved_case_id]
    assert reviewed_contract["rejected_test_case_ids"] == [rejected_case_id]

    audit = client.get(f"/api/v1/test-case-generations/{generation_id}/review-audit")
    assert audit.status_code == 200, audit.text
    latest_audit = audit.json()[0]
    assert latest_audit["reviewed_contract"]["approved_test_case_ids"] == [approved_case_id]
    assert latest_audit["reviewed_items"][0]["test_case_id"] == approved_case_id

    regenerated = client.post(
        f"/api/v1/test-case-generations/{generation_id}/regenerate",
        json={"reviewer": "qa-reviewer", "reason": "Regenerate rejected coverage.", "test_case_ids": [rejected_case_id]},
    )
    assert regenerated.status_code == 201, regenerated.text
    assert regenerated.json()["parent_generation_id"] == generation_id
    assert regenerated.json()["regeneration_request"]["source_test_case_ids"] == [rejected_case_id]

    first_run = client.post(
        f"/api/v1/test-case-generations/{generation_id}/execution-runs",
        json={"reviewer": "qa-reviewer", "comments": "Freeze approved baseline.", "selected_test_case_ids": [approved_case_id]},
    )
    assert first_run.status_code == 201, first_run.text
    second_run = client.post(
        f"/api/v1/test-case-generations/{generation_id}/execution-runs",
        json={"reviewer": "qa-reviewer", "comments": "Freeze second baseline.", "selected_test_case_ids": [approved_case_id]},
    )
    assert second_run.status_code == 201, second_run.text

    cancelled = client.patch(
        f"/api/v1/execution-runs/{first_run.json()['run_id']}/lifecycle",
        json={"lifecycle_state": "cancelled", "reviewer": "qa-reviewer", "comments": "Weekend execution not needed."},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["lifecycle_state"] == "cancelled"
    assert cancelled.json()["cancelled_at"]
    assert any(item["type"] == "baseline_cancelled" for item in cancelled.json()["immutable_observations"])

    failed = client.patch(
        f"/api/v1/execution-runs/{second_run.json()['run_id']}/lifecycle",
        json={"lifecycle_state": "failed", "reviewer": "qa-reviewer", "comments": "Environment was unavailable."},
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["status"] == "failed"
    assert failed.json()["lifecycle_state"] == "failed"
    assert failed.json()["completed_at"]
    assert failed.json()["error"] == "Environment was unavailable."
    assert any(item["type"] == "baseline_failed" for item in failed.json()["immutable_observations"])

    terminal_update = client.patch(
        f"/api/v1/execution-runs/{second_run.json()['run_id']}/lifecycle",
        json={"lifecycle_state": "cancelled", "reviewer": "qa-reviewer"},
    )
    assert terminal_update.status_code == 409
