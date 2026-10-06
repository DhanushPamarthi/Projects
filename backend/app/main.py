import copy, difflib, hashlib, json, logging, mimetypes, os, threading, time, uuid
import re
from datetime import datetime, timezone
from pathlib import Path
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from .config import BRONZE_SCHEMA, DATABASE_URL, EXTRACTION_CONFIG_VERSION, EXTRACTION_PROMPT_VERSION, LOCAL_EXTRACTOR_VERSION, MAX_UPLOAD_BYTES, OPENAI_MODEL, OPENAI_TIMEOUT_SECONDS, REQUIRED_BRD_SECTIONS, REQUIREMENTS_ANALYSIS_PROMPT_VERSION, SUPPORTED_EXTENSIONS, TEST_CASE_GENERATION_PROMPT_VERSION, UPLOAD_DIR
from .openai_requests import responses_create
from .db import close_connection_pool, connection, init_db, warm_connection_pool
from .extractors import _json_sections, extract_local, extract_openai_file, extraction_prompt_hash, missing_sections
from .validation import validate_file_content
from .knowledge_fabric import router as knowledge_fabric_router
from .knowledge_fabric import retrieve_package_context

app = FastAPI(title="TestIQ Source Package API", version="1.0.0")
app.include_router(knowledge_fabric_router)
SOURCE_TYPES = {"brd", "campaign_rules", "api_spec", "database_schema", "message_template", "ui_description"}
logger = logging.getLogger(__name__)

def now(): return datetime.now(timezone.utc).isoformat()
def uid(prefix): return f"{prefix}_{uuid.uuid4().hex}"
def row_dict(row): return dict(row) if row else None

_BUSINESS_RULE_TERMS = re.compile(r"\b(shall|must|should|required|eligible|ineligible|exclude|suppress|fallback|only when|must not|shall not)\b", re.I)

def _business_rule_impact(statement: str) -> str:
    lowered = statement.lower()
    if any(term in lowered for term in ("eligible", "exclude", "suppress")):
        return "Eligibility and negative test scenarios cannot be evaluated consistently without this rule."
    if any(term in lowered for term in ("sms", "phone", "fallback")):
        return "Communication sequencing, fallback, and observable outcome tests remain incomplete without this rule."
    if any(term in lowered for term in ("record", "status", "outcome")):
        return "QA cannot define an objective pass or fail result without this rule."
    return "The affected requirement and its acceptance criteria cannot be treated as fully testable without this rule."

def _package_rule_id(package_id: str, business_requirement_id: str) -> str:
    """Return a stable package-scoped ID for an LLM-extracted BRD rule."""
    digest = hashlib.sha256(f"{package_id}:{business_requirement_id}".encode("utf-8")).hexdigest()[:24]
    return f"rule_{digest}"

def _replace_package_rules_from_business_requirements(conn, package_id: str, business_items: list[dict]) -> None:
    """Make reviewed Business Use Cases the rule input for the Knowledge Base."""
    if conn.execute("SELECT 1 FROM knowledge_index WHERE package_id=%s LIMIT 1", (package_id,)).fetchone():
        raise HTTPException(409, "Business Use Cases are locked because this package already has a Knowledge Base version")
    timestamp = now()
    conn.execute("DELETE FROM package_business_rule WHERE package_id=%s", (package_id,))
    for ordinal, item in enumerate(business_items, 1):
        business_id = str(item.get("business_requirement_id") or f"BR-{ordinal:03d}")
        statement = " ".join(str(item.get("description") or item.get("text") or "").split())
        references = _merge_source_references(item.get("source_references"), item.get("supporting_source_references"))
        reference = references[0] if references else {}
        conn.execute(
            "INSERT INTO package_business_rule (rule_id,package_id,ordinal,statement,source_fragment_id,source_reference,impact_if_missing,status,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s)",
            (_package_rule_id(package_id, business_id), package_id, ordinal, statement,
             reference.get("fragment_id"), reference.get("source_reference") or "BRD source package",
             _business_rule_impact(statement), timestamp, timestamp),
        )
    _refresh_package_business_rule_summary(conn, package_id)

def _sync_business_requirement_rule_review(conn, package_id: str, contract: dict, reviewer: str, timestamp: str) -> dict | None:
    """Persist Business Use Case review decisions into the package Knowledge Base gate."""
    if not contract.get("business_requirements_generation"):
        return None
    existing = {row["rule_id"]: row_dict(row) for row in conn.execute(
        "SELECT * FROM package_business_rule WHERE package_id=%s", (package_id,)
    ).fetchall()}
    for requirement in contract.get("requirements", []):
        for rule in requirement.get("business_rules", []):
            if not isinstance(rule, dict):
                continue
            package_rule_id = rule.get("package_rule_id") or _package_rule_id(package_id, str(rule.get("rule_id") or ""))
            if package_rule_id not in existing:
                continue
            status = rule.get("review_status") or ("approved" if rule.get("approved") else "pending")
            conn.execute(
                "UPDATE package_business_rule SET statement=%s,status=%s,reviewer=%s,reviewed_at=%s,updated_at=%s WHERE rule_id=%s AND package_id=%s",
                (" ".join(str(rule.get("text") or existing[package_rule_id]["statement"]).split()), status,
                 reviewer, timestamp if status != "pending" else None, timestamp, package_rule_id, package_id),
            )
    _refresh_package_business_rule_summary(conn, package_id)
    summary = _package_business_rule_response(conn, package_id)
    return {"review_status": summary["review_status"], "counts": summary["counts"], "rules_reviewed_at": summary["rules_reviewed_at"]}

def _ensure_package_business_rules(conn, package_id: str) -> None:
    if conn.execute("SELECT 1 FROM package_business_rule WHERE package_id=%s LIMIT 1", (package_id,)).fetchone():
        return
    fragments = conn.execute(
        "SELECT f.fragment_id,f.source_reference,f.fragment_text,s.logical_name,r.revision "
        "FROM source_package_item pi JOIN source_document s ON s.source_id=pi.source_id "
        "JOIN source_revision r ON r.revision_id=pi.revision_id "
        "JOIN source_fragment f ON f.revision_id=pi.revision_id AND f.source_id=pi.source_id "
        "WHERE pi.package_id=%s AND s.source_type='brd' ORDER BY f.fragment_index", (package_id,)
    ).fetchall()
    seen, ordinal, timestamp = set(), 0, now()
    for fragment in fragments:
        text = " ".join(str(fragment["fragment_text"] or "").split())
        for candidate in re.split(r"(?<=[.!;])\s+", text):
            statement = candidate.strip(" -•\t")
            if not 15 <= len(statement) <= 1200 or not _BUSINESS_RULE_TERMS.search(statement) or statement.endswith("?"):
                continue
            if statement.casefold() in seen:
                continue
            seen.add(statement.casefold())
            ordinal += 1
            rule_id = "rule_" + hashlib.sha256(f"{package_id}:{fragment['fragment_id']}:{statement}".encode()).hexdigest()[:24]
            source_reference = f"{fragment['logical_name']}@v{fragment['revision']} | {fragment['source_reference']}"
            conn.execute(
                "INSERT INTO package_business_rule (rule_id,package_id,ordinal,statement,source_fragment_id,source_reference,impact_if_missing,status,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s)",
                (rule_id, package_id, ordinal, statement, fragment["fragment_id"], source_reference, _business_rule_impact(statement), timestamp, timestamp),
            )

def _refresh_package_business_rule_summary(conn, package_id: str) -> None:
    rules = [row_dict(row) for row in conn.execute("SELECT rule_id,statement,status,source_reference FROM package_business_rule WHERE package_id=%s ORDER BY ordinal", (package_id,)).fetchall()]
    current = conn.execute("SELECT rule_review_status,rule_set_hash,rules_reviewed_at FROM source_package_manifest WHERE package_id=%s", (package_id,)).fetchone()
    timestamp = now()
    if not rules:
        conn.execute("UPDATE source_package_manifest SET rule_review_status=%s,rule_set_hash=NULL,rules_reviewed_at=NULL WHERE package_id=%s",
            ("pending", package_id))
        return
    approved = [rule for rule in rules if rule["status"] == "approved"]
    review_complete = bool(approved) and not any(rule["status"] == "pending" for rule in rules)
    rule_set_hash = hashlib.sha256(json.dumps([{"rule_id": rule["rule_id"], "statement": rule["statement"], "status": rule["status"], "source_reference": rule["source_reference"]} for rule in rules], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    reviewed_at = (current["rules_reviewed_at"] if review_complete and current and current["rule_review_status"] == "approved"
        and current["rule_set_hash"] == rule_set_hash else timestamp if review_complete else None)
    conn.execute("UPDATE source_package_manifest SET rule_review_status=%s,rule_set_hash=%s,rules_reviewed_at=%s WHERE package_id=%s",
        ("approved" if review_complete else "pending", rule_set_hash if review_complete else None, reviewed_at, package_id))

def _package_business_rule_response(conn, package_id: str) -> dict:
    package = conn.execute("SELECT * FROM source_package_manifest WHERE package_id=%s", (package_id,)).fetchone()
    if not package:
        raise HTTPException(404, "Source package not found")
    _ensure_package_business_rules(conn, package_id)
    _refresh_package_business_rule_summary(conn, package_id)
    package = conn.execute("SELECT * FROM source_package_manifest WHERE package_id=%s", (package_id,)).fetchone()
    rules = [row_dict(row) for row in conn.execute("SELECT * FROM package_business_rule WHERE package_id=%s ORDER BY ordinal", (package_id,)).fetchall()]
    return {"package_id": package_id, "review_status": package["rule_review_status"], "rule_set_hash": package["rule_set_hash"],
            "rules_reviewed_at": package["rules_reviewed_at"], "rules": rules,
            "counts": {status: sum(rule["status"] == status for rule in rules) for status in ("pending", "approved", "rejected")}}

SKILL_PROMPTS_DIR = Path(__file__).resolve().parent / "skills"

def read_skill_instructions(skill_name: str) -> str:
    skill_path = SKILL_PROMPTS_DIR / skill_name / "SKILL.md"
    try:
        content = skill_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise RuntimeError(f"Prompt skill file is missing: {skill_path}") from exc
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) == 3:
            content = parts[2].strip()
    if not content:
        raise RuntimeError(f"Prompt skill file is empty: {skill_path}")
    return content

def extraction_config_hash() -> str:
    payload = {"model": OPENAI_MODEL, "timeout": OPENAI_TIMEOUT_SECONDS, "extractor": LOCAL_EXTRACTOR_VERSION, "config_version": EXTRACTION_CONFIG_VERSION}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

REQUIREMENTS_ANALYSIS_PROMPT = f"{read_skill_instructions('requirements-analysis')}\n\nSOURCE FRAGMENTS:\n"
TEST_CASE_GENERATION_PROMPT = f"{read_skill_instructions('test-case-generation')}\n\nUse the package-scoped retrieved_knowledge_evidence in the input to ground database setup, API behavior, and test steps. Treat approved requirements as the behavior scope and cite only approved requirement/source references. For every acceptance_criteria_id and business_rule_id listed in coverage_plan, include that exact ID in the matching requirement's test case traceability fields (acceptance_criteria_ids or business_rule_ids respectively). For every evidence.source_references entry, copy an exact fragment_id and source_reference pair from the supplied approved requirements or retrieved Knowledge Base evidence; never invent or alter either value. Every test case must include at least one test_data_setup item with created_during_execution set to true; name a supported record key, explain how execution creates that record, and cite its supporting requirement or acceptance-criterion ID in source. Never supply an actual production identifier. Keep each generated case concise: short title, one-sentence description, three to five steps, one or two expected results, and focused setup data only.\n\nAPPROVED REQUIREMENTS:\n"
TEST_CASE_GENERATION_MAX_ATTEMPTS = 2

def test_case_generation_prompt_hash() -> str:
    return hashlib.sha256(TEST_CASE_GENERATION_PROMPT.encode("utf-8")).hexdigest()

def requirements_analysis_prompt_hash() -> str:
    return hashlib.sha256(REQUIREMENTS_ANALYSIS_PROMPT.encode("utf-8")).hexdigest()

def safe_llm_error_message(exc: Exception) -> str:
    message = str(exc)
    if "invalid_api_key" in message or "Incorrect API key" in message:
        return "OpenAI rejected the configured API key."
    return message

def _is_context_length_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "context_length_exceeded" in message or "exceeds the context window" in message or "context window" in message

def _try_retrieve_package_context(package_id: str, requested_by: str):
    timeout_seconds = float(os.getenv("RAG_RETRIEVAL_TIMEOUT_SECONDS", "30"))
    if timeout_seconds <= 0:
        return None
    result, failure = [], []

    def run():
        try:
            result.append(retrieve_package_context(package_id, requested_by=requested_by))
        except Exception as exc:
            failure.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout_seconds)
    if result:
        return result[0]
    if worker.is_alive():
        print(f"Knowledge Base retrieval skipped for {package_id}: timed out after {timeout_seconds:g}s")
        return None
    if failure:
        exc = failure[0]
        if isinstance(exc, HTTPException) and exc.status_code in {404, 409, 422, 502, 503}:
            return None
        raise exc
    return None

def _rag_metadata_or_none(rag):
    return rag.get("rag_metadata") if rag else None

def _rag_status(rag, *, required: bool = False):
    metadata = _rag_metadata_or_none(rag)
    if metadata:
        return {
            "used": True,
            "required": required,
            "index_id": metadata.get("index_id"),
            "context_hash": metadata.get("context_hash"),
            "retrieval_ids": metadata.get("retrieval_ids", []),
            "message": "Knowledge Base used for this run.",
        }
    return {
        "used": False,
        "required": required,
        "index_id": None,
        "context_hash": None,
        "retrieval_ids": [],
        "message": "Knowledge Base was not ready or could not be retrieved for this run.",
    }

def _require_package_context(package_id: str, requested_by: str):
    try:
        return retrieve_package_context(package_id, requested_by=requested_by)
    except HTTPException as exc:
        raise exc
    except Exception as exc:
        raise HTTPException(502, f"Knowledge Base retrieval failed for {requested_by}: {safe_llm_error_message(exc)}") from exc

def _require_test_case_knowledge(package_id: str, requested_by: str):
    try:
        rag = _require_package_context(package_id, requested_by=requested_by)
    except HTTPException as exc:
        if exc.status_code in {409, 422}:
            raise HTTPException(409, f"Test Case generation requires a ready, approved Knowledge Base for this package. Build it or wait for its current build to finish, then retry.") from exc
        if exc.status_code in {502, 503}:
            raise HTTPException(503, f"Knowledge Base retrieval failed for {requested_by}. Check the Knowledge Base provider and package index, then retry. No test cases were generated.") from exc
        raise
    if not (rag.get("brd_fragments") or rag.get("technical_fragments")):
        raise HTTPException(409, "The Knowledge Base is ready, but returned no usable evidence for Test Case generation. Check the package sources or rebuild the Knowledge Base, then retry.")
    return rag

def _require_test_data_knowledge(package_id: str):
    try:
        rag = _require_package_context(package_id, requested_by="Test Data Generation")
    except HTTPException as exc:
        if exc.status_code in {409, 422}:
            raise HTTPException(409, "Test data generation requires a ready Knowledge Base index for this package. Build the index or wait for its current build to finish, then retry.") from exc
        if exc.status_code in {502, 503}:
            raise HTTPException(503, "Knowledge Base retrieval failed for test data generation. Check the provider and package index, then retry.") from exc
        raise
    if not (rag.get("brd_fragments") or rag.get("technical_fragments")):
        raise HTTPException(409, "The Knowledge Base is ready but returned no usable evidence for test data generation. Check the package sources or rebuild the index, then retry.")
    return rag

def _raise_test_case_generation_error(exc: Exception, operation: str):
    detail = safe_llm_error_message(exc)
    lowered = detail.lower()
    if isinstance(exc, TimeoutError) or "timed out" in lowered or "timeout" in lowered:
        message = f"OpenAI timed out during {operation}. Check service availability and retry."
    elif "api key" in lowered or "invalid_api_key" in lowered:
        message = "OpenAI rejected the configured API key. Check OPENAI_API_KEY and retry."
    elif isinstance(exc, ValueError):
        message = f"OpenAI returned test cases that failed schema, evidence, or coverage validation during {operation}: {detail}. No local fallback was used; review the response requirements and retry."
    else:
        message = f"OpenAI failed during {operation}: {detail}. No local fallback was used; review the OpenAI configuration and retry."
    raise HTTPException(502, message) from exc

def _rag_context_for_prompt(rag, max_evidence: int = 24, max_fragment_characters: int = 1200, max_rules: int = 50, max_rule_characters: int = 500):
    if not rag:
        return {}
    fragments = rag.get("brd_fragments", []) + rag.get("technical_fragments", [])
    approved_rules = []
    for rule in (rag.get("approved_rules") or [])[:max_rules]:
        approved_rules.append({
            "rule_id": rule.get("rule_id"),
            "statement": str(rule.get("statement") or "")[:max_rule_characters],
            "source_reference": rule.get("source_reference"),
        })
    return {"index_id": rag.get("index_id"), "context_hash": rag.get("context_hash"), "approved_business_rules": approved_rules, "evidence": [
        {"source_type": item.get("source_type"), "source_reference": item.get("source_reference"),
         "fragment_id": item.get("original_fragment_id") or item.get("fragment_id"),
         "text": str(item.get("fragment_text") or "")[:max_fragment_characters]} for item in fragments[:max_evidence]
    ]}

def requirements_analysis_config_hash() -> str:
    payload = {"model": OPENAI_MODEL, "timeout": OPENAI_TIMEOUT_SECONDS, "prompt_version": REQUIREMENTS_ANALYSIS_PROMPT_VERSION}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

async def read_upload_with_limit(file: UploadFile) -> bytes:
    chunks, total = [], 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk: break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES: raise HTTPException(413, "Uploaded file exceeds the maximum size")
        chunks.append(chunk)
    return b"".join(chunks)

def process_extraction(revision_id, source_id, extraction_id, storage_path, filename, required_sections):
    # Parse machine-readable documents locally first. OpenAI is reserved for
    # scanned/image-only PDFs or files the deterministic parser cannot read.
    started_at = time.monotonic()
    with connection() as conn:
        source_row = conn.execute("SELECT source_type FROM source_document WHERE source_id=%s", (source_id,)).fetchone()
        source_type = source_row["source_type"] if source_row else None
        conn.execute("UPDATE source_extraction SET mode=%s,status=%s WHERE extraction_id=%s", ("local", "processing", extraction_id))
        conn.execute("UPDATE source_revision SET status=%s WHERE revision_id=%s", ("processing", revision_id))
    mode = "local"
    try:
        content = Path(storage_path).read_bytes()
        fallback_message = None
        local_started_at = time.monotonic()
        try:
            text, sections = extract_local(filename, content)
            logger.info(
                "Source extraction local parser completed revision=%s source_type=%s filename=%s size_bytes=%d fragments=%d duration_seconds=%.2f",
                revision_id, source_type, filename, len(content), len(sections), time.monotonic() - local_started_at,
            )
        except Exception as local_error:
            logger.info(
                "Source extraction local parser requires OpenAI revision=%s filename=%s duration_seconds=%.2f reason=%s",
                revision_id, filename, time.monotonic() - local_started_at, local_error,
            )
            openai_started_at = time.monotonic()
            try:
                text, sections = extract_openai_file(content, filename)
                mode = "openai"
                fallback_message = f"Local extraction was unavailable; OpenAI extraction was used: {local_error}"
                logger.info(
                    "Source extraction OpenAI completed revision=%s filename=%s size_bytes=%d duration_seconds=%.2f",
                    revision_id, filename, len(content), time.monotonic() - openai_started_at,
                )
            except Exception as openai_error:
                logger.exception(
                    "Source extraction OpenAI fallback failed revision=%s filename=%s size_bytes=%d duration_seconds=%.2f error=%s",
                    revision_id, filename, len(content), time.monotonic() - openai_started_at, openai_error,
                )
                raise RuntimeError(
                    f"Local extraction failed ({local_error}); OpenAI extraction failed ({openai_error})"
                ) from openai_error
        with connection() as conn:
            source = conn.execute("SELECT validation_messages FROM source_revision WHERE revision_id=%s", (revision_id,)).fetchone()
            messages = json.loads(source["validation_messages"] or "[]") if source else []
            messages.extend([f"Missing expected section: {item}" for item in missing_sections(sections, required_sections)])
            if fallback_message:
                messages.append(fallback_message)
            model = OPENAI_MODEL if mode == "openai" else LOCAL_EXTRACTOR_VERSION
            conn.execute("UPDATE source_extraction SET mode=%s,status=%s,extracted_text=%s,error=%s,model_name=%s,prompt_version=%s,config_version=%s,prompt_hash=%s,config_hash=%s WHERE extraction_id=%s", (mode, "succeeded", text, fallback_message, model, EXTRACTION_PROMPT_VERSION, EXTRACTION_CONFIG_VERSION, extraction_prompt_hash(), extraction_config_hash(), extraction_id))
            fragment_rows = []
            for index, section in enumerate(sections):
                fragment_text = section.get("text", "")
                paragraph_index = section.get("paragraph_index")
                fragment_rows.append((uid("frag"), revision_id, source_id, index, section.get("heading"), section.get("reference", "unknown"), section.get("page_number"), section.get("fragment_type", "paragraph"), paragraph_index, section.get("table_index"), section.get("row_index"), section.get("cell_index"), section.get("heading"), paragraph_index, paragraph_index, None, None, hashlib.sha256(fragment_text.encode("utf-8")).hexdigest(), f"openai:{model}" if mode == "openai" else LOCAL_EXTRACTOR_VERSION, mode, fragment_text))
            if fragment_rows:
                statement = "INSERT INTO source_fragment (fragment_id,revision_id,source_id,fragment_index,heading,source_reference,page_number,fragment_type,paragraph_index,table_index,row_index,cell_index,heading_path,paragraph_start,paragraph_end,character_start,character_end,content_hash,extractor_version,provenance_mode,fragment_text) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                if DATABASE_URL:
                    with conn.cursor() as cursor:
                        cursor.executemany(statement, fragment_rows)
                else:
                    conn.executemany(statement, fragment_rows)
            conn.execute("UPDATE source_revision SET status=%s,validation_messages=%s WHERE revision_id=%s", ("uploaded", json.dumps(messages), revision_id))
        logger.info(
            "Source extraction completed revision=%s filename=%s mode=%s fragments=%d total_duration_seconds=%.2f",
            revision_id, filename, mode, len(fragment_rows), time.monotonic() - started_at,
        )
    except Exception as exc:
        logger.exception(
            "Source extraction failed revision=%s filename=%s total_duration_seconds=%.2f",
            revision_id, filename, time.monotonic() - started_at,
        )
        with connection() as conn:
            model = OPENAI_MODEL if mode == "openai" else LOCAL_EXTRACTOR_VERSION
            conn.execute("UPDATE source_extraction SET mode=%s,status=%s,error=%s,model_name=%s,prompt_version=%s,config_version=%s,prompt_hash=%s,config_hash=%s WHERE extraction_id=%s", (mode, "failed", str(exc), model, EXTRACTION_PROMPT_VERSION, EXTRACTION_CONFIG_VERSION, extraction_prompt_hash(), extraction_config_hash(), extraction_id))
            row = conn.execute("SELECT validation_messages FROM source_revision WHERE revision_id=%s", (revision_id,)).fetchone()
            messages = json.loads(row["validation_messages"] or "[]") if row else []
            messages.append(f"Extraction failed: {exc}")
            conn.execute("UPDATE source_revision SET status=%s,validation_messages=%s WHERE revision_id=%s", ("extraction_failed", json.dumps(messages), revision_id))

class ManifestRequest(BaseModel):
    source_ids: list[str] = Field(min_length=1)
    # Kept optional for API compatibility with older clients; the UI always sends it.
    package_name: str | None = Field(default=None, max_length=200)
    scope: str | None = None

class BusinessRuleReviewItem(BaseModel):
    rule_id: str = Field(min_length=1, max_length=100)
    statement: str = Field(min_length=1, max_length=4000)
    decision: str = Field(pattern="^(pending|approved|rejected)$")
    impact_if_missing: str = Field(min_length=1, max_length=2000)
    review_comment: str | None = Field(default=None, max_length=2000)
    source_reference: str | None = Field(default=None, max_length=2000)

class BusinessRuleReviewRequest(BaseModel):
    reviewer: str = Field(min_length=1, max_length=200)
    rules: list[BusinessRuleReviewItem] = Field(min_length=1)

class ProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)

class RequirementReviewItem(BaseModel):
    item_id: str = Field(min_length=1, max_length=200)
    approved: bool
    review_status: str | None = Field(default=None, pattern="^(pending|approved|rejected)$")
    title: str | None = Field(default=None, max_length=400)
    statement: str | None = Field(default=None, max_length=10000)
    text: str | None = Field(default=None, max_length=10000)
    scenario: str | None = Field(default=None, max_length=400)
    given: str | list[str] | None = None
    when: str | None = Field(default=None, max_length=4000)
    then: str | None = Field(default=None, max_length=4000)

class RequirementReviewRequest(BaseModel):
    reviewer: str = Field(min_length=1, max_length=200)
    decision: str = Field(pattern="^(approved|needs_changes|rejected)$")
    comments: str | None = Field(default=None, max_length=4000)
    accepted_assumptions: list[str] = Field(default_factory=list)
    resolved_gaps: list[str] = Field(default_factory=list)
    approved_items: list[str] = Field(default_factory=list)
    items: list[RequirementReviewItem] = Field(default_factory=list)

class TestCaseGenerationRequest(BaseModel):
    count: int | None = Field(default=None, ge=1, le=100)

class TestCaseReviewItem(BaseModel):
    test_case_id: str = Field(min_length=1, max_length=100)
    review_status: str = Field(pattern="^(pending|approved|rejected)$")
    title: str | None = Field(default=None, max_length=400)
    steps: list[str] | None = None
    expected_results: list[str] | None = None

class TestCaseReviewRequest(BaseModel):
    reviewer: str = Field(min_length=1, max_length=200)
    comments: str | None = Field(default=None, max_length=4000)
    items: list[TestCaseReviewItem] = Field(min_length=1)

class TestCaseRegenerationRequest(BaseModel):
    reviewer: str | None = Field(default=None, max_length=200)
    reason: str | None = Field(default=None, max_length=4000)
    test_case_ids: list[str] = Field(default_factory=list)

class ExecutionRunRequest(BaseModel):
    reviewer: str = Field(min_length=1, max_length=200)
    comments: str | None = Field(default=None, max_length=4000)
    selected_test_case_ids: list[str] = Field(min_length=1)

class ExecutionRunLifecycleRequest(BaseModel):
    lifecycle_state: str = Field(pattern="^(cancelled|failed)$")
    reviewer: str | None = Field(default=None, max_length=200)
    comments: str | None = Field(default=None, max_length=4000)
    error: str | None = Field(default=None, max_length=4000)

class ExecutionRunExecuteRequest(BaseModel):
    reviewer: str | None = Field(default=None, max_length=200)
    comments: str | None = Field(default=None, max_length=4000)

class TestDataTemplateDefinition(BaseModel):
    template_id: str = Field(min_length=1, max_length=100)
    template_name: str = Field(min_length=1, max_length=200)
    entity_name: str = Field(min_length=1, max_length=100)
    payload: dict

class TestDataCaseAssignment(BaseModel):
    test_case_id: str = Field(min_length=1, max_length=100)
    template_ids: list[str] = Field(default_factory=list)

class TestDataGenerationRequest(BaseModel):
    reviewer: str | None = Field(default=None, max_length=200)
    assignments: list[TestDataCaseAssignment] = Field(default_factory=list)
    template_definitions: list[TestDataTemplateDefinition] = Field(default_factory=list)
    replace_existing_drafts: bool = False
    # Legacy fields remain supported for existing clients; they apply all
    # listed templates to each listed case.
    test_case_ids: list[str] = Field(default_factory=list)
    template_ids: list[str] = Field(default_factory=list)

class TestDataReviewItem(BaseModel):
    record_id: str = Field(min_length=1, max_length=100)
    payload: dict

class TestDataReviewRequest(BaseModel):
    items: list[TestDataReviewItem] = Field(min_length=1)

class DiscoveryContextRequest(BaseModel):
    package_id: str | None = None
    context_type: str = Field(pattern="^(web_ui|api|schema)$")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    details: dict = Field(default_factory=dict)
    created_by: str | None = Field(default=None, max_length=200)

def _text_lines(fragments):
    return [{"text": " ".join(str(f.get("fragment_text") or "").split()), "fragment_id": f.get("fragment_id"), "heading": f.get("heading"), "source_id": f.get("source_id"), "revision_id": f.get("revision_id"), "source_reference": f.get("source_reference")} for f in fragments if str(f.get("fragment_text") or "").strip()]

def _analysis_source_references(fragments):
    return [
        {
            "fragment_id": f.get("fragment_id"),
            "source_reference": f.get("source_reference"),
        }
        for f in fragments
        if str(f.get("fragment_text") or "").strip()
    ]

def _requirements_analysis_schema():
    source_reference = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "fragment_id": {"type": "string"},
            "source_reference": {"type": "string"},
        },
        "required": ["fragment_id", "source_reference"],
    }
    references = {"type": "array", "items": source_reference, "minItems": 1}
    readiness_check = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "check_id": {"type": "string", "pattern": "^RD-[0-9]{3}$"},
            "label": {"type": "string"},
            "status": {"type": "string"},
            "details": {"type": "string"},
        },
        "required": ["check_id", "label", "status", "details"],
    }
    conflict = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "conflict_id": {"type": "string", "pattern": "^CF-[0-9]{3}$"},
            "requirement_ids": {"type": "array", "items": {"type": "string", "pattern": "^FR-[0-9]{3}$"}},
            "description": {"type": "string"},
            "severity": {"type": "string"},
            "source_references": references,
        },
        "required": ["conflict_id", "requirement_ids", "description", "severity", "source_references"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "requirements": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "requirement_id": {"type": "string", "pattern": "^FR-[0-9]{3}$"},
                        "title": {"type": "string"},
                        "type": {"type": "string"},
                        "statement": {"type": "string"},
                        "priority": {"type": "string"},
                        "actors": {"type": "array", "items": {"type": "string"}},
                        "entities": {"type": "array", "items": {"type": "string"}},
                        "business_rules": {"type": "array", "minItems": 1, "items": {"type": "object", "additionalProperties": False, "properties": {"rule_id": {"type": "string", "pattern": "^BR-[0-9]{3}$"}, "text": {"type": "string"}, "source_references": references}, "required": ["rule_id", "text", "source_references"]}},
                        "acceptance_criteria": {"type": "array", "minItems": 1, "items": {"type": "object", "additionalProperties": False, "properties": {"acceptance_id": {"type": "string", "pattern": "^AC-[0-9]{3}$"}, "scenario": {"type": "string"}, "given": {"type": "array", "minItems": 1, "items": {"type": "string"}}, "when": {"type": "string"}, "then": {"type": "string"}, "source_references": references}, "required": ["acceptance_id", "scenario", "given", "when", "then", "source_references"]}},
                        "expected_outcomes": {"type": "array", "minItems": 1, "items": {"type": "object", "additionalProperties": False, "properties": {"outcome_id": {"type": "string", "pattern": "^EO-[0-9]{3}$"}, "text": {"type": "string"}, "source_references": references}, "required": ["outcome_id", "text", "source_references"]}},
                        "flow_steps": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                        "ambiguities": {"type": "array", "items": {"type": "string"}},
                        "assumptions": {"type": "array", "items": {"type": "string"}},
                        "source_references": references,
                    },
                    "required": ["requirement_id", "title", "type", "statement", "priority", "actors", "entities", "business_rules", "acceptance_criteria", "expected_outcomes", "flow_steps", "ambiguities", "assumptions", "source_references"],
                },
            },
            "global_gaps": {"type": "array", "items": {"type": "string"}},
            "global_assumptions": {"type": "array", "items": {"type": "string"}},
            "technical_gaps": {"type": "array", "items": {"type": "object", "additionalProperties": False, "properties": {"gap_id": {"type": "string", "pattern": "^TG-[0-9]{3}$"}, "text": {"type": "string"}, "technical_reference": {"type": "string"}, "source_references": references}, "required": ["gap_id", "text", "technical_reference", "source_references"]}},
            "conflicting_requirements": {"type": "array", "items": conflict},
            "readiness": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "status": {"type": "string"},
                    "journey": {"type": "string"},
                    "checks": {"type": "array", "items": readiness_check},
                    "gaps": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["status", "journey", "checks", "gaps"],
            },
        },
        "required": ["requirements", "global_gaps", "global_assumptions", "technical_gaps", "conflicting_requirements", "readiness"],
    }

