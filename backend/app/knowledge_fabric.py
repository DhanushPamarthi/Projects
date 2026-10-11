"""Package-scoped, provenance-preserving Knowledge Fabric and RAG retrieval."""

import hashlib
import json
import math
import os
import re
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone

from importlib.metadata import version as package_version

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from . import db as db_module
from .config import OPENAI_EMBEDDING_DIMENSIONS, OPENAI_TIMEOUT_SECONDS, PGVECTOR_DIMENSIONS
from .db import connection
from .openai_requests import embeddings_create, responses_create
from .rag_evaluation import grade_retrieval
from .token_usage import usage_scope, usage_step


router = APIRouter()

# Builds are synchronous in this service. This process-local guard separates a
# genuinely active request from an orphaned database row left by a terminated
# or previously failed request, allowing safe retry in the common one-worker
# development/product deployment.
_ACTIVE_KNOWLEDGE_BUILDS = set()
CHUNKER_VERSION = "fragment-window-1200-overlap-160-v2"
GRAPH_PROMPT_VERSION = "knowledge-graph-evidence-v3"
RETRIEVAL_VERSION = "hybrid-bm25-vector-graph-rrf-rerank-v3"
RAG_BUILD_VERSION = "rag-persistent-incremental-v4"
GRAPH_INSTRUCTIONS = (
    "Extract only explicit, useful campaign and technical entities and relationships from these cited chunks. "
    "BRD statements are business authority; technical sources describe implementation. Do not infer missing thresholds or precedence. "
    "Every entity and relationship must cite one supplied chunk_id. Use concise stable names, typed directed relationships, and confidence 0..1. "
    "Return empty arrays when evidence is insufficient."
)
DEFAULT_EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large")
DEFAULT_GRAPH_MODEL = os.getenv("OPENAI_GRAPH_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
DEFAULT_RERANKER_MODEL = os.getenv("OPENAI_RERANKER_MODEL", DEFAULT_GRAPH_MODEL)
DEFAULT_MIN_RERANKER_SCORE = min(1.0, max(0.0, float(os.getenv("RAG_MIN_RERANKER_SCORE", "0.15"))))
RRF_K = 60
MAX_CHARS_PER_CHUNK = 1200
CHUNK_OVERLAP_CHARS = 160
EMBEDDING_BATCH_SIZE = 64
GRAPH_BATCH_SIZE = 6
GRAPH_MAX_OUTPUT_TOKENS = 5000
MAX_INDEX_CHUNKS = max(0, int(os.getenv("RAG_MAX_INDEX_CHUNKS", "0")))
MAX_INDEX_CHARACTERS = max(0, int(os.getenv("RAG_MAX_INDEX_CHARACTERS", "0")))
MAX_CONTEXT_CHARACTERS = 30_000
SOURCE_TYPES = {"brd", "campaign_rules", "api_spec", "database_schema", "message_template", "ui_description"}
DEFAULT_EVALUATION_QUESTIONS = [
    {"question": "What determines campaign eligibility and exclusion?", "expected_evidence": ["active membership", "eligible prescriptions", "ineligible members"], "expected_source_types": ["brd"]},
    {"question": "What happens when the primary communication fails?", "expected_evidence": ["SMS communication fails", "phone call"], "expected_source_types": ["brd"]},
    {"question": "What retry or fallback behavior is specified?", "expected_evidence": ["SMS", "phone call"], "expected_source_types": ["brd"]},
    {"question": "What outputs and observable outcomes are expected?", "expected_evidence": ["record the outcomes", "success or failures"], "expected_source_types": ["brd"]},
    {"question": "What is the required email retry interval?", "expected_evidence": [], "expected_source_types": [], "expect_no_evidence": True},
]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _uid(prefix):
    return f"{prefix}_{uuid.uuid4().hex}"


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _vector_literal(vector):
    return "[" + ",".join(format(float(value), ".9g") for value in vector) + "]"


def _dict(row):
    return dict(row) if row else None


def _update_build_progress(index_id, percent, stage, message, **extra_fields):
    percent = max(0, min(100, int(percent or 0)))
    allowed_extras = {key: value for key, value in extra_fields.items() if key in {"status", "chunk_count", "node_count", "edge_count", "error"}}
    assignments = ["progress_percent=%s", "progress_stage=%s", "progress_message=%s"]
    values = [percent, str(stage or "building"), str(message or "")]
    for key, value in allowed_extras.items():
        assignments.append(f"{key}=%s")
        values.append(value)
    values.append(index_id)
    with connection() as conn:
        conn.execute(f"UPDATE knowledge_index SET {','.join(assignments)} WHERE index_id=%s", tuple(values))


def _chunks(fragment):
    """Split a large fragment at sentence or space boundaries and retain offsets."""
    original = str(fragment["fragment_text"] or "")
    leading_space = len(original) - len(original.lstrip())
    text = original.strip()
    if not text:
        return []
    result, start = [], 0
    while start < len(text):
        end = min(start + MAX_CHARS_PER_CHUNK, len(text))
        if end < len(text):
            boundary = max(text.rfind(". ", start, end), text.rfind("; ", start, end), text.rfind(" ", start, end))
            if boundary > start + MAX_CHARS_PER_CHUNK // 2:
                end = boundary + 1
        body = text[start:end].strip()
        if body:
            result.append((body, start + leading_space, end + leading_space))
        if end >= len(text):
            break
        next_start = max(start + 1, end - CHUNK_OVERLAP_CHARS)
        aligned_start = text.find(" ", next_start, min(end, next_start + 80))
        if aligned_start >= next_start:
            next_start = aligned_start + 1
        start = next_start
        while start < len(text) and text[start].isspace():
            start += 1
    return result


def _canonical(name):
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")[:160]


class BuildIndexRequest(BaseModel):
    provider: str = Field(default="openai", pattern="^openai$")
    embedding_model: str = Field(default=DEFAULT_EMBEDDING_MODEL, min_length=1, max_length=100)
    graph_model: str = Field(default=DEFAULT_GRAPH_MODEL, min_length=1, max_length=100)


class IndexReviewRequest(BaseModel):
    reviewer: str = Field(min_length=1, max_length=200)
    decision: str = Field(pattern="^(approved|needs_changes|rejected)$")
    comments: str | None = Field(default=None, max_length=4000)


class RetrievalRequest(BaseModel):
    query: str = Field(min_length=2, max_length=2000)
    top_k: int = Field(default=8, ge=1, le=20)
    candidate_k: int = Field(default=40, ge=5, le=100)
    graph_depth: int = Field(default=1, ge=0, le=2)
    source_types: list[str] = Field(default_factory=list, max_length=6)
    max_context_characters: int = Field(default=12_000, ge=1000, le=MAX_CONTEXT_CHARACTERS)
    minimum_score: float = Field(default=0.0, ge=0.0, le=1.0)
    minimum_reranker_score: float = Field(default=DEFAULT_MIN_RERANKER_SCORE, ge=0.0, le=1.0)
    max_chunks_per_source: int = Field(default=6, ge=1, le=20)
    use_reranker: bool = True
    requested_by: str = Field(default="TestIQ user", min_length=1, max_length=200)
    purpose: str = Field(default="interactive", min_length=1, max_length=100)
    require_approved: bool = True


class EvaluationQuestion(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    expected_terms: list[str] = Field(default_factory=list, max_length=20)
    expected_evidence: list[str] = Field(default_factory=list, max_length=20)
    expected_source_types: list[str] = Field(default_factory=list, max_length=6)
    expect_no_evidence: bool = False

    @classmethod
    def _validate_source_types(cls, value):
        unknown = set(value or []) - SOURCE_TYPES
        if unknown:
            raise ValueError(f"Unsupported expected source types: {', '.join(sorted(unknown))}")
        return value

    # Pydantic v2 validates these request fields before any retrieval is run.
    @field_validator("expected_source_types")
    @classmethod
    def validate_expected_source_types(cls, value):
        return cls._validate_source_types(value)


class EvaluationRequest(BaseModel):
    questions: list[EvaluationQuestion] = Field(default_factory=list, max_length=20)
    requested_by: str = Field(default="Product Owner", min_length=1, max_length=200)


def _merge_node(nodes, index_id, name, entity_type, evidence):
    name = str(name or "").strip()
    canonical = _canonical(name)
    if not canonical or not evidence:
        return None
    node_id = "node_" + _hash(index_id + ":" + canonical)[:32]
    item = nodes.setdefault(canonical, {"node_id": node_id, "canonical_id": canonical, "label": name,
        "entity_type": entity_type, "aliases": set(), "evidence": set()})
    if name != item["label"]:
        item["aliases"].add(name)
    item["evidence"].add(evidence)
    return canonical


def _merge_edge(edges, nodes, source, target, relationship_type, evidence, confidence):
    source_key, target_key = _canonical(str(source or "")), _canonical(str(target or ""))
    relation = _canonical(str(relationship_type or ""))
    if source_key not in nodes or target_key not in nodes or not relation or not evidence:
        return
    key = (source_key, target_key, relation)
    item = edges.setdefault(key, {"source": source_key, "target": target_key, "relationship_type": relation,
        "evidence": set(), "confidence": float(confidence)})
    item["evidence"].add(evidence)
    item["confidence"] = max(item["confidence"], float(confidence))


def _json_object(text):
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _seed_structured_graph(nodes, edges, index_id, chunks):
    """Create deterministic document, section, schema, and API nodes before LLM extraction."""
    document_labels = {}
    table_labels = {}
    for chunk in chunks:
        metadata = chunk["metadata"]
        evidence = chunk["chunk_id"]
        source_type = chunk["source_type"]
        source_id = metadata["source_id"]
        logical_name = chunk["source_reference"].split("@v", 1)[0]
        document_label = document_labels.setdefault(source_id, f"Document {logical_name}")
        document_type = {"brd": "brd", "api_spec": "api", "database_schema": "database_schema"}.get(source_type, "entity")
        _merge_node(nodes, index_id, document_label, document_type, evidence)
        if source_type == "brd" and chunk.get("heading_path"):
            section_label = f"Section {chunk['heading_path']}"
            _merge_node(nodes, index_id, section_label, "section", evidence)
            _merge_edge(edges, nodes, document_label, section_label, "contains_section", evidence, 1.0)
        if source_type == "database_schema":
            value = _json_object(chunk["text"])
            heading = str(chunk.get("heading_path") or "schema")
            table_label = table_labels.setdefault((source_id, heading), f"Table {heading}")
            _merge_node(nodes, index_id, table_label, "table", evidence)
            _merge_edge(edges, nodes, document_label, table_label, "defines_table", evidence, 1.0)
            column_name = value.get("column_name") or value.get("field_name")
            if column_name:
                column_label = f"Column {heading}.{column_name}"
                _merge_node(nodes, index_id, column_label, "column", evidence)
                _merge_edge(edges, nodes, table_label, column_label, "contains_column", evidence, 1.0)
            definition = value.get("definition")
            if definition:
                constraint_label = f"Constraint {heading}: {definition}"
                _merge_node(nodes, index_id, constraint_label, "constraint", evidence)
                _merge_edge(edges, nodes, table_label, constraint_label, "has_constraint", evidence, 1.0)
        if source_type == "api_spec":
            reference = chunk["source_reference"]
            match = re.search(r"paths\.(.+)\.(get|post|put|patch|delete|options|head)$", reference, re.IGNORECASE)
            if match:
                path, method = match.group(1), match.group(2).upper()
                operation_label = f"API {method} {path}"
                _merge_node(nodes, index_id, operation_label, "api", evidence)
                _merge_edge(edges, nodes, document_label, operation_label, "defines_operation", evidence, 1.0)


def _request_graph_payloads(client, model, schema, batch):
    """Return valid graph payloads, splitting/retrying truncated model output."""
    prompt_data = [{"chunk_id": item["chunk_id"], "source_type": item["source_type"],
        "source_reference": item["source_reference"], "heading": item.get("heading_path"), "text": item["text"]} for item in batch]
    attempts = 2 if len(batch) == 1 else 1
    failure = None
    for _ in range(attempts):
        try:
            response = responses_create(client, model=model, instructions=GRAPH_INSTRUCTIONS,
                input=json.dumps(prompt_data, ensure_ascii=False), store=False,
                text={"format": {"type": "json_schema", "name": "testiq_knowledge_graph", "strict": True, "schema": schema}},
                max_output_tokens=GRAPH_MAX_OUTPUT_TOKENS)
            raw = response.output_text or ""
            payload = json.loads(raw)
            if not isinstance(payload, dict) or not isinstance(payload.get("entities"), list) or not isinstance(payload.get("relationships"), list):
                raise ValueError("Graph response did not match the required object shape")
            return [(payload, batch)], []
        except Exception as exc:
            failure = exc
    if len(batch) > 1:
        midpoint = len(batch) // 2
        left, left_warnings = _request_graph_payloads(client, model, schema, batch[:midpoint])
        right, right_warnings = _request_graph_payloads(client, model, schema, batch[midpoint:])
        return left + right, left_warnings + right_warnings
    chunk_id = batch[0]["chunk_id"]
    return [], [f"OpenAI graph augmentation was skipped for {chunk_id}: {type(failure).__name__}: {failure}"]


def _load_reusable_graph(conn, parent_index_id, index_id, old_to_new_chunk_ids):
    """Clone graph facts whose cited evidence still exists in the new version."""
    nodes, edges, old_node_keys = {}, {}, {}
    for row in conn.execute("SELECT * FROM knowledge_node WHERE index_id=%s", (parent_index_id,)).fetchall():
        canonical = row["canonical_id"]
        old_node_keys[row["node_id"]] = canonical
        evidence = {old_to_new_chunk_ids[item] for item in json.loads(row["evidence_chunk_ids_json"] or "[]") if item in old_to_new_chunk_ids}
        if not evidence:
            continue
        nodes[canonical] = {
            "node_id": "node_" + _hash(index_id + ":" + canonical)[:32],
            "canonical_id": canonical,
            "label": row["label"],
            "entity_type": row["entity_type"],
            "aliases": set(json.loads(row["aliases_json"] or "[]")),
            "evidence": evidence,
        }
    for row in conn.execute("SELECT * FROM knowledge_edge WHERE index_id=%s", (parent_index_id,)).fetchall():
        source, target = old_node_keys.get(row["source_node_id"]), old_node_keys.get(row["target_node_id"])
        evidence = {old_to_new_chunk_ids[item] for item in json.loads(row["evidence_chunk_ids_json"] or "[]") if item in old_to_new_chunk_ids}
        relation = row["relationship_type"]
        if source in nodes and target in nodes and evidence:
            edges[(source, target, relation)] = {
                "source": source,
                "target": target,
                "relationship_type": relation,
                "evidence": evidence,
                "confidence": float(row["confidence"]),
            }
    return nodes, edges


def _extract_graph(client, index_id, package_id, chunks, model, progress_callback=None, seed_nodes=None, seed_edges=None, graph_chunks=None):
    entity_types = ["actor", "business_rule", "scenario", "entity", "api", "table", "column", "constraint",
        "requirement", "outcome", "message_template", "brd", "section", "database_schema", "test_case", "recipe", "evidence"]
    schema = {"type": "object", "additionalProperties": False, "properties": {
        "entities": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "properties": {"name": {"type": "string"}, "type": {"type": "string", "enum": entity_types},
                "evidence_chunk_id": {"type": "string"}}, "required": ["name", "type", "evidence_chunk_id"]}},
        "relationships": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "properties": {"source": {"type": "string"}, "target": {"type": "string"}, "type": {"type": "string"},
                "evidence_chunk_id": {"type": "string"}, "confidence": {"type": "number"}},
            "required": ["source", "target", "type", "evidence_chunk_id", "confidence"]}},
    }, "required": ["entities", "relationships"]}
    nodes, edges, warnings = seed_nodes or {}, seed_edges or {}, []
    _seed_structured_graph(nodes, edges, index_id, chunks)
    # Schema and OpenAPI chunks already receive deterministic table/operation
    # nodes and remain fully searchable by embeddings. Restrict generative graph
    # augmentation to narrative business sources to avoid redundant, oversized
    # structured output for large technical catalogs.
    augmentation_chunks = chunks if graph_chunks is None else graph_chunks
    llm_chunks = [item for item in augmentation_chunks if item["source_type"] not in {"database_schema", "api_spec"}]
    total_batches = max(1, math.ceil(len(llm_chunks) / GRAPH_BATCH_SIZE))
    for batch_number, offset in enumerate(range(0, len(llm_chunks), GRAPH_BATCH_SIZE), 1):
        batch = llm_chunks[offset:offset + GRAPH_BATCH_SIZE]
        payloads, batch_warnings = _request_graph_payloads(client, model, schema, batch)
        warnings.extend(batch_warnings)
        for payload, payload_batch in payloads:
            valid_chunks = {item["chunk_id"] for item in payload_batch}
            for draft in payload.get("entities", []):
                if draft.get("evidence_chunk_id") in valid_chunks:
                    _merge_node(nodes, index_id, draft.get("name"), draft.get("type"), draft.get("evidence_chunk_id"))
            for draft in payload.get("relationships", []):
                evidence = draft.get("evidence_chunk_id")
                confidence = float(draft.get("confidence", -1))
                if evidence in valid_chunks and 0 <= confidence <= 1:
                    _merge_edge(edges, nodes, draft.get("source"), draft.get("target"), draft.get("type"), evidence, confidence)
        if progress_callback:
            progress_callback(batch_number, total_batches, len(nodes), len(edges))
    with connection() as conn:
        for item in nodes.values():
            conn.execute("INSERT INTO knowledge_node (node_id,index_id,package_id,canonical_id,label,entity_type,aliases_json,evidence_chunk_ids_json) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (item["node_id"], index_id, package_id, item["canonical_id"], item["label"], item["entity_type"],
                 json.dumps(sorted(item["aliases"])), json.dumps(sorted(item["evidence"]))))
        for item in edges.values():
            source_id, target_id = nodes[item["source"]]["node_id"], nodes[item["target"]]["node_id"]
            edge_id = "edge_" + _hash(index_id + ":" + source_id + ":" + target_id + ":" + item["relationship_type"])[:32]
            conn.execute("INSERT INTO knowledge_edge (edge_id,index_id,package_id,source_node_id,target_node_id,relationship_type,evidence_chunk_ids_json,confidence) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (edge_id, index_id, package_id, source_id, target_id, item["relationship_type"],
                 json.dumps(sorted(item["evidence"])), item["confidence"]))
    return len(nodes), len(edges), warnings


