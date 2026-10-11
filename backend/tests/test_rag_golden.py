import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.rag_evaluation import grade_retrieval


SUITE = json.loads((Path(__file__).parent / "rag_golden_suite.json").read_text(encoding="utf-8"))
BRD = (
    "Eligible members must have active membership, eligible prescriptions, refill dates, and valid contact information. "
    "Ineligible members must not receive any refill reminder communication."
)
ADDITIONAL_FRAGMENTS = [
    ("sms-fallback", "Communication failure", "If SMS communication fails, trigger a phone call to the member as a fallback."),
    ("outcomes", "Communication outcomes", "Record outcomes of SMS communications to track success or failures."),
    ("decoy", "Monthly reporting", "The billing team exports a monthly report for operational review."),
    ("order-cancellation", "Order cancellation", "Customers may cancel an unshipped order and receive a refund within five business days."),
]


class FakeOpenAI:
    def __init__(self):
        self.embeddings = type("Embeddings", (), {"create": staticmethod(self.embed)})()
        self.responses = type("Responses", (), {"create": staticmethod(self.respond)})()

    @staticmethod
    def embed(**kwargs):
        def concept_vector(text):
            lowered = text.casefold()
            if any(term in lowered for term in ("eligib", "membership", "prescription", "exclusion")):
                category = 0
            elif any(term in lowered for term in ("outcome", "success", "tracked")):
                category = 2
            elif any(term in lowered for term in ("sms", "phone", "fallback", "fails", "failure")):
                category = 1
            else:
                category = 3
            return [1.0 if position == category else 0.0 for position in range(kwargs.get("dimensions", 4))]

        data = [type("Datum", (), {"index": index, "embedding": concept_vector(text)})()
                for index, text in enumerate(kwargs["input"])]
        return type("Response", (), {"data": data})()

    @staticmethod
    def respond(**_kwargs):
        # Empty structured output exercises the documented deterministic RRF fallback.
        return type("Response", (), {"output_text": json.dumps({"entities": [], "relationships": []})})()


class SuccessfulRerankerOpenAI(FakeOpenAI):
    def __init__(self):
        super().__init__()
        self.responses = type("Responses", (), {})()
        self.responses.create = self.rerank_or_graph

    def rerank_or_graph(self, **kwargs):
        schema_name = kwargs.get("text", {}).get("format", {}).get("name")
        if schema_name != "testiq_semantic_rerank":
            return type("Response", (), {"output_text": json.dumps({"entities": [], "relationships": []})})()
        prompt_input = kwargs["input"]
        payload = json.loads(prompt_input[prompt_input.find("{"):])
        query = payload["query"].casefold()
        expected_phrase = (
            "__unsupported__" if "email retry" in query else
            "refund within five business days" if "unshipped order" in query else
            "active membership" if "eligibility" in query else
            "sms communication fails" if "primary communication" in query else
            "trigger a phone call" if "fallback action" in query else
            "record outcomes"
        )
        candidates = payload["candidates"]
        ranked = sorted(candidates, key=lambda item: expected_phrase not in item["text"].casefold())
        return type("Response", (), {"output_text": json.dumps({"ranked": [
            {"chunk_id": item["chunk_id"], "relevance_score": 0.99 if expected_phrase in item["text"].casefold() else 0.01,
             "reason": "contains the golden evidence phrase" if expected_phrase in item["text"].casefold() else "does not contain the golden evidence phrase"}
            for item in ranked
        ]})})()