def _contract_readiness(contract):
    requirements = contract.get("requirements", [])
    source_refs = [ref for item in requirements for ref in item.get("source_references", [])]
    checks = [
        {"check_id": "RD-001", "label": "Requirements", "status": "passed" if requirements else "gap", "details": f"{len(requirements)} requirement(s) extracted."},
        {"check_id": "RD-002", "label": "Acceptance criteria", "status": "passed" if requirements and all(item.get("acceptance_criteria") for item in requirements) else "gap", "details": "Every requirement should include acceptance criteria."},
        {"check_id": "RD-003", "label": "Business rules", "status": "passed" if requirements and all(item.get("business_rules") for item in requirements) else "gap", "details": "Every requirement should include business rules."},
        {"check_id": "RD-004", "label": "Source traceability", "status": "passed" if source_refs else "gap", "details": "Requirements should retain source references back to package fragments."},
        {"check_id": "RD-005", "label": "Expected outcomes", "status": "passed" if requirements and all(item.get("expected_outcomes") for item in requirements) else "gap", "details": "Every requirement should include at least one observable expected outcome."},
    ]
    gaps = [check["details"] for check in checks if check["status"] == "gap"]
    if any(check["status"] == "gap" for check in checks):
        status = "needs_review"
    elif any(check["status"] == "needs_review" for check in checks):
        status = "provisional"
    else:
        status = "ready"
    return {"status": status, "journey": contract.get("readiness", {}).get("journey", "selected journey"), "checks": checks, "gaps": list(dict.fromkeys(gaps))}

def _apply_discovery_readiness(contract, discovery_context_count=0):
    readiness = contract.get("readiness") or _contract_readiness(contract)
    checks = readiness.get("checks") or []
    updated_checks = []
    for check in checks:
        if check.get("check_id") == "RD-006":
            continue
        updated_checks.append(check)
    analysis_notice_texts = {
        "The source contains unresolved ambiguities.",
        "Requirements analysis detected source statements that need reviewer clarification.",
        "LLM Requirements Analysis was unavailable, so this contract was generated with the local fallback.",
    }
    gaps = [
        gap for gap in readiness.get("gaps", [])
        if not str(gap).startswith("UI, API, and schema surfaces") and str(gap) not in analysis_notice_texts
    ]
    if any(check.get("status") in {"gap", "blocked"} for check in updated_checks):
        status = "needs_review"
    elif any(check.get("status") in {"needs_review", "provisional"} for check in updated_checks):
        status = "provisional"
    else:
        status = "ready"
    cleaned = {key: value for key, value in readiness.items() if key != "discovery_context_count"}
    contract["readiness"] = {**cleaned, "status": status, "checks": updated_checks, "gaps": gaps}
    return contract

def _discovery_context_response(row):
    result = row_dict(row)
    if not result:
        return None
    try:
        result["details"] = json.loads(result.pop("details_json") or "{}")
    except json.JSONDecodeError:
        result["details"] = {}
    return result

def _discovery_contexts_for_project(conn, project_id, package_id=None):
    if package_id:
        rows = conn.execute("SELECT * FROM discovery_context WHERE project_id=%s AND (package_id IS NULL OR package_id=%s) ORDER BY context_type,name,created_at", (project_id, package_id)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM discovery_context WHERE project_id=%s ORDER BY context_type,name,created_at", (project_id,)).fetchall()
    return [_discovery_context_response(row) for row in rows]

def _discovery_contexts_for_package(conn, package):
    return _discovery_contexts_for_project(conn, package["project_id"], package["package_id"])

def _discovery_context_count(conn, project_id, package_id=None):
    if package_id:
        return conn.execute("SELECT COUNT(*) AS count FROM discovery_context WHERE project_id=%s AND (package_id IS NULL OR package_id=%s)", (project_id, package_id)).fetchone()["count"]
    return conn.execute("SELECT COUNT(*) AS count FROM discovery_context WHERE project_id=%s", (project_id,)).fetchone()["count"]

def _validate_requirements_contract(contract, fragments):
    if not isinstance(contract, dict) or not contract.get("requirements"):
        raise ValueError("The AI returned no requirements")
    fragments_by_id = {f.get("fragment_id"): f for f in fragments if f.get("fragment_id")}
    fragments_by_reference = {}
    for fragment in fragments:
        fragments_by_reference.setdefault(fragment.get("source_reference"), []).append(fragment)
    seen_ids = set()

    def validate_refs(refs, label):
        if not refs:
            raise ValueError(f"AI returned no source references for {label}")
        for ref in refs:
            fragment = fragments_by_id.get(ref.get("fragment_id"))
            if fragment is None:
                matches = fragments_by_reference.get(ref.get("source_reference"), [])
                fragment = matches[0] if len(matches) == 1 else None
            if fragment is None:
                raise ValueError(f"AI returned an unknown source reference for {label}")
            # Store the exact database values even if the model changed the
            # display formatting of the source reference.
            ref.update({field: fragment.get(field) for field in (
                "fragment_id", "source_id", "revision_id", "source_type", "source_reference",
                "heading", "content_hash",
            ) if fragment.get(field) is not None})

    def validate_supported_numbers(value, refs, label):
        evidence_text = " ".join(
            str(fragments_by_id.get(ref.get("fragment_id"), {}).get("fragment_text") or "")
            for ref in refs or []
        )
        for number in re.findall(r"\b\d+(?:\.\d+)?\b", str(value or "")):
            if not re.search(rf"\b{re.escape(number)}\b", evidence_text):
                raise ValueError(f"AI introduced an unsupported numeric value in {label}: {number}")

    for requirement in contract["requirements"]:
        requirement_id = requirement.get("requirement_id")
        if not requirement_id or requirement_id in seen_ids or not requirement.get("title") or not requirement.get("statement"):
            raise ValueError("AI returned an invalid or duplicate requirement")
        seen_ids.add(requirement_id)
        validate_refs(requirement.get("source_references"), requirement_id)
        validate_supported_numbers(requirement.get("statement"), requirement.get("source_references"), requirement_id)
        if not requirement.get("business_rules"):
            raise ValueError(f"AI returned no business rules for {requirement_id}")
        if not requirement.get("acceptance_criteria"):
            raise ValueError(f"AI returned no acceptance criteria for {requirement_id}")
        if not requirement.get("flow_steps"):
            raise ValueError(f"AI returned no flow steps for {requirement_id}")
        for rule in requirement.get("business_rules", []):
            if not rule.get("text"): raise ValueError(f"AI returned an empty business rule for {requirement_id}")
            validate_refs(rule.get("source_references"), rule.get("rule_id", requirement_id))
        for criterion in requirement.get("acceptance_criteria", []):
            if not criterion.get("given") or not criterion.get("when") or not criterion.get("then"):
                raise ValueError(f"AI returned an invalid acceptance criterion for {requirement_id}")
            validate_refs(criterion.get("source_references"), criterion.get("acceptance_id", requirement_id))
            given_text = "; ".join(criterion.get("given", [])) if isinstance(criterion.get("given"), list) else str(criterion.get("given") or "")
            validate_supported_numbers(" ".join([given_text, criterion.get("when", ""), criterion.get("then", "")]), criterion.get("source_references"), criterion.get("acceptance_id", requirement_id))
        if not requirement.get("expected_outcomes"):
            raise ValueError(f"AI returned no expected outcomes for {requirement_id}")
        for outcome in requirement.get("expected_outcomes", []):
            if not outcome.get("text"):
                raise ValueError(f"AI returned an empty expected outcome for {requirement_id}")
            validate_refs(outcome.get("source_references"), outcome.get("outcome_id", requirement_id))
            validate_supported_numbers(outcome.get("text"), outcome.get("source_references"), outcome.get("outcome_id", requirement_id))
    for conflict in contract.get("conflicting_requirements", []):
        if not conflict.get("description"):
            raise ValueError("AI returned an invalid conflict")
        validate_refs(conflict.get("source_references"), conflict.get("conflict_id", "conflict"))
    for gap in contract.get("technical_gaps", []):
        if not gap.get("text"):
            raise ValueError("AI returned an empty technical gap")
        validate_refs(gap.get("source_references"), gap.get("gap_id", "technical gap"))
        exact_references = {ref.get("source_reference") for ref in gap.get("source_references", [])}
        if gap.get("technical_reference") not in exact_references:
            raise ValueError(f"AI returned an unsupported technical reference for {gap.get('gap_id', 'technical gap')}")

def _requirements_contract_with_openai(package, items, fragments, rag_context=None):
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not configured for Requirements Analysis")
    from openai import OpenAI
    retrieved_evidence = (rag_context or {}).get("evidence") or []
    if retrieved_evidence:
        # Requirement generation is RAG-grounded: send and validate only the
        # evidence selected by retrieval, rather than duplicating every source
        # fragment and overflowing the model context window.
        usable_fragments = [
            {
                "fragment_id": evidence.get("fragment_id"),
                "source_reference": evidence.get("source_reference"),
                "fragment_text": evidence.get("text"),
            }
            for evidence in retrieved_evidence
            if evidence.get("fragment_id") and str(evidence.get("text") or "").strip()
        ]
    else:
        usable_fragments = [fragment for fragment in fragments if str(fragment.get("fragment_text") or "").strip()]
    source_fragments = _analysis_source_references(usable_fragments)
    fragment_text = "\n".join(json.dumps({**ref, "text": str(fragment.get("fragment_text") or "")}, ensure_ascii=False) for ref, fragment in zip(source_fragments, usable_fragments))
    client = OpenAI(api_key=key, timeout=OPENAI_TIMEOUT_SECONDS)
    response = responses_create(client,
        model=OPENAI_MODEL,
        input=REQUIREMENTS_ANALYSIS_PROMPT + "\n\nRETRIEVED KNOWLEDGE BASE EVIDENCE (supporting context; cite only original source fragment IDs):\n" + json.dumps(rag_context or {}, ensure_ascii=False) + "\n\nSOURCE FRAGMENTS:\n" + fragment_text,
        store=False,
        text={"format": {"type": "json_schema", "name": "requirements_contract", "strict": True, "schema": _requirements_analysis_schema()}},
    )
    if not response.output_text:
        raise ValueError("OpenAI returned no Requirements Analysis")
    contract = json.loads(response.output_text)
    _validate_requirements_contract(contract, usable_fragments)
    for requirement in contract["requirements"]:
        requirement["approved"] = False
        for rule in requirement.get("business_rules", []): rule["approved"] = False
        for criterion in requirement.get("acceptance_criteria", []): criterion["approved"] = False
    contract.update({
        "contract_version": "1.0",
        "package_id": package["package_id"],
        "project_id": package["project_id"],
        "package_scope": package.get("scope"),
        "gaps": contract.get("global_gaps", []),
        "accepted_assumptions": [],
        "source_package_items": [{"source_id": i["source_id"], "revision_id": i["revision_id"], "source_type": i["source_type"], "logical_name": i["logical_name"], "sha256": i["sha256"]} for i in items],
        "generated_at": now(),
        "generation_mode": "openai",
        "generation_model": OPENAI_MODEL,
        "generation_prompt_version": REQUIREMENTS_ANALYSIS_PROMPT_VERSION,
        "generation_prompt_hash": requirements_analysis_prompt_hash(),
        "generation_config_hash": requirements_analysis_config_hash(),
    })
    contract["readiness"] = _contract_readiness(contract)
    contract["conflicts"] = contract.get("conflicting_requirements", [])
    return contract

def _requirements_contract_with_timeout(package, items, fragments, rag_context=None):
    result, failure = [], []
    def run():
        try: result.append(_requirements_contract_with_openai(package, items, fragments, rag_context))
        except Exception as exc: failure.append(exc)
    worker = threading.Thread(target=run, daemon=True)
    worker.start(); worker.join(OPENAI_TIMEOUT_SECONDS + 5)
    if worker.is_alive(): raise TimeoutError(f"Requirements Analysis timed out after {OPENAI_TIMEOUT_SECONDS + 5:g} seconds")
    if failure: raise failure[0]
    return result[0]

def _requirement_contract(package, items, fragments):
    lines = _text_lines(fragments); text = " ".join(x["text"] for x in lines); lowered = text.lower()
    rules = [x for x in lines if any(t in x["text"].lower() for t in ("must", "shall", "eligible", "exclude", "fallback", "status"))]
    acceptance = [x for x in lines if any(t in x["text"].lower() for t in ("acceptance", "expected", "success", "outcome", "should"))]
    entities = [x for x in ("member", "campaign", "refill", "reminder", "communication", "prescription", "pharmacy") if x in lowered]
    actors = [x for x in ("member", "care team", "pharmacy", "campaign service", "scheduler", "administrator") if x in lowered]
    refs = lambda selected: [{"fragment_id": x.get("fragment_id"), "source_reference": x["source_reference"]} for x in selected]
    selected = lines[:2] or [{"source_id": i["source_id"], "revision_id": i["revision_id"], "source_reference": "unknown", "heading": i["logical_name"]} for i in items]
    # Do not infer conflicts from simple keyword co-occurrence. BRDs commonly
    # mention valid pairs such as eligible/ineligible or SMS/email while
    # describing normal positive, negative, and fallback paths. Only the LLM
    # path should report direct contradictions with supporting context.
    conflicts = []
    def matching(*terms):
        found = [x for x in lines if any(term.lower() in x["text"].lower() for term in terms)]
        return found or selected

    def make_requirement(number, title, statement, terms, criteria, flow):
        evidence = matching(*terms)
        return {"requirement_id": f"FR-{number:02d}", "title": title, "statement": statement,
                "type": "functional", "priority": "high", "actors": actors, "entities": entities,
                "business_rules": [{"rule_id": f"BR-{number:02d}-{index:02d}", "text": x["text"], "source_references": refs([x])} for index, x in enumerate(evidence, 1)],
                "acceptance_criteria": [{"acceptance_id": f"AC-{number:02d}-{index:02d}", "given": "The source-defined precondition is satisfied.", "when": statement, "then": criterion, "source_references": refs(evidence)} for index, criterion in enumerate(criteria, 1)],
                "ambiguities": [], "assumptions": [],
                "flow_steps": flow,
                "source_references": refs(evidence)}

    if "refill" in lowered and "sms" in lowered:
        requirements = [
            make_requirement(1, "Identify active members", "The system shall identify members with an active member record.", ("active member", "member record"), ["An active member is selected for campaign processing.", "An inactive member is excluded from campaign processing."], ["Load member record", "Check member status"]),
            make_requirement(2, "Identify eligible prescriptions", "The system shall identify members who have an eligible prescription.", ("eligible prescription", "prescription eligibility"), ["A member with an eligible prescription continues to refill reminder processing.", "A member without an eligible prescription is excluded."], ["Evaluate prescription eligibility", "Route eligible or ineligible member"]),
            make_requirement(3, "Validate refill eligibility", "The campaign shall identify members who meet the refill reminder criteria.", ("refill reminder", "refill due", "seven days"), ["Members meeting the refill criteria are selected for the campaign."], ["Check refill due date", "Validate campaign eligibility"]),
            make_requirement(4, "Suppress ineligible communication", "The campaign shall not send communication when the member is not eligible.", ("not eligible", "ineligible", "stop campaign"), ["An ineligible member receives no SMS or phone communication."], ["Detect ineligible status", "Stop campaign"]),
            make_requirement(5, "Send SMS as primary communication", "For an eligible member, the campaign shall send an SMS refill reminder as the primary communication channel.", ("primary communication", "send sms", "sms refill"), ["An eligible member receives the refill reminder SMS."], ["Enter communication step", "Send SMS"]),
            make_requirement(6, "Record SMS communication status", "The system shall record the outcome of the SMS communication.", ("record sms", "sms sent", "sms failed", "communication status"), ["SMS sent or delivered and SMS failed statuses are recorded."], ["Receive SMS provider result", "Persist SMS status"]),
            make_requirement(7, "Trigger phone fallback when SMS fails", "If SMS communication fails, the campaign shall trigger the alternate phone-call communication.", ("sms fails", "phone call", "alternate communication"), ["A phone call is triggered after SMS failure.", "A phone call is not triggered after successful SMS delivery."], ["Check SMS outcome", "Trigger phone call when failed"]),
            make_requirement(8, "Record phone-call outcome", "The system shall record the outcome of the alternate phone-call communication.", ("call connected", "no answer", "call failed", "call outcome"), ["Connected, no-answer, and failed call outcomes are recorded."], ["Place phone call", "Capture call outcome"]),
            make_requirement(9, "Revalidate eligibility before communication", "The campaign shall suppress communication if the member becomes ineligible before communication is sent.", ("becomes ineligible", "before communication", "revalidate"), ["A member who becomes ineligible receives no communication."], ["Recheck eligibility", "Suppress communication if status changed"]),
        ]
    else:
        requirements = [make_requirement(1, "Source package requirement", "The system shall implement the reviewed business behavior described by the source package.", tuple(), [x["text"] for x in (acceptance or lines)[:25]], ["Evaluate the source-defined flow"])]
    rule_number = 1
    acceptance_number = 1
    for requirement_number, requirement in enumerate(requirements, 1):
        requirement["requirement_id"] = f"FR-{requirement_number:03d}"
        for rule in requirement.get("business_rules", []):
            rule["rule_id"] = f"BR-{rule_number:03d}"
            rule_number += 1
        for criterion in requirement.get("acceptance_criteria", []):
            criterion["acceptance_id"] = f"AC-{acceptance_number:03d}"
            acceptance_number += 1
    gaps = []
    if not rules: gaps.append("No business rules were extracted")
    if not acceptance and not lines: gaps.append("No acceptance criteria were extracted")
    global_gaps = gaps
    contract = {"contract_version": "1.0", "package_id": package["package_id"], "project_id": package["project_id"], "package_scope": package.get("scope"), "requirements": requirements, "global_gaps": global_gaps, "global_assumptions": [], "conflicting_requirements": conflicts, "conflicts": conflicts, "gaps": global_gaps, "accepted_assumptions": [], "source_package_items": [{"source_id": i["source_id"], "revision_id": i["revision_id"], "source_type": i["source_type"], "logical_name": i["logical_name"], "sha256": i["sha256"]} for i in items], "generated_at": now(), "generation_mode": "source_package_fallback"}
    contract["readiness"] = _contract_readiness(contract)
    return contract

GENERIC_ANALYSIS_NOTICE_TEXTS = {
    "The source contains unresolved ambiguities.",
    "Requirements analysis detected source statements that need reviewer clarification.",
}

GENERIC_FALLBACK_CONFLICT_PAIRS = (("eligible", "ineligible"), ("include", "exclude"), ("retry", "no retry"), ("sms", "email"))

BUSINESS_RULE_STOPWORDS = {
    "a", "an", "and", "are", "as", "be", "by", "for", "from", "if", "in", "is", "it", "of", "on", "or", "shall", "should", "system", "the", "to", "when", "will", "with", "must", "business", "requirement", "rule"
}

def _business_rule_tokens(text):
    words = re.findall(r"[a-z0-9]+", str(text or "").lower())
    return {word for word in words if len(word) > 2 and word not in BUSINESS_RULE_STOPWORDS}

def _business_rule_similarity(left, right):
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, len(left | right))

def _merge_source_references(*collections):
    merged = {}
    for refs in collections:
        for ref in refs or []:
            if isinstance(ref, dict) and (ref.get("fragment_id") or ref.get("source_reference")):
                key = (ref.get("fragment_id"), ref.get("source_reference"))
                merged[key] = {field: ref.get(field) for field in (
                    "fragment_id", "source_id", "revision_id", "source_type", "source_reference",
                    "heading", "content_hash",
                ) if ref.get(field) is not None}
    return list(merged.values())

def _dedupe_business_rules(contract):
    """Remove duplicates inside an FR without changing approved Business Use Case IDs."""
    for requirement in contract.get("requirements", []) or []:
        seen = []
        unique_rules = []
        for index, rule in enumerate(requirement.get("business_rules", []) or [], 1):
            if isinstance(rule, str):
                rule = {"text": rule, "source_references": requirement.get("source_references", [])}
            text = str(rule.get("text") or "").strip()
            tokens = _business_rule_tokens(text)
            if not text or not tokens:
                continue
            duplicate = None
            for entry in seen:
                exact_match = " ".join(sorted(tokens)) == entry["signature"]
                close_match = _business_rule_similarity(tokens, entry["tokens"]) >= 0.82
                if exact_match or close_match:
                    duplicate = entry
                    break
            if duplicate:
                duplicate["rule"]["source_references"] = _merge_source_references(duplicate["rule"].get("source_references"), rule.get("source_references"))
                continue
            rule["rule_id"] = str(rule.get("rule_id") or f"BR-{index:03d}")
            rule["text"] = text
            rule["source_references"] = _merge_source_references(rule.get("source_references"), requirement.get("source_references"))
            unique_rules.append(rule)
            seen.append({"signature": " ".join(sorted(tokens)), "tokens": tokens, "rule": rule})
        requirement["business_rules"] = unique_rules
    return contract

def _is_generic_fallback_conflict(conflict):
    if not isinstance(conflict, dict) or conflict.get("requirement_ids"):
        return False
    terms = [str(term).lower() for term in conflict.get("terms", [])]
    description = str(conflict.get("description", "")).lower()
    if "confirm the intended business rule" not in description and "confirm precedence" not in description:
        return False
    for left, right in GENERIC_FALLBACK_CONFLICT_PAIRS:
        has_terms = left in terms and right in terms
        has_description_pair = f"'{left}'" in description and f"'{right}'" in description
        if has_terms or has_description_pair:
            return True
    return False

def _clean_requirement_contract(contract):
    """Hide generic notices and normalize generated business requirements for review."""
    contract = _dedupe_business_rules(contract)
    conflicts = [item for item in contract.get("conflicting_requirements", []) if not _is_generic_fallback_conflict(item)]
    legacy_conflicts = [item for item in contract.get("conflicts", []) if not _is_generic_fallback_conflict(item)]
    if "conflicting_requirements" in contract:
        contract["conflicting_requirements"] = conflicts
    if "conflicts" in contract:
        contract["conflicts"] = conflicts if "conflicting_requirements" in contract else legacy_conflicts
    for key in ("global_gaps", "gaps"):
        if key in contract:
            contract[key] = [item for item in contract.get(key, []) if item not in GENERIC_ANALYSIS_NOTICE_TEXTS]
    if isinstance(contract.get("readiness"), dict):
        contract["readiness"]["gaps"] = [item for item in contract["readiness"].get("gaps", []) if item not in GENERIC_ANALYSIS_NOTICE_TEXTS]
    return contract

def _analysis_response(row, discovery_context_count=0):
    result = row_dict(row)
    contract = _clean_requirement_contract(json.loads(result.pop("contract_json")))
    result["contract"] = _apply_discovery_readiness(contract, discovery_context_count)
    return result

def _review_items(contract):
    """Return the individually reviewable contract items, including legacy contracts."""
    items = []
    for requirement in contract.get("requirements", []):
        requirement_id = requirement["requirement_id"]
        items.append((requirement_id, requirement, "functional_criterion"))
        for index, rule in enumerate(requirement.get("business_rules", []), 1):
            if isinstance(rule, str):
                requirement["business_rules"][index - 1] = rule = {"rule_id": f"RULE-{index}", "text": rule, "approved": False}
            items.append((f"{requirement_id}:{rule.get('rule_id', f'RULE-{index}')}", rule, "business_rule"))
        for index, criterion in enumerate(requirement.get("acceptance_criteria", []), 1):
            if isinstance(criterion, str):
                requirement["acceptance_criteria"][index - 1] = criterion = {"acceptance_id": f"AC-{index}", "text": criterion, "approved": False}
            items.append((f"{requirement_id}:{criterion.get('acceptance_id', f'AC-{index}')}", criterion, "acceptance_criterion"))
        for index, outcome in enumerate(requirement.get("expected_outcomes", []), 1):
            if isinstance(outcome, str):
                requirement["expected_outcomes"][index - 1] = outcome = {"outcome_id": f"EO-{index}", "text": outcome, "approved": False}
            items.append((f"{requirement_id}:{outcome.get('outcome_id', f'EO-{index}')}", outcome, "expected_outcome"))
    return items

def _approved_requirement_payload(contract):
    approved_ids = set(contract.get("approved_items", []))
    approved = []
    for requirement in contract.get("requirements", []):
        requirement_id = requirement.get("requirement_id")
        if requirement_id not in approved_ids and requirement.get("review_status") != "approved":
            continue
        criteria = []
        for criterion in requirement.get("acceptance_criteria", []):
            criterion_id = criterion.get("acceptance_id") if isinstance(criterion, dict) else None
            if criterion_id and (f"{requirement_id}:{criterion_id}" in approved_ids or criterion.get("review_status") == "approved"):
                criteria.append(criterion)
        rules = []
        for rule in requirement.get("business_rules", []):
            rule_id = rule.get("rule_id") if isinstance(rule, dict) else None
            if rule_id and (f"{requirement_id}:{rule_id}" in approved_ids or rule.get("review_status") == "approved"):
                rules.append(rule)
        outcomes = []
        for outcome in requirement.get("expected_outcomes", []):
            outcome_id = outcome.get("outcome_id") if isinstance(outcome, dict) else None
            if outcome_id and (f"{requirement_id}:{outcome_id}" in approved_ids or outcome.get("review_status") == "approved"):
                outcomes.append(outcome)
        approved.append({"requirement_id": requirement_id, "title": requirement.get("title"), "type": requirement.get("type"), "statement": requirement.get("statement"), "priority": requirement.get("priority"), "actors": requirement.get("actors", []), "entities": requirement.get("entities", []), "flow_steps": requirement.get("flow_steps", []), "acceptance_criteria": criteria, "expected_outcomes": outcomes, "business_rules": rules, "source_references": requirement.get("source_references", [])})
    return approved