def _serialize_index(index):
    value = _dict(index)
    if not value:
        return None
    value.pop("run_id", None)  # tolerate indexes created before run IDs were removed
    value["configuration"] = json.loads(value.pop("configuration_json") or "{}")
    value["warnings"] = value.get("error", "").splitlines() if value.get("status") == "ready" and value.get("error") else []
    if value.get("status") == "ready":
        value["error"] = None
    for key in ("node_count", "edge_count", "chunk_count", "dimensions", "progress_percent", "knowledge_version",
                "reused_chunk_count", "new_chunk_count", "retired_chunk_count"):
        value[key] = int(value[key] or 0)
    if value.get("status") == "ready":
        value["progress_percent"] = 100
        value["progress_stage"] = value.get("progress_stage") or "ready"
        value["progress_message"] = value.get("progress_message") or "Knowledge Base is ready."
    elif value.get("status") == "failed":
        value["progress_stage"] = value.get("progress_stage") or "failed"
        value["progress_message"] = value.get("progress_message") or value.get("error") or "Knowledge Base build failed."
    else:
        value["progress_stage"] = value.get("progress_stage") or value.get("status") or "pending"
        value["progress_message"] = value.get("progress_message") or "Knowledge Base build is waiting to start."
    return value


def _load_package_inputs(conn, package_id):
    """Resolve only revisions pinned by the persisted source-package tables."""
    items = conn.execute(
        "SELECT pi.source_id,pi.revision_id,pi.content_hash,pi.ingestion_status,"
        "s.source_type,s.logical_name,r.revision,r.sha256,r.status AS revision_status "
        "FROM source_package_item pi "
        "JOIN source_document s ON s.source_id=pi.source_id "
        "JOIN source_revision r ON r.revision_id=pi.revision_id AND r.source_id=pi.source_id "
        "WHERE pi.package_id=%s ORDER BY s.source_type,s.logical_name",
        (package_id,),
    ).fetchall()
    if not items:
        raise HTTPException(422, "The source package contains no source revisions")
    for item in items:
        if item["content_hash"] and item["content_hash"] != item["sha256"]:
            raise HTTPException(409, f"Package hash mismatch for source '{item['logical_name']}'")
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM source_fragment WHERE revision_id=%s AND source_id=%s",
            (item["revision_id"], item["source_id"]),
        ).fetchone()["n"]
        if not count:
            raise HTTPException(422, f"Source '{item['logical_name']}' has no cited fragments; fix the source package before indexing")
    fragments = conn.execute(
        "SELECT f.*,s.source_type,s.logical_name,r.revision "
        "FROM source_package_manifest pm "
        "JOIN source_package_item pi ON pi.package_id=pm.package_id "
        "JOIN source_revision r ON r.revision_id=pi.revision_id AND r.source_id=pi.source_id "
        "JOIN source_fragment f ON f.revision_id=r.revision_id AND f.source_id=pi.source_id "
        "JOIN source_document s ON s.source_id=pi.source_id "
        "WHERE pm.package_id=%s ORDER BY s.source_type,s.logical_name,f.fragment_index",
        (package_id,),
    ).fetchall()
    return items, fragments