def test_campaign_golden_suite_through_real_index_and_hybrid_retrieval(monkeypatch):
    from test_knowledge_versions import _modules, _seed_package

    db, knowledge = _modules(monkeypatch)
    fake = FakeOpenAI()
    monkeypatch.setattr(knowledge, "_create_client", lambda: fake)
    _seed_package(db, "golden-package", 1, "golden-revision", "golden-fragment", text=BRD)
    with db.connection() as conn:
        for index, (fragment_id, heading, text) in enumerate(ADDITIONAL_FRAGMENTS, 2):
            conn.execute(
                "INSERT INTO source_fragment (fragment_id,revision_id,source_id,fragment_index,heading,heading_path,source_reference,fragment_type,content_hash,extractor_version,fragment_text) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (fragment_id, "golden-revision", "source-1", index, heading, heading,
                 f"paragraph {index}", "paragraph", f"hash-{fragment_id}", "golden-test", text),
            )
    index = knowledge.build_knowledge_index("golden-package", knowledge.BuildIndexRequest())

    grades = []
    for case in (item for item in SUITE if not item.get("expect_no_evidence")):
        retrieval = knowledge.retrieve_knowledge(index["index_id"], knowledge.RetrievalRequest(
            query=case["question"], top_k=6, candidate_k=10, source_types=case["expected_source_types"],
            use_reranker=True), client=fake)
        question = knowledge.EvaluationQuestion(**case)
        grade = grade_retrieval(question, retrieval)
        assert grade["passed"], f"{case['id']} failed: {grade}; top hits: {[(hit['chunk_text'], hit['vector_score'], hit['lexical_score'], hit['fusion_score']) for hit in retrieval['hits']]}"
        assert grade["top_hit_relevant"]
        assert grade["reciprocal_rank"] == 1.0
        assert grade["ndcg_at_k"] > 0
        assert all(hit["chunk_id"] in retrieval["context"] for hit in retrieval["hits"])
        assert retrieval["reranker"]["status"] == "fallback"
        assert retrieval["configuration"]["vector_backend"].startswith("python-cosine")
        grades.append(grade)

    assert len(grades) == sum(not item.get("expect_no_evidence") for item in SUITE)
    assert all(grade["citation_integrity"] and grade["evidence_recall"] == 1 and grade["top_hit_relevant"] for grade in grades)


def test_successful_reranker_returns_grounded_top_result_for_golden_queries(monkeypatch):
    from test_knowledge_versions import _modules, _seed_package

    db, knowledge = _modules(monkeypatch)
    fake = SuccessfulRerankerOpenAI()
    monkeypatch.setattr(knowledge, "_create_client", lambda: fake)
    _seed_package(db, "rerank-package", 1, "rerank-revision", "eligibility", text=BRD)
    with db.connection() as conn:
        for index, (fragment_id, heading, text) in enumerate(ADDITIONAL_FRAGMENTS, 2):
            conn.execute(
                "INSERT INTO source_fragment (fragment_id,revision_id,source_id,fragment_index,heading,heading_path,source_reference,fragment_type,content_hash,extractor_version,fragment_text) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (fragment_id, "rerank-revision", "source-1", index, heading, heading,
                 f"paragraph {index}", "paragraph", f"hash-{fragment_id}", "golden-test", text),
            )
    index = knowledge.build_knowledge_index("rerank-package", knowledge.BuildIndexRequest())

    for case in SUITE:
        retrieval = knowledge.retrieve_knowledge(index["index_id"], knowledge.RetrievalRequest(
            query=case["question"], top_k=4, candidate_k=10, source_types=case["expected_source_types"],
            use_reranker=True), client=fake)
        assert retrieval["reranker"]["status"] == "completed", retrieval["warnings"]
        if case.get("expect_no_evidence"):
            assert retrieval["hits"] == []
            assert retrieval["reranker"]["filtered_candidate_count"] > 0
        else:
            assert retrieval["hits"][0]["reranker_score"] > 0.9
        grade = grade_retrieval(knowledge.EvaluationQuestion(**case), retrieval)
        assert grade["passed"], f"{case['id']} failed: {grade}; hits: {[hit['chunk_text'] for hit in retrieval['hits']]}"
        if not case.get("expect_no_evidence"):
            assert grade["reciprocal_rank"] == 1.0


def test_golden_grader_rejects_missing_expected_evidence_and_broken_citations():
    from app.knowledge_fabric import EvaluationQuestion

    question = EvaluationQuestion(question="What is required?", expected_evidence=["active membership", "eligible prescriptions"], expected_source_types=["brd"])
    missing_evidence = {"hits": [{"chunk_id": "c1", "source_reference": "BRD@v1", "content_hash": "h1", "source_type": "brd", "chunk_text": "Active membership is required."}]}
    broken_citation = {"hits": [{"chunk_id": "c1", "source_reference": "", "content_hash": "h1", "source_type": "brd", "chunk_text": "Active membership is required."}]}

    assert not grade_retrieval(question, missing_evidence)["passed"]
    assert grade_retrieval(question, missing_evidence)["evidence_recall"] == 0.5
    broken = grade_retrieval(question, broken_citation)
    assert not broken["passed"]
    assert not broken["citation_integrity"]


def test_negative_golden_case_only_passes_when_retrieval_abstains():
    from app.knowledge_fabric import EvaluationQuestion

    question = EvaluationQuestion(question="What is the required email retry interval?", expect_no_evidence=True)
    unsupported = {"hits": []}
    unrelated = {"hits": [{"chunk_id": "c1", "source_reference": "BRD@v1", "content_hash": "h1",
                           "source_type": "brd", "chunk_text": "SMS fallback uses a phone call."}]}

    assert grade_retrieval(question, unsupported)["passed"]
    assert not grade_retrieval(question, unrelated)["passed"]