def _test_case_generation_schema():
    source_reference = {"type": "object", "additionalProperties": False, "properties": {"fragment_id": {"type": "string"}, "source_reference": {"type": "string"}}, "required": ["fragment_id", "source_reference"]}
    surface = {"type": "object", "additionalProperties": False, "properties": {"name": {"type": "string"}, "type": {"type": "string"}, "coverage": {"type": "string"}}, "required": ["name", "type", "coverage"]}
    test_data_setup = {"type": "object", "additionalProperties": False, "properties": {"name": {"type": "string"}, "data_type": {"type": "string"}, "value_strategy": {"type": "string"}, "created_during_execution": {"type": "boolean"}, "setup_step": {"type": "string"}, "source": {"type": "string"}}, "required": ["name", "data_type", "value_strategy", "created_during_execution", "setup_step", "source"]}
    data_need = {"type": "object", "additionalProperties": False, "properties": {"name": {"type": "string"}, "type": {"type": "string"}, "value": {"type": "string"}, "required": {"type": "boolean"}, "source": {"type": "string"}}, "required": ["name", "type", "value", "required", "source"]}
    deterministic_input = {"type": "object", "additionalProperties": False, "properties": {"name": {"type": "string"}, "value": {"type": "string"}, "seed_strategy": {"type": "string"}}, "required": ["name", "value", "seed_strategy"]}
    action = {"type": "object", "additionalProperties": False, "properties": {"step": {"type": "integer"}, "surface": {"type": "string"}, "actor": {"type": "string"}, "action": {"type": "string"}, "data_inputs": {"type": "array", "items": {"type": "string"}}}, "required": ["step", "surface", "actor", "action", "data_inputs"]}
    assertion = {"type": "object", "additionalProperties": False, "properties": {"assertion_id": {"type": "string"}, "text": {"type": "string"}, "assertion_type": {"type": "string"}, "evidence_id": {"type": "string", "pattern": "^EV-[0-9]{3}$", "description": "Local evidence-record ID from this test case's evidence array."}}, "required": ["assertion_id", "text", "assertion_type", "evidence_id"]}
    evidence = {"type": "object", "additionalProperties": False, "properties": {"evidence_id": {"type": "string", "pattern": "^EV-[0-9]{3}$", "description": "Local evidence-record ID, not a source citation."}, "evidence_type": {"type": "string"}, "requirement_id": {"type": "string", "pattern": "^FR-[0-9]{3}$"}, "summary": {"type": "string"}, "source_references": {"type": "array", "minItems": 1, "items": source_reference}}, "required": ["evidence_id", "evidence_type", "requirement_id", "summary", "source_references"]}
    validation_result = {"type": "object", "additionalProperties": False, "properties": {"check": {"type": "string"}, "status": {"type": "string"}, "details": {"type": "string"}}, "required": ["check", "status", "details"]}
    cleanup_ownership = {"type": "object", "additionalProperties": False, "properties": {"owner": {"type": "string"}, "cleanup_required": {"type": "boolean"}, "cleanup_steps": {"type": "array", "items": {"type": "string"}}}, "required": ["owner", "cleanup_required", "cleanup_steps"]}
    approval = {"type": "object", "additionalProperties": False, "properties": {"status": {"type": "string"}, "reviewer": {"type": "string"}, "comments": {"type": "string"}, "reviewed_at": {"type": "string"}}, "required": ["status", "reviewer", "comments", "reviewed_at"]}
    return {"type": "object", "additionalProperties": False, "properties": {
        "test_cases": {"type": "array", "minItems": 1, "items": {"type": "object", "additionalProperties": False, "properties": {
            "test_case_id": {"type": "string", "pattern": "^TC-[0-9]{3}$"},
            "requirement_id": {"type": "string", "pattern": "^FR-[0-9]{3}$"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "scenario": {"type": "string"},
            "category": {"type": "string"},
            "type": {"type": "string"},
            "priority": {"type": "string"},
            "surfaces": {"type": "array", "minItems": 1, "items": surface},
            "preconditions": {"type": "array", "items": {"type": "string"}},
            "test_data_setup": {"type": "array", "minItems": 1, "items": test_data_setup},
            "deterministic_inputs": {"type": "array", "minItems": 1, "items": deterministic_input},
            "data_needs": {"type": "array", "minItems": 1, "items": data_need},
            "actions": {"type": "array", "minItems": 1, "items": action},
            "steps": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            "assertions": {"type": "array", "minItems": 1, "items": assertion},
            "expected_results": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            "acceptance_criteria_ids": {"type": "array", "items": {"type": "string", "pattern": "^AC-[0-9]{3}$"}},
            "business_rule_ids": {"type": "array", "items": {"type": "string", "pattern": "^BR-[0-9]{3}$"}},
            "discovery_context_ids": {"type": "array", "items": {"type": "string"}},
            "evidence": {"type": "array", "minItems": 1, "items": evidence},
            "assumptions": {"type": "array", "items": {"type": "string"}},
            "validation_results": {"type": "array", "minItems": 1, "items": validation_result},
            "cleanup_ownership": cleanup_ownership,
            "approval": approval,
            "source_references": {"type": "array", "items": source_reference, "minItems": 1}
        }, "required": ["test_case_id", "requirement_id", "title", "description", "scenario", "category", "type", "priority", "surfaces", "preconditions", "test_data_setup", "deterministic_inputs", "data_needs", "actions", "steps", "assertions", "expected_results", "acceptance_criteria_ids", "business_rule_ids", "discovery_context_ids", "evidence", "assumptions", "validation_results", "cleanup_ownership", "approval", "source_references"]}},
        "coverage": {"type": "object", "additionalProperties": False, "properties": {"required_categories": {"type": "array", "items": {"type": "string"}}, "generated_categories": {"type": "array", "items": {"type": "string"}}, "missing_categories": {"type": "array", "items": {"type": "string"}}}, "required": ["required_categories", "generated_categories", "missing_categories"]},
        "global_gaps": {"type": "array", "items": {"type": "string"}}
    }, "required": ["test_cases", "coverage", "global_gaps"]}

def _requirement_text(requirement):
    parts = [requirement.get("title"), requirement.get("statement"), *requirement.get("flow_steps", [])]
    parts.extend(rule.get("text") for rule in requirement.get("business_rules", []) if isinstance(rule, dict))
    parts.extend(criterion.get("then") for criterion in requirement.get("acceptance_criteria", []) if isinstance(criterion, dict))
    return " ".join(str(part or "") for part in parts).lower()

def _case_source_refs(requirement):
    refs = list(requirement.get("source_references", []) or [])
    for collection in ("acceptance_criteria", "business_rules"):
        for item in requirement.get(collection, []):
            if isinstance(item, dict):
                refs.extend(item.get("source_references", []) or [])
    unique = {}
    for ref in refs:
        if ref.get("fragment_id") or ref.get("source_reference"):
            unique[(ref.get("fragment_id"), ref.get("source_reference"))] = {"fragment_id": ref.get("fragment_id"), "source_reference": ref.get("source_reference")}
    return list(unique.values())

def _find_requirement_for_terms(approved_requirements, terms):
    for requirement in approved_requirements:
        text = _requirement_text(requirement)
        if any(term in text for term in terms):
            return requirement
    return None

_COVERAGE_STOP_WORDS = {"a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "into", "is", "it", "of", "on", "or", "the", "to", "when", "where", "which", "with", "must", "should", "shall", "given", "then"}
_CONFLICTING_COVERAGE_TERMS = ({"active", "inactive"}, {"eligible", "ineligible"}, {"include", "exclude"}, {"included", "excluded"}, {"send", "suppress"}, {"success", "failure"}, {"valid", "invalid"}, {"present", "missing"}, {"primary", "alternate"}, {"approved", "rejected"})

def _coverage_terms(item, acceptance=False):
    values = (item.get(key) for key in ("given", "when", "then")) if acceptance else (item.get("text"),)
    text = " ".join(str(value or "") for value in values).lower()
    tokens = {token[:-1] if len(token) > 4 and token.endswith("s") else token for token in re.findall(r"[a-z0-9]+", text)}
    return tokens - _COVERAGE_STOP_WORDS

def _coverage_overlap_score(left, right):
    left_terms, right_terms = _coverage_terms(left, "given" in left or "when" in left or "then" in left), _coverage_terms(right, "given" in right or "when" in right or "then" in right)
    if not left_terms or not right_terms:
        return 0.0
    if any(len(group & left_terms) and len(group & right_terms) and not (group & left_terms) == (group & right_terms) for group in _CONFLICTING_COVERAGE_TERMS):
        return 0.0
    common = len(left_terms & right_terms)
    jaccard = common / len(left_terms | right_terms)
    containment = common / min(len(left_terms), len(right_terms))
    if left_terms == right_terms:
        return 1.0
    return containment if common >= 3 and containment >= 0.7 and jaccard >= 0.3 else jaccard

def _test_case_coverage_plan(approved_requirements, requested_count=None):
    categories = [
        ("happy_path", "Eligible-member happy path", ("eligible", "active", "success", "reminder", "campaign")),
        ("exclusion", "Exclusion or missing-data path", ("ineligible", "inactive", "exclude", "not eligible", "suppress", "missing")),
        ("boundary", "Threshold or boundary path", ("threshold", "boundary", "due", "day", "status", "eligibility", "eligible")),
        ("channel_fallback", "Channel fallback path", ("fallback", "alternate", "phone", "sms fail", "channel")),
        ("retry_failure", "Retry, idempotency, or service-failure path", ("retry", "idempot", "failure", "fails", "partial", "service", "timeout")),
    ]
    plan, gaps = [], []
    if requested_count is None:
        for requirement in approved_requirements:
            criteria = [item for item in requirement.get("acceptance_criteria", []) if isinstance(item, dict)]
            rules = [item for item in requirement.get("business_rules", []) if isinstance(item, dict)]
            requirement_plan = []
            for criterion in criteria:
                requirement_plan.append({"category": "requirement_validation", "label": criterion.get("acceptance_id") or "Acceptance criterion validation", "requirement": requirement, "acceptance_criteria": [criterion], "business_rules": []})
            for rule in rules:
                criterion_cases = [item for item in requirement_plan if item["acceptance_criteria"]]
                best_match = max(criterion_cases, key=lambda item: _coverage_overlap_score(item["acceptance_criteria"][0], rule), default=None)
                if best_match and _coverage_overlap_score(best_match["acceptance_criteria"][0], rule) >= 0.58:
                    best_match["business_rules"].append(rule)
                    continue
                requirement_plan.append({"category": "requirement_validation", "label": rule.get("rule_id") or "Business rule validation", "requirement": requirement, "acceptance_criteria": [], "business_rules": [rule]})
            if not requirement_plan:
                requirement_plan.append({"category": "requirement_validation", "label": "Requirement validation", "requirement": requirement, "acceptance_criteria": [], "business_rules": []})
            remaining = 100 - len(plan)
            plan.extend(requirement_plan[:remaining])
            if len(requirement_plan) > remaining:
                gaps.append("Automatic test-case plan was capped at 100 cases for one generation run.")
                return plan, gaps
        return plan[:100], gaps

    first_requirement = approved_requirements[0] if approved_requirements else None
    for category, label, terms in categories:
        requirement = first_requirement if category == "happy_path" else _find_requirement_for_terms(approved_requirements, terms)
        if requirement:
            plan.append({"category": category, "label": label, "requirement": requirement})
        else:
            gaps.append(f"No approved evidence was found for the {label.lower()} category.")
    if not plan and first_requirement:
        plan.append({"category": "requirement_validation", "label": "Requirement validation", "requirement": first_requirement})
    while len(plan) < requested_count and plan:
        base = plan[len(plan) % len(plan)]
        plan.append({"category": base["category"], "label": base["label"], "requirement": base["requirement"]})
    return plan[:requested_count], gaps

def _compact_test_source_refs(refs, limit=4):
    compacted = []
    for ref in refs or []:
        if not isinstance(ref, dict):
            continue
        fragment_id = ref.get("fragment_id")
        source_reference = ref.get("source_reference")
        if not fragment_id and not source_reference:
            continue
        compacted.append({"fragment_id": fragment_id, "source_reference": source_reference})
        if len(compacted) >= limit:
            break
    return compacted

def _compact_requirement_for_test_prompt(requirement):
    criteria = []
    for criterion in requirement.get("acceptance_criteria", [])[:8]:
        if not isinstance(criterion, dict):
            continue
        criteria.append({
            "acceptance_id": criterion.get("acceptance_id"),
            "given": criterion.get("given"),
            "when": criterion.get("when"),
            "then": criterion.get("then"),
            "source_references": _compact_test_source_refs(criterion.get("source_references"), 2),
        })
    rules = []
    for rule in requirement.get("business_rules", [])[:8]:
        if not isinstance(rule, dict):
            continue
        rules.append({
            "rule_id": rule.get("rule_id"),
            "text": rule.get("text"),
            "source_references": _compact_test_source_refs(rule.get("source_references"), 2),
        })
    return {
        "requirement_id": requirement.get("requirement_id"),
        "title": requirement.get("title"),
        "statement": requirement.get("statement"),
        "priority": requirement.get("priority"),
        "entities": requirement.get("entities", [])[:10],
        "acceptance_criteria": criteria,
        "business_rules": rules,
        "source_references": _compact_test_source_refs(requirement.get("source_references"), 4),
    }

def _test_case_generation_input(approved_requirements, package, requested_count, discovery_contexts=None, rag_context=None):
    discovery_contexts = discovery_contexts or []
    plan, gaps = _test_case_coverage_plan(approved_requirements, requested_count)
    context_gaps = [] if discovery_contexts or rag_context else ["No supplementary knowledge context was supplied; use the approved requirement contract and cited source references only."]
    return {
        "requested_test_case_count": len(plan),
        "count_strategy": "derived_from_approved_functional_requirements_acceptance_criteria_and_business_rules" if requested_count is None else "user_requested",
        "package_id": package["package_id"],
        "package_version": package["package_version"],
        "source_package_run_id": package.get("run_id"),
        "required_coverage": [item["category"] for item in plan],
        "coverage_plan": [{"requirement_id": item["requirement"].get("requirement_id"), "category": item["category"], "acceptance_criteria_ids": [criterion.get("acceptance_id") for criterion in item.get("acceptance_criteria", []) if criterion.get("acceptance_id")], "business_rule_ids": [rule.get("rule_id") for rule in item.get("business_rules", []) if rule.get("rule_id")]} for item in plan],
        "context": {
            "mode": "approved_requirement_contract_with_knowledge_context" if discovery_contexts or rag_context else "approved_requirement_contract",
            "graphrag_context_available": bool(discovery_contexts or rag_context),
            "knowledge_contexts": discovery_contexts,
            "gaps": context_gaps,
        },
        "retrieved_knowledge_evidence": _rag_context_for_prompt(rag_context, max_evidence=10, max_fragment_characters=650),
        "database_test_data_instruction": {
            "purpose": "The LLM must infer database presteps and required test data from the approved functional requirements, acceptance criteria, business rules, source package references, and knowledge context.",
            "required_behavior": [
                "For each test case, identify the concrete database keys and attributes needed before execution.",
                "Mark generated identifiers such as member_id as created_during_execution instead of inventing actual production values.",
                "Use setup_step to describe how the execution template or pre-step creates or selects the data.",
                "Cite approved requirement, acceptance criterion, business rule, or knowledge context IDs in source.",
                "Reference table or schema names only when supplied by source package or knowledge context.",
            ],
        },
        "coverage_gaps": gaps,
        "requirements": [_compact_requirement_for_test_prompt(requirement) for requirement in approved_requirements],
    }

def _validate_test_case_contract(contract, approved_requirements, requested_count=None, discovery_contexts=None, rag_context=None):
    discovery_contexts = discovery_contexts or []
    if not isinstance(contract, dict) or not contract.get("test_cases"):
        raise ValueError("The AI returned no test cases")
    approved_by_id = {item["requirement_id"]: item for item in approved_requirements}
    rag_fragment_ids = {item.get("original_fragment_id") or item.get("fragment_id") for item in (rag_context or {}).get("brd_fragments", []) + (rag_context or {}).get("technical_fragments", [])}
    rag_fragment_ids.discard(None)
    fragment_ids_by_reference = {}
    for requirement in approved_requirements:
        for ref in _case_source_refs(requirement):
            if ref.get("fragment_id") and ref.get("source_reference"):
                fragment_ids_by_reference.setdefault(ref["source_reference"], set()).add(ref["fragment_id"])
    for fragment in (rag_context or {}).get("brd_fragments", []) + (rag_context or {}).get("technical_fragments", []):
        fragment_id = fragment.get("original_fragment_id") or fragment.get("fragment_id")
        source_reference = fragment.get("source_reference")
        if fragment_id and source_reference:
            fragment_ids_by_reference.setdefault(source_reference, set()).add(fragment_id)
    plan, _ = _test_case_coverage_plan(approved_requirements, requested_count)
    required_categories = {item["category"] for item in plan}
    generated_categories = {case.get("category") for case in contract["test_cases"]}
    missing_categories = sorted(required_categories - generated_categories)
    if missing_categories:
        raise ValueError(f"AI missed required test-case categories: {', '.join(missing_categories)}")
    for entry in plan:
        requirement_id = entry["requirement"].get("requirement_id")
        criterion_ids = [criterion.get("acceptance_id") for criterion in entry.get("acceptance_criteria", []) if criterion.get("acceptance_id")]
        rule_ids = [rule.get("rule_id") for rule in entry.get("business_rules", []) if rule.get("rule_id")]
        for criterion_id in criterion_ids:
            if not any(case.get("requirement_id") == requirement_id and criterion_id in (case.get("acceptance_criteria_ids") or []) for case in contract["test_cases"]):
                raise ValueError(f"AI missed required acceptance criterion coverage: {criterion_id}")
        for rule_id in rule_ids:
            if not any(case.get("requirement_id") == requirement_id and rule_id in (case.get("business_rule_ids") or []) for case in contract["test_cases"]):
                raise ValueError(f"AI missed required business rule coverage: {rule_id}")
    seen = set()
    required_fields = ("description", "scenario", "category", "surfaces", "test_data_setup", "deterministic_inputs", "data_needs", "actions", "assertions", "evidence", "validation_results", "cleanup_ownership", "approval")
    for test_case in contract["test_cases"]:
        case_id = test_case.get("test_case_id")
        requirement_id = test_case.get("requirement_id")
        if not case_id or case_id in seen or requirement_id not in approved_by_id:
            raise ValueError("AI returned an invalid or duplicate test case")
        seen.add(case_id)
        if not test_case.get("steps") or not test_case.get("expected_results"):
            raise ValueError(f"AI returned an incomplete test case for {requirement_id}")
        for field in required_fields:
            if not test_case.get(field):
                raise ValueError(f"AI returned test case {case_id} without {field}")
        if not any(item.get("created_during_execution") for item in test_case.get("test_data_setup", []) if isinstance(item, dict)):
            raise ValueError(f"AI returned test case {case_id} without execution-created database test data")
        item = approved_by_id[requirement_id]
        criterion_ids = {x.get("acceptance_id") for x in item.get("acceptance_criteria", []) if isinstance(x, dict)}
        rule_ids = {x.get("rule_id") for x in item.get("business_rules", []) if isinstance(x, dict)}
        context_ids = {context["context_id"] for context in discovery_contexts}
        valid_evidence_ids = {requirement_id, *criterion_ids, *rule_ids, *context_ids, *rag_fragment_ids}
        for setup_item in test_case.get("test_data_setup", []):
            if not isinstance(setup_item, dict):
                raise ValueError(f"AI returned invalid test data setup for {case_id}")
            setup_name = str(setup_item.get("name") or "").strip().lower()
            setup_step = str(setup_item.get("setup_step") or "").strip().lower()
            setup_source = str(setup_item.get("source") or "").strip()
            if setup_name in {"data", "test_data", "record", "setup"} or setup_step in {"create test data", "setup test data"}:
                raise ValueError(f"AI returned generic test data setup for {case_id}")
            cited_sources = {value.strip() for value in setup_source.replace(";", ",").split(",") if value.strip()}
            if not cited_sources or not cited_sources <= valid_evidence_ids:
                raise ValueError(f"AI returned uncited test data setup for {case_id}")
        valid_fragments = {ref.get("fragment_id") for ref in _case_source_refs(item) if ref.get("fragment_id")} | rag_fragment_ids
        if not set(test_case.get("acceptance_criteria_ids", [])) <= criterion_ids or not set(test_case.get("business_rule_ids", [])) <= rule_ids:
            raise ValueError(f"AI returned an unapproved evidence reference for {case_id}")
        if discovery_contexts:
            if not set(test_case.get("discovery_context_ids", [])) <= context_ids:
                raise ValueError(f"AI returned an unknown knowledge context for {case_id}")
            if not test_case.get("discovery_context_ids"):
                raise ValueError(f"AI returned no knowledge context citation for {case_id}")
            allowed_surface_names = {context["name"] for context in discovery_contexts}
            case_surface_names = {surface.get("name") for surface in test_case.get("surfaces", []) if surface.get("name")}
            if allowed_surface_names and not case_surface_names & allowed_surface_names:
                raise ValueError(f"AI returned an uncited executable surface for {case_id}")
        evidence_record_ids = set()
        for evidence in test_case.get("evidence", []):
            evidence_id = str(evidence.get("evidence_id") or "")
            if not re.fullmatch(r"EV-\d{3}", evidence_id) or evidence_id in evidence_record_ids:
                raise ValueError(f"AI returned an invalid or duplicate evidence-record id for {case_id}")
            evidence_record_ids.add(evidence_id)
            if evidence.get("requirement_id") != requirement_id:
                raise ValueError(f"AI returned evidence linked to the wrong requirement for {case_id}")
            evidence_refs = evidence.get("source_references") or []
            if not evidence_refs:
                raise ValueError(f"AI returned uncited evidence for {case_id}")
            if valid_fragments:
                for ref in evidence_refs:
                    fragment_id = ref.get("fragment_id")
                    if fragment_id not in valid_fragments:
                        matching_ids = fragment_ids_by_reference.get(ref.get("source_reference"), set())
                        if len(matching_ids) == 1:
                            ref["fragment_id"] = next(iter(matching_ids))
                            fragment_id = ref["fragment_id"]
                    if not fragment_id or fragment_id not in valid_fragments:
                        raise ValueError(f"AI returned an unknown source fragment in evidence for {case_id}")
        for assertion in test_case.get("assertions", []):
            if assertion.get("evidence_id") not in evidence_record_ids:
                raise ValueError(f"AI returned an assertion with an unknown evidence-record id for {case_id}")
        if valid_fragments:
            for ref in test_case.get("source_references", []):
                if ref.get("fragment_id") and ref.get("fragment_id") not in valid_fragments:
                    raise ValueError(f"AI returned an unknown source fragment for {case_id}")

def _test_cases_with_openai(approved_requirements, package, requested_count, discovery_contexts=None, rag_context=None):
    from openai import OpenAI
    # Validation repair is controlled explicitly below; disable hidden SDK
    # retries so a slow request cannot be repeated before our repair logic runs.
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), timeout=OPENAI_TIMEOUT_SECONDS, max_retries=0)
    payload = _test_case_generation_input(approved_requirements, package, requested_count, discovery_contexts, rag_context)
    base_prompt = TEST_CASE_GENERATION_PROMPT + json.dumps(payload, ensure_ascii=False)
    failures = []
    previous_invalid_output = None
    for attempt in range(1, TEST_CASE_GENERATION_MAX_ATTEMPTS + 1):
        try:
            request_prompt = base_prompt
            if attempt > 1:
                previous_error = failures[-1]["details"]
                repair_guidance = (
                    "Correct the specific validation failure while preserving supported case behavior. "
                    "Every test case must have at least one test_data_setup item whose created_during_execution is true. "
                    "Use a record key explicitly supported by that case's approved requirement or Knowledge Base evidence; "
                    "describe how the execution template or pre-step creates it, do not invent a production value, and set "
                    "source to comma-separated approved evidence IDs only. "
                )
                missing_case = re.search(r"test case (TC-\d{3})", previous_error, re.IGNORECASE)
                if missing_case:
                    case_id = missing_case.group(1)
                    invalid_case = next((item for item in (previous_invalid_output or {}).get("test_cases", [])
                                         if item.get("test_case_id") == case_id), None)
                    if invalid_case:
                        requirement = next((item for item in approved_requirements
                                            if item.get("requirement_id") == invalid_case.get("requirement_id")), {})
                        allowed_ids = [requirement.get("requirement_id")]
                        allowed_ids.extend(item.get("acceptance_id") for item in requirement.get("acceptance_criteria", []) if item.get("acceptance_id"))
                        allowed_ids.extend(item.get("rule_id") for item in requirement.get("business_rules", []) if item.get("rule_id"))
                        allowed_ids.extend(context.get("context_id") for context in discovery_contexts or [] if context.get("context_id"))
                        repair_guidance += f"For {case_id}, use one or more of these evidence IDs in test_data_setup.source: {', '.join(dict.fromkeys(x for x in allowed_ids if x))}. "
                        if "unknown source fragment in evidence" in previous_error.lower():
                            allowed_pairs = _case_source_refs(requirement)
                            for fragment in (rag_context or {}).get("brd_fragments", []) + (rag_context or {}).get("technical_fragments", []):
                                fragment_id = fragment.get("original_fragment_id") or fragment.get("fragment_id")
                                source_reference = fragment.get("source_reference")
                                if fragment_id and source_reference:
                                    allowed_pairs.append({"fragment_id": fragment_id, "source_reference": source_reference})
                            unique_pairs = {(pair.get("fragment_id"), pair.get("source_reference")) for pair in allowed_pairs
                                            if pair.get("fragment_id") and pair.get("source_reference")}
                            allowed_pairs_json = json.dumps(
                                [{"fragment_id": fragment_id, "source_reference": source_reference}
                                 for fragment_id, source_reference in sorted(unique_pairs)],
                                ensure_ascii=False,
                            )
                            repair_guidance += (
                                f"For evidence.source_references in {case_id}, copy exact pairs only from this allowed list: "
                                f"{allowed_pairs_json}. "
                            )
                missing_criterion = re.search(r"acceptance criterion coverage: (AC-\d{3})", previous_error, re.IGNORECASE)
                if missing_criterion:
                    criterion_id = missing_criterion.group(1)
                    for requirement in approved_requirements:
                        criterion = next((item for item in requirement.get("acceptance_criteria", [])
                                          if item.get("acceptance_id") == criterion_id), None)
                        if criterion:
                            repair_guidance += (
                                f"The missing criterion {criterion_id} belongs to requirement {requirement.get('requirement_id')}. "
                                f"Create or update a case for that requirement and include the exact ID {criterion_id} "
                                "in acceptance_criteria_ids. Its approved condition is "
                                f"given={criterion.get('given')}; when={criterion.get('when')}; then={criterion.get('then')}. "
                                "The case must test this condition and its expected outcome, not merely add an unrelated citation. "
                            )
                            break
                missing_rule = re.search(r"business rule coverage: (BR-\d{3})", previous_error, re.IGNORECASE)
                if missing_rule:
                    rule_id = missing_rule.group(1)
                    for requirement in approved_requirements:
                        rule = next((item for item in requirement.get("business_rules", [])
                                     if item.get("rule_id") == rule_id), None)
                        if rule:
                            repair_guidance += (
                                f"The missing business rule {rule_id} belongs to requirement {requirement.get('requirement_id')}. "
                                f"Create or update a case for that requirement and include the exact ID {rule_id} "
                                f"in business_rule_ids. The approved rule is: {rule.get('text')}. "
                                "The case must exercise the rule's behavior, not merely add an unrelated citation. "
                            )
                            break
                request_prompt = (
                    f"{base_prompt}\n\n"
                    "VALIDATION REPAIR REQUIRED: Your previous test-case response failed validation. "
                    "Return a complete corrected test-case contract. Preserve valid cases, add or correct cases as needed. "
                    f"{repair_guidance}"
                    "and ensure every acceptance criterion and business rule listed in coverage_plan is covered by its exact ID "
                    "in acceptance_criteria_ids or business_rule_ids on a case for the matching requirement. Do not omit "
                    "required coverage to satisfy the requested case count.\n"
                    f"Validation error: {previous_error}\n"
                    f"Previous invalid response: {json.dumps(previous_invalid_output, ensure_ascii=False) if previous_invalid_output is not None else 'unavailable'}"
                )
            response = responses_create(client, model=OPENAI_MODEL, input=request_prompt, store=False, text={"format": {"type": "json_schema", "name": "test_case_contract", "strict": True, "schema": _test_case_generation_schema()}})
            if not response.output_text:
                raise ValueError("OpenAI returned no test cases")
            contract = json.loads(response.output_text)
            _deduplicate_generated_test_cases(contract)
            _validate_test_case_contract(contract, approved_requirements, requested_count, discovery_contexts, rag_context)
            contract["agent_attempts"] = [{"attempt": attempt, "status": "passed", "details": "Schema, evidence, and coverage validation passed."}]
            contract.setdefault("global_gaps", []).extend(payload["context"]["gaps"] + payload.get("coverage_gaps", []))
            return contract
        except Exception as exc:
            if "contract" in locals() and isinstance(contract, dict):
                previous_invalid_output = contract
            failures.append({"attempt": attempt, "status": "failed", "details": safe_llm_error_message(exc)})
    raise ValueError(f"LLM test-case generation failed validation after {TEST_CASE_GENERATION_MAX_ATTEMPTS} attempts: {failures[-1]['details']}")

def _normalized_case_text(test_case):
    value = " ".join([
        str(test_case.get("title") or ""),
        str(test_case.get("scenario") or ""),
        " ".join(test_case.get("steps", []) or []),
        " ".join(test_case.get("expected_results", []) or []),
    ])
    return " ".join(value.lower().split())

def _deduplicate_generated_test_cases(contract):
    def normalize(value):
        if isinstance(value, list):
            return tuple(normalize(item) for item in value)
        if isinstance(value, dict):
            return tuple((key, normalize(value[key])) for key in sorted(value))
        if isinstance(value, str):
            return " ".join(re.findall(r"[a-z0-9]+", value.lower()))
        return value

    merged, by_signature = [], {}
    array_fields = ("acceptance_criteria_ids", "business_rule_ids", "discovery_context_ids", "evidence", "source_references", "deterministic_inputs", "data_needs", "actions", "steps", "assertions", "expected_results", "preconditions", "assumptions", "validation_results")
    for test_case in contract.get("test_cases", []):
        signature = (
            test_case.get("requirement_id"),
            test_case.get("category"),
            normalize(test_case.get("scenario", "")),
            normalize(test_case.get("preconditions", [])),
            normalize(test_case.get("steps", [])),
            normalize(test_case.get("expected_results", [])),
        )
        duplicate_index = by_signature.get(signature)
        if duplicate_index is None:
            by_signature[signature] = len(merged)
            merged.append(test_case)
            continue
        existing = merged[duplicate_index]
        for field in array_fields:
            values = existing.setdefault(field, [])
            seen = {json.dumps(item, sort_keys=True, ensure_ascii=False) for item in values}
            for item in test_case.get(field, []) or []:
                encoded = json.dumps(item, sort_keys=True, ensure_ascii=False)
                if encoded not in seen:
                    values.append(item)
                    seen.add(encoded)
        setup_by_name = {str(item.get("name") or "").strip().lower(): item for item in existing.get("test_data_setup", []) if isinstance(item, dict)}
        for setup in test_case.get("test_data_setup", []) or []:
            name = str(setup.get("name") or "").strip().lower() if isinstance(setup, dict) else ""
            current = setup_by_name.get(name) if name else None
            if current is None:
                existing.setdefault("test_data_setup", []).append(setup)
                if name: setup_by_name[name] = setup
            else:
                sources = list(dict.fromkeys([value.strip() for value in f"{current.get('source', '')},{setup.get('source', '')}".split(",") if value.strip()]))
                current["source"] = ",".join(sources)

    for index, test_case in enumerate(merged, 1):
        test_case["test_case_id"] = f"TC-{index:03d}"
        for assertion_index, assertion in enumerate(test_case.get("assertions", []) or [], 1):
            if isinstance(assertion, dict):
                assertion["assertion_id"] = f"AS-{index:03d}-{assertion_index:03d}"
    contract["test_cases"] = merged
    coverage = contract.setdefault("coverage", {})
    coverage["generated_categories"] = list(dict.fromkeys(test_case.get("category") for test_case in merged if test_case.get("category")))
    coverage["missing_categories"] = sorted(set(coverage.get("required_categories", [])) - set(coverage["generated_categories"]))
    return contract

def _review_status(test_case):
    return test_case.get("review_status") or (test_case.get("approval") or {}).get("status") or "pending"

def _coverage_item(item_id, label, mapped_ids):
    mapped = sorted(set(mapped_ids))
    return {"id": item_id, "label": label or item_id, "test_case_ids": mapped, "status": "covered" if mapped else "missing"}

def _build_test_case_coverage_review(contract, approved_requirements):
    test_cases = contract.get("test_cases", []) or []
    required_categories = list(dict.fromkeys((contract.get("coverage") or {}).get("required_categories", []) or []))
    if not required_categories:
        required_categories = list(dict.fromkeys(case.get("category") for case in test_cases if case.get("category")))

    matrix = []
    totals = {
        "requirements_total": len(approved_requirements),
        "requirements_covered": 0,
        "acceptance_criteria_total": 0,
        "acceptance_criteria_covered": 0,
        "business_rules_total": 0,
        "business_rules_covered": 0,
        "scenario_branches_total": 0,
        "scenario_branches_covered": 0,
    }

    for requirement in approved_requirements:
        requirement_id = requirement.get("requirement_id")
        requirement_cases = [case for case in test_cases if case.get("requirement_id") == requirement_id]
        requirement_case_ids = [case.get("test_case_id") for case in requirement_cases if case.get("test_case_id")]
        if requirement_case_ids:
            totals["requirements_covered"] += 1

        acceptance_items = []
        for criterion in requirement.get("acceptance_criteria", []) or []:
            if not isinstance(criterion, dict):
                continue
            criterion_id = criterion.get("acceptance_id")
            mapped = [case.get("test_case_id") for case in requirement_cases if criterion_id in (case.get("acceptance_criteria_ids") or [])]
            acceptance_items.append(_coverage_item(criterion_id, criterion.get("scenario") or criterion.get("then") or criterion.get("text"), mapped))
        totals["acceptance_criteria_total"] += len(acceptance_items)
        totals["acceptance_criteria_covered"] += sum(1 for item in acceptance_items if item["status"] == "covered")

        rule_items = []
        for rule in requirement.get("business_rules", []) or []:
            if not isinstance(rule, dict):
                continue
            rule_id = rule.get("rule_id")
            mapped = [case.get("test_case_id") for case in requirement_cases if rule_id in (case.get("business_rule_ids") or [])]
            rule_items.append(_coverage_item(rule_id, rule.get("text"), mapped))
        totals["business_rules_total"] += len(rule_items)
        totals["business_rules_covered"] += sum(1 for item in rule_items if item["status"] == "covered")

        branch_items = []
        for category in required_categories:
            mapped = [case.get("test_case_id") for case in requirement_cases if case.get("category") == category]
            branch_items.append(_coverage_item(category, category.replace("_", " ").title(), mapped))
        totals["scenario_branches_total"] += len(branch_items)
        totals["scenario_branches_covered"] += sum(1 for item in branch_items if item["status"] == "covered")

        matrix.append({
            "requirement_id": requirement_id,
            "title": requirement.get("title") or requirement_id,
            "priority": requirement.get("priority") or "medium",
            "test_case_ids": sorted(set(requirement_case_ids)),
            "status": "covered" if requirement_case_ids else "missing",
            "acceptance_criteria": acceptance_items,
            "business_rules": rule_items,
            "scenario_branches": branch_items,
        })

    return {"summary": totals, "matrix": matrix}

def _build_test_case_quality_review(contract):
    findings = []
    test_cases = contract.get("test_cases", []) or []
    by_signature = {}
    category_groups = {}
    for test_case in test_cases:
        case_id = test_case.get("test_case_id")
        signature = _normalized_case_text(test_case)
        if signature:
            by_signature.setdefault(signature, []).append(case_id)
        category_key = (test_case.get("requirement_id"), test_case.get("category"))
        category_groups.setdefault(category_key, []).append(case_id)

        evidence = test_case.get("evidence") or []
        source_refs = test_case.get("source_references") or []
        assertions = test_case.get("assertions") or []
        data_needs = test_case.get("data_needs") or []
        steps = test_case.get("steps") or []
        expected_results = test_case.get("expected_results") or []
        text = _normalized_case_text(test_case)

        if not evidence or not source_refs:
            findings.append({"finding_id": f"QF-{len(findings)+1:03d}", "type": "ungrounded", "severity": "high", "test_case_ids": [case_id], "message": "Case is missing evidence or source-reference grounding.", "recommendation": "Reject or regenerate unless a reviewer can add traceable evidence."})
        if any(ref.get("fragment_id") == "unavailable" for ref in source_refs if isinstance(ref, dict)):
            findings.append({"finding_id": f"QF-{len(findings)+1:03d}", "type": "ungrounded", "severity": "medium", "test_case_ids": [case_id], "message": "Case uses fallback source text instead of a precise source fragment.", "recommendation": "Review manually and regenerate after Knowledge Base/source context is available if needed."})
        if len(steps) < 3 or any(len(str(step).strip()) < 12 for step in steps):
            findings.append({"finding_id": f"QF-{len(findings)+1:03d}", "type": "vague", "severity": "medium", "test_case_ids": [case_id], "message": "Case has too few or too-short execution steps.", "recommendation": "Edit the steps or request regeneration."})
        if not expected_results or not assertions:
            findings.append({"finding_id": f"QF-{len(findings)+1:03d}", "type": "vague", "severity": "high", "test_case_ids": [case_id], "message": "Case is missing assertions or expected results.", "recommendation": "Do not approve until expected outcomes are explicit."})
        if not data_needs or "not specified" in text or "the requirement behaves as approved" in text:
            findings.append({"finding_id": f"QF-{len(findings)+1:03d}", "type": "low_value", "severity": "medium", "test_case_ids": [case_id], "message": "Case appears generic or has weak test-data detail.", "recommendation": "Edit data needs or regenerate with richer context."})

    for ids in by_signature.values():
        unique_ids = sorted(set(ids))
        if len(unique_ids) > 1:
            findings.append({"finding_id": f"QF-{len(findings)+1:03d}", "type": "duplicate", "severity": "medium", "test_case_ids": unique_ids, "message": "These cases have near-identical title, scenario, steps, and expected results.", "recommendation": "Reject redundant copies or regenerate one of them."})

    for (requirement_id, category), ids in category_groups.items():
        unique_ids = sorted(set(ids))
        if requirement_id and category and len(unique_ids) > 1:
            findings.append({"finding_id": f"QF-{len(findings)+1:03d}", "type": "redundant", "severity": "low", "test_case_ids": unique_ids, "message": f"Multiple cases cover {requirement_id} / {category}.", "recommendation": "Keep only if each case exercises different data, surface, or assertion coverage."})

    summary = {"total": len(findings), "high": 0, "medium": 0, "low": 0, "duplicate": 0, "vague": 0, "ungrounded": 0, "low_value": 0, "redundant": 0}
    for finding in findings:
        summary[finding["severity"]] = summary.get(finding["severity"], 0) + 1
        summary[finding["type"]] = summary.get(finding["type"], 0) + 1
    return {"summary": summary, "findings": findings}

def _apply_coverage_and_quality_review(contract, approved_requirements, timestamp=None):
    coverage_review = _build_test_case_coverage_review(contract, approved_requirements)
    quality_review = _build_test_case_quality_review(contract)
    approved_ids = [case["test_case_id"] for case in contract.get("test_cases", []) if _review_status(case) == "approved"]
    rejected_ids = [case["test_case_id"] for case in contract.get("test_cases", []) if _review_status(case) == "rejected"]
    pending_ids = [case["test_case_id"] for case in contract.get("test_cases", []) if _review_status(case) == "pending"]
    coverage_review["summary"].update({
        "approved_cases": len(approved_ids),
        "rejected_cases": len(rejected_ids),
        "pending_cases": len(pending_ids),
    })
    coverage_review["quality"] = quality_review
    coverage_review["updated_at"] = timestamp or now()
    contract["coverage_review"] = coverage_review
    return contract

def _test_execution_result_response(row):
    result = row_dict(row)
    if not result:
        return None
    for key in ("expected_json", "actual_json", "assertions_json", "bronze_refs_json"):
        value = result.pop(key, None)
        result[key[:-5]] = json.loads(value or ("[]" if key in {"assertions_json", "bronze_refs_json"} else "{}"))
    return result

def _execution_results_for_run(conn, run_id: str):
    rows = conn.execute("SELECT * FROM test_execution_result WHERE run_id=%s ORDER BY test_case_id", (run_id,)).fetchall()
    return [_test_execution_result_response(row) for row in rows]

def _execution_summary(results):
    total = len(results)
    passed = sum(1 for item in results if item.get("status") == "passed")
    failed = sum(1 for item in results if item.get("status") == "failed")
    if not total:
        status = "not_executed"
    elif failed:
        status = "fail"
    else:
        status = "pass"
    return {"total": total, "passed": passed, "failed": failed, "status": status}