def _current_object_hash(conn, index_id):
    contents = {
        "index": _dict(conn.execute("SELECT config_hash,manifest_hash,dimensions FROM knowledge_index WHERE index_id=%s", (index_id,)).fetchone()),
        "chunks": [_dict(row) for row in conn.execute("SELECT chunk_id,content_hash,metadata_json FROM knowledge_chunk WHERE index_id=%s ORDER BY chunk_id", (index_id,)).fetchall()],
        "nodes": [_dict(row) for row in conn.execute("SELECT canonical_id,label,entity_type,aliases_json,evidence_chunk_ids_json FROM knowledge_node WHERE index_id=%s ORDER BY canonical_id", (index_id,)).fetchall()],
        "edges": [_dict(row) for row in conn.execute("SELECT source_node_id,target_node_id,relationship_type,evidence_chunk_ids_json,confidence FROM knowledge_edge WHERE index_id=%s ORDER BY edge_id", (index_id,)).fetchall()],
    }
    return _hash(json.dumps(contents, sort_keys=True, separators=(",", ":")))


def _latest_review(conn, index_id):
    return conn.execute("SELECT reviewer,decision,comments,object_hash,reviewed_at FROM knowledge_index_review WHERE index_id=%s ORDER BY reviewed_at DESC LIMIT 1", (index_id,)).fetchone()


def _ensure_system_approval(conn, index_id):
    """Record validation without introducing a human approval gate."""
    object_hash = _current_object_hash(conn, index_id)
    review = _latest_review(conn, index_id)
    if review and review["decision"] == "approved" and review["object_hash"] == object_hash:
        return review
    conn.execute(
        "INSERT INTO knowledge_index_review (review_id,index_id,reviewer,decision,comments,object_hash,reviewed_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (_uid("krev"), index_id, "system:auto", "approved", "Automatically validated after a successful Knowledge Base build; no human approval gate.", object_hash, _now()),
    )
    return _latest_review(conn, index_id)


def _assert_approved(conn, index):
    review = _latest_review(conn, index["index_id"])
    if not review or review["decision"] != "approved":
        raise HTTPException(409, "Knowledge Base index is not ready for use")
    if review["object_hash"] != _current_object_hash(conn, index["index_id"]):
        raise HTTPException(409, "The Knowledge Base index changed and must be rebuilt")
    return review


def _latest_ready_index(conn, indexes, *, verify_integrity=True):
    for index in indexes:
        if index["status"] != "ready":
            continue
        review = _latest_review(conn, index["index_id"])
        if not review or review["decision"] != "approved":
            review = _ensure_system_approval(conn, index["index_id"])
        if verify_integrity:
            _assert_approved(conn, index)
        return index
    return None


def _cosine(left, right):
    if len(left) != len(right) or not left:
        raise ValueError("Query and indexed embeddings have different dimensions")
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


def _context_pack(hits, max_characters):
    sections, used = [], 0
    for hit in hits:
        heading = f"[{hit['chunk_id']}] {hit['source_reference']}"
        if hit.get("heading_path"):
            heading += f" | {hit['heading_path']}"
        section = f"{heading}\n{hit['chunk_text']}"
        remaining = max_characters - used
        if remaining <= 0:
            break
        if len(section) > remaining:
            section = section[:remaining].rstrip()
        sections.append(section)
        used += len(section) + 2
    return "\n\n".join(sections)


def _search_tokens(value):
    return [token.rstrip("._/-") for token in re.findall(r"[a-z0-9][a-z0-9_./-]*", str(value or "").lower())]


def _bm25_scores(query, documents, k1=1.5, b=0.75):
    """Small-corpus BM25 used alongside embeddings; scores are normalized 0..1."""
    query_terms = list(dict.fromkeys(_search_tokens(query)))
    tokenized = {key: _search_tokens(value) for key, value in documents.items()}
    if not query_terms or not tokenized:
        return {key: 0.0 for key in documents}
    document_count = len(tokenized)
    average_length = sum(len(tokens) for tokens in tokenized.values()) / max(document_count, 1)
    document_frequency = Counter(term for term in query_terms for tokens in tokenized.values() if term in set(tokens))
    scores = {}
    for key, tokens in tokenized.items():
        frequencies = Counter(tokens)
        score = 0.0
        for term in query_terms:
            frequency = frequencies[term]
            if not frequency:
                continue
            inverse_frequency = math.log(1 + (document_count - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5))
            denominator = frequency + k1 * (1 - b + b * len(tokens) / max(average_length, 1))
            score += inverse_frequency * (frequency * (k1 + 1)) / denominator
        scores[key] = score
    maximum = max(scores.values(), default=0.0)
    return {key: (value / maximum if maximum else 0.0) for key, value in scores.items()}


def _rrf(ranked_lists, k=RRF_K):
    scores = defaultdict(float)
    ranks = defaultdict(dict)
    for channel, values in ranked_lists.items():
        for rank, value in enumerate(values, 1):
            chunk_id = value["chunk_id"] if isinstance(value, dict) else value
            scores[chunk_id] += 1.0 / (k + rank)
            ranks[chunk_id][channel] = rank
    maximum = max(scores.values(), default=1.0)
    return {key: value / maximum for key, value in scores.items()}, ranks


def _rerank_candidates(client, model, query, candidates):
    schema = {"type": "object", "additionalProperties": False, "properties": {
        "ranked": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "properties": {"chunk_id": {"type": "string"}, "relevance_score": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string"}}, "required": ["chunk_id", "relevance_score", "reason"]}},
    }, "required": ["ranked"]}
    prompt = {"query": query, "candidates": [{"chunk_id": item["chunk_id"], "source_type": item["source_type"],
        "source_reference": item["source_reference"], "heading": item.get("heading_path"), "text": item["chunk_text"][:1600]}
        for item in candidates]}
    instructions = (
        "Rerank cited evidence for the query. Score only explicit relevance from 0 to 1. "
        "Keep every supplied chunk_id exactly once, never add an id, and prefer passages that directly answer the query."
    )
    response = responses_create(client, model=model, instructions=instructions, input=json.dumps(prompt, ensure_ascii=False), store=False,
        text={"format": {"type": "json_schema", "name": "testiq_semantic_rerank", "strict": True, "schema": schema}},
        max_output_tokens=min(7000, 500 + len(candidates) * 90))
    payload = json.loads(response.output_text or "{}")
    allowed = {item["chunk_id"] for item in candidates}
    response_items = payload.get("ranked")
    if not isinstance(response_items, list):
        raise ValueError("Reranker did not return a ranked list")
    returned_ids = [item.get("chunk_id") for item in response_items if isinstance(item, dict)]
    if len(returned_ids) != len(candidates) or len(set(returned_ids)) != len(returned_ids) or set(returned_ids) != allowed:
        raise ValueError("Reranker must return every supplied candidate exactly once")
    ranked = []
    for item in response_items:
        chunk_id = item.get("chunk_id")
        ranked.append((chunk_id, max(0.0, min(1.0, float(item.get("relevance_score", 0.0)))), str(item.get("reason") or "")))
    if not ranked:
        raise ValueError("Reranker returned no valid candidate identifiers")
    return ranked


def _near_duplicate(left, right):
    left_tokens, right_tokens = set(_search_tokens(left)), set(_search_tokens(right))
    if not left_tokens or not right_tokens:
        return False
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens) >= 0.92


def _select_diverse_hits(candidates, top_k, minimum_score, max_chunks_per_source):
    selected, per_source = [], Counter()
    for item in candidates:
        if item["final_score"] < minimum_score:
            continue
        source_id = item.get("metadata", {}).get("source_id") or item["source_reference"].split("@v", 1)[0]
        if per_source[source_id] >= max_chunks_per_source:
            continue
        if any(item["content_hash"] == chosen["content_hash"] or _near_duplicate(item["chunk_text"], chosen["chunk_text"]) for chosen in selected):
            continue
        selected.append(item)
        per_source[source_id] += 1
        if len(selected) >= top_k:
            break
    return selected