def test_ranking_metrics_reward_relevant_evidence_at_the_top():
    from app.knowledge_fabric import EvaluationQuestion

    question = EvaluationQuestion(question="What is required?", expected_evidence=["refund within five business days"], expected_source_types=["brd"])
    top_relevant = {"hits": [
        {"chunk_id": "relevant", "source_reference": "BRD@v2", "content_hash": "h1", "source_type": "brd", "chunk_text": "Refund within five business days."},
        {"chunk_id": "decoy", "source_reference": "BRD@v2", "content_hash": "h2", "source_type": "brd", "chunk_text": "Monthly reports are exported."},
    ]}
    buried_relevant = {"hits": list(reversed(top_relevant["hits"]))}

    best = grade_retrieval(question, top_relevant)
    worse = grade_retrieval(question, buried_relevant)
    assert best["reciprocal_rank"] == 1.0
    assert worse["reciprocal_rank"] == 0.5
    assert best["ndcg_at_k"] > worse["ndcg_at_k"]


def test_test_case_alignment_repair_maps_only_fields_named_by_actions():
    from app.main import _repair_test_case_data_alignment

    contract = {"test_cases": [{
        "test_case_id": "TC-001",
        "requirement_id": "FR-001",
        "steps": ["Step 1: Select the target member record.", "Step 2: Set the prescription status to active."],
        "data_needs": [
            {"name": "member_id", "type": "string", "value": "synthetic member", "required": True, "source": "BRD"},
            {"name": "prescription_status", "type": "string", "value": "active", "required": True, "source": "BRD"},
            {"name": "email_retry_interval", "type": "string", "value": "unknown", "required": True, "source": "BRD"},
        ],
        "actions": [
            {"step": 1, "actor": "QA", "surface": "Member record", "action": "Select the target member", "data_inputs": []},
            {"step": 2, "actor": "QA", "surface": "Prescription record", "action": "Set prescription status", "data_inputs": []},
        ],
    }]}

    findings = _repair_test_case_data_alignment(contract)
    case = contract["test_cases"][0]
    assert case["actions"][0]["data_inputs"] == ["member_id"]
    assert case["actions"][1]["data_inputs"] == ["prescription_status"]
    assert len(findings) == 2
    assert all(finding["status"] == "requires_qa_confirmation" for finding in findings)
    assert "email_retry_interval" not in [field for action in case["actions"] for field in action["data_inputs"]]


def test_chunker_keeps_sentence_safe_overlap_for_long_fragments():
    from app.knowledge_fabric import CHUNK_OVERLAP_CHARS, MAX_CHARS_PER_CHUNK, _chunks

    text = " ".join(f"Requirement sentence number {index} requires an observable outcome." for index in range(80))
    chunks = _chunks({"fragment_text": text})

    assert len(chunks) > 1
    assert all(len(body) <= MAX_CHARS_PER_CHUNK for body, _, _ in chunks)
    assert all(left_end > right_start for (_, _, left_end), (_, right_start, _) in zip(chunks, chunks[1:]))
    assert all(left_end - right_start <= CHUNK_OVERLAP_CHARS + 100 for (_, _, left_end), (_, right_start, _) in zip(chunks, chunks[1:]))


def test_test_case_citation_requires_matching_fragment_and_source_reference_pair():
    from app.main import _validate_case_citation_pairs

    valid_pairs = {("fragment-1", "Campaign BRD@v1 | paragraph 1")}
    by_reference = {"Campaign BRD@v1 | paragraph 1": {"fragment-1"}}
    citation = {"fragment_id": "fragment-1", "source_reference": "Other document@v1 | paragraph 4"}

    with pytest.raises(ValueError, match="mismatched source citation"):
        _validate_case_citation_pairs([citation], valid_pairs, by_reference, "TC-001", "evidence")


@pytest.mark.parametrize("case", SUITE, ids=[case["id"] for case in SUITE])
def test_golden_cases_have_specific_queries_and_expected_evidence(case):
    assert len(case["question"]) > 10
    if case.get("expect_no_evidence"):
        assert not case["expected_evidence"]
        assert not case["expected_source_types"]
    else:
        assert case["expected_evidence"]
        assert case["expected_source_types"]