def _test_run_response(row, conn=None):
    result = row_dict(row)
    if not result:
        return None
    result["configuration_snapshot"] = json.loads(result.get("configuration_snapshot") or "{}")
    for key in ("selected_test_case_ids_json", "frozen_contract_json", "immutable_observations_json"):
        value = result.pop(key, None)
        result[key[:-5]] = json.loads(value or ("{}" if key == "frozen_contract_json" else "[]"))
    results = _execution_results_for_run(conn, result["run_id"]) if conn else []
    result["execution_results"] = results
    result["execution_summary"] = _execution_summary(results)
    return result

def _test_case_generation_response(row):
    result = row_dict(row); result["contract"] = json.loads(result.pop("contract_json")); return result

def _audit_response(row):
    result = row_dict(row)
    for key in list(result):
        if key.endswith("_json"):
            value = result.pop(key)
            result[key[:-5]] = json.loads(value or "[]")
    return result

@app.on_event("startup")
def startup(): warm_connection_pool()

@app.on_event("shutdown")
def shutdown(): close_connection_pool()

@app.get("/health")
def health(): return {"status": "ok", "service": "source-package"}

@app.post("/api/v1/projects", status_code=201)
def create_project(request: ProjectRequest):
    project_id = uid("prj")
    with connection() as conn: conn.execute("INSERT INTO project (project_id,project_name,created_at) VALUES (%s,%s,%s)", (project_id, request.name.strip(), now()))
    return {"project_id": project_id, "project_name": request.name.strip()}

@app.get("/api/v1/projects")
def list_projects():
    with connection() as conn:
        return [row_dict(x) for x in conn.execute("SELECT p.project_id,p.project_name,p.created_at,COUNT(s.source_id) AS source_count FROM project p LEFT JOIN source_document s ON s.project_id=p.project_id GROUP BY p.project_id ORDER BY p.created_at DESC").fetchall()]

@app.get("/api/v1/projects/{project_id}")
def get_project(project_id: str):
    with connection() as conn:
        project = conn.execute("SELECT project_id,project_name,created_at FROM project WHERE project_id=%s", (project_id,)).fetchone()
        if not project:
            raise HTTPException(404, "Project not found")
        return row_dict(project)