def _graph_expansion(conn, index_id, seed_chunk_ids, graph_depth):
    if graph_depth <= 0 or not seed_chunk_ids:
        return set(), []
    import networkx as nx
    nodes = {}
    for row in conn.execute("SELECT node_id,label,evidence_chunk_ids_json FROM knowledge_node WHERE index_id=%s", (index_id,)).fetchall():
        item = _dict(row)
        item["evidence"] = set(json.loads(item.pop("evidence_chunk_ids_json") or "[]"))
        nodes[item["node_id"]] = item
    graph = nx.MultiGraph()
    for node_id, node in nodes.items():
        graph.add_node(node_id, **node)
    for row in conn.execute("SELECT source_node_id,target_node_id,relationship_type,evidence_chunk_ids_json,confidence FROM knowledge_edge WHERE index_id=%s", (index_id,)).fetchall():
        item = _dict(row)
        item["evidence"] = set(json.loads(item.pop("evidence_chunk_ids_json") or "[]"))
        if item["source_node_id"] in nodes and item["target_node_id"] in nodes:
            graph.add_edge(item["source_node_id"], item["target_node_id"], **item)
    seeds = {node_id for node_id, node in nodes.items() if node["evidence"] & seed_chunk_ids}
    if not seeds:
        return set(), []
    distances = nx.multi_source_dijkstra_path_length(graph, seeds, cutoff=graph_depth, weight=None)
    evidence, paths, seen = set(), [], set()
    for source_id, target_id, edge_key, edge in graph.edges(keys=True, data=True):
        if source_id not in distances or target_id not in distances:
            continue
        depth = max(1, distances[source_id], distances[target_id])
        if depth > graph_depth:
            continue
        identity = (min(source_id, target_id), max(source_id, target_id), edge_key)
        if identity in seen:
            continue
        seen.add(identity)
        source, target = nodes[source_id], nodes[target_id]
        path_evidence = edge["evidence"] | source["evidence"] | target["evidence"]
        evidence.update(path_evidence)
        paths.append({"source": source["label"], "relationship": edge["relationship_type"], "target": target["label"],
            "confidence": float(edge["confidence"]), "depth": int(depth), "evidence_chunk_ids": sorted(path_evidence)})
    paths.sort(key=lambda item: (item["depth"], -item["confidence"], item["source"], item["target"]))
    return evidence, paths[:50]


def _create_client():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(503, "OPENAI_API_KEY is not configured. Add it to backend/.env and restart the API.")
    from openai import OpenAI
    return OpenAI(api_key=api_key, timeout=OPENAI_TIMEOUT_SECONDS, max_retries=0)


def _retrieve_with_client(index, request, client, query_vector=None):
    source_types = set(request.source_types)
    unknown = source_types - SOURCE_TYPES
    if unknown:
        raise HTTPException(422, f"Unsupported source types: {', '.join(sorted(unknown))}")
    if query_vector is None:
        response = embeddings_create(client, model=index["embedding_model"], input=[request.query.strip()],
            encoding_format="float", dimensions=int(index["dimensions"]))
        query_vector = response.data[0].embedding
    with connection() as conn:
        scope = "index_id=%s"
        params = [index["index_id"]]
        if source_types:
            placeholders = ",".join(["%s"] * len(source_types))
            scope += f" AND source_type IN ({placeholders})"
            params.extend(sorted(source_types))
        sql = f"SELECT chunk_id,source_type,source_reference,heading_path,chunk_text,content_hash,metadata_json FROM knowledge_chunk WHERE {scope}"
        rows = conn.execute(sql, tuple(params)).fetchall()
        if not rows:
            raise HTTPException(404, "No indexed chunks matched the requested source scope")
        scored = {}
        for row in rows:
            item = _dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            item["vector_score"] = 0.0
            item["graph_expanded"] = False
            scored[item["chunk_id"]] = item
        lexical_scores = _bm25_scores(request.query, {key: f"{value.get('heading_path') or ''} {value['chunk_text']}" for key, value in scored.items()})
        for chunk_id, score in lexical_scores.items():
            scored[chunk_id]["lexical_score"] = score
        lexical_ranked = sorted(scored.values(), key=lambda item: (-item["lexical_score"], item["chunk_id"]))
        lexical_candidates = lexical_ranked[:request.candidate_k]
        use_pgvector = bool(db_module.PGVECTOR_AVAILABLE) and int(index["dimensions"]) == PGVECTOR_DIMENSIONS
        vector_backend = "pgvector-hnsw" if use_pgvector else "python-cosine"
        vector_candidates = []
        if use_pgvector:
            literal = _vector_literal(query_vector)
            vector_sql = (
                f"SELECT chunk_id, 1 - (embedding_vector <=> %s::vector) AS vector_score "
                f"FROM knowledge_chunk WHERE {scope} AND embedding_vector IS NOT NULL "
                f"ORDER BY embedding_vector <=> %s::vector LIMIT %s"
            )
            vector_rows = conn.execute(vector_sql, tuple([literal, *params, literal, request.candidate_k])).fetchall()
            for row in vector_rows:
                item = scored[row["chunk_id"]]
                item["vector_score"] = float(row["vector_score"])
                vector_candidates.append(item)
        if not vector_candidates:
            # Without pgvector, avoid transferring every large JSON embedding
            # across a remote PostgreSQL connection. BM25 supplies a broad,
            # deterministic candidate pool and cosine similarity then provides
            # the vector ordering before graph expansion and RRF fusion.
            vector_backend = "python-cosine-prefiltered" if not use_pgvector else "python-cosine-prefiltered-fallback"
            prefilter_count = min(len(lexical_ranked), max(request.candidate_k * 10, 200))
            prefilter_ids = [item["chunk_id"] for item in lexical_ranked[:prefilter_count]]
            placeholders = ",".join(["%s"] * len(prefilter_ids))
            embedding_rows = conn.execute(
                f"SELECT chunk_id,embedding_json FROM knowledge_chunk WHERE {scope} AND chunk_id IN ({placeholders})",
                tuple([*params, *prefilter_ids]),
            ).fetchall()
            for row in embedding_rows:
                vector = json.loads(row["embedding_json"] or "[]")
                scored[row["chunk_id"]]["vector_score"] = _cosine(query_vector, vector)
            vector_candidates = sorted(
                (scored[chunk_id] for chunk_id in prefilter_ids),
                key=lambda item: (-item["vector_score"], item["chunk_id"]),
            )[:request.candidate_k]
        seeds = {item["chunk_id"] for item in vector_candidates[:min(8, request.candidate_k)]}
        graph_evidence, graph_paths = _graph_expansion(conn, index["index_id"], seeds, request.graph_depth)
        graph_ranked_ids = []
        for path in graph_paths:
            for chunk_id in path["evidence_chunk_ids"]:
                if chunk_id in scored and chunk_id not in seeds and chunk_id not in graph_ranked_ids:
                    graph_ranked_ids.append(chunk_id)
        for chunk_id in graph_evidence:
            if chunk_id in scored:
                scored[chunk_id]["graph_expanded"] = chunk_id not in seeds
                if chunk_id not in seeds and chunk_id not in graph_ranked_ids:
                    graph_ranked_ids.append(chunk_id)
        fusion_scores, channel_ranks = _rrf({"vector": vector_candidates, "keyword": lexical_candidates, "graph": graph_ranked_ids})
        candidate_ids = sorted(fusion_scores, key=lambda chunk_id: (-fusion_scores[chunk_id], chunk_id))[:request.candidate_k]
        candidates = [scored[chunk_id] for chunk_id in candidate_ids]
        for item in candidates:
            item["fusion_score"] = fusion_scores[item["chunk_id"]]
            item["channel_ranks"] = channel_ranks[item["chunk_id"]]
        reranker = {"enabled": request.use_reranker, "model": DEFAULT_RERANKER_MODEL if request.use_reranker else None,
            "status": "disabled" if not request.use_reranker else "completed", "version": "openai-semantic-reranker-v2"}
        warnings = []
        reranked = []
        if request.use_reranker:
            try:
                reranked = _rerank_candidates(client, DEFAULT_RERANKER_MODEL, request.query.strip(), candidates)
            except Exception as exc:
                reranker["status"] = "fallback"
                warnings.append(f"Semantic reranking was unavailable; RRF order was used: {type(exc).__name__}: {exc}")
        rerank_by_id = {chunk_id: (position, score, reason) for position, (chunk_id, score, reason) in enumerate(reranked, 1)}
        for item in candidates:
            rank, reranker_score, reason = rerank_by_id.get(item["chunk_id"], (None, None, ""))
            item["reranker_rank"] = rank
            item["reranker_score"] = reranker_score
            item["reranker_reason"] = reason
            item["final_score"] = (0.8 * reranker_score + 0.2 * item["fusion_score"]) if reranker_score is not None else item["fusion_score"]
        candidates.sort(key=lambda item: (-item["final_score"], -item["fusion_score"], item["chunk_id"]))
        candidate_count = len(candidates)
        reranker_filtered_count = 0
        if reranker["status"] == "completed":
            before_filter = len(candidates)
            candidates = [item for item in candidates if item["reranker_score"] is not None
                          and item["reranker_score"] >= request.minimum_reranker_score]
            reranker_filtered_count = before_filter - len(candidates)
        hits = _select_diverse_hits(candidates, request.top_k, request.minimum_score, request.max_chunks_per_source)
    for rank, hit in enumerate(hits, 1):
        hit["rank"] = rank
        hit["vector_score"] = round(hit["vector_score"], 6)
        hit["lexical_score"] = round(hit["lexical_score"], 6)
        hit["fusion_score"] = round(hit["fusion_score"], 6)
        if hit["reranker_score"] is not None:
            hit["reranker_score"] = round(hit["reranker_score"], 6)
        hit["final_score"] = round(hit["final_score"], 6)
        hit["retrieval_score"] = hit["final_score"]
    context = _context_pack(hits, request.max_context_characters)
    reranker["minimum_relevance_score"] = request.minimum_reranker_score if request.use_reranker else None
    reranker["filtered_candidate_count"] = reranker_filtered_count
    return hits, graph_paths, context, reranker, warnings, candidate_count, vector_backend


def retrieve_knowledge(index_id, request, client=None, query_vector=None):
    with connection() as conn:
        index = conn.execute("SELECT * FROM knowledge_index WHERE index_id=%s", (index_id,)).fetchone()
        if not index:
            raise HTTPException(404, "Knowledge Base index not found")
        if index["status"] != "ready":
            raise HTTPException(409, "Only a ready Knowledge Base index can be queried")
        if request.require_approved:
            _assert_approved(conn, index)
        index_data = _dict(index)
    client = client or _create_client()
    hits, graph_paths, context, reranker, warnings, candidate_count, vector_backend = _retrieve_with_client(
        index_data, request, client, query_vector=query_vector
    )
    retrieval_id, created_at = _uid("ret"), _now()
    context_hash = _hash(context)
    configuration = {"retrieval_version": RETRIEVAL_VERSION, "embedding_model": index_data["embedding_model"],
        "vector_backend": vector_backend, "candidate_k": request.candidate_k, "candidate_count": candidate_count, "top_k": request.top_k,
        "graph_depth": request.graph_depth, "source_types": sorted(request.source_types), "rrf_k": RRF_K,
        "minimum_score": request.minimum_score, "max_chunks_per_source": request.max_chunks_per_source,
        "minimum_reranker_score": request.minimum_reranker_score,
        "reranker": reranker, "max_context_characters": request.max_context_characters,
        "require_approved": request.require_approved}
    stored_result = {"chunk_ids": [hit["chunk_id"] for hit in hits],
        "scores": {hit["chunk_id"]: {"vector": hit["vector_score"], "keyword": hit["lexical_score"],
            "fusion": hit["fusion_score"], "reranker": hit["reranker_score"], "final": hit["final_score"],
            "channel_ranks": hit["channel_ranks"]} for hit in hits}, "graph_paths": graph_paths,
        "warnings": warnings}
    with connection() as conn:
        conn.execute("INSERT INTO knowledge_retrieval_run (retrieval_id,index_id,package_id,query_text,query_hash,requested_by,purpose,retrieval_config_json,result_json,context_hash,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (retrieval_id, index_id, index_data["package_id"], request.query.strip(), _hash(request.query.strip()),
             request.requested_by.strip(), request.purpose.strip(), json.dumps(configuration, sort_keys=True),
             json.dumps(stored_result, sort_keys=True), context_hash, created_at))
    return {"retrieval_id": retrieval_id, "index_id": index_id, "package_id": index_data["package_id"],
        "query": request.query.strip(), "retrieval_version": RETRIEVAL_VERSION, "embedding_model": index_data["embedding_model"],
        "reranker": reranker, "configuration": configuration, "warnings": warnings,
        "hits": hits, "graph_paths": graph_paths, "context": context, "context_hash": context_hash, "created_at": created_at}


@usage_step("knowledge_base")
def retrieve_package_context(package_id, requested_by="Requirements Analysis"):
    with connection() as conn:
        indexes = conn.execute("SELECT * FROM knowledge_index WHERE package_id=%s ORDER BY created_at DESC", (package_id,)).fetchall()
        if not indexes:
            raise HTTPException(409, "Build the Knowledge Base before requesting package context")
        selected_index = _latest_ready_index(conn, indexes)
        if not selected_index:
            raise HTTPException(409, "No ready Knowledge Base index is available for this package")
        # Knowledge Base builds are automatic. Refresh the system validation
        # record for the current index contents instead of requiring a human
        # approval or blocking downstream generation on a stale hash.
        approval = _ensure_system_approval(conn, selected_index["index_id"])
        selected = _dict(selected_index)
        approved_rules = [dict(row) for row in conn.execute(
            "SELECT rule_id,statement,source_fragment_id,source_reference,impact_if_missing FROM package_business_rule WHERE package_id=%s AND status='approved' ORDER BY ordinal",
            (package_id,),
        ).fetchall()]
        available_source_types = {row["source_type"] for row in conn.execute(
            "SELECT DISTINCT source_type FROM knowledge_chunk WHERE index_id=%s", (selected["index_id"],)
        ).fetchall()}
    client = _create_client()
    query_specs = [
        ("Campaign requirements, business rules, eligibility, exclusions, conditions, and expected outcomes", ["brd"]),
        ("Acceptance criteria, observable outcomes, alternate flows, failures, fallback, and retry behavior", ["brd"]),
    ]
    technical_source_types = sorted(available_source_types & {"database_schema", "api_spec", "message_template", "campaign_rules", "ui_description"})
    if technical_source_types:
        query_specs.append((
            "Database tables, fields, relationships, API operations, request contracts, message templates, data templates, and implementation gaps",
            technical_source_types,
        ))
    embedding_response = embeddings_create(client,
        model=selected["embedding_model"],
        input=[query for query, _ in query_specs],
        encoding_format="float",
        dimensions=int(selected["dimensions"]),
    )
    query_vectors = [item.embedding for item in embedding_response.data]
    if len(query_vectors) != len(query_specs):
        raise RuntimeError("OpenAI returned an incomplete batch of retrieval query embeddings")
    retrievals, unique_hits = [], {}
    for (query, source_types), query_vector in zip(query_specs, query_vectors):
        result = retrieve_knowledge(selected["index_id"], RetrievalRequest(query=query, top_k=10, candidate_k=30, graph_depth=1,
            source_types=source_types, max_context_characters=16_000, requested_by=requested_by,
            purpose=str(requested_by).strip().lower().replace(" ", "_"), require_approved=True,
            use_reranker=True), client=client, query_vector=query_vector)
        retrievals.append(result)
        for hit in result["hits"]:
            unique_hits.setdefault(hit["chunk_id"], hit)
    hits = sorted(unique_hits.values(), key=lambda item: (item["source_type"] != "brd", item["rank"], item["chunk_id"]))
    fragments = []
    for hit in hits:
        metadata = hit["metadata"]
        fragments.append({"fragment_id": hit["chunk_id"], "original_fragment_id": metadata.get("fragment_id"),
            "source_id": metadata.get("source_id"), "revision_id": metadata.get("revision_id"),
            "source_type": hit["source_type"], "source_reference": hit["source_reference"],
            "heading": hit.get("heading_path"), "content_hash": hit["content_hash"], "fragment_text": hit["chunk_text"]})
    retrieval_trace = [{
        "retrieval_id": result["retrieval_id"], "query": result["query"],
        "source_types": result["configuration"]["source_types"], "hit_count": len(result["hits"]),
        "chunk_ids": [hit["chunk_id"] for hit in result["hits"]], "context_hash": result["context_hash"],
        "vector_backend": result["configuration"]["vector_backend"],
        "reranker": result["configuration"]["reranker"], "warnings": result["warnings"],
    } for result in retrievals]
    rag_metadata = {
        "index_id": selected["index_id"], "index_config_hash": selected["config_hash"],
        "manifest_hash": selected["manifest_hash"], "provider": selected["provider"],
        "embedding_model": selected["embedding_model"], "embedding_dimensions": selected["dimensions"],
        "graph_model": selected["graph_model"], "chunker_version": selected["chunker_version"],
        "graph_prompt_version": selected["graph_prompt_version"], "retrieval_version": RETRIEVAL_VERSION,
        "retrieval_ids": [result["retrieval_id"] for result in retrievals],
        "context_hash": _hash(json.dumps([result["context_hash"] for result in retrievals], sort_keys=True)),
        "retrieved_chunk_ids": sorted(unique_hits), "retrievals": retrieval_trace,
        "index_approval": {"reviewer": approval["reviewer"], "decision": approval["decision"],
                           "object_hash": approval["object_hash"], "reviewed_at": approval["reviewed_at"]},
    }
    return {"index_id": selected["index_id"], "knowledge_version": int(selected.get("knowledge_version") or 1),
        "parent_index_id": selected.get("parent_index_id"), "index_config_hash": selected["config_hash"],
        "retrieval_ids": [result["retrieval_id"] for result in retrievals],
        "context_hash": rag_metadata["context_hash"], "rag_metadata": rag_metadata,
        "approved_rules": approved_rules,
        "brd_fragments": [item for item in fragments if item["source_type"] == "brd"],
        "technical_fragments": [item for item in fragments if item["source_type"] in {"database_schema", "api_spec", "message_template", "campaign_rules", "ui_description"}]}