@app.post("/api/v1/projects/{project_id}/sources")
async def upload_source(project_id: str, file: UploadFile = File(...), source_type: str = Form(...), owner: str | None = Form(None), owner_version: str | None = Form(None), brd_source_id: str | None = Form(None), required_sections: str | None = Form(None), background_tasks: BackgroundTasks = None):
    if source_type not in SOURCE_TYPES: raise HTTPException(422, "Unsupported source type")
    if source_type != "brd" and not brd_source_id: raise HTTPException(422, "Supporting sources must reference a BRD source")
    content = await read_upload_with_limit(file)
    init_db()
    with connection() as conn:
        if not conn.execute("SELECT project_id FROM project WHERE project_id=%s", (project_id,)).fetchone():
            raise HTTPException(404, "Project not found")
        if source_type != "brd":
            brd = conn.execute("SELECT * FROM source_document WHERE source_id=%s AND project_id=%s", (brd_source_id, project_id)).fetchone()
            if not brd: raise HTTPException(422, "Associated BRD was not found in this project")
            if brd["source_type"] != "brd": raise HTTPException(422, "Associated source must be a BRD")
            if not brd["active_revision_id"]: raise HTTPException(422, "Associated BRD has no usable active revision")
            brd_revision = conn.execute("SELECT status FROM source_revision WHERE revision_id=%s", (brd["active_revision_id"],)).fetchone()
            brd_extraction = conn.execute("SELECT status FROM source_extraction WHERE revision_id=%s ORDER BY created_at DESC LIMIT 1", (brd["active_revision_id"],)).fetchone()
            if not brd_revision or brd_revision["status"] in {"rejected", "extraction_failed"} or not brd_extraction or brd_extraction["status"] != "succeeded":
                raise HTTPException(422, "Associated BRD must finish extraction successfully before adding supporting sources")
    try: detected_type, messages = validate_file_content(file.filename or "source", content, file.content_type)
    except ValueError as exc:
        source_id, revision_id = uid("src"), uid("rev"); path = UPLOAD_DIR / f"{revision_id}_{Path(file.filename or 'source').name}"; path.write_bytes(content)
        with connection() as conn:
            existing = conn.execute("SELECT * FROM source_document WHERE project_id=%s AND source_type=%s AND logical_name=%s", (project_id, source_type, file.filename or "source")).fetchone()
            if existing:
                source_id = existing["source_id"]
                revision = (conn.execute("SELECT COALESCE(MAX(revision),0)+1 AS next_revision FROM source_revision WHERE source_id=%s", (source_id,)).fetchone()["next_revision"])
            else:
                revision = 1
                conn.execute("INSERT INTO source_document (source_id,logical_source_id,project_id,source_type,logical_name,owner_name,active_revision_id,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", (source_id, source_id, project_id, source_type, file.filename or "source", owner, None, now(), now()))
            conn.execute("INSERT INTO source_revision (revision_id,source_id,revision,filename,content_type,size_bytes,sha256,storage_path,owner_version,ingestion_version,status,validation_messages,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (revision_id, source_id, revision, file.filename or "source", file.content_type, len(content), hashlib.sha256(content).hexdigest(), str(path), owner_version, "source-intake-v1", "rejected", json.dumps([str(exc)]), now()))
        return JSONResponse(status_code=415 if "Unsupported document format" in str(exc) else 422, content={"status": "rejected", "revision_id": revision_id, "source_id": source_id, "validation_messages": [str(exc)], "original_preserved": True})
    digest = hashlib.sha256(content).hexdigest(); filename = file.filename or "source"
    with connection() as conn:
        existing = conn.execute("SELECT s.*,r.* FROM source_document s JOIN source_revision r ON r.source_id=s.source_id WHERE s.project_id=%s AND s.source_type=%s AND s.logical_name=%s ORDER BY r.revision DESC", (project_id, source_type, filename)).fetchall()
        # Content identity is project-scoped and independent of the physical
        # filename. A renamed upload should remain traceable to its canonical
        # logical source instead of creating a second package source.
        if not existing:
            renamed = conn.execute("SELECT s.*,r.* FROM source_document s JOIN source_revision r ON r.source_id=s.source_id WHERE s.project_id=%s AND s.source_type=%s AND r.sha256=%s ORDER BY r.revision DESC LIMIT 1", (project_id, source_type, digest)).fetchall()
            if renamed:
                existing = conn.execute("SELECT s.*,r.* FROM source_document s JOIN source_revision r ON r.source_id=s.source_id WHERE s.source_id=%s ORDER BY r.revision DESC", (renamed[0]["source_id"],)).fetchall()
        if not existing and source_type == "brd":
            # A project has one primary BRD source. A changed filename must
            # still create a revision of that source, not a second BRD.
            existing = conn.execute("SELECT s.*,r.* FROM source_document s JOIN source_revision r ON r.source_id=s.source_id WHERE s.project_id=%s AND s.source_type=%s ORDER BY s.updated_at DESC, r.revision DESC", (project_id, source_type)).fetchall()
        source = existing[0] if existing else None
        source_id = source["source_id"] if source else uid("src")
        same_content_revision = next((item for item in existing if item["sha256"] == digest), None)
        reusable_revision = None
        if same_content_revision:
            previous_extraction = conn.execute(
                "SELECT status FROM source_extraction WHERE revision_id=%s ORDER BY created_at DESC LIMIT 1",
                (same_content_revision["revision_id"],),
            ).fetchone()
            has_fragments = conn.execute(
                "SELECT 1 FROM source_fragment WHERE revision_id=%s LIMIT 1",
                (same_content_revision["revision_id"],),
            ).fetchone()
            if previous_extraction and previous_extraction["status"] == "succeeded" and has_fragments:
                reusable_revision = same_content_revision
        if source and reusable_revision:
            reused_revision = reusable_revision
            reused_revision_id = reused_revision["revision_id"]
            revision_id = uid("rev"); revision = max(x["revision"] for x in existing) + 1
            conn.execute("INSERT INTO source_revision (revision_id,source_id,revision,filename,content_type,size_bytes,sha256,storage_path,owner_version,ingestion_version,status,validation_messages,duplicate_of_revision_id,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (revision_id, source_id, revision, filename, detected_type, len(content), digest, reused_revision["storage_path"], owner_version, "source-intake-v1", "uploaded", json.dumps(messages), reused_revision_id, now()))
            conn.execute("INSERT INTO source_extraction (extraction_id,revision_id,mode,status,extracted_text,error,extractor_version,model_name,prompt_version,config_version,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (uid("ext"), revision_id, "reused", "skipped", None, reused_revision_id, LOCAL_EXTRACTOR_VERSION, None, EXTRACTION_PROMPT_VERSION, EXTRACTION_CONFIG_VERSION, now()))
            return JSONResponse(status_code=201, content={"status": "duplicate", "source_id": source_id, "revision_id": revision_id, "active_revision_id": reused_revision_id, "duplicate_revision_id": revision_id, "extraction": {"status": "skipped", "mode": "reused"}, "extraction_summary": {"status": "succeeded", "extraction_mode": "reused", "reused_revision_id": reused_revision_id}})
        revision_id, path = uid("rev"), UPLOAD_DIR / f"{uid('file')}_{Path(filename).name}"; path.write_bytes(content)
        revision = (existing[0]["revision"] + 1) if existing else 1
        if not source: conn.execute("INSERT INTO source_document (source_id,logical_source_id,project_id,source_type,logical_name,owner_name,active_revision_id,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", (source_id, source_id, project_id, source_type, filename, owner, revision_id, now(), now()))
        else: conn.execute("UPDATE source_document SET active_revision_id=%s,updated_at=%s WHERE source_id=%s", (revision_id, now(), source_id))
        conn.execute("INSERT INTO source_revision (revision_id,source_id,revision,filename,content_type,size_bytes,sha256,storage_path,owner_version,ingestion_version,status,validation_messages,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (revision_id, source_id, revision, filename, detected_type, len(content), digest, str(path), owner_version, "source-intake-v1", "pending", json.dumps(messages), now()))
        conn.execute("INSERT INTO source_extraction (extraction_id,revision_id,mode,status,extracted_text,error,extractor_version,model_name,prompt_version,config_version,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (uid("ext"), revision_id, "pending", "pending", None, None, LOCAL_EXTRACTOR_VERSION, None, EXTRACTION_PROMPT_VERSION, EXTRACTION_CONFIG_VERSION, now()))
        if brd_source_id: conn.execute("INSERT INTO source_relationship (relationship_id,brd_source_id,supporting_source_id,relationship_type,created_at) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (brd_source_id,supporting_source_id) DO NOTHING", (uid("rel"), brd_source_id, source_id, "supports", now()))
        extraction_id = conn.execute("SELECT extraction_id FROM source_extraction WHERE revision_id=%s", (revision_id,)).fetchone()["extraction_id"]
    expected_sections = [x.strip() for x in (required_sections or ",".join(REQUIRED_BRD_SECTIONS)).split(",") if x.strip()] if source_type == "brd" else []
    if background_tasks is not None: background_tasks.add_task(process_extraction, revision_id, source_id, extraction_id, str(path), filename, expected_sections)
    return JSONResponse(status_code=202, content={"status": "accepted", "active": True, "source_id": source_id, "revision_id": revision_id, "revision": revision, "sha256": digest, "validation_messages": messages, "extraction": {"status": "pending"}})

@app.get("/api/v1/projects/{project_id}/sources")
def list_sources(project_id: str):
    with connection() as conn:
        sources = conn.execute("SELECT * FROM source_document WHERE project_id=%s ORDER BY created_at", (project_id,)).fetchall(); result=[]
        for source in sources:
            item = row_dict(source)
            active = conn.execute("SELECT * FROM source_revision WHERE revision_id=%s", (source["active_revision_id"],)).fetchone() if source["active_revision_id"] else None
            latest = active or conn.execute("SELECT * FROM source_revision WHERE source_id=%s ORDER BY revision DESC LIMIT 1", (source["source_id"],)).fetchone()
            item["active_revision"] = row_dict(active)
            item["latest_revision"] = row_dict(latest)
            item["status"] = latest["status"] if latest else "unknown"
            item["validation_messages"] = json.loads((latest["validation_messages"] if latest else "[]") or "[]")
            item["extraction"] = row_dict(conn.execute("SELECT status,mode,error,model_name FROM source_extraction WHERE revision_id=%s ORDER BY created_at DESC LIMIT 1", (active["revision_id"] if active else (latest["revision_id"] if latest else ""),)).fetchone()) if latest else None
            if item["source_type"] == "brd" and item["extraction"] and item["extraction"]["status"] == "succeeded":
                has_fragments = conn.execute("SELECT 1 FROM source_fragment WHERE revision_id=%s LIMIT 1", (latest["revision_id"],)).fetchone()
                if not has_fragments:
                    item["status"] = "extraction_failed"
                    item["validation_messages"].append("The BRD extraction produced no readable content")
            item["supporting_sources"] = [row_dict(x) for x in conn.execute("SELECT s.source_id,s.source_type,s.logical_name FROM source_relationship r JOIN source_document s ON s.source_id=r.supporting_source_id WHERE r.brd_source_id=%s", (source["source_id"],)).fetchall()]
            result.append(item)
        return result

@app.get("/api/v1/revisions/{revision_id}")
def get_revision(revision_id: str):
    with connection() as conn:
        row=conn.execute("SELECT r.*,s.source_type,s.logical_name FROM source_revision r JOIN source_document s ON s.source_id=r.source_id WHERE r.revision_id=%s", (revision_id,)).fetchone()
        if not row: raise HTTPException(404,"Revision not found")
        result=row_dict(row); extraction=conn.execute("SELECT * FROM source_extraction WHERE revision_id=%s ORDER BY created_at DESC LIMIT 1",(revision_id,)).fetchone(); result["extraction"]=row_dict(extraction); result["source"]={"source_id":row["source_id"],"source_type":row["source_type"],"logical_name":row["logical_name"]}; result["validation_messages"]=json.loads(row["validation_messages"] or "[]"); result["fragments"]=[row_dict(x) for x in conn.execute("SELECT * FROM source_fragment WHERE revision_id=%s ORDER BY fragment_index",(revision_id,)).fetchall()]; missing=[m.split(": ",1)[1] for m in result["validation_messages"] if m.startswith("Missing expected section: ")]; summary={"status":extraction["status"] if extraction else None,"extraction_mode":extraction["mode"] if extraction else None,"text_available":bool(extraction and extraction["extracted_text"]),"section_count":len(result["fragments"]),"missing_sections":missing};
        if extraction and extraction["mode"] == "reused": summary["reused_revision_id"] = extraction["error"]
        result["extraction_summary"]=summary; return result

@app.get("/api/v1/sources/{source_id}/revisions")
def list_source_revisions(source_id: str):
    with connection() as conn:
        source = conn.execute("SELECT * FROM source_document WHERE source_id=%s", (source_id,)).fetchone()
        if not source: raise HTTPException(404, "Source not found")
        revisions = conn.execute("SELECT r.*,e.mode,e.status AS extraction_status,e.model_name,e.prompt_version,e.config_version,e.prompt_hash,e.config_hash,e.extractor_version FROM source_revision r LEFT JOIN source_extraction e ON e.revision_id=r.revision_id WHERE r.source_id=%s ORDER BY r.revision DESC", (source_id,)).fetchall()
        return {"source": row_dict(source), "revisions": [row_dict(revision) for revision in revisions]}

@app.get("/api/v1/sources/{source_id}/parent-brd")
def get_source_parent_brd(source_id: str):
    with connection() as conn:
        source = conn.execute("SELECT * FROM source_document WHERE source_id=%s", (source_id,)).fetchone()
        if not source: raise HTTPException(404, "Source not found")
        parent = conn.execute("SELECT b.* FROM source_relationship rel JOIN source_document b ON b.source_id=rel.brd_source_id WHERE rel.supporting_source_id=%s", (source_id,)).fetchone()
        if not parent: raise HTTPException(404, "No parent BRD is associated with this source")
        return row_dict(parent)

class ActiveRevisionRequest(BaseModel):
    revision_id: str = Field(min_length=1)

@app.post("/api/v1/sources/{source_id}/active-revision")
def select_active_revision(source_id: str, request: ActiveRevisionRequest):
    with connection() as conn:
        source = conn.execute("SELECT * FROM source_document WHERE source_id=%s", (source_id,)).fetchone()
        revision = conn.execute("SELECT r.*,e.status AS extraction_status FROM source_revision r LEFT JOIN source_extraction e ON e.revision_id=r.revision_id WHERE r.source_id=%s AND r.revision_id=%s ORDER BY e.created_at DESC LIMIT 1", (source_id, request.revision_id)).fetchone()
        if not source: raise HTTPException(404, "Source not found")
        if not revision: raise HTTPException(404, "Revision not found for this source")
        if revision["status"] in {"rejected", "extraction_failed", "pending", "processing"} or revision["extraction_status"] != "succeeded":
            raise HTTPException(422, "Only a successfully extracted revision can be made active")
        conn.execute("UPDATE source_document SET active_revision_id=%s,updated_at=%s WHERE source_id=%s", (request.revision_id, now(), source_id))
        return {"source_id": source_id, "active_revision_id": request.revision_id}

@app.get("/api/v1/revisions/{revision_id}/compare/{other_revision_id}")
def compare_revisions(revision_id: str, other_revision_id: str):
    with connection() as conn:
        revisions = conn.execute("SELECT r.*,s.source_id,s.logical_name,s.source_type FROM source_revision r JOIN source_document s ON s.source_id=r.source_id WHERE r.revision_id IN (%s,%s)", (revision_id, other_revision_id)).fetchall()
        by_id = {row["revision_id"]: row for row in revisions}
        if revision_id not in by_id or other_revision_id not in by_id: raise HTTPException(404, "One or both revisions were not found")
        def details(row):
            extraction = conn.execute("SELECT status,mode,extracted_text FROM source_extraction WHERE revision_id=%s ORDER BY created_at DESC LIMIT 1", (row["revision_id"],)).fetchone()
            return {"revision_id": row["revision_id"], "source_id": row["source_id"], "logical_name": row["logical_name"], "revision": row["revision"], "filename": row["filename"], "sha256": row["sha256"], "status": row["status"], "extraction_status": extraction["status"] if extraction else None, "text": extraction["extracted_text"] if extraction else None}
        left, right = details(by_id[revision_id]), details(by_id[other_revision_id])
        diff = list(difflib.unified_diff((left["text"] or "").splitlines(), (right["text"] or "").splitlines(), fromfile=revision_id, tofile=other_revision_id, lineterm=""))
        return {"from": left, "to": right, "changed": left["sha256"] != right["sha256"], "same_source": left["source_id"] == right["source_id"], "unified_diff": diff}

@app.get("/api/v1/ingestion-runs/{run_id}")
def get_ingestion_run(run_id: str):
    with connection() as conn:
        run = conn.execute("SELECT * FROM source_ingestion_run WHERE run_id=%s", (run_id,)).fetchone()
        if not run: raise HTTPException(404, "Ingestion run not found")
        result = row_dict(run)
        result["configuration_snapshot"] = json.loads(result["configuration_snapshot"] or "{}")
        return result

class ManifestLifecycleRequest(BaseModel):
    status: str = Field(pattern="^(ready|invalidated|published)$")

@app.post("/api/v1/manifests/{package_id}/lifecycle")
def update_manifest_lifecycle(package_id: str, request: ManifestLifecycleRequest):
    with connection() as conn:
        manifest = conn.execute("SELECT * FROM source_package_manifest WHERE package_id=%s", (package_id,)).fetchone()
        if not manifest: raise HTTPException(404, "Package manifest not found")
        current = manifest["lifecycle_status"] or "draft"
        allowed = {"draft": {"ready", "invalidated"}, "ready": {"published", "invalidated"}, "invalidated": {"ready"}, "published": set()}
        if request.status not in allowed.get(current, set()):
            raise HTTPException(409, f"Cannot change manifest lifecycle from {current} to {request.status}")
        conn.execute("UPDATE source_package_manifest SET lifecycle_status=%s WHERE package_id=%s", (request.status, package_id))
        return {"package_id": package_id, "lifecycle_status": request.status}

@app.post("/api/v1/projects/{project_id}/manifests", status_code=201)
def create_manifest(project_id: str, request: ManifestRequest, background_tasks: BackgroundTasks):
    with connection() as conn:
        if not conn.execute("SELECT project_id FROM project WHERE project_id=%s", (project_id,)).fetchone(): raise HTTPException(404, "Project not found")
        source_ids = list(dict.fromkeys(request.source_ids))
        if not source_ids: raise HTTPException(422, "At least one source is required")
        sources = []
        for sid in source_ids:
            source = conn.execute("SELECT * FROM source_document WHERE source_id=%s AND project_id=%s", (sid, project_id)).fetchone()
            if not source: raise HTTPException(404, "Source not found")
            if not source["active_revision_id"]: raise HTTPException(422, "Source has no usable active revision")
            sources.append(source)
        brds = [source for source in sources if source["source_type"] == "brd"]
        if len(brds) != 1: raise HTTPException(422, "A source package must contain exactly one BRD")
        resolved = []
        for source in sources:
            revision = conn.execute("SELECT r.*,e.extraction_id,e.mode,e.status AS extraction_status,e.extracted_text,e.error,e.model_name,e.prompt_version,e.config_version,e.prompt_hash,e.config_hash,e.extractor_version FROM source_revision r LEFT JOIN source_extraction e ON e.revision_id=r.revision_id WHERE r.revision_id=%s ORDER BY e.created_at DESC LIMIT 1", (source["active_revision_id"],)).fetchone()
            if not revision: raise HTTPException(422, "Source revision metadata is incomplete")
            if revision["extraction_status"] == "skipped" and revision["mode"] == "reused" and revision["error"]:
                canonical = conn.execute("SELECT r.*,e.extraction_id,e.mode,e.status AS extraction_status,e.error,e.model_name,e.prompt_version,e.config_version,e.prompt_hash,e.config_hash,e.extractor_version FROM source_revision r LEFT JOIN source_extraction e ON e.revision_id=r.revision_id WHERE r.revision_id=%s", (revision["error"],)).fetchone()
                revision = canonical or revision
            if revision["extraction_status"] != "succeeded" or revision["status"] in {"rejected", "extraction_failed", "processing", "pending"}:
                raise HTTPException(422, f"Source '{source['logical_name']}' is not ready for packaging")
            if source["source_type"] == "brd":
                has_fragments = conn.execute("SELECT 1 FROM source_fragment WHERE revision_id=%s LIMIT 1", (revision["revision_id"],)).fetchone()
                if not (revision["extracted_text"] or "").strip() or not has_fragments:
                    raise HTTPException(422, f"BRD '{source['logical_name']}' contains no readable extracted content")
            if source["source_type"] != "brd":
                placeholders = ",".join(["%s"] * len(brds))
                relation = conn.execute(f"SELECT 1 FROM source_relationship WHERE supporting_source_id=%s AND brd_source_id IN ({placeholders})", tuple([source["source_id"]] + [brd["source_id"] for brd in brds])).fetchone()
                if not relation: raise HTTPException(422, f"Supporting source '{source['logical_name']}' is not linked to a BRD in this package")
            resolved.append((source, revision))
        manifest_payload = sorted(([{"revision_id": revision["revision_id"], "content_hash": revision["sha256"]} for _, revision in resolved]), key=lambda item: item["revision_id"])
        manifest_hash = hashlib.sha256(json.dumps(manifest_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        package_name = (request.package_name or f"{request.scope or 'Source'} package").strip()
        if not package_name: raise HTTPException(422, "Package name cannot be empty")
        # PostgreSQL does not allow FOR UPDATE with aggregate functions. Lock
        # the owning project row first so package-version allocation remains
        # serialized for this project, then calculate MAX safely.
        if DATABASE_URL:
            conn.execute("SELECT project_id FROM project WHERE project_id=%s FOR UPDATE", (project_id,)).fetchone()
        version_query = "SELECT COALESCE(MAX(package_version),0)+1 AS next_version FROM source_package_manifest WHERE project_id=%s"
        version = conn.execute(version_query, (project_id,)).fetchone()["next_version"]
        package_id, run_id, timestamp = uid("pkg"), uid("run"), now()
        configuration_snapshot = json.dumps({"extractor_version": LOCAL_EXTRACTOR_VERSION, "prompt_version": EXTRACTION_PROMPT_VERSION, "prompt_hash": extraction_prompt_hash(), "config_version": EXTRACTION_CONFIG_VERSION, "config_hash": extraction_config_hash()}, sort_keys=True)
        conn.execute("INSERT INTO source_ingestion_run (run_id,project_id,package_id,status,configuration_snapshot,created_at) VALUES (%s,%s,%s,%s,%s,%s)", (run_id, project_id, package_id, "succeeded", configuration_snapshot, timestamp))
        conn.execute("INSERT INTO source_package_manifest (package_id,project_id,package_version,package_name,scope,manifest_hash,lifecycle_status,run_id,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", (package_id, project_id, version, package_name, request.scope, manifest_hash, "ready", run_id, timestamp))
        items = []
        for source, revision in resolved:
            conn.execute("INSERT INTO source_package_item (package_id,source_id,revision_id,source_version,ingestion_status,ingestion_version,content_hash,model_name,prompt_version,config_version,extractor_version,prompt_hash,config_hash,duplicate_of_revision_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (package_id, source["source_id"], revision["revision_id"], revision["owner_version"], revision["extraction_status"], revision["ingestion_version"], revision["sha256"], revision["model_name"], revision["prompt_version"], revision["config_version"], revision["extractor_version"], revision["prompt_hash"], revision["config_hash"], revision["duplicate_of_revision_id"]))
            items.append({"source_id": source["source_id"], "revision_id": revision["revision_id"], "ingestion_status": revision["extraction_status"], "content_hash": revision["sha256"], "model_name": revision["model_name"], "prompt_version": revision["prompt_version"], "config_version": revision["config_version"], "extractor_version": revision["extractor_version"], "prompt_hash": revision["prompt_hash"], "config_hash": revision["config_hash"]})
        _ensure_package_business_rules(conn, package_id)
        _refresh_package_business_rule_summary(conn, package_id)
        return {"package_id": package_id, "project_id": project_id, "package_version": version, "package_name": package_name, "scope": request.scope, "manifest_hash": manifest_hash, "lifecycle_status": "ready", "run_id": run_id, "knowledge_build_status": "awaiting_rule_review", "items": items}

@app.get("/api/v1/projects/{project_id}/manifests")
def list_manifests(project_id: str):
    with connection() as conn: return [row_dict(x) for x in conn.execute("SELECT m.*,COUNT(i.source_id) AS source_count FROM source_package_manifest m LEFT JOIN source_package_item i ON i.package_id=m.package_id WHERE m.project_id=%s GROUP BY m.package_id ORDER BY m.package_version DESC",(project_id,)).fetchall()]

@app.get("/api/v1/manifests/{package_id}")
def get_package(package_id: str):
    with connection() as conn:
        m=conn.execute("SELECT * FROM source_package_manifest WHERE package_id=%s",(package_id,)).fetchone()
        if not m: raise HTTPException(404,"Package manifest not found")
        items=conn.execute("SELECT mi.*,s.logical_name,s.source_type,r.revision,r.sha256,r.owner_version FROM source_package_item mi JOIN source_document s ON s.source_id=mi.source_id JOIN source_revision r ON r.revision_id=mi.revision_id WHERE mi.package_id=%s",(package_id,)).fetchall(); return {**row_dict(m),"items":[row_dict(x) for x in items]}

@app.get("/api/v1/packages/{package_id}/business-rules")
def list_package_business_rules(package_id: str):
    with connection() as conn:
        return _package_business_rule_response(conn, package_id)

@app.post("/api/v1/packages/{package_id}/business-rules/review")
def review_package_business_rules(package_id: str, request: BusinessRuleReviewRequest):
    with connection() as conn:
        if conn.execute("SELECT 1 FROM knowledge_index WHERE package_id=%s LIMIT 1", (package_id,)).fetchone():
            raise HTTPException(409, "Clear the existing Knowledge Base index before changing package business rules")
        _ensure_package_business_rules(conn, package_id)
        existing = {row["rule_id"]: row_dict(row) for row in conn.execute("SELECT * FROM package_business_rule WHERE package_id=%s", (package_id,)).fetchall()}
        submitted = {item.rule_id for item in request.rules if not item.rule_id.startswith("new-")}
        if set(existing) != submitted:
            raise HTTPException(422, {"missing_rules": sorted(set(existing) - submitted), "unknown_rules": sorted(submitted - set(existing))})
        timestamp = now()
        next_ordinal = max((rule["ordinal"] for rule in existing.values()), default=0)
        seen_statements = set()
        for item in request.rules:
            statement = " ".join(item.statement.split())
            if statement.casefold() in seen_statements:
                raise HTTPException(422, "Business rules must be unique")
            seen_statements.add(statement.casefold())
            if item.rule_id.startswith("new-"):
                next_ordinal += 1
                conn.execute("INSERT INTO package_business_rule (rule_id,package_id,ordinal,statement,source_reference,impact_if_missing,status,reviewer,review_comment,reviewed_at,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (uid("rule"), package_id, next_ordinal, statement, item.source_reference or "Reviewer-added business rule", item.impact_if_missing.strip(), item.decision, request.reviewer.strip(), item.review_comment, timestamp, timestamp, timestamp))
            else:
                rule = existing[item.rule_id]
                conn.execute("UPDATE package_business_rule SET statement=%s,impact_if_missing=%s,status=%s,reviewer=%s,review_comment=%s,reviewed_at=%s,updated_at=%s WHERE rule_id=%s AND package_id=%s",
                    (statement, item.impact_if_missing.strip(), item.decision, request.reviewer.strip(), item.review_comment, timestamp, timestamp, item.rule_id, package_id))
        rules = [row_dict(row) for row in conn.execute("SELECT * FROM package_business_rule WHERE package_id=%s ORDER BY ordinal", (package_id,)).fetchall()]
        approved = [rule for rule in rules if rule["status"] == "approved"]
        review_complete = bool(approved) and not any(rule["status"] == "pending" for rule in rules)
        rule_set_hash = hashlib.sha256(json.dumps([{"rule_id": rule["rule_id"], "statement": rule["statement"], "status": rule["status"], "source_reference": rule["source_reference"]} for rule in rules], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        conn.execute("UPDATE source_package_manifest SET rule_review_status=%s,rule_set_hash=%s,rules_reviewed_at=%s WHERE package_id=%s",
            ("approved" if review_complete else "pending", rule_set_hash if review_complete else None, timestamp if review_complete else None, package_id))
        return _package_business_rule_response(conn, package_id)

@app.get("/api/v1/projects/{project_id}/discovery-contexts")
def list_discovery_contexts(project_id: str, package_id: str | None = None):
    with connection() as conn:
        if not conn.execute("SELECT project_id FROM project WHERE project_id=%s", (project_id,)).fetchone():
            raise HTTPException(404, "Project not found")
        if package_id:
            package = conn.execute("SELECT project_id FROM source_package_manifest WHERE package_id=%s", (package_id,)).fetchone()
            if not package or package["project_id"] != project_id:
                raise HTTPException(404, "Package manifest not found for this project")
        return _discovery_contexts_for_project(conn, project_id, package_id)

@app.post("/api/v1/projects/{project_id}/discovery-contexts", status_code=201)
def create_discovery_context(project_id: str, request: DiscoveryContextRequest):
    with connection() as conn:
        if not conn.execute("SELECT project_id FROM project WHERE project_id=%s", (project_id,)).fetchone():
            raise HTTPException(404, "Project not found")
        if request.package_id:
            package = conn.execute("SELECT project_id FROM source_package_manifest WHERE package_id=%s", (request.package_id,)).fetchone()
            if not package or package["project_id"] != project_id:
                raise HTTPException(404, "Package manifest not found for this project")
        timestamp = now()
        context_id = uid("ctx")
        conn.execute("INSERT INTO discovery_context (context_id,project_id,package_id,context_type,name,description,details_json,created_by,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (context_id, project_id, request.package_id, request.context_type, request.name.strip(), request.description, json.dumps(request.details or {}), request.created_by, timestamp, timestamp))
        return _discovery_context_response(conn.execute("SELECT * FROM discovery_context WHERE context_id=%s", (context_id,)).fetchone())

@app.delete("/api/v1/discovery-contexts/{context_id}")
def delete_discovery_context(context_id: str):
    with connection() as conn:
        row = conn.execute("SELECT context_id FROM discovery_context WHERE context_id=%s", (context_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Discovery context not found")
        conn.execute("DELETE FROM discovery_context WHERE context_id=%s", (context_id,))
        return {"deleted": True, "context_id": context_id}

def _business_requirements_schema():
    source_reference = {"type": "object", "additionalProperties": False, "properties": {"fragment_id": {"type": "string"}, "source_reference": {"type": "string"}}, "required": ["fragment_id", "source_reference"]}
    references = {"type": "array", "items": source_reference, "minItems": 1}
    item = {"type": "object", "additionalProperties": False, "properties": {
        "business_requirement_id": {"type": "string", "pattern": "^BR-[0-9]{3}$"},
        "description": {"type": "string"},
        "priority": {"type": "string"},
        "business_use_case": {"type": "string"},
        "data_entities": {"type": "array", "items": {"type": "string"}},
        "source_references": references,
        "supporting_source_references": {"type": "array", "items": source_reference},
    }, "required": ["business_requirement_id", "description", "priority", "business_use_case", "data_entities", "source_references", "supporting_source_references"]}
    return {"type": "object", "additionalProperties": False, "properties": {
        "business_requirements": {"type": "array", "minItems": 1, "items": item},
        "global_gaps": {"type": "array", "items": {"type": "string"}},
        "global_assumptions": {"type": "array", "items": {"type": "string"}},
    }, "required": ["business_requirements", "global_gaps", "global_assumptions"]}

def _business_requirement_prompt_payload(package, items, fragments, brd_text_limit: int = 6_000, supporting_text_limit: int = 1_200, citation_anchor_limit: int = 80):
    item_by_source = {item.get("source_id"): item for item in items}
    source_documents = []
    for item in items:
        source_type = item.get("source_type")
        full_text = " ".join(str(item.get("extracted_text") or "").split())
        text_limit = brd_text_limit if source_type == "brd" else supporting_text_limit
        source_documents.append({
            "source_id": item.get("source_id"),
            "revision_id": item.get("revision_id"),
            "source_type": source_type,
            "logical_name": item.get("logical_name"),
            "filename": item.get("filename"),
            "role": "business_scope" if source_type == "brd" else "supporting_context",
            "full_text": full_text[:text_limit],
            "full_text_truncated": len(full_text) > text_limit,
            "source_text_length": len(full_text),
        })
    return {
        "package": {"package_id": package.get("package_id"), "package_name": package.get("package_name"), "scope": package.get("scope")},
        "source_documents": source_documents,
        "citation_anchors": [
            {
                "fragment_id": fragment.get("fragment_id"),
                "source_reference": fragment.get("source_reference"),
                "source_id": fragment.get("source_id"),
                "source_type": item_by_source.get(fragment.get("source_id"), {}).get("source_type"),
                "heading": fragment.get("heading"),
            }
            for fragment in fragments[:citation_anchor_limit] if fragment.get("fragment_id")
        ],
    }

def _validate_business_requirements_contract(contract, fragments):
    if not isinstance(contract, dict) or not contract.get("business_requirements"):
        raise ValueError("The AI returned no business requirements")
    fragments_by_id = {fragment.get("fragment_id"): fragment for fragment in fragments if fragment.get("fragment_id")}
    ordered_fragments = [fragment for fragment in fragments if fragment.get("fragment_id")]
    fragments_by_reference = {}
    for fragment in ordered_fragments:
        fragments_by_reference.setdefault(fragment.get("source_reference"), []).append(fragment)

    def best_fragment_for(text):
        if not ordered_fragments:
            return None
        text_tokens = _business_rule_tokens(text)
        best_fragment = ordered_fragments[0]
        best_score = 0.0
        for fragment in ordered_fragments:
            score = _business_rule_similarity(text_tokens, _business_rule_tokens(fragment.get("fragment_text")))
            if score > best_score:
                best_score = score
                best_fragment = fragment
        return best_fragment

    def normalize_ref(ref, text):
        fragment = fragments_by_id.get(ref.get("fragment_id"))
        if fragment is None:
            matches = fragments_by_reference.get(ref.get("source_reference"), [])
            fragment = matches[0] if matches else None
        if fragment is None:
            source_reference = str(ref.get("source_reference") or "")
            for candidate in ordered_fragments:
                candidate_reference = str(candidate.get("source_reference") or "")
                if source_reference and candidate_reference and (source_reference in candidate_reference or candidate_reference in source_reference):
                    fragment = candidate
                    break
        if fragment is None:
            fragment = best_fragment_for(text)
        if fragment is None:
            raise ValueError("The source package has no traceable fragments")
        ref["fragment_id"] = fragment["fragment_id"]
        ref["source_reference"] = fragment["source_reference"]

    seen = set()
    for item in contract.get("business_requirements", []):
        business_id = item.get("business_requirement_id")
        description = str(item.get("description") or "").strip()
        if not business_id or business_id in seen or not description:
            raise ValueError("The AI returned an invalid or duplicate business requirement")
        seen.add(business_id)
        refs = item.get("source_references") or []
        if not refs:
            fallback = best_fragment_for(description)
            if not fallback:
                raise ValueError(f"AI returned no source references for {business_id}")
            refs = [{"fragment_id": fallback["fragment_id"], "source_reference": fallback["source_reference"]}]
            item["source_references"] = refs
        for ref in [*refs, *(item.get("supporting_source_references") or [])]:
            normalize_ref(ref, description)

def _dedupe_business_requirement_items(items):
    deduped = []
    for item in items:
        text = str(item.get("description") or item.get("text") or "").strip()
        if not text:
            continue
        tokens = _business_rule_tokens(text)
        duplicate = None
        for existing in deduped:
            if _business_rule_similarity(tokens, existing["_tokens"]) >= 0.72:
                duplicate = existing
                break
        if duplicate:
            duplicate["source_references"] = _merge_source_references(duplicate.get("source_references"), item.get("source_references"))
            duplicate["supporting_source_references"] = _merge_source_references(duplicate.get("supporting_source_references"), item.get("supporting_source_references"))
            duplicate["data_entities"] = sorted(set((duplicate.get("data_entities") or []) + (item.get("data_entities") or [])))
            continue
        next_item = dict(item)
        next_item["description"] = text
        next_item["source_references"] = _merge_source_references(item.get("source_references"))
        next_item["supporting_source_references"] = _merge_source_references(item.get("supporting_source_references"))
        next_item["data_entities"] = list(dict.fromkeys(item.get("data_entities") or []))
        next_item["_tokens"] = tokens
        deduped.append(next_item)
    for index, item in enumerate(deduped, 1):
        item["business_requirement_id"] = f"BR-{index:03d}"
        item.pop("_tokens", None)
    return deduped

def _business_requirements_to_review_contract(package, items, business_items, mode, model, note=None):
    business_items = _dedupe_business_requirement_items(business_items)
    source_refs = _merge_source_references(*(item.get("source_references") for item in business_items))
    if not source_refs and business_items:
        source_refs = business_items[0].get("source_references", [])
    rules = [
        {
            "rule_id": item["business_requirement_id"],
            "package_rule_id": _package_rule_id(package["package_id"], item["business_requirement_id"]),
            "text": item["description"],
            "business_use_case": item.get("business_use_case") or item["description"],
            "priority": item.get("priority") or "Must have",
            "data_entities": item.get("data_entities") or [],
            "source_references": _merge_source_references(item.get("source_references"), item.get("supporting_source_references")),
            "approved": False,
            "review_status": "pending",
        }
        for item in business_items
    ]
    contract = {
        "contract_version": "1.0",
        "package_id": package["package_id"],
        "project_id": package["project_id"],
        "package_scope": package.get("scope"),
        "business_requirements_generation": True,
        "requirements": [{
            "requirement_id": "BRD-001",
            "title": "Business requirement scope",
            "type": "Business",
            "statement": "Review the deduplicated business use cases extracted from the BRD and supporting source package.",
            "priority": "Must have",
            "actors": [],
            "entities": sorted({entity for item in business_items for entity in item.get("data_entities", [])}),
            "business_rules": rules,
            "acceptance_criteria": [{"acceptance_id": "AC-001", "given": "A source package has been created.", "when": "Business requirements are reviewed.", "then": "Approved business requirements define the scope for Functional Requirements.", "source_references": source_refs or []}],
            "flow_steps": ["Extract BRD business intent", "Consolidate duplicates", "Review and approve business scope"],
            "ambiguities": [],
            "assumptions": [],
            "source_references": source_refs or [],
        }],
        "business_requirements": business_items,
        "global_gaps": [],
        "global_assumptions": [],
        "conflicting_requirements": [],
        "conflicts": [],
        "accepted_assumptions": [],
        "source_package_items": [{"source_id": item["source_id"], "revision_id": item["revision_id"], "source_type": item["source_type"], "logical_name": item["logical_name"], "sha256": item["sha256"]} for item in items],
        "generated_at": now(),
        "generation_mode": mode,
        "generation_model": model,
    }
    if note:
        contract["generation_note"] = note
        contract["global_gaps"].append(note)
    contract["readiness"] = _contract_readiness(contract)
    return contract

def _business_requirements_with_openai(package, items, fragments, rag_context=None):
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not configured")
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), timeout=OPENAI_TIMEOUT_SECONDS)

    use_knowledge_base = bool(rag_context and rag_context.get("brd_fragments"))
    source_type_by_id = {item.get("source_id"): item.get("source_type") for item in items}

    def make_payload(brd_limit, supporting_limit, anchor_limit, evidence_limit, evidence_characters, rule_limit, rule_characters):
        if use_knowledge_base:
            # With retrieval ready, send only retrieved Knowledge Base evidence
            # and anchors for those retrieved fragments.
            retrieved_evidence = _rag_context_for_prompt(
                rag_context, max_evidence=evidence_limit, max_fragment_characters=evidence_characters,
                max_rules=rule_limit, max_rule_characters=rule_characters,
            )
            evidence_ids = {item.get("fragment_id") for item in retrieved_evidence.get("evidence", [])}
            allowed = [item for item in fragments if item.get("fragment_id") in evidence_ids]
            value = {
                "package": {"package_id": package.get("package_id"), "package_name": package.get("package_name"), "scope": package.get("scope")},
                "source_documents": [],
                "citation_anchors": [
                    {"fragment_id": item.get("fragment_id"), "source_reference": item.get("source_reference"),
                     "source_id": item.get("source_id"), "source_type": item.get("source_type"), "heading": item.get("heading")}
                    for item in allowed
                ],
                "retrieved_knowledge_evidence": retrieved_evidence,
            }
            return value, allowed

        # If the Knowledge Base has no usable retrieved evidence, use only the
        # BRD. Supporting documents must not silently replace missing KB evidence.
        brd_items = [item for item in items if item.get("source_type") == "brd"]
        brd_fragments = [item for item in fragments if source_type_by_id.get(item.get("source_id")) == "brd"]
        value = _business_requirement_prompt_payload(
            package, brd_items, brd_fragments, brd_text_limit=brd_limit,
            supporting_text_limit=supporting_limit, citation_anchor_limit=anchor_limit,
        )
        value["retrieved_knowledge_evidence"] = {}
        return value, brd_fragments

    def run_with_limits(brd_limit, supporting_limit, anchor_limit, evidence_limit, evidence_characters, rule_limit, rule_characters):
        payload, allowed_fragments = make_payload(
            brd_limit, supporting_limit, anchor_limit, evidence_limit,
            evidence_characters, rule_limit, rule_characters,
        )
        prompt = (
            "Generate a flat list of distinct Business Use Cases / Business Requirements. "
            + ("Use retrieved_knowledge_evidence from the package Knowledge Base as the only content source. Treat retrieved BRD evidence as authoritative for business scope; use retrieved supporting evidence only to clarify domain terms, and do not expand business scope. "
               if use_knowledge_base else
               "The Knowledge Base has no usable retrieved evidence for this run. Use only the BRD text in source_documents[].full_text; ignore supporting documents. Do not invent requirements beyond the BRD. ")
            + "Cite only source fragment IDs listed in citation_anchors. "
            + ("Put BRD citations in source_references and technical supporting citations in supporting_source_references. "
               if use_knowledge_base else
               "Put all citations in source_references and leave supporting_source_references empty. ")
            + "Do not derive requirements by enumerating citation_anchors; citation_anchors exist only so you can return traceable source IDs. "
            "Do not generate Functional Requirements, acceptance criteria, flow steps, UI behavior, or test cases. "
            "Merge duplicate and near-duplicate business intent within the supplied evidence. "
            "Each item must be a meaningful end-to-end business use case or business decision, not a copied sentence fragment. "
            "Prefer 5 to 12 consolidated requirements for a small BRD; create more only when the BRD has truly separate business outcomes. "
            "Use Must have/Should have/Could have priority. "
            "If a precise anchor is not obvious, choose the nearest anchor from the same source document and heading.\n\nINPUT JSON:\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        response = responses_create(client,
            model=OPENAI_MODEL,
            input=prompt,
            store=False,
            text={"format": {"type": "json_schema", "name": "business_requirements_contract", "strict": True, "schema": _business_requirements_schema()}},
        )
        if not response.output_text:
            raise ValueError("OpenAI returned no Business Requirements")
        contract = json.loads(response.output_text)
        _validate_business_requirements_contract(contract, allowed_fragments)
        return contract["business_requirements"]

    try:
        return run_with_limits(6_000, 1_200, 80, 8, 450, 8, 260)
    except Exception as exc:
        if not _is_context_length_error(exc):
            raise
        return run_with_limits(3_000, 600, 50, 4, 300, 4, 180)

def _business_requirements_with_timeout(package, items, fragments, rag_context=None):
    result, failure = [], []

    def run():
        try:
            result.append(_business_requirements_with_openai(package, items, fragments, rag_context))
        except Exception as exc:
            failure.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(OPENAI_TIMEOUT_SECONDS + 5)
    if result:
        return result[0]
    if failure:
        raise failure[0]
    raise TimeoutError("Business Use Case generation timed out")

def _approved_business_requirements(contract):
    if contract.get("approved_business_requirements"):
        return contract["approved_business_requirements"]
    approved_ids = set(contract.get("approved_items", []))
    result = []
    for requirement in contract.get("requirements", []):
        requirement_id = requirement.get("requirement_id", "")
        for index, rule in enumerate(requirement.get("business_rules", []), 1):
            rule_id = rule.get("rule_id") if isinstance(rule, dict) else f"RULE-{index}"
            item_id = f"{requirement_id}:{rule_id}"
            status = (rule.get("review_status") if isinstance(rule, dict) else None) or ("approved" if item_id in approved_ids else "pending")
            if status != "approved" and item_id not in approved_ids:
                continue
            text = rule if isinstance(rule, str) else rule.get("text", "")
            if text:
                result.append({"business_requirement_id": rule_id, "text": text, "source_references": rule.get("source_references", []) if isinstance(rule, dict) else [], "source_requirement_id": requirement_id})
    return result


def _functional_requirement_schema():
    schema = copy.deepcopy(_requirements_analysis_schema())
    item = schema["properties"]["requirements"]["items"]
    item["properties"]["business_requirement_ids"] = {"type": "array", "minItems": 1, "items": {"type": "string"}}
    item["properties"]["knowledge_context_ids"] = {"type": "array", "items": {"type": "string"}, "description": "Always empty. Cite retrieved Knowledge Base evidence using source_references."}
    item["required"].extend(["business_requirement_ids", "knowledge_context_ids"])
    return schema

def _generate_functional_contract(package, items, fragments, approved_business_requirements, rag_context):
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    approved_ids = {item["business_requirement_id"] for item in approved_business_requirements}
    retrieved_evidence = _rag_context_for_prompt(rag_context, max_evidence=20, max_fragment_characters=700, max_rules=12, max_rule_characters=320)
    if not retrieved_evidence.get("evidence"):
        raise ValueError("The ready Knowledge Base returned no usable evidence for Functional Requirements")
    evidence_ids = {item.get("fragment_id") for item in retrieved_evidence["evidence"]}
    approved_reference_ids = {
        ref.get("fragment_id")
        for business in approved_business_requirements
        for ref in business.get("source_references", [])
        if ref.get("fragment_id")
    }
    allowed_fragments = [item for item in fragments if item.get("fragment_id") in evidence_ids | approved_reference_ids]
    fragment_metadata = {item.get("fragment_id"): item for item in allowed_fragments}
    for business in approved_business_requirements:
        for ref in business.get("source_references", []):
            fragment = fragment_metadata.get(ref.get("fragment_id"))
            if fragment:
                ref.update({field: fragment.get(field) for field in (
                    "fragment_id", "source_id", "revision_id", "source_type", "source_reference",
                    "heading", "content_hash",
                ) if fragment.get(field) is not None})
    from openai import OpenAI
    payload = {
        "approved_business_use_cases": approved_business_requirements,
        "retrieved_knowledge_base_evidence": retrieved_evidence,
    }
    prompt = (
        "Generate Functional Requirements using only the approved_business_use_cases and retrieved_knowledge_base_evidence below. "
        "The approved business use cases define the allowed scope. Use the Knowledge Base to provide the package context, system behavior, entities, rules, and implementation detail needed to make each requirement testable. "
        "Do not use or infer from any other package sources or structured context. Do not expand scope beyond approved use cases. "
        "Every requirement must link to one or more supplied business_requirement_ids and cite only source fragments from the approved use cases or retrieved Knowledge Base evidence. "
        "Set knowledge_context_ids to an empty array; cite Knowledge Base evidence using source_references. Create clear system behaviors, business rules, flow steps, testable acceptance criteria, and at least one observable expected outcome for every requirement. "
        "Each acceptance criterion must include a concise scenario and one or more Given conditions. Report a technical gap only when retrieved technical evidence directly supports it, and copy the exact source_reference into technical_reference. "
        "Do not introduce thresholds, values, or behavior that are absent from the cited evidence. Do not return unapproved business use cases.\n\nINPUT JSON:\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    client = OpenAI(api_key=key, timeout=OPENAI_TIMEOUT_SECONDS)
    previous_output = None
    validation_error = None
    response = None
    for attempt in range(2):
        repair_prompt = prompt
        if attempt:
            repair_prompt = (
                "Repair the previous Functional Requirements JSON. Return the complete corrected object. "
                "Use only the exact fragment_id and source_reference pairs present in the supplied approved use cases "
                "and retrieved Knowledge Base evidence. Do not cite acceptance-criterion IDs or invent evidence IDs. "
                f"Validation error: {validation_error}\n\nPREVIOUS INVALID RESPONSE:\n{previous_output}\n\nORIGINAL INPUT:\n{prompt}"
            )
        response = responses_create(client,
            model=OPENAI_MODEL,
            input=repair_prompt,
            store=False,
            text={"format": {"type": "json_schema", "name": "functional_requirements_contract", "strict": True, "schema": _functional_requirement_schema()}},
        )
        try:
            if not response.output_text:
                raise ValueError("OpenAI returned no Functional Requirements")
            previous_output = json.loads(response.output_text)
            contract = previous_output
            _validate_requirements_contract(contract, allowed_fragments)
            for requirement in contract["requirements"]:
                unknown_business = set(requirement.get("business_requirement_ids", [])) - approved_ids
                if unknown_business or not requirement.get("business_requirement_ids"):
                    raise ValueError(f"AI returned an unapproved business requirement link for {requirement['requirement_id']}")
                if requirement.get("knowledge_context_ids"):
                    raise ValueError(f"AI returned non-Knowledge-Base context IDs for {requirement['requirement_id']}")
            break
        except (ValueError, json.JSONDecodeError) as exc:
            validation_error = str(exc)
            if attempt == 1:
                raise ValueError(
                    f"OpenAI returned Functional Requirements that failed validation after one repair attempt: {validation_error}"
                ) from exc
    contract["generation_attempts"] = attempt + 1
    for requirement in contract["requirements"]:
        requirement["approved"] = False
        requirement["review_status"] = "pending"
        requirement["business_rules"] = [
            {"rule_id": business["business_requirement_id"], "text": business["text"], "approved": True, "review_status": "approved", "mapping_basis": "Linked by the Functional Requirement generator to an approved Business Use Case", "origin": "approved_business_use_case", "source_references": business.get("source_references", [])}
            for business in approved_business_requirements if business["business_requirement_id"] in requirement["business_requirement_ids"]
        ]
        for criterion in requirement.get("acceptance_criteria", []):
            criterion.update({"text": criterion.get("then", ""), "approved": False, "review_status": "pending", "origin": "openai_rag_draft"})
        for outcome in requirement.get("expected_outcomes", []):
            outcome.update({"approved": False, "review_status": "pending", "origin": "openai_rag_draft"})
        requirement["origin"] = "openai_rag_draft"
    rag_metadata = rag_context.get("rag_metadata") or {}
    input_fragment_hash = hashlib.sha256(json.dumps({
        "fragments": [{"fragment_id": item.get("fragment_id"), "content_hash": item.get("content_hash")} for item in allowed_fragments],
        "approved_business_use_cases": [{"business_requirement_id": item.get("business_requirement_id"), "text": item.get("text")} for item in approved_business_requirements],
        "model": OPENAI_MODEL,
        "prompt_version": REQUIREMENTS_ANALYSIS_PROMPT_VERSION,
        "rag": rag_metadata,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    contract.update({
        "contract_version": "1.1", "document_type": "functional_requirement_analysis", "schema_version": "testiq.functional-requirements.v1",
        "package_id": package["package_id"], "project_id": package["project_id"], "package_scope": package.get("scope"),
        "accepted_assumptions": [], "source_package_items": [{"source_id": item["source_id"], "revision_id": item["revision_id"], "source_type": item["source_type"], "logical_name": item["logical_name"], "sha256": item["sha256"]} for item in items],
        "approved_business_requirements": approved_business_requirements, "knowledge_contexts": [],
        "generated_at": now(), "generation_mode": "openai_rag_grounded",
        "generation_model": OPENAI_MODEL, "model_name": OPENAI_MODEL,
        "model_response_id": getattr(response, "id", None), "prompt_version": REQUIREMENTS_ANALYSIS_PROMPT_VERSION,
        "knowledge_index_id": rag_metadata.get("index_id"), "knowledge_index_config_hash": rag_metadata.get("index_config_hash"),
        "retrieval_ids": rag_metadata.get("retrieval_ids", []), "rag_context_hash": rag_metadata.get("context_hash"),
        "rag_trace": rag_metadata, "input_fragment_hash": input_fragment_hash,
        "quality_controls": {"strict_json_schema": True, "source_citations_required": True, "approved_business_use_cases_only": True, "unsupported_numeric_values_rejected": True, "automatic_validation_retry": True, "openai_response_storage": False},
        "functional_requirements_generation": True,
    })
    contract["readiness"] = _contract_readiness(contract)
    contract["conflicts"] = contract.get("conflicting_requirements", [])
    return contract


@app.post("/api/v1/packages/{package_id}/functional-requirements/generate", status_code=201)
def generate_functional_requirements(package_id: str):
    with connection() as conn:
        package = conn.execute("SELECT * FROM source_package_manifest WHERE package_id=%s", (package_id,)).fetchone()
        if not package:
            raise HTTPException(404, "Package manifest not found")
        source_items = conn.execute("SELECT pi.*,s.logical_name,s.source_type,r.sha256,e.status AS extraction_status FROM source_package_item pi JOIN source_document s ON s.source_id=pi.source_id JOIN source_revision r ON r.revision_id=pi.revision_id LEFT JOIN source_extraction e ON e.revision_id=pi.revision_id WHERE pi.package_id=%s", (package_id,)).fetchall()
        not_ready = [item["logical_name"] for item in source_items if item["extraction_status"] != "succeeded"]
        if not_ready:
            raise HTTPException(422, f"Functional Requirements cannot be generated until extraction succeeds for: {', '.join(not_ready)}")
        prior_analyses = conn.execute("SELECT * FROM requirement_analysis WHERE package_id=%s ORDER BY analysis_version DESC", (package_id,)).fetchall()
        approved = []
        for prior in prior_analyses:
            prior_contract = json.loads(prior["contract_json"])
            if prior_contract.get("functional_requirements_generation"):
                continue
            approved = _approved_business_requirements(prior_contract)
            if approved:
                break
        if not approved:
            for prior in prior_analyses:
                prior_contract = json.loads(prior["contract_json"])
                if prior_contract.get("functional_requirements_generation"):
                    approved = prior_contract.get("approved_business_requirements", [])
                    if approved:
                        break
        if not approved:
            raise HTTPException(409, "Approve at least one Business Use Case before generating Functional Requirements")
        revision_ids = [item["revision_id"] for item in source_items]
        fragments = conn.execute(f"SELECT * FROM source_fragment WHERE revision_id IN ({','.join('%s' for _ in revision_ids)}) ORDER BY revision_id,fragment_index", revision_ids).fetchall()
        package_row = row_dict(package)
        item_rows = [row_dict(item) for item in source_items]
        fragment_rows = [row_dict(fragment) for fragment in fragments]
        source_types = {item["revision_id"]: item["source_type"] for item in item_rows}
        for fragment in fragment_rows:
            fragment["source_type"] = source_types.get(fragment.get("revision_id"))
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(503, "Functional Requirement generation requires OpenAI. Configure OPENAI_API_KEY and retry.")
    try:
        rag = _require_package_context(package_id, requested_by="Functional Requirements")
    except HTTPException as exc:
        if exc.status_code in {409, 422, 502, 503}:
            raise HTTPException(409, "Build the Knowledge Base for this Resource Package before generating Functional Requirements.") from exc
        raise
    try:
        contract = _generate_functional_contract(package_row, item_rows, fragment_rows, approved, rag)
    except Exception as exc:
        detail = safe_llm_error_message(exc)
        if isinstance(exc, TimeoutError):
            message = "OpenAI timed out while generating Functional Requirements. Check service availability and retry."
        elif "api key" in detail.lower() or "invalid_api_key" in detail.lower():
            message = "OpenAI rejected the configured API key. Check OPENAI_API_KEY and retry."
        elif isinstance(exc, ValueError):
            message = f"OpenAI returned Functional Requirements that failed validation: {detail}. Review the model response or configuration and retry."
        else:
            message = f"OpenAI Functional Requirement generation failed: {detail}. Review the OpenAI configuration and retry."
        raise HTTPException(502, message) from exc
    contract["rag_metadata"] = _rag_metadata_or_none(rag)
    contract["rag_status"] = _rag_status(rag, required=True)
    with connection() as conn:
        version = conn.execute("SELECT COALESCE(MAX(analysis_version),0)+1 AS v FROM requirement_analysis WHERE package_id=%s", (package_id,)).fetchone()["v"]
        analysis_id, timestamp = uid("req"), now()
        conn.execute("INSERT INTO requirement_analysis (analysis_id,package_id,project_id,analysis_version,status,analysis_kind,contract_json,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", (analysis_id, package_id, package_row["project_id"], version, "needs_review", "functional", json.dumps(contract), timestamp, timestamp))
        return _analysis_response(conn.execute("SELECT * FROM requirement_analysis WHERE analysis_id=%s", (analysis_id,)).fetchone(), 0)



@app.post("/api/v1/packages/{package_id}/business-requirements/generate", status_code=201)
def generate_business_requirements(package_id: str):
    with connection() as conn:
        package = conn.execute("SELECT * FROM source_package_manifest WHERE package_id=%s", (package_id,)).fetchone()
        if not package:
            raise HTTPException(404, "Package manifest not found")
        items = conn.execute("SELECT pi.*,s.logical_name,s.source_type,r.sha256,r.filename,e.status AS extraction_status,e.extracted_text AS extracted_text FROM source_package_item pi JOIN source_document s ON s.source_id=pi.source_id JOIN source_revision r ON r.revision_id=pi.revision_id LEFT JOIN source_extraction e ON e.revision_id=pi.revision_id WHERE pi.package_id=%s", (package_id,)).fetchall()
        not_ready = [item["logical_name"] for item in items if item["extraction_status"] != "succeeded"]
        if not_ready:
            raise HTTPException(422, f"Business Use Cases cannot be generated until extraction succeeds for: {', '.join(not_ready)}")
        ids = [item["revision_id"] for item in items]
        fragments = conn.execute(f"SELECT * FROM source_fragment WHERE revision_id IN ({','.join('%s' for _ in ids)}) ORDER BY revision_id,fragment_index", ids).fetchall()
        if conn.execute("SELECT 1 FROM knowledge_index WHERE package_id=%s LIMIT 1", (package_id,)).fetchone():
            raise HTTPException(409, "Business Use Cases are locked because this package already has a Knowledge Base version")
        package_row = row_dict(package)
        item_rows = [row_dict(item) for item in items]
        fragment_rows = [row_dict(fragment) for fragment in fragments]
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(503, "Business Use Case generation requires OpenAI. OPENAI_API_KEY is not configured; add a valid key and retry. No local generation fallback is used.")
    try:
        # Business Use Cases are the approved rule input to the Knowledge Base,
        # so they are extracted directly from the BRD before RAG is available.
        business_items = _business_requirements_with_timeout(package_row, item_rows, fragment_rows, None)
    except HTTPException:
        raise
    except Exception as exc:
        detail = safe_llm_error_message(exc)
        if isinstance(exc, TimeoutError):
            message = "OpenAI timed out while generating Business Use Cases. Check service availability and retry."
        elif "api key" in detail.lower() or "invalid_api_key" in detail.lower():
            message = "OpenAI rejected the configured API key. Check OPENAI_API_KEY and retry."
        else:
            message = f"OpenAI could not generate Business Use Cases: {detail}. No local fallback was used; review the OpenAI configuration and retry."
        raise HTTPException(502, message) from exc
    contract = _business_requirements_to_review_contract(package_row, item_rows, business_items, "openai", OPENAI_MODEL)
    contract["source_stage"] = "brd_rule_extraction"
    contract["knowledge_base_input"] = True
    contract["rag_metadata"] = None
    contract["rag_status"] = {"used": False, "required": False, "message": "Generated from BRD source evidence before Knowledge Base creation. Approved rules become Knowledge Base input."}
    with connection() as conn:
        _replace_package_rules_from_business_requirements(conn, package_id, contract.get("business_requirements", []))
        version = conn.execute("SELECT COALESCE(MAX(analysis_version),0)+1 AS v FROM requirement_analysis WHERE package_id=%s", (package_id,)).fetchone()["v"]
        analysis_id, timestamp = uid("req"), now()
        conn.execute("INSERT INTO requirement_analysis (analysis_id,package_id,project_id,analysis_version,status,analysis_kind,contract_json,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", (analysis_id, package_id, package_row["project_id"], version, "needs_review", "business", json.dumps(contract), timestamp, timestamp))
        discovery_count = _discovery_context_count(conn, package_row["project_id"], package_id)
        return _analysis_response(conn.execute("SELECT * FROM requirement_analysis WHERE analysis_id=%s", (analysis_id,)).fetchone(), discovery_count)


@app.post("/api/v1/packages/{package_id}/requirements/analyze", status_code=201)
def analyze_requirements(package_id: str):
    with connection() as conn:
        package=conn.execute("SELECT * FROM source_package_manifest WHERE package_id=%s",(package_id,)).fetchone()
        if not package: raise HTTPException(404,"Package manifest not found")
        items=conn.execute("SELECT pi.*,s.logical_name,s.source_type,r.sha256,e.status AS extraction_status FROM source_package_item pi JOIN source_document s ON s.source_id=pi.source_id JOIN source_revision r ON r.revision_id=pi.revision_id LEFT JOIN source_extraction e ON e.revision_id=pi.revision_id WHERE pi.package_id=%s",(package_id,)).fetchall(); not_ready=[x["logical_name"] for x in items if x["extraction_status"]!="succeeded"]
        if not_ready: raise HTTPException(422,f"Requirements cannot be analyzed until extraction succeeds for: {', '.join(not_ready)}")
        ids = [x["revision_id"] for x in items]
        fragments = conn.execute(f"SELECT * FROM source_fragment WHERE revision_id IN ({','.join('%s' for _ in ids)}) ORDER BY revision_id,fragment_index", ids).fetchall()
        package_row = row_dict(package)
        item_rows = [row_dict(x) for x in items]
        fragment_rows = [row_dict(x) for x in fragments]
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(503, "Requirements Analysis requires OpenAI and an approved Knowledge Base. Configure OPENAI_API_KEY and retry.")
    rag = _require_package_context(package_row["package_id"], requested_by="Requirements Analysis")
    if not rag.get("brd_fragments"):
        raise HTTPException(422, "Requirements Analysis requires retrieved BRD evidence from the approved Knowledge Base")
    try:
        contract = _requirements_contract_with_timeout(package_row, item_rows, fragment_rows, _rag_context_for_prompt(rag))
    except Exception as exc:
        raise HTTPException(502, f"RAG-grounded Requirements Analysis failed: {safe_llm_error_message(exc)}") from exc
    contract["rag_metadata"] = _rag_metadata_or_none(rag)
    contract["rag_status"] = _rag_status(rag, required=True)
    with connection() as conn:
        version = conn.execute("SELECT COALESCE(MAX(analysis_version),0)+1 AS v FROM requirement_analysis WHERE package_id=%s", (package_id,)).fetchone()["v"]
        analysis_id, timestamp = uid("req"), now()
        conn.execute("INSERT INTO requirement_analysis (analysis_id,package_id,project_id,analysis_version,status,analysis_kind,contract_json,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", (analysis_id, package_id, package_row["project_id"], version, "needs_review", "analysis", json.dumps(contract), timestamp, timestamp))
        discovery_count = _discovery_context_count(conn, package_row["project_id"], package_id)
        return _analysis_response(conn.execute("SELECT * FROM requirement_analysis WHERE analysis_id=%s", (analysis_id,)).fetchone(), discovery_count)

@app.get("/api/v1/packages/{package_id}/requirements")
def list_requirement_analyses(package_id: str):
    with connection() as conn:
        package = conn.execute("SELECT * FROM source_package_manifest WHERE package_id=%s", (package_id,)).fetchone()
        discovery_count = _discovery_context_count(conn, package["project_id"], package_id) if package else 0
        return [_analysis_response(x, discovery_count) for x in conn.execute("SELECT * FROM requirement_analysis WHERE package_id=%s ORDER BY analysis_version DESC",(package_id,)).fetchall()]

@app.get("/api/v1/packages/{package_id}/requirements/latest-set")
def get_latest_requirement_analyses(package_id: str):
    """Return only the latest Business Use Case and Functional Requirement artifacts."""
    with connection() as conn:
        business = functional = None
        rows = conn.execute(
            """SELECT * FROM (
                   SELECT requirement_analysis.*,
                          ROW_NUMBER() OVER (
                              PARTITION BY analysis_kind
                              ORDER BY analysis_version DESC
                          ) AS kind_rank
                   FROM requirement_analysis
                   WHERE package_id=%s
                     AND analysis_kind IN ('business','functional')
               ) latest
               WHERE kind_rank=1
               ORDER BY analysis_version DESC""",
            (package_id,),
        ).fetchall()
        if not rows:
            package = conn.execute(
                "SELECT package_id FROM source_package_manifest WHERE package_id=%s",
                (package_id,),
            ).fetchone()
            if not package:
                raise HTTPException(404, "Resource Package not found")
        for row in rows:
            response = _analysis_response(row, 0)
            if row["analysis_kind"] == "functional":
                functional = response
            elif row["analysis_kind"] == "business":
                business = response
        return {"package_id": package_id, "business": business, "functional": functional}

@app.get("/api/v1/requirement-analyses/{analysis_id}")
def get_requirement_analysis(analysis_id: str):
    with connection() as conn:
        row=conn.execute("SELECT * FROM requirement_analysis WHERE analysis_id=%s",(analysis_id,)).fetchone()
        if not row: raise HTTPException(404,"Requirement analysis not found")
        discovery_count = _discovery_context_count(conn, row["project_id"], row["package_id"])
        return _analysis_response(row, discovery_count)

@app.post("/api/v1/requirement-analyses/{analysis_id}/review")
def review_requirement_analysis(analysis_id: str, request: RequirementReviewRequest):
    with connection() as conn:
        row=conn.execute("SELECT * FROM requirement_analysis WHERE analysis_id=%s",(analysis_id,)).fetchone()
        if not row: raise HTTPException(404,"Requirement analysis not found")
        contract = _clean_requirement_contract(json.loads(row["contract_json"]))
        reviewed_contract_hash = hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        reviewed_contract_version = contract.get("contract_version", "1.0")
        contract["accepted_assumptions"] = list(dict.fromkeys(contract.get("accepted_assumptions",[]) + request.accepted_assumptions))
        contract["resolved_gaps"] = list(dict.fromkeys(contract.get("resolved_gaps", []) + request.resolved_gaps))
        review_items = _review_items(contract)
        if request.items:
            submitted = {item.item_id for item in request.items}
            expected = {key for key, _, _ in review_items}
            if len(submitted) != len(request.items):
                raise HTTPException(422, "Each review item must appear only once")
            missing = expected - submitted
            unknown = submitted - expected
            if missing or unknown:
                raise HTTPException(422, {"missing_items": sorted(missing), "unknown_items": sorted(unknown)})
            for submitted_item in request.items:
                target = next(item for key, item, _ in review_items if key == submitted_item.item_id)
                item_type = next(item_type for key, _, item_type in review_items if key == submitted_item.item_id)
                if item_type == "functional_criterion":
                    if submitted_item.title is not None: target["title"] = submitted_item.title.strip()
                    if submitted_item.statement is not None: target["statement"] = submitted_item.statement.strip()
                elif item_type == "business_rule":
                    if submitted_item.text is None or not submitted_item.text.strip(): raise HTTPException(422, f"text is required for {submitted_item.item_id}")
                    target["text"] = submitted_item.text.strip()
                else:
                    for field in ("scenario", "when", "then"):
                        value = getattr(submitted_item, field)
                        if value is not None: target[field] = value.strip()
                    if submitted_item.given is not None:
                        if isinstance(submitted_item.given, str):
                            target["given"] = submitted_item.given.strip()
                        else:
                            target["given"] = "; ".join(value.strip() for value in submitted_item.given if value.strip())
                    if submitted_item.text is not None: target["text"] = submitted_item.text.strip()
                review_status = submitted_item.review_status or ("approved" if submitted_item.approved else "pending")
                target["review_status"] = review_status
                target["approved"] = review_status == "approved"
            approved = {key for key, value, _ in review_items if value.get("approved")}
            contract["approved_items"] = sorted(approved)
            contract["rejected_items"] = sorted(key for key, value, _ in review_items if value.get("review_status") == "rejected")
            contract["pending_items"] = sorted(key for key, value, _ in review_items if value.get("review_status", "pending") == "pending")
            status = "approved" if not contract["rejected_items"] and not contract["pending_items"] else "needs_changes"
            decision = status
        else:
            contract["approved_items"] = list(dict.fromkeys(request.approved_items))
            status = "approved" if request.decision == "approved" else request.decision
            decision = request.decision
        timestamp=now()
        contract["review_metadata"] = {
            "reviewer": request.reviewer,
            "decision": decision,
            "reviewed_at": timestamp,
            "comments": request.comments,
            "analysis_version": row["analysis_version"],
            "contract_version": reviewed_contract_version,
            "reviewed_contract_hash": reviewed_contract_hash,
        }
        rule_summary = _sync_business_requirement_rule_review(conn, row["package_id"], contract, request.reviewer, timestamp)
        if rule_summary:
            contract["knowledge_base_rule_status"] = rule_summary
        conn.execute("UPDATE requirement_analysis SET status=%s,contract_json=%s,reviewer=%s,review_decision=%s,review_comments=%s,reviewed_at=%s,updated_at=%s WHERE analysis_id=%s",(status,json.dumps(contract),request.reviewer,decision,request.comments,timestamp,timestamp,analysis_id))
        conn.execute("INSERT INTO requirement_review_audit (audit_id,analysis_id,package_id,project_id,reviewer,decision,comments,analysis_version,contract_version,reviewed_contract_hash,approved_items_json,rejected_items_json,pending_items_json,accepted_assumptions_json,resolved_gaps_json,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (uid("raudit"), analysis_id, row["package_id"], row["project_id"], request.reviewer, decision, request.comments, row["analysis_version"], reviewed_contract_version, reviewed_contract_hash, json.dumps(contract.get("approved_items", [])), json.dumps(contract.get("rejected_items", [])), json.dumps(contract.get("pending_items", [])), json.dumps(contract.get("accepted_assumptions", [])), json.dumps(contract.get("resolved_gaps", [])), timestamp))
        discovery_count = _discovery_context_count(conn, row["project_id"], row["package_id"])
        return _analysis_response(conn.execute("SELECT * FROM requirement_analysis WHERE analysis_id=%s", (analysis_id,)).fetchone(), discovery_count)

@app.post("/api/v1/requirement-analyses/{analysis_id}/test-cases", status_code=201)
def generate_test_cases_from_analysis(analysis_id: str, request: TestCaseGenerationRequest):
    with connection() as conn:
        analysis = conn.execute("SELECT * FROM requirement_analysis WHERE analysis_id=%s", (analysis_id,)).fetchone()
        if not analysis: raise HTTPException(404, "Requirement analysis not found")
        contract = _clean_requirement_contract(json.loads(analysis["contract_json"]))
        approved_requirements = _approved_requirement_payload(contract)
        if not approved_requirements: raise HTTPException(422, "Approve at least one requirement before generating test cases")
        package = conn.execute("SELECT * FROM source_package_manifest WHERE package_id=%s", (analysis["package_id"],)).fetchone()
        if not package: raise HTTPException(404, "Source package not found")
        package_row = row_dict(package)
        discovery_contexts = []
        requested_count = request.count
        if not os.getenv("OPENAI_API_KEY"):
            raise HTTPException(503, "Test Case generation requires OpenAI. OPENAI_API_KEY is not configured; add a valid key and retry. No local generation fallback is used.")
        rag = _require_test_case_knowledge(package_row["package_id"], requested_by="Test Case Generation")
        try:
            generated = _test_cases_with_openai(approved_requirements, package_row, requested_count, discovery_contexts, rag)
        except Exception as exc:
            _raise_test_case_generation_error(exc, "Test Case generation")
        generation_id, timestamp = uid("tcg"), now()
        requirement_contract_hash = hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        generated.update({"contract_version": "1.0", "generation_id": generation_id, "analysis_id": analysis_id, "analysis_version": analysis["analysis_version"], "project_id": analysis["project_id"], "requirement_contract_hash": requirement_contract_hash, "package_id": package["package_id"], "package_version": package["package_version"], "source_package_run_id": package["run_id"], "requested_test_case_count": len(generated.get("test_cases", [])), "count_strategy": "derived_from_approved_functional_requirements_acceptance_criteria_and_business_rules" if requested_count is None else "user_requested", "generation_mode": "openai", "generation_model": OPENAI_MODEL, "generation_prompt_version": TEST_CASE_GENERATION_PROMPT_VERSION, "generation_prompt_hash": test_case_generation_prompt_hash(), "generated_at": timestamp, "approved_requirement_ids": [item["requirement_id"] for item in approved_requirements], "source_package_items": contract.get("source_package_items", []), "discovery_contexts": discovery_contexts, "discovery_context_ids": [context["context_id"] for context in discovery_contexts], "rag_metadata": _rag_metadata_or_none(rag), "rag_status": _rag_status(rag, required=True)})
        for test_case in generated.get("test_cases", []):
            test_case["review_status"] = "pending"
            test_case["approved"] = False
        _apply_coverage_and_quality_review(generated, approved_requirements, timestamp)
        conn.execute("INSERT INTO test_case_generation (generation_id,analysis_id,package_id,project_id,status,contract_json,model_name,prompt_version,prompt_hash,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (generation_id, analysis_id, package["package_id"], package["project_id"], "ready", json.dumps(generated), generated["generation_model"], TEST_CASE_GENERATION_PROMPT_VERSION, test_case_generation_prompt_hash(), timestamp, timestamp))
        return {"generation_id": generation_id, **generated}

@app.get("/api/v1/requirement-analyses/{analysis_id}/test-cases")
def list_generated_test_cases(analysis_id: str):
    with connection() as conn:
        return [_test_case_generation_response(row) for row in conn.execute("SELECT * FROM test_case_generation WHERE analysis_id=%s ORDER BY created_at DESC", (analysis_id,)).fetchall()]

@app.post("/api/v1/test-case-generations/{generation_id}/review")
def review_test_case_generation(generation_id: str, request: TestCaseReviewRequest):
    with connection() as conn:
        row = conn.execute("SELECT * FROM test_case_generation WHERE generation_id=%s", (generation_id,)).fetchone()
        if not row: raise HTTPException(404, "Test-case generation run not found")
        contract = json.loads(row["contract_json"])
        cases = contract.get("test_cases", [])
        by_id = {case.get("test_case_id"): case for case in cases}
        if len({item.test_case_id for item in request.items}) != len(request.items): raise HTTPException(422, "Each test case must appear only once")
        unknown = [item.test_case_id for item in request.items if item.test_case_id not in by_id]
        missing = [case_id for case_id in by_id if case_id not in {item.test_case_id for item in request.items}]
        if unknown or missing: raise HTTPException(422, {"unknown_items": unknown, "missing_items": missing})
        timestamp = now()
        for item in request.items:
            case = by_id[item.test_case_id]
            case["review_status"] = item.review_status
            case["approved"] = item.review_status == "approved"
            case["approval"] = {"status": item.review_status, "reviewer": request.reviewer, "comments": request.comments or "", "reviewed_at": timestamp}
            if item.title is not None: case["title"] = item.title.strip()
            if item.steps is not None: case["steps"] = [value.strip() for value in item.steps if value.strip()]
            if item.expected_results is not None: case["expected_results"] = [value.strip() for value in item.expected_results if value.strip()]
        contract["approved_test_case_ids"] = [case["test_case_id"] for case in cases if case.get("review_status") == "approved"]
        contract["rejected_test_case_ids"] = [case["test_case_id"] for case in cases if case.get("review_status") == "rejected"]
        contract["pending_test_case_ids"] = [case["test_case_id"] for case in cases if case.get("review_status", "pending") == "pending"]
        contract["review_metadata"] = {"reviewer": request.reviewer, "comments": request.comments, "reviewed_at": timestamp, "approved_count": len(contract["approved_test_case_ids"]), "rejected_count": len(contract["rejected_test_case_ids"]), "pending_count": len(contract["pending_test_case_ids"])}
        analysis = conn.execute("SELECT * FROM requirement_analysis WHERE analysis_id=%s", (row["analysis_id"],)).fetchone()
        if analysis:
            _apply_coverage_and_quality_review(contract, _approved_requirement_payload(json.loads(analysis["contract_json"])), timestamp)
        status = "approved" if not contract["pending_test_case_ids"] and not contract["rejected_test_case_ids"] else "needs_review"
        reviewed_contract_hash = hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        reviewed_items = [item.model_dump(exclude_none=True) for item in request.items]
        conn.execute("UPDATE test_case_generation SET status=%s,contract_json=%s,updated_at=%s WHERE generation_id=%s", (status, json.dumps(contract), timestamp, generation_id))
        conn.execute("INSERT INTO test_case_review_audit (audit_id,generation_id,analysis_id,package_id,project_id,reviewer,comments,reviewed_contract_hash,approved_test_case_ids_json,rejected_test_case_ids_json,pending_test_case_ids_json,reviewed_contract_json,reviewed_items_json,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (uid("tcaudit"), generation_id, row["analysis_id"], row["package_id"], row["project_id"], request.reviewer, request.comments, reviewed_contract_hash, json.dumps(contract.get("approved_test_case_ids", [])), json.dumps(contract.get("rejected_test_case_ids", [])), json.dumps(contract.get("pending_test_case_ids", [])), json.dumps(contract), json.dumps(reviewed_items), timestamp))
        result = _test_case_generation_response(conn.execute("SELECT * FROM test_case_generation WHERE generation_id=%s", (generation_id,)).fetchone())
        result["reviewer"] = request.reviewer
        result["review_comments"] = request.comments
        return result

@app.get("/api/v1/test-case-generations/{generation_id}/coverage-review")
def get_test_case_coverage_review(generation_id: str):
    with connection() as conn:
        row = conn.execute("SELECT * FROM test_case_generation WHERE generation_id=%s", (generation_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Test-case generation run not found")
        analysis = conn.execute("SELECT * FROM requirement_analysis WHERE analysis_id=%s", (row["analysis_id"],)).fetchone()
        if not analysis:
            raise HTTPException(404, "Requirement analysis not found")
        contract = json.loads(row["contract_json"])
        approved_requirements = _approved_requirement_payload(json.loads(analysis["contract_json"]))
        enriched = _apply_coverage_and_quality_review(contract, approved_requirements)
        review = enriched["coverage_review"]
        review["generation_id"] = generation_id
        review["execution_ready"] = bool(enriched.get("approved_test_case_ids"))
        return review

@app.post("/api/v1/test-case-generations/{generation_id}/regenerate", status_code=201)
def regenerate_test_cases(generation_id: str, request: TestCaseRegenerationRequest):
    with connection() as conn:
        parent = conn.execute("SELECT * FROM test_case_generation WHERE generation_id=%s", (generation_id,)).fetchone()
        if not parent:
            raise HTTPException(404, "Test-case generation run not found")
        analysis = conn.execute("SELECT * FROM requirement_analysis WHERE analysis_id=%s", (parent["analysis_id"],)).fetchone()
        if not analysis:
            raise HTTPException(404, "Requirement analysis not found")
        requirement_contract = json.loads(analysis["contract_json"])
        approved_requirements = _approved_requirement_payload(requirement_contract)
        if not approved_requirements:
            raise HTTPException(422, "Approve at least one requirement before regenerating test cases")

        parent_contract = json.loads(parent["contract_json"])
        parent_cases = parent_contract.get("test_cases", [])
        parent_case_ids = {case.get("test_case_id") for case in parent_cases}
        selected_ids = request.test_case_ids or parent_contract.get("rejected_test_case_ids", [])
        if len(set(selected_ids)) != len(selected_ids):
            raise HTTPException(422, "Each regeneration item must appear only once")
        unknown = sorted(set(selected_ids) - parent_case_ids)
        if unknown:
            raise HTTPException(422, {"unknown_items": unknown})

        requested_count = len(selected_ids) if selected_ids else int(parent_contract.get("requested_test_case_count") or len(parent_cases) or 5)
        package = conn.execute("SELECT * FROM source_package_manifest WHERE package_id=%s", (parent["package_id"],)).fetchone()
        if not package:
            raise HTTPException(404, "Source package not found")
        package_row = row_dict(package)
        discovery_contexts = []
        if not os.getenv("OPENAI_API_KEY"):
            raise HTTPException(503, "Test Case regeneration requires OpenAI. OPENAI_API_KEY is not configured; add a valid key and retry. No local generation fallback is used.")
        rag = _require_test_case_knowledge(package["package_id"], requested_by="Test Case Regeneration")
        try:
            generated = _test_cases_with_openai(approved_requirements, package_row, requested_count, discovery_contexts, rag)
        except Exception as exc:
            _raise_test_case_generation_error(exc, "Test Case regeneration")

        new_generation_id, timestamp = uid("tcg"), now()
        requirement_contract_hash = hashlib.sha256(json.dumps(requirement_contract, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        generation_version = conn.execute("SELECT COUNT(*)+1 AS v FROM test_case_generation WHERE analysis_id=%s", (parent["analysis_id"],)).fetchone()["v"]
        generated.update({
            "contract_version": "1.0",
            "generation_id": new_generation_id,
            "analysis_id": parent["analysis_id"],
            "project_id": parent["project_id"],
            "analysis_version": analysis["analysis_version"],
            "requirement_contract_hash": requirement_contract_hash,
            "package_id": package["package_id"],
            "package_version": package["package_version"],
            "source_package_run_id": package["run_id"],
            "requested_test_case_count": requested_count,
            "generation_mode": "openai",
            "generation_model": OPENAI_MODEL,
            "generation_prompt_version": TEST_CASE_GENERATION_PROMPT_VERSION,
            "generation_prompt_hash": test_case_generation_prompt_hash(),
            "generated_at": timestamp,
            "approved_requirement_ids": [item["requirement_id"] for item in approved_requirements],
            "source_package_items": requirement_contract.get("source_package_items", []),
            "discovery_contexts": discovery_contexts,
            "discovery_context_ids": [context["context_id"] for context in discovery_contexts],
            "rag_metadata": _rag_metadata_or_none(rag),
            "rag_status": _rag_status(rag, required=True),
            "parent_generation_id": generation_id,
            "generation_version": generation_version,
            "regeneration_request": {
                "requested_by": request.reviewer,
                "reason": request.reason,
                "source_test_case_ids": selected_ids,
                "requested_at": timestamp,
            },
        })
        for test_case in generated.get("test_cases", []):
            test_case["review_status"] = "pending"
            test_case["approved"] = False
            test_case["regenerated_from_generation_id"] = generation_id
            if selected_ids:
                test_case["regeneration_source_test_case_ids"] = selected_ids
        _apply_coverage_and_quality_review(generated, approved_requirements, timestamp)

        parent_contract.setdefault("regeneration_requests", []).append({"target_generation_id": new_generation_id, "source_test_case_ids": selected_ids, "requested_by": request.reviewer, "reason": request.reason, "requested_at": timestamp})
        conn.execute("UPDATE test_case_generation SET contract_json=%s,updated_at=%s WHERE generation_id=%s", (json.dumps(parent_contract), timestamp, generation_id))
        conn.execute("INSERT INTO test_case_generation (generation_id,analysis_id,package_id,project_id,status,contract_json,model_name,prompt_version,prompt_hash,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (new_generation_id, parent["analysis_id"], package["package_id"], package["project_id"], "ready", json.dumps(generated), generated["generation_model"], TEST_CASE_GENERATION_PROMPT_VERSION, test_case_generation_prompt_hash(), timestamp, timestamp))
        return {"generation_id": new_generation_id, **generated}

@app.post("/api/v1/test-case-generations/{generation_id}/execution-runs", status_code=201)
def create_execution_run(generation_id: str, request: ExecutionRunRequest):
    with connection() as conn:
        row = conn.execute("SELECT * FROM test_case_generation WHERE generation_id=%s", (generation_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Test-case generation run not found")
        if len(set(request.selected_test_case_ids)) != len(request.selected_test_case_ids):
            raise HTTPException(422, "Each selected test case must appear only once")
        contract = json.loads(row["contract_json"])
        cases = contract.get("test_cases", [])
        by_id = {case.get("test_case_id"): case for case in cases}
        unknown = sorted(set(request.selected_test_case_ids) - set(by_id))
        if unknown:
            raise HTTPException(422, {"unknown_items": unknown})
        not_approved = [case_id for case_id in request.selected_test_case_ids if _review_status(by_id[case_id]) != "approved"]
        if not_approved:
            raise HTTPException(422, {"message": "Only approved test cases can be frozen for execution", "not_approved": not_approved})
        if not request.selected_test_case_ids:
            raise HTTPException(422, "Select at least one approved test case with saved test data before freezing a baseline")

        timestamp = now()
        selected_cases = [json.loads(json.dumps(by_id[case_id])) for case_id in request.selected_test_case_ids]
        selected_test_data = []
        if request.selected_test_case_ids:
            placeholders = ",".join(["%s"] * len(request.selected_test_case_ids))
            data_rows = conn.execute(f"SELECT r.* FROM test_data_record r WHERE r.generation_id=%s AND r.test_case_id IN ({placeholders}) AND r.status='saved' ORDER BY r.test_case_id,r.entity_name", tuple([generation_id] + request.selected_test_case_ids)).fetchall()
            selected_test_data = [_test_data_record_response(data_row) for data_row in data_rows]
        cases_with_saved_data = {record["test_case_id"] for record in selected_test_data}
        missing_test_data = sorted(set(request.selected_test_case_ids) - cases_with_saved_data)
        if missing_test_data:
            raise HTTPException(422, {"message": "Generate and save test data to Bronze before freezing the execution baseline", "missing_test_data": missing_test_data})
        source_contract_hash = hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        frozen_contract = {
            "baseline_version": "1.0",
            "generation_id": generation_id,
            "analysis_id": row["analysis_id"],
            "package_id": row["package_id"],
            "project_id": row["project_id"],
            "source_contract_hash": source_contract_hash,
            "selected_test_case_ids": request.selected_test_case_ids,
            "test_cases": selected_cases,
            "test_data_records": selected_test_data,
            "coverage_review": contract.get("coverage_review", {}),
            "review_metadata": contract.get("review_metadata", {}),
            "frozen_by": request.reviewer,
            "frozen_at": timestamp,
            "comments": request.comments,
        }
        baseline_hash = hashlib.sha256(json.dumps(frozen_contract, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        run_id = uid("run")
        configuration_snapshot = {
            "generation_id": generation_id,
            "baseline_hash": baseline_hash,
            "selected_test_case_ids": request.selected_test_case_ids,
            "selected_case_count": len(request.selected_test_case_ids),
            "frozen_at": timestamp,
            "reviewer": request.reviewer,
        }
        observations = [{"type": "baseline_frozen", "message": "Approved test cases were selected and frozen for an execution run.", "created_at": timestamp}]
        conn.execute("INSERT INTO test_run (run_id,package_id,project_id,status,configuration_snapshot,generation_id,selected_test_case_ids_json,frozen_contract_json,baseline_hash,reviewer,review_comments,lifecycle_state,approved_at,immutable_observations_json,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (run_id, row["package_id"], row["project_id"], "selected", json.dumps(configuration_snapshot), generation_id, json.dumps(request.selected_test_case_ids), json.dumps(frozen_contract), baseline_hash, request.reviewer, request.comments, "selected", timestamp, json.dumps(observations), timestamp))
        return _test_run_response(conn.execute("SELECT * FROM test_run WHERE run_id=%s", (run_id,)).fetchone(), conn)

@app.get("/api/v1/test-case-generations/{generation_id}/execution-runs")
def list_execution_runs_for_generation(generation_id: str):
    with connection() as conn:
        if not conn.execute("SELECT generation_id FROM test_case_generation WHERE generation_id=%s", (generation_id,)).fetchone():
            raise HTTPException(404, "Test-case generation run not found")
        rows = conn.execute("SELECT * FROM test_run WHERE generation_id=%s ORDER BY created_at DESC", (generation_id,)).fetchall()
        return [_test_run_response(row, conn) for row in rows]

@app.post("/api/v1/execution-runs/{run_id}/execute")
def execute_run(run_id: str, request: ExecutionRunExecuteRequest | None = None):
    if not DATABASE_URL:
        raise HTTPException(503, f"Execution requires PostgreSQL and the {BRONZE_SCHEMA} schema")
    with connection() as conn:
        row = conn.execute("SELECT * FROM test_run WHERE run_id=%s", (run_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Execution run not found")
        current_state = str(row["lifecycle_state"] or row["status"] or "selected").lower()
        if current_state == "cancelled":
            raise HTTPException(409, "Execution run is cancelled")

        started_at = now()
        selected_case_ids = json.loads(row["selected_test_case_ids_json"] or "[]")
        if not selected_case_ids:
            raise HTTPException(422, "Execution baseline has no selected test cases")

        observations = json.loads(row["immutable_observations_json"] or "[]")
        reviewer = request.reviewer if request else None
        comments = request.comments if request else None
        observations.append({
            "type": "execution_started",
            "message": "Execution started by validating each selected test case against its saved Bronze rows and PBM Silver results.",
            "created_at": started_at,
            "reviewer": reviewer or row["reviewer"],
            "comments": comments,
        })
        conn.execute(
            "UPDATE test_run SET status=%s,lifecycle_state=%s,started_at=%s,error=%s,immutable_observations_json=%s WHERE run_id=%s",
            ("running", "running", started_at, None, json.dumps(observations), run_id),
        )

        statuses = [_execute_case_against_bronze(conn, run_id, row["generation_id"], test_case_id, started_at) for test_case_id in selected_case_ids]
        completed_at = now()
        passed = sum(status == "passed" for status in statuses)
        failed = sum(status == "failed" for status in statuses)
        message = f"Execution completed with {passed} passed and {failed} failed test case(s)."
        observations.append({
            "type": "execution_completed",
            "message": message,
            "created_at": completed_at,
            "reviewer": reviewer or row["reviewer"],
            "comments": comments,
        })
        conn.execute(
            "UPDATE test_run SET status=%s,lifecycle_state=%s,completed_at=%s,error=%s,immutable_observations_json=%s WHERE run_id=%s",
            ("completed", "completed", completed_at, None if failed == 0 else message, json.dumps(observations), run_id),
        )
        return _test_run_response(conn.execute("SELECT * FROM test_run WHERE run_id=%s", (run_id,)).fetchone(), conn)

@app.patch("/api/v1/execution-runs/{run_id}/lifecycle")
def update_execution_run_lifecycle(run_id: str, request: ExecutionRunLifecycleRequest):
    with connection() as conn:
        row = conn.execute("SELECT * FROM test_run WHERE run_id=%s", (run_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Execution run not found")

        current_state = str(row["lifecycle_state"] or row["status"] or "selected").lower()
        if current_state in {"cancelled", "failed", "completed"}:
            raise HTTPException(409, f"Execution run is already {current_state}")

        timestamp = now()
        reviewer = request.reviewer or row["reviewer"]
        comments = request.comments
        observations = json.loads(row["immutable_observations_json"] or "[]")
        lifecycle_state = request.lifecycle_state
        if lifecycle_state == "cancelled":
            status = "cancelled"
            cancelled_at = timestamp
            completed_at = row["completed_at"]
            error = request.error or comments
            message = "Execution baseline was cancelled before execution."
        else:
            status = "failed"
            cancelled_at = row["cancelled_at"]
            completed_at = timestamp
            error = request.error or comments or "Execution baseline was marked failed during review."
            message = error

        observations.append({
            "type": f"baseline_{lifecycle_state}",
            "message": message,
            "created_at": timestamp,
            "reviewer": reviewer,
            "comments": comments,
        })
        conn.execute(
            "UPDATE test_run SET status=%s,lifecycle_state=%s,cancelled_at=%s,completed_at=%s,error=%s,immutable_observations_json=%s WHERE run_id=%s",
            (status, lifecycle_state, cancelled_at, completed_at, error, json.dumps(observations), run_id),
        )
        return _test_run_response(conn.execute("SELECT * FROM test_run WHERE run_id=%s", (run_id,)).fetchone(), conn)

def _unique_test_value(key, template_id, test_case_id, ordinal=0):
    suffix = hashlib.sha256(f"{template_id}:{test_case_id}:{ordinal}".encode("utf-8")).hexdigest()[:10].upper()
    normalized = str(key).lower()
    if normalized in {"member_id", "subscriber_id", "prescription_id", "claim_id", "payment_method_id", "campaign_id", "communication_id"}:
        return f"QA-{normalized.upper()}-{suffix}"
    if normalized == "token":
        return f"tok_mock_{suffix.lower()}"
    if "email" in normalized:
        return f"{normalized.replace('_address', '')}.{suffix.lower()}@example.test"
    if "phone" in normalized:
        return f"+1-555-{int(suffix[:6], 16) % 1000000:06d}"
    return None

def _test_data_record_response(row):
    item = row_dict(row)
    if not item:
        return None
    item["payload"] = json.loads(item.pop("payload_json"))
    item["pre_steps"] = json.loads(item.pop("pre_steps_json"))
    item["schema_context"] = json.loads(item.pop("schema_context_json") or "[]")
    return item

def _test_data_value_schema(value):
    if isinstance(value, dict):
        properties = {key: _test_data_value_schema(child) for key, child in value.items()}
        return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
    if isinstance(value, list):
        return {"type": "array", "items": _test_data_value_schema(value[0]) if value else {"type": "string"}}
    if isinstance(value, bool): return {"type": "boolean"}
    if isinstance(value, int): return {"type": "integer"}
    if isinstance(value, float): return {"type": "number"}
    if value is None: return {"type": ["string", "null"]}
    return {"type": "string"}

def _matches_test_data_template_shape(value, template):
    if isinstance(template, dict):
        return isinstance(value, dict) and set(value) == set(template) and all(_matches_test_data_template_shape(value[key], child) for key, child in template.items())
    if isinstance(template, list):
        return isinstance(value, list) and len(value) == len(template) and all(_matches_test_data_template_shape(item, template[index]) for index, item in enumerate(value))
    if template is None:
        return value is None or isinstance(value, str)
    if isinstance(template, bool):
        return isinstance(value, bool)
    if isinstance(template, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(template, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, str)

def _test_data_template_plan_schema(case_ids):
    assignment_properties = {
        case_id: {"type": "array", "items": {"type": "string"}, "minItems": 1}
        for case_id in case_ids
    }
    return {
        "type": "object",
        "properties": {
            "templates": {
                "type": "array",
                "minItems": 1,
                "maxItems": 12,
                "items": {
                    "type": "object",
                    "properties": {
                        "template_id": {"type": "string"},
                        "template_name": {"type": "string"},
                        "entity_name": {"type": "string"},
                        "fields": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 40,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "field_type": {"type": "string", "enum": ["string", "integer", "number", "boolean", "null"]},
                                    "sample_value": {"type": "string"},
                                    "required": {"type": "boolean"},
                                },
                                "required": ["path", "field_type", "sample_value", "required"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["template_id", "template_name", "entity_name", "fields"],
                    "additionalProperties": False,
                },
            },
            "assignments": {
                "type": "object",
                "properties": assignment_properties,
                "required": list(assignment_properties),
                "additionalProperties": False,
            },
        },
        "required": ["templates", "assignments"],
        "additionalProperties": False,
    }

def _template_value_from_sample(field_type, sample_value):
    value = "" if sample_value is None else str(sample_value)
    if field_type == "integer":
        try:
            return int(value)
        except Exception:
            return 0
    if field_type == "number":
        try:
            return float(value)
        except Exception:
            return 0.0
    if field_type == "boolean":
        return value.strip().lower() in {"true", "1", "yes", "y"}
    if field_type == "null":
        return None
    return value

def _payload_from_template_fields(fields):
    payload = {}
    for field in fields or []:
        raw_path = str(field.get("path") or "").strip()
        # Keep template shape simple and JSON-object based. Arrays can still be
        # generated as values later, but the field source itself must come from
        # Knowledge Base paths/columns.
        clean_path = re.sub(r"\[[^\]]*\]", "", raw_path)
        clean_path = re.sub(r"[^A-Za-z0-9_.]", "_", clean_path).strip("._")
        parts = [part for part in clean_path.split(".") if part]
        if not parts:
            continue
        current = payload
        for part in parts[:-1]:
            next_value = current.setdefault(part, {})
            if not isinstance(next_value, dict):
                next_value = {}
                current[part] = next_value
            current = next_value
        current[parts[-1]] = _template_value_from_sample(field.get("field_type"), field.get("sample_value"))
    return payload

def _normalize_test_data_templates(plan, case_ids):
    templates = []
    by_raw_id = {}
    used_ids = set()
    for index, item in enumerate(plan.get("templates") or [], 1):
        entity = re.sub(r"[^a-z0-9_]+", "_", str(item.get("entity_name") or f"record_{index}").lower()).strip("_") or f"record_{index}"
        raw_id = str(item.get("template_id") or entity).strip()
        template_id = re.sub(r"[^a-zA-Z0-9_:-]+", "_", raw_id).strip("_") or f"kb_{entity}"
        if not template_id.startswith("kb_"):
            template_id = f"kb_{template_id}"
        base_id, suffix = template_id, 2
        while template_id in used_ids:
            template_id = f"{base_id}_{suffix}"
            suffix += 1
        payload = _payload_from_template_fields(item.get("fields") or [])
        if not payload:
            continue
        normalized = {
            "template_id": template_id,
            "template_name": str(item.get("template_name") or entity.replace("_", " ").title()).strip() or entity,
            "entity_name": entity,
            "payload_json": json.dumps(payload, sort_keys=True),
        }
        used_ids.add(template_id)
        templates.append(normalized)
        by_raw_id[raw_id] = normalized
        by_raw_id[item.get("template_id")] = normalized
        by_raw_id[template_id] = normalized
    if not templates:
        raise ValueError("Knowledge Base did not provide any usable test data templates")
    assignments = {}
    default_template = templates[0]
    for case_id in case_ids:
        assigned = []
        for raw_template_id in (plan.get("assignments") or {}).get(case_id, []):
            template = by_raw_id.get(raw_template_id)
            if template and template["template_id"] not in {item["template_id"] for item in assigned}:
                assigned.append(template)
        assignments[case_id] = assigned or [default_template]
    return assignments

def _derive_test_data_templates_with_openai(case_ids, cases, rag_context):
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not configured for Knowledge Base template derivation")
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), timeout=OPENAI_TIMEOUT_SECONDS)
    selected_cases = [
        {key: cases[case_id].get(key) for key in ("test_case_id", "title", "description", "scenario", "preconditions", "test_data_setup", "data_needs", "steps", "expected_results", "deterministic_inputs") if cases[case_id].get(key) is not None}
        for case_id in case_ids
    ]
    prompt = {
        "instructions": (
            "Derive test data templates only from the selected package Knowledge Base evidence. "
            "Use database columns, API request fields, message template variables, and BRD data terms found in retrieved_knowledge_evidence. "
            "Do not use built-in or assumed application templates. Do not invent large unrelated record shapes. "
            "Return compact templates with fields needed to create the pre-step data for the approved test cases. "
            "Use dot-separated object paths such as member.member_id or prescription.status; do not use array paths. "
            "Assign at least one returned template to every approved test case."
        ),
        "approved_test_cases": selected_cases,
        "retrieved_knowledge_evidence": _rag_context_for_prompt(rag_context, max_evidence=18, max_fragment_characters=1100, max_rules=10, max_rule_characters=320),
    }
    response = responses_create(client,
        model=OPENAI_MODEL,
        input="Derive Knowledge Base test data templates as JSON.\n" + json.dumps(prompt, ensure_ascii=False),
        store=False,
        text={"format": {"type": "json_schema", "name": "knowledge_base_test_data_templates", "strict": True, "schema": _test_data_template_plan_schema(case_ids)}},
    )
    if not response.output_text:
        raise ValueError("OpenAI returned no Knowledge Base test data templates")
    return _normalize_test_data_templates(json.loads(response.output_text), case_ids)

_TEST_DATA_FIELD_STOPWORDS = {
    "select", "from", "where", "table", "column", "string", "integer", "number", "boolean", "object", "array",
    "description", "required", "optional", "status", "request", "response", "schema", "properties", "example",
    "business", "requirement", "criteria", "should", "shall", "must", "with", "without", "source", "package",
}

def _knowledge_field_names(rag_context):
    text = "\n".join(str(item.get("fragment_text") or "") for item in (rag_context or {}).get("brd_fragments", []) + (rag_context or {}).get("technical_fragments", []))
    names = []
    for value in re.findall(r'"([A-Za-z][A-Za-z0-9_]{1,60})"\s*:', text):
        names.append(value)
    for value in re.findall(r"\b[A-Za-z][A-Za-z0-9_]{2,60}\b", text):
        names.append(value)
    cleaned = []
    for name in names:
        normalized = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_")
        lowered = normalized.lower()
        if not lowered or lowered in _TEST_DATA_FIELD_STOPWORDS:
            continue
        if "_" not in lowered and not any(term in lowered for term in ("id", "date", "phone", "email", "name", "status", "type", "code")):
            continue
        if lowered not in cleaned:
            cleaned.append(lowered)
    return cleaned[:120]

_TEST_DATA_DOMAIN_ENTITIES = ("member", "prescription", "refill", "communication", "campaign", "eligibility", "eligibility_decision", "pharmacy")

def _test_case_text(test_case):
    parts = []
    for key in ("test_case_id", "title", "description", "scenario", "preconditions", "test_data_setup", "data_needs", "steps", "expected_results", "deterministic_inputs"):
        value = test_case.get(key)
        if value is not None:
            parts.append(json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value))
    return "\n".join(parts)

def _scenario_profile_for_case(test_case):
    text = _test_case_text(test_case).lower()
    ineligible = any(term in text for term in ("ineligible", "inactive", "terminated", "excluded", "not eligible", "suppress", "no communication"))
    sms_failure = any(term in text for term in ("sms fail", "sms fails", "delivery fail", "delivery fails", "undelivered", "fallback", "phone call", "phone-call"))
    phone_expected = sms_failure or any(term in text for term in ("phone", "call fallback", "alternate channel"))
    not_due = any(term in text for term in ("not due", "no refill", "not refill", "no prescription"))
    should_send_sms = not ineligible
    should_trigger_phone = bool(should_send_sms and phone_expected)
    return {
        "ineligible": ineligible,
        "sms_failure": sms_failure,
        "phone_expected": phone_expected,
        "not_due": not_due,
        "member_status": "INACTIVE" if ineligible else "ACTIVE",
        "eligibility_status": "INELIGIBLE" if ineligible else "ELIGIBLE",
        "prescription_status": "INACTIVE" if ineligible or not_due else "ACTIVE",
        "refill_status": "NOT_DUE" if not_due else "DUE",
        "sms_delivery_status": "SUPPRESSED" if ineligible else ("FAILED" if sms_failure else "PENDING"),
        "phone_call_status": "PENDING" if should_trigger_phone else "NOT_REQUIRED",
        "should_send_sms": should_send_sms,
        "should_trigger_phone": should_trigger_phone,
        "should_suppress_communication": ineligible,
    }

def _case_expected_text(test_case):
    expected = test_case.get("expected_results")
    if isinstance(expected, list):
        return "; ".join(str(item.get("text") if isinstance(item, dict) else item) for item in expected if item)
    return str(expected or test_case.get("description") or "")

def _knowledge_case_field_values(test_case, rag_context):
    fields = _knowledge_field_names(rag_context)
    if not fields:
        return {}
    text = _test_case_text(test_case).lower()
    terms = {term for term in re.findall(r"[a-z][a-z0-9_]{2,}", text)}
    priority_terms = {"member", "prescription", "refill", "communication", "sms", "phone", "campaign", "eligibility", "status", "date", "channel", "outcome"}
    selected = []
    for field in fields:
        parts = set(field.lower().split("_"))
        if parts & priority_terms or parts & terms:
            selected.append(field)
        if len(selected) >= 16:
            break
    return {field: "" for field in selected}

_TEST_DATA_SECTION_TERMS = {
    "member": ("member", "subscriber", "patient", "customer", "eligib", "active", "inactive", "contactable", "phone", "sms", "communication", "prescription", "refill", "campaign"),
    "prescription": ("prescription", "refill", "rx", "medication", "drug", "due", "not due"),
    "campaign": ("campaign", "reminder", "trigger", "rule", "suppress", "suppression"),
    "communication": ("communication", "sms", "message", "phone", "call", "delivery", "contactable", "channel"),
    "eligibility_decision": ("eligibility decision", "decision", "capture", "store", "stored", "validation", "validate", "qualify", "qualification"),
}

_TEST_DATA_FIELD_SECTION_HINTS = {
    "member": ("member", "subscriber", "patient", "customer", "first_name", "last_name", "phone", "email", "language", "opt_in"),
    "prescription": ("prescription", "refill", "rx", "medication", "drug", "due_date"),
    "campaign": ("campaign", "reminder", "trigger", "rule", "template", "suppression", "suppress"),
    "communication": ("communication", "sms", "message", "phone_call", "call", "delivery", "channel"),
    "eligibility_decision": ("eligibility", "decision", "qualification", "validation"),
}

_TEST_DATA_TEMPLATE_METADATA_FIELDS = {
    "column_name", "table_name", "schema_name", "type", "mapping_type", "source", "target",
    "mapping_validation_api_v1_mappings_validate_post",
}

def _required_test_data_sections(test_case):
    text = _test_case_text(test_case).lower()
    sections = {"expected_outcome"}
    for section, terms in _TEST_DATA_SECTION_TERMS.items():
        if any(term in text for term in terms):
            sections.add(section)
    for item in test_case.get("test_data_setup") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").lower()
        for section, terms in _TEST_DATA_SECTION_TERMS.items():
            if section in name or any(term in name for term in terms):
                sections.add(section)
    if len(sections) == 1:
        sections.add("member")
    if {"prescription", "communication", "campaign", "eligibility_decision"} & sections:
        sections.add("member")
    if "campaign" in sections and "reminder" in text:
        sections.add("communication")
    return sections

def _primary_test_data_entity(required_sections, test_case):
    text = _test_case_text(test_case).lower()
    if "communication" in required_sections and any(term in text for term in ("sms", "phone", "call", "message", "communication", "delivery")):
        return "communication"
    if "eligibility_decision" in required_sections:
        return "eligibility_decision"
    if "prescription" in required_sections:
        return "prescription"
    if "campaign" in required_sections:
        return "campaign"
    return "member"

def _scenario_test_data_payload(test_case, rag_context):
    profile = _scenario_profile_for_case(test_case)
    sections = _required_test_data_sections(test_case)
    payload = {}
    if "member" in sections:
        payload["member"] = {
            "member_id": "",
            "member_status": profile["member_status"],
            "eligibility_status": profile["eligibility_status"],
        }
        if "communication" in sections:
            payload["member"].update({
                "phone_number": "",
                "sms_opt_in": bool(profile["should_send_sms"]),
                "preferred_language": "EN",
            })
    if "prescription" in sections:
        payload["prescription"] = {
            "prescription_id": "",
            "member_id": "",
            "prescription_status": profile["prescription_status"],
            "refill_status": profile["refill_status"],
            "refill_due_date": "2026-01-01",
            "days_until_refill": 0,
            "refill_eligible": bool(not profile["ineligible"] and not profile["not_due"]),
        }
    if "campaign" in sections:
        payload["campaign"] = {
            "campaign_id": "",
            "campaign_name": "Refill Reminder",
            "eligibility_status": profile["eligibility_status"],
            "trigger_reason": "Prescription refill due",
            "suppress_communication": bool(profile["should_suppress_communication"]),
        }
    if "communication" in sections:
        payload["communication"] = {
            "communication_id": "",
            "member_id": "",
            "primary_channel": "SMS",
            "sms_delivery_status": profile["sms_delivery_status"],
            "phone_call_required": bool(profile["should_trigger_phone"]),
            "phone_call_status": profile["phone_call_status"],
            "message_template": "refill reminder",
        }
    if "eligibility_decision" in sections:
        payload["eligibility_decision"] = {
            "member_id": "",
            "eligibility_status": profile["eligibility_status"],
            "decision_reason": "Prescription refill due" if "prescription" in sections else "Requirement validation",
            "decision_stored": True,
        }
    expected = {"expected_result": _case_expected_text(test_case)}
    if "member" in sections:
        expected["should_identify_member"] = bool(not profile["ineligible"])
    if "communication" in sections:
        expected["should_send_sms"] = bool(profile["should_send_sms"])
        expected["should_trigger_phone_call"] = bool(profile["should_trigger_phone"])
    if "communication" in sections or "campaign" in sections:
        expected["should_suppress_communication"] = bool(profile["should_suppress_communication"])
    if "eligibility_decision" in sections:
        expected["should_store_eligibility_decision"] = True
    payload["expected_outcome"] = expected
    return payload

def _section_for_template_field(field_name, required_sections):
    field = str(field_name or "").lower()
    if field in _TEST_DATA_TEMPLATE_METADATA_FIELDS or field.startswith("mapping_validation_"):
        return None
    for section, terms in _TEST_DATA_FIELD_SECTION_HINTS.items():
        if section in required_sections and any(term in field for term in terms):
            return section
    return None

def _merge_relevant_template_payload(payload, source, required_sections):
    if not isinstance(payload, dict) or not isinstance(source, dict):
        return payload
    for key, value in source.items():
        normalized_key = str(key or "").lower()
        if normalized_key in _TEST_DATA_TEMPLATE_METADATA_FIELDS or normalized_key.startswith("mapping_validation_"):
            continue
        if normalized_key in required_sections and isinstance(value, dict):
            _merge_missing(payload.setdefault(normalized_key, {}), value)
            continue
        if normalized_key in required_sections:
            payload.setdefault(normalized_key, value)
            continue
        target_section = _section_for_template_field(normalized_key, required_sections)
        if target_section and target_section in payload:
            if isinstance(value, dict):
                _merge_missing(payload[target_section], value)
            elif normalized_key not in payload[target_section]:
                payload[target_section][normalized_key] = value
    return payload

def _merge_missing(target, source):
    if not isinstance(target, dict) or not isinstance(source, dict):
        return target
    for key, value in source.items():
        if key in target and isinstance(target[key], dict) and isinstance(value, dict):
            _merge_missing(target[key], value)
        elif key not in target:
            target[key] = value
    return target

def _domain_payload_from_template(template):
    payload = json.loads(template.get("payload_json") or "{}")
    if not isinstance(payload, dict):
        return {}
    entity = str(template.get("entity_name") or "").lower()
    if entity in _TEST_DATA_DOMAIN_ENTITIES and entity not in payload:
        return {entity: payload}
    return payload

def _expand_test_data_templates_for_cases(case_ids, cases, templates_by_case, rag_context):
    expanded = {}
    for case_id in case_ids:
        case = cases[case_id]
        required_sections = _required_test_data_sections(case)
        payload = _scenario_test_data_payload(case, rag_context)
        for template in templates_by_case.get(case_id, []):
            _merge_relevant_template_payload(payload, _domain_payload_from_template(template), required_sections)
        safe_case_id = re.sub(r"[^A-Za-z0-9_:-]+", "_", case_id).strip("_").lower()
        expanded[case_id] = [{
            "template_id": f"kb_case_dataset_{safe_case_id}",
            "template_name": "Knowledge Base Test Data",
            "entity_name": _primary_test_data_entity(required_sections, case),
            "payload_json": json.dumps(payload, sort_keys=True),
        }]
    return expanded

def _first_nested_value(payload, path):
    current = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current if current not in ("", None) else None

def _set_all_matching_keys(payload, key_name, value):
    if isinstance(payload, dict):
        for key, item in payload.items():
            if key.lower() == key_name.lower():
                payload[key] = value
            else:
                _set_all_matching_keys(item, key_name, value)
    elif isinstance(payload, list):
        for item in payload:
            _set_all_matching_keys(item, key_name, value)
    return payload

def _normalize_test_data_relationships(payload):
    if not isinstance(payload, dict):
        return payload
    member_id = _first_nested_value(payload, ("member", "member_id"))
    if not member_id:
        def find_member_id(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key.lower() == "member_id" and item not in ("", None):
                        return item
                    found = find_member_id(item)
                    if found:
                        return found
            elif isinstance(value, list):
                for item in value:
                    found = find_member_id(item)
                    if found:
                        return found
            return None
        member_id = find_member_id(payload)
    if member_id:
        _set_all_matching_keys(payload, "member_id", member_id)
    prescription_id = _first_nested_value(payload, ("prescription", "prescription_id"))
    if prescription_id:
        _set_all_matching_keys(payload, "prescription_id", prescription_id)
    return payload

def _generate_test_data_with_openai(templates, rag_context=None):
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not configured for test-data generation")
    from openai import OpenAI

    client = OpenAI(api_key=key, timeout=OPENAI_TIMEOUT_SECONDS)
    generated = {}
    for template, case_batch in templates:
        case_properties = {case["test_case_id"]: {"type": "object", "properties": {"payload": _test_data_value_schema(template["payload"])}, "required": ["payload"], "additionalProperties": False} for case in case_batch}
        schema = {"type": "object", "properties": {"records": {"type": "object", "properties": case_properties, "required": list(case_properties), "additionalProperties": False}}, "required": ["records"], "additionalProperties": False}
        prompt = {
            "instructions": "Generate synthetic scenario data for each approved test case using only the supplied versioned event template, approved case details, and retrieved package Knowledge Base evidence. Return a complete event for every case. Preserve the template's exact keys, nesting, array lengths, and value types. Choose values that make the event setup satisfy that specific case, including negative, boundary, consent, eligibility, and refill conditions when applicable. Keep member, prescription, drug, and contact values internally consistent. Use fictional test values only and the reserved +1-555 test phone shown by the template. Do not create event id, correlationId, time, source, type, or specversion values; the application sets the trace metadata after generation. Do not include explanations or keys outside the template.",
            "template": {"entity_name": template["entity_name"], "template_name": template["template_name"], "payload_shape_and_examples": template["payload"]},
            "approved_test_cases": [{key: case.get(key) for key in ("test_case_id", "title", "description", "scenario", "preconditions", "test_data_setup", "data_needs", "actions", "steps", "expected_results", "deterministic_inputs") if case.get(key) is not None} for case in case_batch],
            "retrieved_knowledge_evidence": _rag_context_for_prompt(rag_context),
        }
        response = responses_create(client,
            model=OPENAI_MODEL,
            input="Generate template-based test data as JSON.\n" + json.dumps(prompt, ensure_ascii=False),
            store=False,
            text={"format": {"type": "json_schema", "name": "test_data_records", "strict": True, "schema": schema}},
        )
        if not response.output_text:
            raise ValueError("OpenAI returned no test data")
        result = json.loads(response.output_text).get("records", {})
        expected_ids = {case["test_case_id"] for case in case_batch}
        if set(result) != expected_ids or any(not isinstance(item, dict) or not isinstance(item.get("payload"), dict) or not _matches_test_data_template_shape(item["payload"], template["payload"]) for item in result.values()):
            raise ValueError("OpenAI returned incomplete or invalid test data for the selected cases")
        generated.update({(case_id, template["template_id"]): item["payload"] for case_id, item in result.items()})
    return generated

def _test_data_generation_batches(assignments, cases, templates_by_case):
    by_template = {}
    for case_id in assignments:
        case = cases[case_id]
        for template in templates_by_case[case_id]:
            key = template["template_id"]
            by_template.setdefault(key, {"template": {"template_id": key, "entity_name": template["entity_name"], "template_name": template["template_name"], "payload": json.loads(template["payload_json"])}, "cases": []})["cases"].append(case)
    batches = []
    for item in by_template.values():
        batches.extend((item["template"], item["cases"][index:index + 10]) for index in range(0, len(item["cases"]), 10))
    return batches

def _pharmacy_notification_template():
    template_path = Path(__file__).resolve().parent / "templates" / "pharmacy_sample_notification_v1.json"
    try:
        template = json.loads(template_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(500, "The pharmacy notification test data template is unavailable or invalid.") from exc
    if not isinstance(template, dict) or not template.get("data", {}).get("members"):
        raise HTTPException(500, "The pharmacy notification test data template is missing its required member structure.")
    return {
        "template_id": "pharmacy_sample_notification_v1",
        "template_name": "Pharmacy Sample Notification v1",
        "entity_name": "pharmacy_notification",
        "payload": template,
    }

def _stamp_pharmacy_event_fields(payload, generation_id, test_case_id, timestamp=None):
    template = _pharmacy_notification_template()["payload"]
    digest = hashlib.sha256(f"{generation_id}:{test_case_id}".encode("utf-8")).hexdigest().upper()
    payload["id"] = f"evt-testiq-{digest[:24]}"
    payload["correlationId"] = f"testiq-run-{digest[:12].lower()}-case-{digest[12:24].lower()}"
    payload["time"] = timestamp or datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    for key in ("type", "source", "specversion", "growthplatform", "product"):
        payload[key] = template[key]
    if isinstance(payload.get("data"), dict):
        payload["data"]["recipientType"] = template["data"]["recipientType"]
        members = payload["data"].get("members")
        if isinstance(members, list):
            for ordinal, member in enumerate(members):
                if not isinstance(member, dict):
                    continue
                person = member.get("person")
                if isinstance(person, dict):
                    person["resourceID"] = _unique_test_value("member_id", f"{generation_id}:pharmacy_notification_v1", test_case_id, ordinal)
                preference = member.get("contactPreference")
                if isinstance(preference, dict):
                    phone_digest = hashlib.sha256(f"{generation_id}:{test_case_id}:phone:{ordinal}".encode("utf-8")).hexdigest()
                    phone_suffix = int(phone_digest[:12], 16) % 10_000_000
                    preference["safeTestMobile"] = f"+1555{phone_suffix:07d}"
    return payload

def _pharmacy_case_text(test_case):
    return " ".join(str(test_case.get(key) or "") for key in ("title", "description", "scenario", "preconditions", "steps", "expected_results", "assertions", "test_data_setup", "data_needs")).lower()

def _validate_pharmacy_notification(payload, test_case, seen_member_ids=None, seen_phones=None, seen_event_ids=None, seen_correlations=None, seen_times=None):
    template = _pharmacy_notification_template()["payload"]
    if not _matches_test_data_template_shape(payload, template):
        raise HTTPException(422, "Generated pharmacy notification does not match the required template structure or field types.")

    def check_populated(value, path="event"):
        if isinstance(value, dict):
            for key, child in value.items():
                check_populated(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                check_populated(child, f"{path}[{index}]")
        elif isinstance(value, str) and not value.strip():
            raise HTTPException(422, f"Generated pharmacy notification is missing a value at {path}.")

    check_populated(payload)
    try:
        event_time = datetime.fromisoformat(payload["time"].replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise HTTPException(422, "Generated pharmacy notification has an invalid event time.")
    for value, seen_values, label in (
        (payload["id"], seen_event_ids, "event id"),
        (payload["correlationId"], seen_correlations, "correlationId"),
        (event_time.isoformat(), seen_times, "event time"),
    ):
        if seen_values is not None and value in seen_values:
            raise HTTPException(422, f"Generated test data reuses a {label} across test cases.")
        if seen_values is not None:
            seen_values.add(value)

    members = payload["data"]["members"]
    member_ids, phone_values = [], []
    for member in members:
        member_id = member["person"].get("resourceID")
        if not isinstance(member_id, str) or not member_id.strip():
            raise HTTPException(422, "Each generated member must have a resourceID.")
        member_ids.append(member_id)
        mobile = member["contactPreference"].get("safeTestMobile")
        if not re.fullmatch(r"\+1555\d{7}", mobile or ""):
            raise HTTPException(422, "Generated contact data must use a safe synthetic +1-555 test mobile number.")
        phone_values.append(mobile)
        if not member["prescription"]:
            raise HTTPException(422, "Each generated member must have at least one prescription.")

    if len(member_ids) != len(set(member_ids)) or (seen_member_ids is not None and any(item in seen_member_ids for item in member_ids)):
        raise HTTPException(422, "Generated test data reuses a member resourceID across members or test cases.")
    if len(phone_values) != len(set(phone_values)) or (seen_phones is not None and any(item in seen_phones for item in phone_values)):
        raise HTTPException(422, "Generated test data reuses a safe test mobile across members or test cases.")
    if seen_member_ids is not None:
        seen_member_ids.update(member_ids)
    if seen_phones is not None:
        seen_phones.update(phone_values)

    scenario_text = _pharmacy_case_text(test_case)
    prescriptions = [prescription for member in members for prescription in member["prescription"]]
    excluded_case = any(term in scenario_text for term in ("ineligible", "not eligible", "excluded member", "eligibility exclusion"))
    eligible_case = not excluded_case and any(term in scenario_text for term in ("eligible member", "member is eligible", "eligible patient"))
    if excluded_case and any(prescription["eligibleInd"] is not False for prescription in prescriptions):
        raise HTTPException(422, "Generated prescription eligibility does not match the approved ineligible or exclusion test case.")
    if eligible_case and not any(prescription["eligibleInd"] is True for prescription in prescriptions):
        raise HTTPException(422, "Generated prescription eligibility does not match the approved eligible-member test case.")

    no_consent = any(term in scenario_text for term in ("without sms consent", "no sms consent", "sms consent is false", "consent revoked", "declined sms consent"))
    consented = not no_consent and any(term in scenario_text for term in ("with sms consent", "sms consent is true", "consented to sms"))
    if no_consent and any(member["contactPreference"]["smsConsent"] is not False for member in members):
        raise HTTPException(422, "Generated SMS consent does not match the approved no-consent test case.")
    if consented and not any(member["contactPreference"]["smsConsent"] is True for member in members):
        raise HTTPException(422, "Generated SMS consent does not match the approved consented test case.")

@app.post("/api/v1/test-case-generations/{generation_id}/test-data", status_code=201)
def generate_test_data(generation_id: str, request: TestDataGenerationRequest):
    with connection() as conn:
        generation = conn.execute("SELECT * FROM test_case_generation WHERE generation_id=%s", (generation_id,)).fetchone()
        if not generation:
            raise HTTPException(404, "Test-case generation run not found")
        contract = json.loads(generation["contract_json"])
        assignments = request.assignments or [TestDataCaseAssignment(test_case_id=case_id, template_ids=request.template_ids) for case_id in request.test_case_ids]
        if not assignments:
            raise HTTPException(422, "Assign at least one template to each selected test case")
        assignment_case_ids = [item.test_case_id for item in assignments]
        if len(set(assignment_case_ids)) != len(assignment_case_ids):
            raise HTTPException(422, "Each test case assignment must be unique")
        cases = {case.get("test_case_id"): case for case in contract.get("test_cases", [])}
        unknown = sorted(set(assignment_case_ids) - set(cases))
        if unknown:
            raise HTTPException(422, {"unknown_test_case_ids": unknown})
        unapproved = [case_id for case_id in assignment_case_ids if _review_status(cases[case_id]) != "approved"]
        if unapproved:
            raise HTTPException(422, {"message": "Test data can only be generated for approved test cases", "not_approved": unapproved})
        existing_rows = conn.execute("SELECT status FROM test_data_record WHERE generation_id=%s", (generation_id,)).fetchall()
        if existing_rows:
            if not request.replace_existing_drafts:
                raise HTTPException(409, "Test data already exists for this generation. Use regenerate for draft records or create a new test-case generation.")
            if any(item["status"] == "saved" for item in existing_rows):
                raise HTTPException(409, "Saved Bronze records are immutable. Create a new test-case generation to produce another dataset.")
        rag = _require_test_data_knowledge(generation["package_id"])
        event_template = _pharmacy_notification_template()
        template_record = {
            "template_id": event_template["template_id"],
            "template_name": event_template["template_name"],
            "entity_name": event_template["entity_name"],
            "payload_json": json.dumps(event_template["payload"], sort_keys=True),
        }
        selected_templates_by_case = {case_id: [template_record] for case_id in assignment_case_ids}
        try:
            generated_payloads = _generate_test_data_with_openai(_test_data_generation_batches(assignment_case_ids, cases, selected_templates_by_case), rag)
        except Exception as exc:
            raise HTTPException(502, f"OpenAI could not generate test data for the approved cases: {safe_llm_error_message(exc)}. No local data fallback was used.") from exc
        if existing_rows:
            conn.execute("DELETE FROM test_data_record WHERE generation_id=%s AND status='draft'", (generation_id,))
        created = []
        timestamp = now()
        generated_member_ids, generated_phones = set(), set()
        generated_event_ids, generated_correlations, generated_times = set(), set(), set()
        for case_id in assignment_case_ids:
            test_case = cases[case_id]
            payload = generated_payloads[(case_id, event_template["template_id"])]
            payload = _stamp_pharmacy_event_fields(payload, generation_id, case_id)
            _validate_pharmacy_notification(payload, test_case, generated_member_ids, generated_phones, generated_event_ids, generated_correlations, generated_times)
            for template in selected_templates_by_case[case_id]:
                record_id = uid("tdr")
                pre_steps = test_case.get("test_data_setup") or []
                schema_context = [{"source_type": item.get("source_type"), "source_reference": item.get("source_reference"), "fragment_id": item.get("original_fragment_id") or item.get("fragment_id"), "text": item.get("fragment_text")} for item in (rag or {}).get("technical_fragments", [])]
                conn.execute("INSERT INTO test_data_record (record_id,generation_id,test_case_id,template_id,template_name,entity_name,payload_json,pre_steps_json,schema_context_json,status,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (record_id, generation_id, case_id, template["template_id"], template["template_name"], template["entity_name"], json.dumps(payload, sort_keys=True), json.dumps(pre_steps), json.dumps(schema_context), "draft", timestamp, timestamp))
                created.append({"record_id": record_id, "generation_id": generation_id, "test_case_id": case_id, "template_id": template["template_id"], "template_name": template["template_name"], "entity_name": template["entity_name"], "payload": payload, "pre_steps": pre_steps, "schema_context": schema_context, "status": "draft", "created_at": timestamp})
        return {"generation_id": generation_id, "generation_mode": "openai", "generation_model": OPENAI_MODEL, "rag_metadata": _rag_metadata_or_none(rag), "rag_status": _rag_status(rag, required=True), "records": created}

@app.get("/api/v1/test-case-generations/{generation_id}/test-data")
def list_test_data(generation_id: str):
    with connection() as conn:
        if not conn.execute("SELECT generation_id FROM test_case_generation WHERE generation_id=%s", (generation_id,)).fetchone():
            raise HTTPException(404, "Test-case generation run not found")
        rows = conn.execute("SELECT r.* FROM test_data_record r WHERE r.generation_id=%s ORDER BY r.test_case_id,r.entity_name", (generation_id,)).fetchall()
        return [_test_data_record_response(row) for row in rows]

@app.put("/api/v1/test-case-generations/{generation_id}/test-data/review")
def review_test_data(generation_id: str, request: TestDataReviewRequest):
    with connection() as conn:
        rows = conn.execute("SELECT record_id FROM test_data_record WHERE generation_id=%s", (generation_id,)).fetchall()
        known = {row["record_id"] for row in rows}
        submitted = {item.record_id for item in request.items}
        if submitted != known:
            raise HTTPException(422, {"unknown_record_ids": sorted(submitted - known), "missing_record_ids": sorted(known - submitted)})
        timestamp = now()
        for item in request.items:
            conn.execute("UPDATE test_data_record SET payload_json=%s,updated_at=%s WHERE generation_id=%s AND record_id=%s AND status='draft'", (json.dumps(item.payload, sort_keys=True), timestamp, generation_id, item.record_id))
        saved_rows = conn.execute("SELECT r.* FROM test_data_record r WHERE r.generation_id=%s ORDER BY r.test_case_id,r.entity_name", (generation_id,)).fetchall()
        return [_test_data_record_response(row) for row in saved_rows]

_BRONZE_TABLES = {"raw_file", "membership", "prescription", "order", "claim", "benefit", "drug", "medical_claim", "product"}

def _bronze_trace_fields(row):
    return {
        "source_event_id": f"testiq-{row['record_id']}",
        "source_correlation_id": f"testiq-{row['generation_id']}-{row['test_case_id']}",
        "testiq_record_id": row["record_id"],
        "testiq_generation_id": row["generation_id"],
        "testiq_test_case_id": row["test_case_id"],
    }

def _bronze_member_data(payload, row):
    member = payload.get("member") or payload.get("membership") or {}
    decision = payload.get("eligibility_decision") or {}
    communication = payload.get("communication") or {}
    member_id = member.get("member_id") or decision.get("member_id") or communication.get("member_id")
    phone_number = member.get("phone_number") or member.get("phone") or communication.get("phone_number")
    sms_enabled = member.get("sms_opt_in")
    if sms_enabled is None:
        sms_enabled = communication.get("primary_channel") == "SMS"
    data = {
        **_bronze_trace_fields(row),
        "member_id": member_id,
        "subscriber_id": member.get("subscriber_id") or member_id,
        "first_name": member.get("first_name"),
        "last_name": member.get("last_name"),
        "status": member.get("member_status") or member.get("status") or decision.get("eligibility_status"),
        "preferred_language": member.get("preferred_language"),
        "eligibility_status": member.get("eligibility_status") or decision.get("eligibility_status"),
        "eligibility_decision": decision or None,
        "phones": [{"phone_type": "MOBILE", "phone_number": phone_number, "is_primary": True}] if phone_number else [],
        "emails": [],
        "addresses": [],
        "payment_methods": [],
        "communication_preferences": [{
            "channel": "SMS",
            "value": phone_number,
            "enabled": bool(sms_enabled),
            "language": member.get("preferred_language"),
            "preference_type": "PRIMARY",
            "address_reference": None,
        }] if phone_number else [],
    }
    return {key: value for key, value in data.items() if value is not None}

def _bronze_prescription_data(payload, row):
    prescription = payload.get("prescription") or {}
    data = {
        **_bronze_trace_fields(row),
        "prescription_id": prescription.get("prescription_id"),
        "rx_number": prescription.get("rx_number") or prescription.get("prescription_id"),
        "member_id": prescription.get("member_id") or (payload.get("member") or {}).get("member_id"),
        "status": prescription.get("prescription_status") or prescription.get("status"),
        "refill_status": prescription.get("refill_status"),
        "next_refill_date": prescription.get("refill_due_date") or prescription.get("next_refill_date"),
        "days_until_refill": prescription.get("days_until_refill"),
        "eligible_ind": prescription.get("refill_eligible"),
        "refills_remaining": prescription.get("refills_remaining"),
        "refills_allowed": prescription.get("refills_allowed"),
        "fill_method": prescription.get("fill_method") or "MANUAL",
        "autofill_enrolled_ind": prescription.get("autofill_enrolled_ind", False),
    }
    return {key: value for key, value in data.items() if value is not None}

def _bronze_order_data(payload, row):
    communication = payload.get("communication") or {}
    campaign = payload.get("campaign") or {}
    member = payload.get("member") or {}
    prescription = payload.get("prescription") or {}
    data = {
        **_bronze_trace_fields(row),
        "order_id": communication.get("communication_id") or campaign.get("campaign_id") or f"ORDER-{row['record_id']}",
        "member_id": communication.get("member_id") or member.get("member_id"),
        "prescription_id": prescription.get("prescription_id"),
        "campaign_id": campaign.get("campaign_id"),
        "campaign_name": campaign.get("campaign_name"),
        "trigger_reason": campaign.get("trigger_reason"),
        "suppress_communication": campaign.get("suppress_communication"),
        "primary_channel": communication.get("primary_channel"),
        "message_template": communication.get("message_template"),
        "sms_delivery_status": communication.get("sms_delivery_status"),
        "phone_call_required": communication.get("phone_call_required"),
        "phone_call_status": communication.get("phone_call_status"),
        "status": communication.get("sms_delivery_status") or "PENDING",
    }
    return {key: value for key, value in data.items() if value is not None}

def _bronze_rows_for_payload(payload, row):
    if isinstance(payload, dict) and payload.get("type") == "pharmacy.orders.v1.sample-notification" and payload.get("specversion") == "1.0":
        # PBM's Silver procedure consumes domain rows tied to testiq.ingestion_load.
        return [("membership", payload), ("prescription", payload)]
    rows = []
    if payload.get("member") or payload.get("membership") or payload.get("eligibility_decision"):
        rows.append(("membership", _bronze_member_data(payload, row)))
    if payload.get("prescription"):
        rows.append(("prescription", _bronze_prescription_data(payload, row)))
    if payload.get("communication") or payload.get("campaign") or payload.get("order"):
        rows.append(("order", _bronze_order_data(payload, row)))
    entity_name = str(row["entity_name"] or "").lower()
    if not rows and entity_name in _BRONZE_TABLES:
        rows.append((entity_name, {**_bronze_trace_fields(row), **payload}))
    return [(table, data) for table, data in rows if table in _BRONZE_TABLES and data]

def _insert_bronze_domain_row(conn, table, load_id, data):
    if table == "raw_file":
        from psycopg import sql
        import hashlib
        from psycopg.types.json import Jsonb
        raw_bytes = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(raw_bytes).hexdigest()
        source_uri = f"testiq://test-data/{data['correlationId']}"
        filename = f"{data['id']}.json"
        row = conn.execute(
            sql.SQL("INSERT INTO {} "
                "(source_uri,source_path,original_filename,file_size_bytes,source_modified_at,sha256,raw_bytes,payload,status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'BRONZE_LOADED') "
                "ON CONFLICT (sha256) DO NOTHING RETURNING raw_file_id").format(sql.Identifier(BRONZE_SCHEMA, "raw_file")),
            (source_uri, source_uri, filename, len(raw_bytes), datetime.now(timezone.utc), digest, raw_bytes, Jsonb(data)),
        ).fetchone()
        if not row:
            row = conn.execute(
                sql.SQL("SELECT raw_file_id FROM {} WHERE sha256=%s").format(sql.Identifier(BRONZE_SCHEMA, "raw_file")),
                (digest,),
            ).fetchone()
        if not row:
            raise RuntimeError("Bronze raw-file insert did not return a stored event reference")
        return {"table": table, "id": str(row["raw_file_id"])}
    from psycopg import sql
    from psycopg.types.json import Jsonb
    sequence_name = f"{BRONZE_SCHEMA}.{table}_id_seq"
    statement = sql.SQL("INSERT INTO {} (id, load_id, data) VALUES (nextval({}::regclass), %s, %s) RETURNING id").format(
        sql.Identifier(BRONZE_SCHEMA, table),
        sql.Literal(sequence_name),
    )
    bronze_row = conn.execute(statement, (load_id, Jsonb(data))).fetchone()
    return {"table": table, "id": str(bronze_row["id"])}

def _event_bronze_records(payload):
    """Map the pharmacy CloudEvent to the same Bronze JSON contracts as PBM ingestion."""
    metadata = {
        "source_event_id": payload["id"],
        "source_correlation_id": payload["correlationId"],
    }
    members, prescriptions = [], []
    for item in payload["data"].get("members") or []:
        person = item["person"]
        preference = item.get("contactPreference") or {}
        comms, phones = [], []
        channel_values = (
            ("SMS", preference.get("smsConsent", False), preference.get("safeTestMobile")),
            ("EMAIL", preference.get("emailConsent", False), preference.get("safeTestEmail")),
            ("PORTAL", preference.get("portalConsent", False), preference.get("safeTestPortal")),
        )
        for channel, enabled, destination in channel_values:
            if destination:
                comms.append({
                    "preference_type": "PRIMARY" if preference.get("preferredChannel") == channel else "SECONDARY",
                    "channel": channel, "value": destination, "address_reference": None,
                    "language": None, "enabled": enabled,
                })
        if preference.get("safeTestMobile"):
            phones.append({"phone_type": "MOBILE", "phone_number": preference["safeTestMobile"],
                           "is_primary": preference.get("preferredChannel") == "SMS"})
        members.append({
            **metadata, "member_id": person["resourceID"], "subscriber_id": person["resourceID"],
            "dependent_number": None, "relationship_to_subscriber": None,
            "first_name": person["firstName"], "middle_name": None, "last_name": person["lastName"],
            "date_of_birth": None, "gender": None, "status": "ACTIVE", "preferred_language": None,
            "marital_status": None, "race_ethnicity": None, "effective_date": None,
            "termination_date": None, "addresses": [], "phones": phones, "emails": [],
            "communication_preferences": comms, "payment_methods": [],
            "speciality_id": person["specialityID"],
        })
        for rx in item["prescription"]:
            drug = rx["drug"]
            prescriptions.append({
                **metadata, "prescription_id": rx["resourceID"], "rx_number": rx["resourceID"],
                "member_id": person["resourceID"], "status": rx.get("rxStatus", "ACTIVE"),
                "eligible_ind": rx.get("eligibleInd", True), "refills_remaining": rx.get("refillsRemaining", 0),
                "next_refill_date": rx.get("nextRefillDate"),
                "autofill_enrolled_ind": rx.get("autofillEnrolled", False),
                "fill_method": rx.get("fillMethod", "MANUAL"),
                "refills_allowed": rx.get("refillsRemaining", 0), "refill_number": 0,
                "drug_name": drug["name"], "strength": drug["strength"],
                "therapy_code": drug["therapyCode"], "manufacturer": drug["manufacturer"],
                "ndc": None, "route": None, "dosage_form": None, "prescribed_date": None,
                "dispensed_date": None, "days_supply": None, "quantity_dispensed": None,
            })
    return members, prescriptions

def _insert_pbm_event_load(conn, payload, generation_id, test_case_id, row):
    from psycopg.types.json import Jsonb
    from psycopg import sql
    members, prescriptions = _event_bronze_records(payload)
    load_id = str(conn.execute("SELECT nextval('testiq.ingestion_load_id_seq') AS value").fetchone()["value"])
    event_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(event_bytes).hexdigest()
    conn.execute(
        "INSERT INTO testiq.ingestion_load (load_id,event_id,correlation_id,original_filename,sha256,source_system,document_type,document_version,file_size_bytes,status,member_count,prescription_count) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'BRONZE_LOADED',%s,%s)",
        (load_id, payload["id"], payload["correlationId"], f"{payload['id']}.json", digest,
         payload["source"], payload["type"], payload["specversion"], len(event_bytes), len(members), len(prescriptions)),
    )
    refs = []
    created_time = payload["time"]
    for table, records in (("membership", members), ("prescription", prescriptions)):
        for record in records:
            result = conn.execute(
                sql.SQL("INSERT INTO {} (load_id,created_time,data) VALUES (%s,%s,%s) RETURNING id").format(
                    sql.Identifier(BRONZE_SCHEMA, table)
                ),
                (load_id, created_time, Jsonb(record)),
            ).fetchone()
            refs.append({"table": table, "id": str(result["id"])})
    return load_id, refs

def _bronze_ref_id(record_id: str, bronze_schema: str, bronze_table: str, bronze_id: str) -> str:
    digest = hashlib.sha256(f"{record_id}:{bronze_schema}:{bronze_table}:{bronze_id}".encode("utf-8")).hexdigest()[:24]
    return f"bref_{digest}"

def _record_bronze_refs(conn, row, bronze_refs, load_id: str) -> None:
    timestamp = now()
    for ref in bronze_refs:
        bronze_table = ref["table"]
        bronze_id = str(ref["id"])
        conn.execute(
            "INSERT INTO test_data_bronze_ref (bronze_ref_id,record_id,generation_id,test_case_id,bronze_schema,bronze_table,bronze_id,bronze_load_id,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (record_id, bronze_schema, bronze_table, bronze_id) DO NOTHING",
            (_bronze_ref_id(row["record_id"], BRONZE_SCHEMA, bronze_table, bronze_id), row["record_id"], row["generation_id"], row["test_case_id"], BRONZE_SCHEMA, bronze_table, bronze_id, load_id, timestamp),
        )

def _parse_legacy_bronze_refs(row):
    refs = []
    for item in str(row.get("bronze_raw_file_id") or "").split(","):
        if ":" not in item:
            continue
        table, bronze_id = [part.strip() for part in item.split(":", 1)]
        if table in _BRONZE_TABLES and bronze_id:
            refs.append({
                "bronze_ref_id": _bronze_ref_id(row["record_id"], BRONZE_SCHEMA, table, bronze_id),
                "record_id": row["record_id"],
                "generation_id": row["generation_id"],
                "test_case_id": row["test_case_id"],
                "bronze_schema": BRONZE_SCHEMA,
                "bronze_table": table,
                "bronze_id": bronze_id,
                "bronze_load_id": f"testiq-{row['generation_id']}",
                "created_at": row.get("updated_at") or row.get("created_at") or now(),
            })
    return refs

def _bronze_refs_for_record(conn, row):
    refs = [row_dict(item) for item in conn.execute(
        "SELECT * FROM test_data_bronze_ref WHERE record_id=%s ORDER BY bronze_table,bronze_id",
        (row["record_id"],),
    ).fetchall()]
    if refs:
        return refs
    refs = _parse_legacy_bronze_refs(row_dict(row))
    for ref in refs:
        conn.execute(
            "INSERT INTO test_data_bronze_ref (bronze_ref_id,record_id,generation_id,test_case_id,bronze_schema,bronze_table,bronze_id,bronze_load_id,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (record_id, bronze_schema, bronze_table, bronze_id) DO NOTHING",
            (ref["bronze_ref_id"], ref["record_id"], ref["generation_id"], ref["test_case_id"], ref["bronze_schema"], ref["bronze_table"], ref["bronze_id"], ref["bronze_load_id"], ref["created_at"]),
        )
    return refs

def _fetch_bronze_domain_row(conn, table: str, bronze_id: str):
    if table not in _BRONZE_TABLES:
        return None
    if table == "raw_file":
        try:
            identifier = uuid.UUID(str(bronze_id))
        except (TypeError, ValueError):
            return None
        from psycopg import sql
        row = conn.execute(
            sql.SQL("SELECT raw_file_id AS id,status AS load_id,created_at AS created_time,payload AS data "
                "FROM {} WHERE raw_file_id=%s").format(sql.Identifier(BRONZE_SCHEMA, "raw_file")),
            (identifier,),
        ).fetchone()
        if not row:
            return None
        item = row_dict(row)
        item["id"] = str(item["id"])
        return item
    from psycopg import sql
    identifier = int(bronze_id) if str(bronze_id).isdigit() else bronze_id
    statement = sql.SQL("SELECT id, load_id, created_time, data FROM {} WHERE id=%s").format(sql.Identifier(BRONZE_SCHEMA, table))
    row = conn.execute(statement, (identifier,)).fetchone()
    if not row:
        return None
    item = row_dict(row)
    item["id"] = str(item["id"])
    return item

def _add_assertion(assertions, name: str, passed: bool, expected=None, actual=None, message: str | None = None) -> None:
    assertions.append({
        "name": name,
        "status": "passed" if passed else "failed",
        "expected": expected,
        "actual": actual,
        "message": message or ("Matched expected value." if passed else "Value did not match expected data."),
    })

def _first_actual(actual_by_table, table: str):
    rows = actual_by_table.get(table) or []
    return rows[0]["data"] if rows else None

def _assert_equal_if_present(assertions, name: str, expected, actual):
    if expected is None:
        return
    _add_assertion(assertions, name, expected == actual, expected, actual)

def _evaluate_record_payload(payload, actual_by_table, assertions):
    if isinstance(payload, dict) and payload.get("type") == "pharmacy.orders.v1.sample-notification":
        expected_members = payload.get("data", {}).get("members", [])
        bronze_members = [_item["data"] for _item in actual_by_table.get("membership", [])]
        bronze_prescriptions = [_item["data"] for _item in actual_by_table.get("prescription", [])]
        expected_member_ids = [item["person"]["resourceID"] for item in expected_members]
        expected_rx_ids = [rx["resourceID"] for item in expected_members for rx in item["prescription"]]
        _add_assertion(assertions, "PBM membership Bronze rows exist", len(bronze_members) == len(expected_member_ids), len(expected_member_ids), len(bronze_members))
        _add_assertion(assertions, "PBM prescription Bronze rows exist", len(bronze_prescriptions) == len(expected_rx_ids), len(expected_rx_ids), len(bronze_prescriptions))
        _add_assertion(assertions, "member resourceIDs mapped to Bronze", sorted(item.get("member_id") for item in bronze_members) == sorted(expected_member_ids), sorted(expected_member_ids), sorted(item.get("member_id") for item in bronze_members))
        _add_assertion(assertions, "prescription resourceIDs mapped to Bronze", sorted(item.get("prescription_id") for item in bronze_prescriptions) == sorted(expected_rx_ids), sorted(expected_rx_ids), sorted(item.get("prescription_id") for item in bronze_prescriptions))
        return
    member = payload.get("member") or payload.get("membership") or {}
    decision = payload.get("eligibility_decision") or {}
    prescription = payload.get("prescription") or {}
    communication = payload.get("communication") or {}
    campaign = payload.get("campaign") or {}
    expected_outcome = payload.get("expected_outcome") or {}

    membership = _first_actual(actual_by_table, "membership")
    if member or decision:
        _add_assertion(assertions, "membership row exists", bool(membership), "membership row", "found" if membership else "missing")
    if membership:
        _assert_equal_if_present(assertions, "member_id stored in Bronze", member.get("member_id") or decision.get("member_id") or communication.get("member_id"), membership.get("member_id"))
        _assert_equal_if_present(assertions, "member status stored in Bronze", member.get("member_status") or member.get("status") or decision.get("eligibility_status"), membership.get("status"))
        _assert_equal_if_present(assertions, "eligibility status stored in Bronze", member.get("eligibility_status") or decision.get("eligibility_status"), membership.get("eligibility_status"))
        if expected_outcome.get("should_identify_member") is not None:
            _add_assertion(assertions, "member can be identified", bool(membership.get("member_id")) == bool(expected_outcome.get("should_identify_member")), expected_outcome.get("should_identify_member"), bool(membership.get("member_id")))

    prescription_row = _first_actual(actual_by_table, "prescription")
    if prescription:
        _add_assertion(assertions, "prescription row exists", bool(prescription_row), "prescription row", "found" if prescription_row else "missing")
    if prescription_row:
        _assert_equal_if_present(assertions, "prescription_id stored in Bronze", prescription.get("prescription_id"), prescription_row.get("prescription_id"))
        _assert_equal_if_present(assertions, "prescription member_id stored in Bronze", prescription.get("member_id") or member.get("member_id"), prescription_row.get("member_id"))
        _assert_equal_if_present(assertions, "refill eligibility stored in Bronze", prescription.get("refill_eligible"), prescription_row.get("eligible_ind"))
        _assert_equal_if_present(assertions, "refill status stored in Bronze", prescription.get("refill_status"), prescription_row.get("refill_status"))

    order_row = _first_actual(actual_by_table, "order")
    if communication or campaign:
        _add_assertion(assertions, "communication or campaign row exists", bool(order_row), "order row", "found" if order_row else "missing")
    if order_row:
        _assert_equal_if_present(assertions, "communication member_id stored in Bronze", communication.get("member_id") or member.get("member_id"), order_row.get("member_id"))
        _assert_equal_if_present(assertions, "primary channel stored in Bronze", communication.get("primary_channel"), order_row.get("primary_channel"))
        _assert_equal_if_present(assertions, "SMS status stored in Bronze", communication.get("sms_delivery_status"), order_row.get("sms_delivery_status"))
        _assert_equal_if_present(assertions, "phone fallback flag stored in Bronze", communication.get("phone_call_required"), order_row.get("phone_call_required"))
        _assert_equal_if_present(assertions, "campaign_id stored in Bronze", campaign.get("campaign_id"), order_row.get("campaign_id"))
        _assert_equal_if_present(assertions, "suppression flag stored in Bronze", campaign.get("suppress_communication"), order_row.get("suppress_communication"))
        if expected_outcome.get("should_send_sms") is not None:
            _add_assertion(assertions, "SMS expectation matches setup", (order_row.get("primary_channel") == "SMS") == bool(expected_outcome.get("should_send_sms")), expected_outcome.get("should_send_sms"), order_row.get("primary_channel") == "SMS")
        if expected_outcome.get("should_trigger_phone_call") is not None:
            _add_assertion(assertions, "phone fallback expectation matches setup", bool(order_row.get("phone_call_required")) == bool(expected_outcome.get("should_trigger_phone_call")), expected_outcome.get("should_trigger_phone_call"), bool(order_row.get("phone_call_required")))
        if expected_outcome.get("should_suppress_communication") is not None and order_row.get("suppress_communication") is not None:
            _add_assertion(assertions, "suppression expectation matches setup", bool(order_row.get("suppress_communication")) == bool(expected_outcome.get("should_suppress_communication")), expected_outcome.get("should_suppress_communication"), bool(order_row.get("suppress_communication")))

def _execute_case_against_bronze(conn, run_id: str, generation_id: str, test_case_id: str, started_at: str):
    rows = conn.execute(
        "SELECT * FROM test_data_record WHERE generation_id=%s AND test_case_id=%s AND status='saved' ORDER BY entity_name,record_id",
        (generation_id, test_case_id),
    ).fetchall()
    assertions = []
    expected_records = []
    actual_records = []
    all_refs = []
    silver_actual = {"members": [], "prescriptions": []}
    if not rows:
        _add_assertion(assertions, "saved test data exists", False, "saved test data", "none")
    for row in rows:
        payload = json.loads(row["payload_json"])
        refs = _bronze_refs_for_record(conn, row)
        serializable_refs = [{key: ref[key] for key in ("bronze_ref_id", "record_id", "bronze_schema", "bronze_table", "bronze_id", "bronze_load_id", "created_at") if key in ref} for ref in refs]
        all_refs.extend(serializable_refs)
        _add_assertion(assertions, f"{row['record_id']} has Bronze reference", bool(refs), "at least one Bronze reference", f"{len(refs)} reference(s)")
        actual_by_table = {}
        for ref in refs:
            bronze_row = _fetch_bronze_domain_row(conn, ref["bronze_table"], ref["bronze_id"])
            _add_assertion(assertions, f"{ref['bronze_table']}:{ref['bronze_id']} exists", bool(bronze_row), "Bronze row", "found" if bronze_row else "missing")
            if bronze_row:
                actual_item = {
                    "record_id": row["record_id"],
                    "bronze_table": ref["bronze_table"],
                    "bronze_id": ref["bronze_id"],
                    "load_id": bronze_row.get("load_id"),
                    "data": bronze_row.get("data") or {},
                }
                actual_records.append(actual_item)
                actual_by_table.setdefault(ref["bronze_table"], []).append(actual_item)
        _evaluate_record_payload(payload, actual_by_table, assertions)
        if payload.get("type") == "pharmacy.orders.v1.sample-notification":
            load_ids = sorted({str(ref["bronze_load_id"]) for ref in refs if ref.get("bronze_load_id")})
            for load_id in load_ids:
                load = conn.execute("SELECT status,error_message FROM testiq.ingestion_load WHERE load_id=%s", (load_id,)).fetchone()
                if not load:
                    _add_assertion(assertions, f"PBM load {load_id} exists", False, "testiq.ingestion_load row", "missing")
                    continue
                if load["status"] == "BRONZE_LOADED":
                    conn.execute("CALL testiq.process_bronze_load_to_silver(%s,'TESTIQ_EXECUTION')", (load_id,))
                    load = conn.execute("SELECT status,error_message FROM testiq.ingestion_load WHERE load_id=%s", (load_id,)).fetchone()
                _add_assertion(assertions, f"PBM Silver promotion {load_id} completed", load["status"] == "COMPLETED", "COMPLETED", load["status"], load.get("error_message") or "PBM load did not complete cleanly.")
                audit = conn.execute("SELECT status,membership_records_read,membership_records_processed,membership_records_failed,prescription_records_read,prescription_records_processed,prescription_records_failed,unhandled_entity_counts,error_message FROM testiq.bronze_silver_run_audit WHERE load_id=%s ORDER BY started_at DESC LIMIT 1", (load_id,)).fetchone()
                _add_assertion(assertions, f"PBM orchestration audit {load_id} completed", bool(audit) and audit["status"] == "COMPLETED", "COMPLETED audit", audit["status"] if audit else "missing", audit.get("error_message") if audit else "No PBM orchestration audit was recorded.")
                if audit:
                    _add_assertion(assertions, f"PBM membership rows processed {load_id}", audit["membership_records_failed"] == 0 and audit["membership_records_processed"] == audit["membership_records_read"], audit["membership_records_read"], audit["membership_records_processed"])
                    _add_assertion(assertions, f"PBM prescription rows processed {load_id}", audit["prescription_records_failed"] == 0 and audit["prescription_records_processed"] == audit["prescription_records_read"], audit["prescription_records_read"], audit["prescription_records_processed"])
                silver_members = conn.execute("SELECT member_id,first_name,last_name,status FROM silverbox.member WHERE load_id=%s ORDER BY member_id", (load_id,)).fetchall()
                silver_prescriptions = conn.execute("SELECT prescription_id,member_id,status,eligible_ind,refills_remaining,next_refill_date,drug_name,strength FROM silverbox.prescription WHERE load_id=%s ORDER BY prescription_id", (load_id,)).fetchall()
                silver_actual["members"].extend([dict(item) for item in silver_members])
                silver_actual["prescriptions"].extend([dict(item) for item in silver_prescriptions])
                event_members = payload.get("data", {}).get("members", [])
                expected_member_ids = sorted(item["person"]["resourceID"] for item in event_members)
                expected_rx = sorted(rx["resourceID"] for item in event_members for rx in item["prescription"])
                _add_assertion(assertions, f"Silver member IDs match event {load_id}", sorted(item["member_id"] for item in silver_members) == expected_member_ids, expected_member_ids, sorted(item["member_id"] for item in silver_members))
                _add_assertion(assertions, f"Silver prescription IDs match event {load_id}", sorted(item["prescription_id"] for item in silver_prescriptions) == expected_rx, expected_rx, sorted(item["prescription_id"] for item in silver_prescriptions))
                silver_preferences = conn.execute(
                    "SELECT m.member_id,p.channel,p.value,p.enabled,p.preference_type "
                    "FROM silverbox.member_communication_preference p "
                    "JOIN silverbox.member m ON m.member_sk=p.member_sk "
                    "WHERE m.load_id=%s ORDER BY m.member_id,p.channel,p.value",
                    (load_id,),
                ).fetchall()
                silver_actual.setdefault("communication_preferences", []).extend([dict(item) for item in silver_preferences])
                expected_preferences = sorted(
                    (member["person"]["resourceID"], channel, destination, enabled)
                    for member in event_members
                    for channel, enabled, destination in (
                        ("SMS", member.get("contactPreference", {}).get("smsConsent", False), member.get("contactPreference", {}).get("safeTestMobile")),
                        ("EMAIL", member.get("contactPreference", {}).get("emailConsent", False), member.get("contactPreference", {}).get("safeTestEmail")),
                        ("PORTAL", member.get("contactPreference", {}).get("portalConsent", False), member.get("contactPreference", {}).get("safeTestPortal")),
                    ) if destination
                )
                actual_preferences = sorted((item["member_id"], item["channel"].upper(), item["value"], item["enabled"]) for item in silver_preferences)
                _add_assertion(assertions, f"Silver communication preferences match event {load_id}", actual_preferences == expected_preferences, expected_preferences, actual_preferences)
                actual_rx = {item["prescription_id"]: item for item in silver_prescriptions}
                for event_member in event_members:
                    member_id = event_member["person"]["resourceID"]
                    for expected in event_member["prescription"]:
                        actual = actual_rx.get(expected["resourceID"])
                        if actual:
                            prefix = f"Silver prescription {expected['resourceID']}"
                            _assert_equal_if_present(assertions, f"{prefix} member", member_id, actual["member_id"])
                            _assert_equal_if_present(assertions, f"{prefix} status", expected.get("rxStatus", "ACTIVE"), actual["status"])
                            _assert_equal_if_present(assertions, f"{prefix} eligibility", expected.get("eligibleInd", True), actual["eligible_ind"])
                            _assert_equal_if_present(assertions, f"{prefix} remaining refills", expected.get("refillsRemaining", 0), actual["refills_remaining"])
                            _assert_equal_if_present(assertions, f"{prefix} refill date", expected.get("nextRefillDate"), str(actual["next_refill_date"]) if actual["next_refill_date"] else None)
                            _assert_equal_if_present(assertions, f"{prefix} drug", expected["drug"]["name"], actual["drug_name"])
                            _assert_equal_if_present(assertions, f"{prefix} strength", expected["drug"]["strength"], actual["strength"])
        expected_records.append({
            "record_id": row["record_id"],
            "entity_name": row["entity_name"],
            "payload": payload,
            "pre_steps": json.loads(row["pre_steps_json"] or "[]"),
        })
    completed_at = now()
    failed = [item for item in assertions if item["status"] == "failed"]
    status = "failed" if failed else "passed"
    error = "; ".join(item["name"] for item in failed[:5]) if failed else None
    result_id = "xres_" + hashlib.sha256(f"{run_id}:{test_case_id}".encode("utf-8")).hexdigest()[:24]
    conn.execute(
        "INSERT INTO test_execution_result (result_id,run_id,generation_id,test_case_id,status,expected_json,actual_json,assertions_json,bronze_refs_json,started_at,completed_at,error) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (run_id, test_case_id) DO UPDATE SET status=EXCLUDED.status,expected_json=EXCLUDED.expected_json,actual_json=EXCLUDED.actual_json,assertions_json=EXCLUDED.assertions_json,bronze_refs_json=EXCLUDED.bronze_refs_json,started_at=EXCLUDED.started_at,completed_at=EXCLUDED.completed_at,error=EXCLUDED.error",
        (result_id, run_id, generation_id, test_case_id, status, json.dumps({"records": expected_records}, sort_keys=True), json.dumps({"bronze_rows": actual_records, "silver": silver_actual}, sort_keys=True), json.dumps(assertions, sort_keys=True), json.dumps(all_refs, sort_keys=True), started_at, completed_at, error),
    )
    return status

@app.post("/api/v1/test-case-generations/{generation_id}/test-data/save")
def save_test_data_to_bronze(generation_id: str):
    if not DATABASE_URL:
        raise HTTPException(503, f"Bronze insertion requires PostgreSQL and the {BRONZE_SCHEMA} schema")
    saved = []
    with connection() as conn:
        pbm_objects = conn.execute("""SELECT to_regclass('testiq.ingestion_load') AS load_table,
            to_regclass('testiq.ingestion_load_id_seq') AS load_sequence,
            to_regclass('bronzebox.membership') AS membership_table,
            to_regclass('bronzebox.prescription') AS prescription_table,
            to_regclass('silverbox.member') AS silver_member_table,
            to_regclass('silverbox.prescription') AS silver_prescription_table,
            to_regprocedure('testiq.process_bronze_load_to_silver(text,text)') AS promote_procedure""").fetchone()
        if not all(pbm_objects.values()):
            raise HTTPException(503, "PBM Bronze/Silver integration objects are unavailable in the shared database. Check database connectivity and the PBM ingestion service readiness.")
        generation = conn.execute("SELECT * FROM test_case_generation WHERE generation_id=%s", (generation_id,)).fetchone()
        if not generation:
            raise HTTPException(404, "Test-case generation run not found")
        generation_contract = json.loads(generation["contract_json"])
        cases = {case.get("test_case_id"): case for case in generation_contract.get("test_cases", [])}
        rows = conn.execute("SELECT * FROM test_data_record WHERE generation_id=%s ORDER BY test_case_id,entity_name", (generation_id,)).fetchall()
        if not rows:
            raise HTTPException(404, "Generate test data before saving it to Bronze")
        member_ids, phone_values, event_ids, correlations, event_times, validated_payloads = set(), set(), set(), set(), set(), {}
        for row in rows:
            test_case = cases.get(row["test_case_id"])
            if not test_case:
                raise HTTPException(422, f"Approved test case {row['test_case_id']} is missing from its generation contract.")
            payload = json.loads(row["payload_json"])
            if row["status"] != "saved":
                payload = _stamp_pharmacy_event_fields(payload, generation_id, row["test_case_id"])
                conn.execute("UPDATE test_data_record SET payload_json=%s,updated_at=%s WHERE record_id=%s", (json.dumps(payload, sort_keys=True), now(), row["record_id"]))
            _validate_pharmacy_notification(payload, test_case, member_ids, phone_values, event_ids, correlations, event_times)
            validated_payloads[row["record_id"]] = payload
        for row in rows:
            if row["status"] == "saved":
                saved.append(_test_data_record_response(row))
                continue
            payload = validated_payloads[row["record_id"]]
            bronze_rows = _bronze_rows_for_payload(payload, row)
            if not bronze_rows:
                raise HTTPException(422, f"No supported Bronze records could be derived for {row['test_case_id']} / {row['entity_name']}")
            try:
                if payload.get("type") == "pharmacy.orders.v1.sample-notification":
                    load_id, bronze_refs = _insert_pbm_event_load(conn, payload, generation_id, row["test_case_id"], row)
                else:
                    load_id = f"testiq-{generation_id}"
                    bronze_refs = [_insert_bronze_domain_row(conn, table, load_id, data) for table, data in bronze_rows]
                _record_bronze_refs(conn, row, bronze_refs, load_id)
            except Exception as exc:
                raise HTTPException(503, f"Could not insert into {BRONZE_SCHEMA} Bronze tables: {exc}") from exc
            bronze_ref_text = ",".join(f"{item['table']}:{item['id']}" for item in bronze_refs)
            conn.execute("UPDATE test_data_record SET status='saved',bronze_raw_file_id=%s,updated_at=%s WHERE record_id=%s", (bronze_ref_text, now(), row["record_id"]))
            row = conn.execute("SELECT r.* FROM test_data_record r WHERE r.record_id=%s", (row["record_id"],)).fetchone()
            saved.append(_test_data_record_response(row))
        return {"generation_id": generation_id, "saved_count": len(saved), "records": saved}

@app.get("/api/v1/requirement-analyses/{analysis_id}/review-audit")
def list_requirement_review_audit(analysis_id: str):
    with connection() as conn:
        if not conn.execute("SELECT analysis_id FROM requirement_analysis WHERE analysis_id=%s", (analysis_id,)).fetchone():
            raise HTTPException(404, "Requirement analysis not found")
        rows = conn.execute("SELECT * FROM requirement_review_audit WHERE analysis_id=%s ORDER BY created_at DESC", (analysis_id,)).fetchall()
        return [_audit_response(row) for row in rows]

@app.get("/api/v1/test-case-generations/{generation_id}/review-audit")
def list_test_case_review_audit(generation_id: str):
    with connection() as conn:
        if not conn.execute("SELECT generation_id FROM test_case_generation WHERE generation_id=%s", (generation_id,)).fetchone():
            raise HTTPException(404, "Test-case generation run not found")
        rows = conn.execute("SELECT * FROM test_case_review_audit WHERE generation_id=%s ORDER BY created_at DESC", (generation_id,)).fetchall()
        return [_audit_response(row) for row in rows]

@app.exception_handler(ValueError)
async def value_error(_, exc): return JSONResponse(status_code=422, content={"detail":str(exc)})