@router.post("/api/v1/packages/{package_id}/knowledge-index", status_code=201)
@usage_step("knowledge_base")
def build_knowledge_index(package_id: str, request: BuildIndexRequest):
    with connection() as conn:
        package = conn.execute("SELECT * FROM source_package_manifest WHERE package_id=%s", (package_id,)).fetchone()
        if not package:
            raise HTTPException(404, "Source package not found")
        if package["lifecycle_status"] not in {"ready", "published"} or not package["manifest_hash"]:
            raise HTTPException(422, "Only a ready, hash-verified source package can be indexed")
        if db_module.DATABASE_URL:
            conn.execute("SELECT project_id FROM project WHERE project_id=%s FOR UPDATE", (package["project_id"],)).fetchone()
        approved_rules = conn.execute("SELECT rule_id FROM package_business_rule WHERE package_id=%s AND status='approved'", (package_id,)).fetchall()
        approved_rule_count = len(approved_rules)
        items, fragments = _load_package_inputs(conn, package_id)
        fragment_set_hash = _hash(json.dumps([{"fragment_id": row["fragment_id"], "content_hash": row["content_hash"], "extractor_version": row["extractor_version"]} for row in fragments], sort_keys=True, separators=(",", ":")))
        configuration = {"manifest_hash": package["manifest_hash"], "provider": request.provider,
            "embedding_model": request.embedding_model, "embedding_dimensions": OPENAI_EMBEDDING_DIMENSIONS,
            "vector_backend": "pgvector-hnsw" if db_module.PGVECTOR_AVAILABLE else "python-cosine",
            "graph_model": request.graph_model,
            "graph_engine": f"networkx-{package_version('networkx')}",
            "chunker_version": CHUNKER_VERSION, "graph_prompt_version": GRAPH_PROMPT_VERSION,
            "graph_prompt_hash": _hash(GRAPH_INSTRUCTIONS), "fragment_set_hash": fragment_set_hash,
            "approved_rule_set": {"hash": package["rule_set_hash"], "count": approved_rule_count, "mode": "automatic"},
            "rag_build_version": RAG_BUILD_VERSION,
            "update_policy": "project-versioned-incremental",
            "build_limits": {"chunks": MAX_INDEX_CHUNKS or None, "characters": MAX_INDEX_CHARACTERS or None},
            "graph_llm_source_types": ["brd", "campaign_rules", "message_template", "ui_description"],
            "package_resolution": {
                "root": "source_package_manifest.package_id",
                "path": ["source_package_item.package_id", "source_package_item.revision_id", "source_revision.revision_id", "source_fragment.revision_id"],
                "source_count": len(items), "fragment_count": len(fragments),
            }}
        configuration_json = json.dumps(configuration, sort_keys=True, separators=(",", ":"))
        config_hash = _hash(configuration_json)
        existing = conn.execute("SELECT * FROM knowledge_index WHERE package_id=%s AND config_hash=%s", (package_id, config_hash)).fetchone()
        if existing and existing["status"] == "ready":
            _ensure_system_approval(conn, existing["index_id"])
            return {**_serialize_index(existing), "reused": True}
        if existing and existing["status"] == "building" and package_id in _ACTIVE_KNOWLEDGE_BUILDS:
            raise HTTPException(409, "A Knowledge Base build with this package and configuration is already running")
        index_id = existing["index_id"] if existing else _uid("kidx")
        parent = conn.execute(
            "SELECT * FROM knowledge_index WHERE project_id=%s AND status='ready' AND index_id<>%s "
            "ORDER BY knowledge_version DESC,created_at DESC LIMIT 1",
            (package["project_id"], index_id),
        ).fetchone()
        embedding_parent_compatible = bool(parent and parent["embedding_model"] == request.embedding_model
            and int(parent["dimensions"] or 0) == OPENAI_EMBEDDING_DIMENSIONS
            and parent["chunker_version"] == CHUNKER_VERSION)
        graph_parent_compatible = bool(embedding_parent_compatible and parent["graph_model"] == request.graph_model
            and parent["graph_prompt_version"] == GRAPH_PROMPT_VERSION
            and parent["graph_prompt_hash"] == configuration["graph_prompt_hash"])
        if existing:
            knowledge_version = int(existing["knowledge_version"] or 1)
        else:
            latest_version = conn.execute("SELECT COALESCE(MAX(knowledge_version),0) AS version FROM knowledge_index WHERE project_id=%s", (package["project_id"],)).fetchone()["version"]
            knowledge_version = int(latest_version or 0) + 1
        if existing:
            conn.execute("DELETE FROM knowledge_evaluation_run WHERE index_id=%s", (index_id,))
            conn.execute("DELETE FROM knowledge_retrieval_run WHERE index_id=%s", (index_id,))
            conn.execute("DELETE FROM knowledge_index_review WHERE index_id=%s", (index_id,))
            conn.execute("DELETE FROM knowledge_edge WHERE index_id=%s", (index_id,))
            conn.execute("DELETE FROM knowledge_node WHERE index_id=%s", (index_id,))
            conn.execute("DELETE FROM knowledge_chunk WHERE index_id=%s", (index_id,))
            conn.execute("UPDATE knowledge_index SET status=%s,error=NULL,chunk_count=0,node_count=0,edge_count=0,parent_index_id=%s,build_mode=%s,reused_chunk_count=0,new_chunk_count=0,retired_chunk_count=0,progress_percent=%s,progress_stage=%s,progress_message=%s,created_at=%s WHERE index_id=%s",
                ("building", parent["index_id"] if parent else None, "full", 5, "preparing", "Preparing package evidence.", _now(), index_id))
        else:
            conn.execute("INSERT INTO knowledge_index (index_id,package_id,project_id,manifest_hash,knowledge_version,parent_index_id,build_mode,reused_chunk_count,new_chunk_count,retired_chunk_count,config_hash,provider,embedding_model,graph_model,dimensions,status,configuration_json,chunker_version,graph_prompt_version,graph_prompt_hash,progress_percent,progress_stage,progress_message,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (index_id, package_id, package["project_id"], package["manifest_hash"], knowledge_version,
                 parent["index_id"] if parent else None, "full", 0, 0, 0, config_hash, request.provider,
                 request.embedding_model, request.graph_model, OPENAI_EMBEDDING_DIMENSIONS,
                 "building", configuration_json, CHUNKER_VERSION, GRAPH_PROMPT_VERSION, configuration["graph_prompt_hash"],
                 5, "preparing", "Preparing package evidence.", _now()))
        chunk_rows = []
        fingerprint_occurrences = Counter()
        for fragment in fragments:
            for part, (body, start, end) in enumerate(_chunks(fragment), 1):
                fingerprint_base = _hash(json.dumps({"source_id": fragment["source_id"], "source_type": fragment["source_type"],
                    "heading_path": fragment["heading_path"] or fragment["heading"] or "", "text": body}, sort_keys=True, separators=(",", ":")))
                fingerprint_occurrences[fingerprint_base] += 1
                fingerprint = _hash(f"{fingerprint_base}:{fingerprint_occurrences[fingerprint_base]}")
                chunk_id = "chk_" + _hash(index_id + ":" + fingerprint)[:32]
                source_ref = f"{fragment['logical_name']}@v{fragment['revision']} | {fragment['source_reference']}"
                metadata = {"fragment_id": fragment["fragment_id"], "fragment_type": fragment["fragment_type"],
                    "source_id": fragment["source_id"], "revision_id": fragment["revision_id"], "source_type": fragment["source_type"],
                    "source_reference": source_ref, "heading": fragment["heading"], "heading_path": fragment["heading_path"],
                    "page_number": fragment["page_number"], "paragraph_index": fragment["paragraph_index"],
                    "table_index": fragment["table_index"], "row_index": fragment["row_index"], "cell_index": fragment["cell_index"],
                    "char_start": start, "char_end": end, "fragment_content_hash": fragment["content_hash"],
                    "chunk_fingerprint": fingerprint, "chunk_part": part}
                chunk_rows.append({"chunk_id": chunk_id, "source_type": fragment["source_type"], "source_reference": source_ref,
                    "heading_path": fragment["heading_path"] or fragment["heading"], "text": body, "content_hash": _hash(body),
                    "fingerprint": fingerprint, "metadata": metadata, "vector": None, "reused_from_chunk_id": None})
        if not chunk_rows:
            raise HTTPException(422, "No readable source fragments were found in this package")
        character_count = sum(len(item["text"]) for item in chunk_rows)
        chunk_limit_exceeded = MAX_INDEX_CHUNKS > 0 and len(chunk_rows) > MAX_INDEX_CHUNKS
        character_limit_exceeded = MAX_INDEX_CHARACTERS > 0 and character_count > MAX_INDEX_CHARACTERS
        if chunk_limit_exceeded or character_limit_exceeded:
            chunk_limit = f"{MAX_INDEX_CHUNKS:,}" if MAX_INDEX_CHUNKS else "unlimited"
            character_limit = f"{MAX_INDEX_CHARACTERS:,}" if MAX_INDEX_CHARACTERS else "unlimited"
            raise HTTPException(413, f"This package produced {len(chunk_rows):,} chunks and {character_count:,} characters. The configured build limits are {chunk_limit} chunks and {character_limit} characters.")
        parent_chunk_count = 0
        old_to_new_chunk_ids = {}
        if embedding_parent_compatible:
            parent_chunks = conn.execute("SELECT * FROM knowledge_chunk WHERE index_id=%s ORDER BY chunk_id", (parent["index_id"],)).fetchall()
            parent_chunk_count = len(parent_chunks)
            reusable = defaultdict(list)
            fallback_occurrences = Counter()
            for row in parent_chunks:
                metadata = json.loads(row["metadata_json"] or "{}")
                fingerprint = metadata.get("chunk_fingerprint")
                if not fingerprint:
                    base = _hash(json.dumps({"source_id": row["source_id"], "source_type": row["source_type"],
                        "heading_path": row["heading_path"] or "", "text": row["chunk_text"]}, sort_keys=True, separators=(",", ":")))
                    fallback_occurrences[base] += 1
                    fingerprint = _hash(f"{base}:{fallback_occurrences[base]}")
                reusable[fingerprint].append(row)
            for item in chunk_rows:
                matches = reusable.get(item["fingerprint"])
                if not matches:
                    continue
                prior = matches.pop(0)
                vector = json.loads(prior["embedding_json"] or "[]")
                if len(vector) != OPENAI_EMBEDDING_DIMENSIONS:
                    continue
                item["vector"] = vector
                item["reused_from_chunk_id"] = prior["chunk_id"]
                old_to_new_chunk_ids[prior["chunk_id"]] = item["chunk_id"]
        reused_chunk_count = len(old_to_new_chunk_ids)
        new_chunk_count = len(chunk_rows) - reused_chunk_count
        retired_chunk_count = max(0, parent_chunk_count - reused_chunk_count)
        build_mode = "incremental" if embedding_parent_compatible else "full"
        seed_nodes, seed_edges = ({}, {})
        if graph_parent_compatible and reused_chunk_count:
            seed_nodes, seed_edges = _load_reusable_graph(conn, parent["index_id"], index_id, old_to_new_chunk_ids)
        conn.execute("UPDATE knowledge_index SET build_mode=%s,reused_chunk_count=%s,new_chunk_count=%s,retired_chunk_count=%s,progress_percent=%s,progress_stage=%s,progress_message=%s WHERE index_id=%s",
            (build_mode, reused_chunk_count, new_chunk_count, retired_chunk_count, 15, "chunking",
             f"Prepared {len(chunk_rows):,} chunks: {reused_chunk_count:,} reused and {new_chunk_count:,} new or changed.", index_id))
    _ACTIVE_KNOWLEDGE_BUILDS.add(package_id)
    try:
        chunks_to_embed = [item for item in chunk_rows if item["vector"] is None]
        graph_chunks = [item for item in chunk_rows if not item["reused_from_chunk_id"]] if graph_parent_compatible else chunk_rows
        needs_graph_client = any(item["source_type"] not in {"database_schema", "api_spec"} for item in graph_chunks)
        client = _create_client() if chunks_to_embed or needs_graph_client else None
        embedding_batches = max(1, math.ceil(len(chunks_to_embed) / EMBEDDING_BATCH_SIZE))
        _update_build_progress(index_id, 20, "embedding", f"Reusing {reused_chunk_count:,} embeddings and generating {len(chunks_to_embed):,} new embeddings.")
        embedded_count = 0
        for batch_number, start in enumerate(range(0, len(chunks_to_embed), EMBEDDING_BATCH_SIZE), 1):
            batch = chunks_to_embed[start:start + EMBEDDING_BATCH_SIZE]
            response = embeddings_create(client, model=request.embedding_model, input=[item["text"] for item in batch],
                encoding_format="float", dimensions=OPENAI_EMBEDDING_DIMENSIONS)
            batch_vectors = [item.embedding for item in sorted(response.data, key=lambda item: item.index)]
            if len(batch_vectors) != len(batch):
                raise ValueError("Embedding response count did not match the submitted chunk count")
            for chunk, vector in zip(batch, batch_vectors):
                chunk["vector"] = vector
            embedded_count += len(batch_vectors)
            embedding_progress = 20 + round((batch_number / embedding_batches) * 45)
            _update_build_progress(index_id, embedding_progress, "embedding", f"Generated {embedded_count:,} of {len(chunks_to_embed):,} new embeddings; {reused_chunk_count:,} were reused.")
        vectors = [item["vector"] for item in chunk_rows]
        dimensions = len(vectors[0]) if vectors else 0
        if not dimensions or any(not vector or len(vector) != dimensions for vector in vectors):
            raise ValueError("Embedding model returned inconsistent vector dimensions")
        _update_build_progress(index_id, 70, "saving", "Saving embedded evidence chunks.")
        with connection() as conn:
            conn.execute("UPDATE knowledge_index SET dimensions=%s WHERE index_id=%s", (dimensions, index_id))
            for item, vector in zip(chunk_rows, vectors):
                conn.execute("INSERT INTO knowledge_chunk (chunk_id,index_id,package_id,source_id,revision_id,fragment_id,source_type,source_reference,heading_path,chunk_text,content_hash,embedding_json,metadata_json) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (item["chunk_id"], index_id, package_id, item["metadata"]["source_id"], item["metadata"]["revision_id"],
                     item["metadata"]["fragment_id"], item["source_type"], item["source_reference"], item["heading_path"], item["text"],
                     item["content_hash"], json.dumps(vector), json.dumps(item["metadata"], ensure_ascii=False)))
                if db_module.PGVECTOR_AVAILABLE:
                    if dimensions != PGVECTOR_DIMENSIONS:
                        raise ValueError(f"Embedding dimensions {dimensions} do not match PGVECTOR_DIMENSIONS={PGVECTOR_DIMENSIONS}")
                    conn.execute("UPDATE knowledge_chunk SET embedding_vector=%s::vector WHERE chunk_id=%s",
                        (_vector_literal(vector), item["chunk_id"]))
        _update_build_progress(index_id, 75, "saving", f"Saved {len(chunk_rows):,} embedded chunks.", chunk_count=len(chunk_rows))
        graph_message = f"Updating the knowledge graph from {len(graph_chunks):,} new or changed chunks." if graph_parent_compatible and reused_chunk_count else "Building the knowledge graph from indexed evidence."
        _update_build_progress(index_id, 80, "graphing", graph_message)
        def graph_progress(done, total, nodes, edges):
            graph_percent = 80 + round((done / max(1, total)) * 16)
            _update_build_progress(index_id, graph_percent, "graphing", f"Built graph batch {done:,} of {total:,}.", node_count=nodes, edge_count=edges)
        node_count, edge_count, graph_warnings = _extract_graph(client, index_id, package_id, chunk_rows, request.graph_model,
            graph_progress, seed_nodes=seed_nodes, seed_edges=seed_edges, graph_chunks=graph_chunks)
        _update_build_progress(index_id, 98, "finalizing", "Finalizing the Knowledge Base index.", chunk_count=len(chunk_rows), node_count=node_count, edge_count=edge_count)
        warning_text = "\n".join(graph_warnings)[:2000] if graph_warnings else None
        with connection() as conn:
            ready_message = (f"Knowledge Base version {knowledge_version} is ready: {reused_chunk_count:,} chunks reused, "
                f"{new_chunk_count:,} added or changed, and {retired_chunk_count:,} retired.")
            conn.execute("UPDATE knowledge_index SET status=%s,chunk_count=%s,node_count=%s,edge_count=%s,progress_percent=%s,progress_stage=%s,progress_message=%s,error=%s WHERE index_id=%s",
                ("ready", len(chunk_rows), node_count, edge_count, 100, "ready", ready_message, warning_text, index_id))
            _ensure_system_approval(conn, index_id)
            row = conn.execute("SELECT * FROM knowledge_index WHERE index_id=%s", (index_id,)).fetchone()
            return {**_serialize_index(row), "reused": False}
    except HTTPException as exc:
        message = str(exc.detail) if isinstance(exc.detail, str) else json.dumps(exc.detail, ensure_ascii=False)
        with connection() as conn:
            conn.execute("UPDATE knowledge_index SET status=%s,progress_stage=%s,progress_message=%s,error=%s WHERE index_id=%s", ("failed", "failed", message[:2000], message[:2000], index_id))
        raise
    except Exception as exc:
        message = f"Knowledge Base build failed: {type(exc).__name__}: {exc}"
        with connection() as conn:
            conn.execute("UPDATE knowledge_index SET status=%s,progress_stage=%s,progress_message=%s,error=%s WHERE index_id=%s", ("failed", "failed", message[:2000], message[:2000], index_id))
        raise HTTPException(502, message) from exc
    finally:
        _ACTIVE_KNOWLEDGE_BUILDS.discard(package_id)


@router.get("/api/v1/packages/{package_id}/knowledge-indexes")
def list_package_indexes(package_id: str):
    with connection() as conn:
        if not conn.execute("SELECT 1 FROM source_package_manifest WHERE package_id=%s", (package_id,)).fetchone():
            raise HTTPException(404, "Source package not found")
        return [_serialize_index(row) for row in conn.execute("SELECT * FROM knowledge_index WHERE package_id=%s ORDER BY created_at DESC", (package_id,)).fetchall()]


@router.get("/api/v1/packages/{package_id}/knowledge-summary")
def get_package_knowledge_summary(package_id: str):
    """Load all Knowledge Base screen artifacts with one PostgreSQL network flush."""
    with connection() as conn:
        package_sql = "SELECT * FROM source_package_manifest WHERE package_id=%s"
        indexes_sql = "SELECT * FROM knowledge_index WHERE package_id=%s ORDER BY created_at DESC LIMIT 25"
        reviews_sql = (
            "SELECT r.index_id,r.reviewer,r.decision,r.comments,r.object_hash,r.reviewed_at "
            "FROM knowledge_index_review r JOIN knowledge_index ki ON ki.index_id=r.index_id "
            "WHERE ki.package_id=%s ORDER BY r.reviewed_at DESC"
        )
        rules_sql = "SELECT * FROM package_business_rule WHERE package_id=%s ORDER BY ordinal"
        versions_sql = (
            "SELECT ki.*,pm.package_version,pm.package_name FROM knowledge_index ki "
            "JOIN source_package_manifest pm ON pm.package_id=ki.package_id "
            "WHERE ki.project_id=(SELECT project_id FROM source_package_manifest WHERE package_id=%s) "
            "ORDER BY ki.knowledge_version DESC,ki.created_at DESC LIMIT 50"
        )

        # PostgreSQL pipeline mode sends these independent reads in one network
        # flush. The SQLite test/development fallback executes the same queries
        # normally because it has no network latency to hide.
        if db_module.DATABASE_URL and hasattr(conn, "pipeline"):
            with conn.pipeline():
                package_cursor = conn.execute(package_sql, (package_id,))
                indexes_cursor = conn.execute(indexes_sql, (package_id,))
                reviews_cursor = conn.execute(reviews_sql, (package_id,))
                rules_cursor = conn.execute(rules_sql, (package_id,))
                versions_cursor = conn.execute(versions_sql, (package_id,))
        else:
            package_cursor = conn.execute(package_sql, (package_id,))
            indexes_cursor = conn.execute(indexes_sql, (package_id,))
            reviews_cursor = conn.execute(reviews_sql, (package_id,))
            rules_cursor = conn.execute(rules_sql, (package_id,))
            versions_cursor = conn.execute(versions_sql, (package_id,))

        package = package_cursor.fetchone()
        if not package:
            raise HTTPException(404, "Resource Package not found")
        index_rows = indexes_cursor.fetchall()
        review_rows = reviews_cursor.fetchall()
        latest_reviews = {}
        for review_row in review_rows:
            latest_reviews.setdefault(review_row["index_id"], review_row)
        indexes = [_serialize_index(row) for row in index_rows]
        ready_index = next((row for row in index_rows if row["status"] == "ready"), None)
        if ready_index:
            review = latest_reviews.get(ready_index["index_id"])
            if not review or review["decision"] != "approved":
                review = _ensure_system_approval(conn, ready_index["index_id"])
                latest_reviews[ready_index["index_id"]] = review
        latest = index_rows[0] if index_rows else None
        if ready_index:
            review = latest_reviews.get(ready_index["index_id"])
            readiness = {
                "package_id": package_id, "index_id": ready_index["index_id"], "status": "approved", "ready": True,
                "index_status": ready_index["status"], "review": _dict(review), "progress_percent": 100,
                "progress_stage": ready_index["progress_stage"] or "ready",
                "progress_message": ready_index["progress_message"] or "Knowledge Base is ready.",
                "message": "Knowledge Base is ready." if latest["index_id"] == ready_index["index_id"] else f"Knowledge Base is ready from the latest successful index. Newest build is {latest['status']}.",
            }
        elif latest:
            review = latest_reviews.get(latest["index_id"])
            status = review["decision"] if latest["status"] == "ready" and review else latest["status"]
            readiness = {
                "package_id": package_id, "index_id": latest["index_id"], "status": status, "ready": False,
                "index_status": latest["status"], "review": _dict(review),
                "progress_percent": int(latest["progress_percent"] or 0),
                "progress_stage": latest["progress_stage"] or latest["status"],
                "progress_message": latest["progress_message"], "message": f"Knowledge Base is {latest['status']}.",
            }
        else:
            readiness = {"package_id": package_id, "status": "missing", "ready": False, "message": "Knowledge Base has not been built for this Resource Package yet."}
        rule_rows = [_dict(row) for row in rules_cursor.fetchall()]
        rules = {
            "package_id": package_id, "review_status": package["rule_review_status"],
            "rule_set_hash": package["rule_set_hash"], "rules_reviewed_at": package["rules_reviewed_at"],
            "rules": rule_rows,
            "counts": {status: sum(rule["status"] == status for rule in rule_rows) for status in ("pending", "approved", "rejected")},
        }
        version_rows = versions_cursor.fetchall()
        return {"package_id": package_id, "indexes": indexes, "readiness": readiness,
                "business_rules": rules, "versions": [_serialize_index(row) for row in version_rows]}


@router.get("/api/v1/projects/{project_id}/knowledge-versions")
def list_project_knowledge_versions(project_id: str):
    with connection() as conn:
        if not conn.execute("SELECT 1 FROM project WHERE project_id=%s", (project_id,)).fetchone():
            raise HTTPException(404, "Project not found")
        rows = conn.execute(
            "SELECT ki.*,pm.package_version,pm.package_name FROM knowledge_index ki "
            "JOIN source_package_manifest pm ON pm.package_id=ki.package_id "
            "WHERE ki.project_id=%s ORDER BY ki.knowledge_version DESC,ki.created_at DESC",
            (project_id,),
        ).fetchall()
        return [_serialize_index(row) for row in rows]


@router.get("/api/v1/packages/{package_id}/rag-readiness")
def get_rag_readiness(package_id: str):
    with connection() as conn:
        if not conn.execute("SELECT 1 FROM source_package_manifest WHERE package_id=%s", (package_id,)).fetchone():
            raise HTTPException(404, "Source package not found")
        indexes = conn.execute("SELECT * FROM knowledge_index WHERE package_id=%s ORDER BY created_at DESC", (package_id,)).fetchall()
        if not indexes:
            return {"package_id": package_id, "status": "missing", "ready": False, "message": "Knowledge Base has not been built for this source package yet."}
        latest = indexes[0]
        ready_index = _latest_ready_index(conn, indexes, verify_integrity=False)
        if ready_index:
            review = _latest_review(conn, ready_index["index_id"])
            message = "Knowledge Base is ready."
            if latest["index_id"] != ready_index["index_id"] and latest["status"] in {"building", "failed"}:
                message = f"Knowledge Base is ready from the latest successful index. Newest build is {latest['status']}."
            return {"package_id": package_id, "index_id": ready_index["index_id"], "status": "approved", "ready": True,
                "index_status": ready_index["status"], "review": _dict(review),
                "progress_percent": 100,
                "progress_stage": ready_index["progress_stage"] or "ready",
                "progress_message": ready_index["progress_message"] or "Knowledge Base is ready.",
                "message": message}
        review = _latest_review(conn, latest["index_id"])
        status = review["decision"] if latest["status"] == "ready" and review else latest["status"]
        return {"package_id": package_id, "index_id": latest["index_id"], "status": status, "ready": False,
            "index_status": latest["status"], "review": _dict(review),
            "progress_percent": int(latest["progress_percent"] or 0),
            "progress_stage": latest["progress_stage"] or latest["status"],
            "progress_message": latest["progress_message"],
            "message": f"Knowledge Base is {latest['status']}."}


@router.get("/api/v1/knowledge-indexes/{index_id}")
def get_knowledge_index(index_id: str):
    with connection() as conn:
        index = conn.execute("SELECT * FROM knowledge_index WHERE index_id=%s", (index_id,)).fetchone()
        if not index:
            raise HTTPException(404, "Knowledge Base index not found")
        if index["status"] == "ready":
            _ensure_system_approval(conn, index_id)
        result = _serialize_index(index)
        result["chunks"] = []
        for row in conn.execute("SELECT chunk_id,source_type,source_reference,heading_path,chunk_text,content_hash,metadata_json FROM knowledge_chunk WHERE index_id=%s ORDER BY source_reference,chunk_id", (index_id,)).fetchall():
            item = _dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            result["chunks"].append(item)
        result["nodes"] = []
        for row in conn.execute("SELECT node_id,canonical_id,label,entity_type,aliases_json,evidence_chunk_ids_json FROM knowledge_node WHERE index_id=%s ORDER BY entity_type,label", (index_id,)).fetchall():
            item = _dict(row)
            item["aliases"] = json.loads(item.pop("aliases_json") or "[]")
            item["evidence_chunk_ids"] = json.loads(item.pop("evidence_chunk_ids_json") or "[]")
            result["nodes"].append(item)
        result["edges"] = []
        for row in conn.execute("SELECT e.edge_id,e.relationship_type,e.evidence_chunk_ids_json,e.confidence,s.label AS source,t.label AS target FROM knowledge_edge e JOIN knowledge_node s ON s.node_id=e.source_node_id JOIN knowledge_node t ON t.node_id=e.target_node_id WHERE e.index_id=%s ORDER BY s.label,e.relationship_type,t.label", (index_id,)).fetchall():
            item = _dict(row)
            item["evidence_chunk_ids"] = json.loads(item.pop("evidence_chunk_ids_json") or "[]")
            result["edges"].append(item)
        result["reviews"] = [_dict(row) for row in conn.execute("SELECT reviewer,decision,comments,object_hash,reviewed_at FROM knowledge_index_review WHERE index_id=%s ORDER BY reviewed_at DESC", (index_id,)).fetchall()]
        return result


@router.post("/api/v1/knowledge-indexes/{index_id}/review", status_code=201)
def review_knowledge_index(index_id: str, request: IndexReviewRequest):
    with connection() as conn:
        index = conn.execute("SELECT * FROM knowledge_index WHERE index_id=%s", (index_id,)).fetchone()
        if not index:
            raise HTTPException(404, "Knowledge Base index not found")
        if index["status"] != "ready":
            raise HTTPException(409, "Only a successfully built Knowledge Base index can be reviewed")
        object_hash = _current_object_hash(conn, index_id)
        review_id, timestamp = _uid("krev"), _now()
        conn.execute("INSERT INTO knowledge_index_review (review_id,index_id,reviewer,decision,comments,object_hash,reviewed_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (review_id, index_id, request.reviewer.strip(), request.decision, request.comments, object_hash, timestamp))
        return {"review_id": review_id, "index_id": index_id, "reviewer": request.reviewer.strip(), "decision": request.decision,
            "comments": request.comments, "object_hash": object_hash, "reviewed_at": timestamp}


@router.post("/api/v1/knowledge-indexes/{index_id}/retrieve", status_code=201)
@usage_step("knowledge_base")
def retrieve_knowledge_endpoint(index_id: str, request: RetrievalRequest):
    return retrieve_knowledge(index_id, request)


@router.get("/api/v1/knowledge-indexes/{index_id}/retrieval-runs")
def list_retrieval_runs(index_id: str):
    with connection() as conn:
        if not conn.execute("SELECT 1 FROM knowledge_index WHERE index_id=%s", (index_id,)).fetchone():
            raise HTTPException(404, "Knowledge Base index not found")
        rows = conn.execute("SELECT retrieval_id,query_text,query_hash,requested_by,purpose,retrieval_config_json,result_json,context_hash,created_at FROM knowledge_retrieval_run WHERE index_id=%s ORDER BY created_at DESC", (index_id,)).fetchall()
        result = []
        for row in rows[:100]:
            item = _dict(row)
            item["retrieval_config"] = json.loads(item.pop("retrieval_config_json") or "{}")
            item["result"] = json.loads(item.pop("result_json") or "{}")
            result.append(item)
        return result


@router.post("/api/v1/knowledge-indexes/{index_id}/evaluate", status_code=201)
@usage_step("knowledge_base")
def evaluate_knowledge_index(index_id: str, request: EvaluationRequest):
    questions = request.questions or [EvaluationQuestion(**value) for value in DEFAULT_EVALUATION_QUESTIONS]
    client = _create_client()
    results = []
    for question in questions:
        retrieval = retrieve_knowledge(index_id, RetrievalRequest(query=question.question, top_k=6, graph_depth=1,
            max_context_characters=10_000, requested_by=request.requested_by, purpose="retrieval_evaluation",
            require_approved=True), client=client)
        lowered = retrieval["context"].lower()
        legacy_term_hits = {term: term.lower() in lowered for term in question.expected_terms}
        grading_question = question.model_copy(update={
            "expected_evidence": question.expected_evidence or question.expected_terms,
        })
        grounding = grade_retrieval(grading_question, retrieval)
        passed = grounding["passed"]
        results.append({"question": question.question, **grounding, "passed": passed, "retrieval_id": retrieval["retrieval_id"],
            "citation_count": len(retrieval["hits"]), "source_types": sorted({hit["source_type"] for hit in retrieval["hits"]}),
            "graph_path_count": len(retrieval["graph_paths"]),
            "top_retrieval_score": retrieval["hits"][0]["retrieval_score"] if retrieval["hits"] else None,
            "legacy_expected_term_hits": legacy_term_hits})
    with connection() as conn:
        index = conn.execute("SELECT package_id FROM knowledge_index WHERE index_id=%s", (index_id,)).fetchone()
        if not index:
            raise HTTPException(404, "Knowledge Base index not found")
        evaluation_id, timestamp = _uid("eval"), _now()
        passed = all(item["passed"] for item in results)
        conn.execute("INSERT INTO knowledge_evaluation_run (evaluation_id,index_id,package_id,requested_by,questions_json,result_json,passed,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (evaluation_id, index_id, index["package_id"], request.requested_by.strip(),
             json.dumps([question.model_dump() for question in questions]), json.dumps(results), int(passed), timestamp))
    return {"evaluation_id": evaluation_id, "index_id": index_id, "passed": passed,
        "score_semantics": "Evidence coverage and citation integrity; retrieval scores are ranking signals, not confidence probabilities.",
        "results": results, "created_at": timestamp}


@router.get("/api/v1/knowledge-indexes/{index_id}/evaluations")
def list_knowledge_evaluations(index_id: str):
    with connection() as conn:
        if not conn.execute("SELECT 1 FROM knowledge_index WHERE index_id=%s", (index_id,)).fetchone():
            raise HTTPException(404, "Knowledge Base index not found")
        rows = conn.execute("SELECT evaluation_id,requested_by,questions_json,result_json,passed,created_at FROM knowledge_evaluation_run WHERE index_id=%s ORDER BY created_at DESC", (index_id,)).fetchall()
        result = []
        for row in rows[:50]:
            item = _dict(row)
            item["questions"] = json.loads(item.pop("questions_json") or "[]")
            item["results"] = json.loads(item.pop("result_json") or "[]")
            item["passed"] = bool(item["passed"])
            result.append(item)
        return result
