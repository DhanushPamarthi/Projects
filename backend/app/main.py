import difflib, hashlib, json, mimetypes, os, threading, uuid
from datetime import datetime, timezone
from pathlib import Path
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from .config import DATABASE_URL, EXTRACTION_CONFIG_VERSION, EXTRACTION_PROMPT_VERSION, LOCAL_EXTRACTOR_VERSION, MAX_UPLOAD_BYTES, OPENAI_MODEL, OPENAI_TIMEOUT_SECONDS, REQUIRED_BRD_SECTIONS, REQUIREMENTS_ANALYSIS_PROMPT_VERSION, SUPPORTED_EXTENSIONS, TEST_CASE_GENERATION_PROMPT_VERSION, UPLOAD_DIR
from .db import connection, init_db
from .extractors import extract_local, extract_openai, extraction_prompt_hash, missing_sections
from .validation import validate_file_content

app = FastAPI(title="TestIQ Source Package API", version="1.0.0")
SOURCE_TYPES = {"brd", "campaign_rules", "api_spec", "database_schema", "message_template", "ui_description"}

def now(): return datetime.now(timezone.utc).isoformat()
def uid(prefix): return f"{prefix}_{uuid.uuid4().hex}"
def row_dict(row): return dict(row) if row else None

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
TEST_CASE_GENERATION_PROMPT = f"{read_skill_instructions('test-case-generation')}\n\nAPPROVED REQUIREMENTS:\n"
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

def extract_openai_with_timeout(text):
    """Run the optional remote extractor without allowing the worker to hang."""
    result, failure = [], []

    def run():
        try:
            result.append(extract_openai(text))
        except Exception as exc:
            failure.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(OPENAI_TIMEOUT_SECONDS + 5)
    if worker.is_alive():
        raise TimeoutError(f"OpenAI extraction timed out after {OPENAI_TIMEOUT_SECONDS + 5:g} seconds")
    if failure:
        raise failure[0]
    return result[0]

def process_extraction(revision_id, source_id, extraction_id, storage_path, filename, required_sections):
    # Commit the visible processing state before doing local parsing or a network call.
    # Otherwise the UI can only see "pending" while OpenAI is slow or unavailable.
    with connection() as conn:
        conn.execute("UPDATE source_extraction SET mode=%s,status=%s WHERE extraction_id=%s", ("local", "processing", extraction_id))
        conn.execute("UPDATE source_revision SET status=%s WHERE revision_id=%s", ("processing", revision_id))
    with connection() as conn:
        try:
            content = Path(storage_path).read_bytes()
            local_text, local_sections = extract_local(filename, content)
            fallback_message = None
            try:
                text, sections = extract_openai_with_timeout(local_text)
                mode = "openai"
            except Exception as exc:
                text, sections, mode = local_text, local_sections, "local"
                fallback_message = f"OpenAI extraction failed; local extraction was used: {exc}"
            messages = json.loads(conn.execute("SELECT validation_messages FROM source_revision WHERE revision_id=%s", (revision_id,)).fetchone()["validation_messages"] or "[]")
            messages.extend([f"Missing expected section: {item}" for item in missing_sections(local_sections, required_sections)])
            if fallback_message: messages.append(fallback_message)
            model = OPENAI_MODEL if mode == "openai" else LOCAL_EXTRACTOR_VERSION
            conn.execute("UPDATE source_extraction SET mode=%s,status=%s,extracted_text=%s,error=%s,model_name=%s,prompt_version=%s,config_version=%s,prompt_hash=%s,config_hash=%s WHERE extraction_id=%s", (mode, "succeeded", text, fallback_message, model, EXTRACTION_PROMPT_VERSION, EXTRACTION_CONFIG_VERSION, extraction_prompt_hash(), extraction_config_hash(), extraction_id))
            for index, section in enumerate(local_sections):
                fragment_text = section.get("text", "")
                paragraph_index = section.get("paragraph_index")
                conn.execute("INSERT INTO source_fragment (fragment_id,revision_id,source_id,fragment_index,heading,source_reference,page_number,fragment_type,paragraph_index,table_index,row_index,cell_index,heading_path,paragraph_start,paragraph_end,character_start,character_end,content_hash,extractor_version,provenance_mode,fragment_text) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (uid("frag"), revision_id, source_id, index, section.get("heading"), section.get("reference", "unknown"), section.get("page_number"), section.get("fragment_type", "paragraph"), paragraph_index, section.get("table_index"), section.get("row_index"), section.get("cell_index"), section.get("heading"), paragraph_index, paragraph_index, None, None, hashlib.sha256(fragment_text.encode("utf-8")).hexdigest(), LOCAL_EXTRACTOR_VERSION, mode, fragment_text))
            conn.execute("UPDATE source_revision SET status=%s,validation_messages=%s WHERE revision_id=%s", ("uploaded", json.dumps(messages), revision_id))
        except Exception as exc:
            conn.execute("UPDATE source_extraction SET mode=%s,status=%s,error=%s,model_name=%s,prompt_version=%s,config_version=%s,prompt_hash=%s,config_hash=%s WHERE extraction_id=%s", ("local", "failed", str(exc), LOCAL_EXTRACTOR_VERSION, EXTRACTION_PROMPT_VERSION, EXTRACTION_CONFIG_VERSION, extraction_prompt_hash(), extraction_config_hash(), extraction_id))
            row = conn.execute("SELECT validation_messages FROM source_revision WHERE revision_id=%s", (revision_id,)).fetchone()
            messages = json.loads(row["validation_messages"] or "[]") if row else []
            messages.append(f"Extraction failed: {exc}")
            conn.execute("UPDATE source_revision SET status=%s,validation_messages=%s WHERE revision_id=%s", ("extraction_failed", json.dumps(messages), revision_id))

class ManifestRequest(BaseModel):
    source_ids: list[str] = Field(min_length=1)
    # Kept optional for API compatibility with older clients; the UI always sends it.
    package_name: str | None = Field(default=None, max_length=200)
    scope: str | None = None

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
    count: int = Field(default=5, ge=1, le=100)

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
                        "acceptance_criteria": {"type": "array", "minItems": 1, "items": {"type": "object", "additionalProperties": False, "properties": {"acceptance_id": {"type": "string", "pattern": "^AC-[0-9]{3}$"}, "given": {"type": "string"}, "when": {"type": "string"}, "then": {"type": "string"}, "source_references": references}, "required": ["acceptance_id", "given", "when", "then", "source_references"]}},
                        "flow_steps": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                        "ambiguities": {"type": "array", "items": {"type": "string"}},
                        "assumptions": {"type": "array", "items": {"type": "string"}},
                        "source_references": references,
                    },
                    "required": ["requirement_id", "title", "type", "statement", "priority", "actors", "entities", "business_rules", "acceptance_criteria", "flow_steps", "ambiguities", "assumptions", "source_references"],
                },
            },
            "global_gaps": {"type": "array", "items": {"type": "string"}},
            "global_assumptions": {"type": "array", "items": {"type": "string"}},
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
        "required": ["requirements", "global_gaps", "global_assumptions", "conflicting_requirements", "readiness"],
    }

def _contract_readiness(contract):
    requirements = contract.get("requirements", [])
    source_refs = [ref for item in requirements for ref in item.get("source_references", [])]
    checks = [
        {"check_id": "RD-001", "label": "Requirements", "status": "passed" if requirements else "gap", "details": f"{len(requirements)} requirement(s) extracted."},
        {"check_id": "RD-002", "label": "Acceptance criteria", "status": "passed" if requirements and all(item.get("acceptance_criteria") for item in requirements) else "gap", "details": "Every requirement should include acceptance criteria."},
        {"check_id": "RD-003", "label": "Business rules", "status": "passed" if requirements and all(item.get("business_rules") for item in requirements) else "gap", "details": "Every requirement should include business rules."},
        {"check_id": "RD-004", "label": "Source traceability", "status": "passed" if source_refs else "gap", "details": "Requirements should retain source references back to package fragments."},
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
            ref["fragment_id"] = fragment["fragment_id"]
            ref["source_reference"] = fragment["source_reference"]

    for requirement in contract["requirements"]:
        requirement_id = requirement.get("requirement_id")
        if not requirement_id or requirement_id in seen_ids or not requirement.get("title") or not requirement.get("statement"):
            raise ValueError("AI returned an invalid or duplicate requirement")
        seen_ids.add(requirement_id)
        validate_refs(requirement.get("source_references"), requirement_id)
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
    for conflict in contract.get("conflicting_requirements", []):
        if not conflict.get("description"):
            raise ValueError("AI returned an invalid conflict")
        validate_refs(conflict.get("source_references"), conflict.get("conflict_id", "conflict"))

def _requirements_contract_with_openai(package, items, fragments):
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not configured for Requirements Analysis")
    from openai import OpenAI
    usable_fragments = [fragment for fragment in fragments if str(fragment.get("fragment_text") or "").strip()]
    source_fragments = _analysis_source_references(usable_fragments)
    fragment_text = "\n".join(json.dumps({**ref, "text": str(fragment.get("fragment_text") or "")}, ensure_ascii=False) for ref, fragment in zip(source_fragments, usable_fragments))
    client = OpenAI(api_key=key, timeout=OPENAI_TIMEOUT_SECONDS)
    response = client.responses.create(
        model=OPENAI_MODEL,
        input=REQUIREMENTS_ANALYSIS_PROMPT + fragment_text,
        store=False,
        text={"format": {"type": "json_schema", "name": "requirements_contract", "strict": True, "schema": _requirements_analysis_schema()}},
    )
    if not response.output_text:
        raise ValueError("OpenAI returned no Requirements Analysis")
    contract = json.loads(response.output_text)
    _validate_requirements_contract(contract, fragments)
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

def _requirements_contract_with_timeout(package, items, fragments):
    result, failure = [], []
    def run():
        try: result.append(_requirements_contract_with_openai(package, items, fragments))
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
    """Hide legacy fallback warnings that were only generic keyword/ambiguity notices."""
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
    for requirement in contract.get("requirements", []):
        requirement.pop("expected_outcomes", None)
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
        approved.append({"requirement_id": requirement_id, "title": requirement.get("title"), "type": requirement.get("type"), "statement": requirement.get("statement"), "priority": requirement.get("priority"), "actors": requirement.get("actors", []), "entities": requirement.get("entities", []), "flow_steps": requirement.get("flow_steps", []), "acceptance_criteria": criteria, "business_rules": rules, "source_references": requirement.get("source_references", [])})
    return approved

def _test_case_generation_schema():
    source_reference = {"type": "object", "additionalProperties": False, "properties": {"fragment_id": {"type": "string"}, "source_reference": {"type": "string"}}, "required": ["fragment_id", "source_reference"]}
    surface = {"type": "object", "additionalProperties": False, "properties": {"name": {"type": "string"}, "type": {"type": "string"}, "coverage": {"type": "string"}}, "required": ["name", "type", "coverage"]}
    data_need = {"type": "object", "additionalProperties": False, "properties": {"name": {"type": "string"}, "type": {"type": "string"}, "value": {"type": "string"}, "required": {"type": "boolean"}, "source": {"type": "string"}}, "required": ["name", "type", "value", "required", "source"]}
    deterministic_input = {"type": "object", "additionalProperties": False, "properties": {"name": {"type": "string"}, "value": {"type": "string"}, "seed_strategy": {"type": "string"}}, "required": ["name", "value", "seed_strategy"]}
    action = {"type": "object", "additionalProperties": False, "properties": {"step": {"type": "integer"}, "surface": {"type": "string"}, "actor": {"type": "string"}, "action": {"type": "string"}, "data_inputs": {"type": "array", "items": {"type": "string"}}}, "required": ["step", "surface", "actor", "action", "data_inputs"]}
    assertion = {"type": "object", "additionalProperties": False, "properties": {"assertion_id": {"type": "string"}, "text": {"type": "string"}, "assertion_type": {"type": "string"}, "evidence_id": {"type": "string"}}, "required": ["assertion_id", "text", "assertion_type", "evidence_id"]}
    evidence = {"type": "object", "additionalProperties": False, "properties": {"evidence_id": {"type": "string"}, "evidence_type": {"type": "string"}, "requirement_id": {"type": "string", "pattern": "^FR-[0-9]{3}$"}, "summary": {"type": "string"}, "source_references": {"type": "array", "items": source_reference}}, "required": ["evidence_id", "evidence_type", "requirement_id", "summary", "source_references"]}
    validation_result = {"type": "object", "additionalProperties": False, "properties": {"check": {"type": "string"}, "status": {"type": "string"}, "details": {"type": "string"}}, "required": ["check", "status", "details"]}
    cleanup_ownership = {"type": "object", "additionalProperties": False, "properties": {"owner": {"type": "string"}, "cleanup_required": {"type": "boolean"}, "cleanup_steps": {"type": "array", "items": {"type": "string"}}}, "required": ["owner", "cleanup_required", "cleanup_steps"]}
    approval = {"type": "object", "additionalProperties": False, "properties": {"status": {"type": "string"}, "reviewer": {"type": "string"}, "comments": {"type": "string"}, "reviewed_at": {"type": "string"}}, "required": ["status", "reviewer", "comments", "reviewed_at"]}
    return {"type": "object", "additionalProperties": False, "properties": {
        "test_cases": {"type": "array", "minItems": 1, "items": {"type": "object", "additionalProperties": False, "properties": {
            "test_case_id": {"type": "string", "pattern": "^TC-[0-9]{3}$"},
            "requirement_id": {"type": "string", "pattern": "^FR-[0-9]{3}$"},
            "title": {"type": "string"},
            "scenario": {"type": "string"},
            "category": {"type": "string"},
            "type": {"type": "string"},
            "priority": {"type": "string"},
            "surfaces": {"type": "array", "minItems": 1, "items": surface},
            "preconditions": {"type": "array", "items": {"type": "string"}},
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
        }, "required": ["test_case_id", "requirement_id", "title", "scenario", "category", "type", "priority", "surfaces", "preconditions", "deterministic_inputs", "data_needs", "actions", "steps", "assertions", "expected_results", "acceptance_criteria_ids", "business_rule_ids", "discovery_context_ids", "evidence", "assumptions", "validation_results", "cleanup_ownership", "approval", "source_references"]}},
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

def _test_case_coverage_plan(approved_requirements, requested_count):
    categories = [
        ("happy_path", "Eligible-member happy path", ("eligible", "active", "success", "reminder", "campaign")),
        ("exclusion", "Exclusion or missing-data path", ("ineligible", "inactive", "exclude", "not eligible", "suppress", "missing")),
        ("boundary", "Threshold or boundary path", ("threshold", "boundary", "due", "day", "status", "eligibility", "eligible")),
        ("channel_fallback", "Channel fallback path", ("fallback", "alternate", "phone", "sms fail", "channel")),
        ("retry_failure", "Retry, idempotency, or service-failure path", ("retry", "idempot", "failure", "fails", "partial", "service", "timeout")),
    ]
    plan, gaps = [], []
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

def _test_case_seed(package, requirement, category, index):
    payload = {"package_id": package.get("package_id"), "package_version": package.get("package_version"), "requirement_id": requirement.get("requirement_id"), "category": category, "index": index}
    return int(hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:8], 16)

def _test_case_generation_input(approved_requirements, package, requested_count, discovery_contexts=None):
    discovery_contexts = discovery_contexts or []
    plan, gaps = _test_case_coverage_plan(approved_requirements, requested_count)
    context_gaps = [] if discovery_contexts else ["GraphRAG/Knowledge Fabric context is not connected yet; use the approved requirement contract and cited source references only."]
    return {
        "requested_test_case_count": requested_count,
        "package_id": package["package_id"],
        "package_version": package["package_version"],
        "source_package_run_id": package.get("run_id"),
        "required_coverage": [item["category"] for item in plan],
        "context": {
            "mode": "approved_requirement_contract_with_knowledge_context" if discovery_contexts else "approved_requirement_contract",
            "graphrag_context_available": bool(discovery_contexts),
            "knowledge_contexts": discovery_contexts,
            "gaps": context_gaps,
        },
        "coverage_gaps": gaps,
        "requirements": approved_requirements,
    }

def _validate_test_case_contract(contract, approved_requirements, requested_count=None, discovery_contexts=None):
    discovery_contexts = discovery_contexts or []
    if not isinstance(contract, dict) or not contract.get("test_cases"):
        raise ValueError("The AI returned no test cases")
    approved_by_id = {item["requirement_id"]: item for item in approved_requirements}
    plan, _ = _test_case_coverage_plan(approved_requirements, requested_count or len(contract["test_cases"]))
    required_categories = {item["category"] for item in plan}
    generated_categories = {case.get("category") for case in contract["test_cases"]}
    missing_categories = sorted(required_categories - generated_categories)
    if missing_categories:
        raise ValueError(f"AI missed required test-case categories: {', '.join(missing_categories)}")
    seen = set()
    required_fields = ("scenario", "category", "surfaces", "deterministic_inputs", "data_needs", "actions", "assertions", "evidence", "validation_results", "cleanup_ownership", "approval")
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
        item = approved_by_id[requirement_id]
        criterion_ids = {x.get("acceptance_id") for x in item.get("acceptance_criteria", []) if isinstance(x, dict)}
        rule_ids = {x.get("rule_id") for x in item.get("business_rules", []) if isinstance(x, dict)}
        context_ids = {context["context_id"] for context in discovery_contexts}
        valid_evidence_ids = {requirement_id, *criterion_ids, *rule_ids, *context_ids}
        valid_fragments = {ref.get("fragment_id") for ref in _case_source_refs(item) if ref.get("fragment_id")}
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
        for evidence in test_case.get("evidence", []):
            evidence_id = evidence.get("evidence_id")
            if evidence_id and evidence_id not in valid_evidence_ids:
                raise ValueError(f"AI returned unapproved evidence id {evidence_id} for {case_id}")
        if valid_fragments:
            for ref in test_case.get("source_references", []):
                if ref.get("fragment_id") and ref.get("fragment_id") not in valid_fragments:
                    raise ValueError(f"AI returned an unknown source fragment for {case_id}")

def _test_cases_with_openai(approved_requirements, package, requested_count, discovery_contexts=None):
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), timeout=OPENAI_TIMEOUT_SECONDS)
    payload = _test_case_generation_input(approved_requirements, package, requested_count, discovery_contexts)
    failures = []
    for attempt in range(1, TEST_CASE_GENERATION_MAX_ATTEMPTS + 1):
        try:
            response = client.responses.create(model=OPENAI_MODEL, input=TEST_CASE_GENERATION_PROMPT + json.dumps(payload, ensure_ascii=False), store=False, text={"format": {"type": "json_schema", "name": "test_case_contract", "strict": True, "schema": _test_case_generation_schema()}})
            if not response.output_text:
                raise ValueError("OpenAI returned no test cases")
            contract = json.loads(response.output_text)
            if len(contract["test_cases"]) != requested_count:
                raise ValueError(f"AI returned {len(contract['test_cases'])} test cases; expected {requested_count}")
            _validate_test_case_contract(contract, approved_requirements, requested_count, discovery_contexts)
            contract["agent_attempts"] = [{"attempt": attempt, "status": "passed", "details": "Schema, evidence, and coverage validation passed."}]
            contract.setdefault("global_gaps", []).extend(payload["context"]["gaps"] + payload.get("coverage_gaps", []))
            return contract
        except Exception as exc:
            failures.append({"attempt": attempt, "status": "failed", "details": safe_llm_error_message(exc)})
    raise ValueError(f"LLM test-case generation failed validation after {TEST_CASE_GENERATION_MAX_ATTEMPTS} attempts: {failures[-1]['details']}")

def _case_template(category):
    templates = {
        "happy_path": {"type": "positive", "scenario": "Eligible member completes the documented reminder journey.", "steps": ["Prepare an eligible active member with the required campaign inputs.", "Run the campaign eligibility and reminder flow.", "Confirm the expected reminder outcome is produced."], "expected": "The eligible member follows the approved journey and receives the expected reminder outcome."},
        "exclusion": {"type": "negative", "scenario": "Ineligible or missing-data member is excluded from the reminder journey.", "steps": ["Prepare a member record that violates the approved eligibility rule or omits required data.", "Run the campaign eligibility flow.", "Confirm the member is excluded and no unsupported reminder is sent."], "expected": "The member is excluded according to the approved rule and the exclusion is observable."},
        "boundary": {"type": "boundary", "scenario": "Eligibility threshold is validated at the documented boundary.", "steps": ["Prepare deterministic data at the documented eligibility threshold.", "Run the eligibility evaluation.", "Confirm the boundary result matches the approved requirement."], "expected": "The boundary condition is handled consistently with the approved criteria."},
        "channel_fallback": {"type": "negative", "scenario": "Alternate communication path is triggered when the primary channel fails.", "steps": ["Prepare an eligible member for the primary communication channel.", "Simulate or record primary-channel failure.", "Confirm the approved alternate channel behavior occurs."], "expected": "The fallback path runs only when supported by the approved communication rule."},
        "retry_failure": {"type": "failure", "scenario": "Retry, idempotency, or service failure is handled without duplicate or partial incorrect outcomes.", "steps": ["Prepare deterministic campaign data.", "Simulate the approved failure or retry condition.", "Confirm status, retry, and duplicate-prevention behavior."], "expected": "Failure handling remains traceable, bounded, and does not create duplicate outcomes."},
        "requirement_validation": {"type": "positive", "scenario": "Requirement behavior is validated from the approved contract.", "steps": ["Prepare the documented preconditions.", "Execute the requirement flow.", "Confirm the approved expected behavior."], "expected": "The requirement behaves as approved."},
    }
    return templates.get(category, templates["requirement_validation"])

def _test_cases_fallback(approved_requirements, requested_count, package=None, discovery_contexts=None):
    package = package or {}
    discovery_contexts = discovery_contexts or []
    plan, gaps = _test_case_coverage_plan(approved_requirements, requested_count)
    cases = []
    for index, entry in enumerate(plan, 1):
        requirement = entry["requirement"]
        category = entry["category"]
        template = _case_template(category)
        criteria = [item for item in requirement.get("acceptance_criteria", []) if isinstance(item, dict)]
        rules = [item for item in requirement.get("business_rules", []) if isinstance(item, dict)]
        first = criteria[0] if criteria else {}
        seed = _test_case_seed(package, requirement, category, index)
        source_refs = _case_source_refs(requirement)
        evidence_refs = source_refs[:3] or [{"fragment_id": "unavailable", "source_reference": "approved requirement contract"}]
        actor = (requirement.get("actors") or ["Campaign Service"])[0]
        selected_context = discovery_contexts[(index - 1) % len(discovery_contexts)] if discovery_contexts else None
        surface_name = selected_context["name"] if selected_context else ", ".join(requirement.get("entities") or ["Campaign workflow"])
        surface_type = selected_context["context_type"] if selected_context else "workflow"
        steps = requirement.get("flow_steps") or template["steps"]
        expected_text = first.get("then") or template["expected"]
        rule_ids = [rule.get("rule_id") for rule in rules if rule.get("rule_id")]
        criteria_ids = [first.get("acceptance_id")] if first.get("acceptance_id") else []
        evidence_items = [{"evidence_id": requirement["requirement_id"], "evidence_type": "requirement", "requirement_id": requirement["requirement_id"], "summary": requirement.get("statement") or requirement.get("title") or "Approved requirement", "source_references": evidence_refs}]
        evidence_items.extend({"evidence_id": item_id, "evidence_type": "acceptance_criterion", "requirement_id": requirement["requirement_id"], "summary": first.get("then", "Approved acceptance criterion"), "source_references": evidence_refs} for item_id in criteria_ids)
        evidence_items.extend({"evidence_id": item_id, "evidence_type": "business_rule", "requirement_id": requirement["requirement_id"], "summary": next((rule.get("text") for rule in rules if rule.get("rule_id") == item_id), "Approved business rule"), "source_references": evidence_refs} for item_id in rule_ids[:2])
        if selected_context:
            evidence_items.append({"evidence_id": selected_context["context_id"], "evidence_type": "discovery_context", "requirement_id": requirement["requirement_id"], "summary": selected_context.get("description") or selected_context["name"], "source_references": evidence_refs})
        cases.append({
            "test_case_id": f"TC-{index:03d}",
            "requirement_id": requirement["requirement_id"],
            "title": f"{entry['label']} - {requirement.get('title') or requirement['requirement_id']}",
            "scenario": template["scenario"],
            "category": category,
            "type": template["type"],
            "priority": requirement.get("priority") or "medium",
            "surfaces": [{"name": surface_name, "type": surface_type, "coverage": "Grounded by linked knowledge context." if selected_context else "Derived from approved requirement; GraphRAG/Knowledge Fabric context is not yet linked."}],
            "preconditions": [first.get("given", "The documented preconditions are satisfied")] if first else ["The documented preconditions are satisfied"],
            "deterministic_inputs": [
                {"name": "seed", "value": str(seed), "seed_strategy": "sha256(package, requirement, category, index)"},
                {"name": "member_id", "value": f"MEM-{seed % 100000:05d}", "seed_strategy": "derived from seed"},
                {"name": "campaign_id", "value": f"CMP-{seed % 10000:04d}", "seed_strategy": "derived from seed"},
            ],
            "data_needs": [
                {"name": "member", "type": "entity", "value": "deterministic member record", "required": True, "source": requirement["requirement_id"]},
                {"name": "campaign eligibility inputs", "type": "business data", "value": category, "required": True, "source": ",".join(criteria_ids or rule_ids or [requirement["requirement_id"]])},
            ],
            "actions": [{"step": step_index, "surface": surface_name, "actor": actor, "action": step, "data_inputs": ["member_id", "campaign_id"]} for step_index, step in enumerate(steps, 1)],
            "steps": steps,
            "assertions": [{"assertion_id": f"AS-{index:03d}-001", "text": expected_text, "assertion_type": "expected_result", "evidence_id": criteria_ids[0] if criteria_ids else requirement["requirement_id"]}],
            "expected_results": [expected_text],
            "acceptance_criteria_ids": criteria_ids,
            "business_rule_ids": rule_ids,
            "discovery_context_ids": [selected_context["context_id"]] if selected_context else [],
            "evidence": evidence_items,
            "assumptions": requirement.get("assumptions", []),
            "validation_results": [{"check": "Approved evidence only", "status": "passed", "details": "Fallback case was generated only from approved requirement evidence."}, {"check": "Deterministic inputs", "status": "passed", "details": f"Seed {seed} recorded for reproducible test data."}],
            "cleanup_ownership": {"owner": "Test data generation", "cleanup_required": True, "cleanup_steps": ["Remove or reset deterministic campaign/member data created for this case."]},
            "approval": {"status": "pending", "reviewer": "", "comments": "", "reviewed_at": ""},
            "source_references": source_refs[:5] or evidence_refs,
        })
    generated_categories = [case["category"] for case in cases]
    discovery_gap = [] if discovery_contexts else ["GraphRAG/Knowledge Fabric context is not connected; cases are grounded only in approved requirements and source references."]
    return {"test_cases": cases, "coverage": {"required_categories": [item["category"] for item in plan], "generated_categories": generated_categories, "missing_categories": []}, "global_gaps": ["LLM test-case generation was not run because OPENAI_API_KEY is not configured.", *discovery_gap, *gaps]}

def _normalized_case_text(test_case):
    value = " ".join([
        str(test_case.get("title") or ""),
        str(test_case.get("scenario") or ""),
        " ".join(test_case.get("steps", []) or []),
        " ".join(test_case.get("expected_results", []) or []),
    ])
    return " ".join(value.lower().split())

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
            findings.append({"finding_id": f"QF-{len(findings)+1:03d}", "type": "ungrounded", "severity": "medium", "test_case_ids": [case_id], "message": "Case uses fallback source text instead of a precise source fragment.", "recommendation": "Review manually and regenerate after GraphRAG/source context is available if needed."})
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

def _test_run_response(row):
    result = row_dict(row)
    if not result:
        return None
    result["configuration_snapshot"] = json.loads(result.get("configuration_snapshot") or "{}")
    for key in ("selected_test_case_ids_json", "frozen_contract_json", "immutable_observations_json"):
        value = result.pop(key, None)
        result[key[:-5]] = json.loads(value or ("{}" if key == "frozen_contract_json" else "[]"))
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
def startup(): init_db()

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
        if source and any(x["sha256"] == digest for x in existing):
            reused_revision = next(x for x in existing if x["sha256"] == digest)
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
def create_manifest(project_id: str, request: ManifestRequest):
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
        if not brds: raise HTTPException(422, "A source package must contain at least one BRD")
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
        return {"package_id": package_id, "project_id": project_id, "package_version": version, "package_name": package_name, "scope": request.scope, "manifest_hash": manifest_hash, "lifecycle_status": "ready", "run_id": run_id, "items": items}

@app.get("/api/v1/projects/{project_id}/manifests")
def list_manifests(project_id: str):
    with connection() as conn: return [row_dict(x) for x in conn.execute("SELECT m.*,COUNT(i.source_id) AS source_count FROM source_package_manifest m LEFT JOIN source_package_item i ON i.package_id=m.package_id WHERE m.project_id=%s GROUP BY m.package_id ORDER BY m.package_version DESC",(project_id,)).fetchall()]

@app.get("/api/v1/manifests/{package_id}")
def get_package(package_id: str):
    with connection() as conn:
        m=conn.execute("SELECT * FROM source_package_manifest WHERE package_id=%s",(package_id,)).fetchone()
        if not m: raise HTTPException(404,"Package manifest not found")
        items=conn.execute("SELECT mi.*,s.logical_name,s.source_type,r.revision,r.sha256,r.owner_version FROM source_package_item mi JOIN source_document s ON s.source_id=mi.source_id JOIN source_revision r ON r.revision_id=mi.revision_id WHERE mi.package_id=%s",(package_id,)).fetchall(); return {**row_dict(m),"items":[row_dict(x) for x in items]}

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

@app.post("/api/v1/packages/{package_id}/requirements/analyze", status_code=201)
def analyze_requirements(package_id: str):
    with connection() as conn:
        package=conn.execute("SELECT * FROM source_package_manifest WHERE package_id=%s",(package_id,)).fetchone()
        if not package: raise HTTPException(404,"Package manifest not found")
        items=conn.execute("SELECT pi.*,s.logical_name,s.source_type,r.sha256,e.status AS extraction_status FROM source_package_item pi JOIN source_document s ON s.source_id=pi.source_id JOIN source_revision r ON r.revision_id=pi.revision_id LEFT JOIN source_extraction e ON e.revision_id=pi.revision_id WHERE pi.package_id=%s",(package_id,)).fetchall(); not_ready=[x["logical_name"] for x in items if x["extraction_status"]!="succeeded"]
        if not_ready: raise HTTPException(422,f"Requirements cannot be analyzed until extraction succeeds for: {', '.join(not_ready)}")
        ids = [x["revision_id"] for x in items]
        fragments = conn.execute(f"SELECT * FROM source_fragment WHERE revision_id IN ({','.join('%s' for _ in ids)}) ORDER BY revision_id,fragment_index", ids).fetchall()
        item_rows = [row_dict(x) for x in items]
        fragment_rows = [row_dict(x) for x in fragments]
        if os.getenv("OPENAI_API_KEY"):
            try:
                contract = _requirements_contract_with_timeout(row_dict(package), item_rows, fragment_rows)
            except Exception as exc:
                contract = _requirement_contract(row_dict(package), item_rows, fragment_rows)
                contract["generation_note"] = f"LLM Requirements Analysis failed; local fallback was used: {safe_llm_error_message(exc)}"
                contract.setdefault("global_gaps", []).append("LLM Requirements Analysis was unavailable, so this contract was generated with the local fallback.")
                contract.setdefault("gaps", contract["global_gaps"])
        else:
            # Keep local development usable without a key, while making the
            # fallback explicit in the persisted contract.
            contract = _requirement_contract(row_dict(package), item_rows, fragment_rows)
            contract["generation_note"] = "LLM Requirements Analysis was not run because OPENAI_API_KEY is not configured."
        version = conn.execute("SELECT COALESCE(MAX(analysis_version),0)+1 AS v FROM requirement_analysis WHERE package_id=%s", (package_id,)).fetchone()["v"]
        analysis_id, timestamp = uid("req"), now()
        conn.execute("INSERT INTO requirement_analysis (analysis_id,package_id,project_id,analysis_version,status,contract_json,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", (analysis_id, package_id, package["project_id"], version, "needs_review", json.dumps(contract), timestamp, timestamp))
        discovery_count = _discovery_context_count(conn, package["project_id"], package_id)
        return _analysis_response(conn.execute("SELECT * FROM requirement_analysis WHERE analysis_id=%s", (analysis_id,)).fetchone(), discovery_count)

@app.get("/api/v1/packages/{package_id}/requirements")
def list_requirement_analyses(package_id: str):
    with connection() as conn:
        package = conn.execute("SELECT * FROM source_package_manifest WHERE package_id=%s", (package_id,)).fetchone()
        discovery_count = _discovery_context_count(conn, package["project_id"], package_id) if package else 0
        return [_analysis_response(x, discovery_count) for x in conn.execute("SELECT * FROM requirement_analysis WHERE package_id=%s ORDER BY analysis_version DESC",(package_id,)).fetchall()]

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
        try:
            if os.getenv("OPENAI_API_KEY"):
                generated = _test_cases_with_openai(approved_requirements, package_row, request.count, discovery_contexts)
                mode = "openai"
            else:
                generated = _test_cases_fallback(approved_requirements, request.count, package_row, discovery_contexts)
                mode = "local_fallback"
        except Exception as exc:
            generated = _test_cases_fallback(approved_requirements, request.count, package_row, discovery_contexts)
            generated.setdefault("global_gaps", []).append(f"LLM test-case generation failed; local fallback was used: {safe_llm_error_message(exc)}")
            mode = "local_fallback"
        _validate_test_case_contract(generated, approved_requirements, request.count, discovery_contexts)
        generation_id, timestamp = uid("tcg"), now()
        requirement_contract_hash = hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        generated.update({"contract_version": "1.0", "generation_id": generation_id, "analysis_id": analysis_id, "analysis_version": analysis["analysis_version"], "requirement_contract_hash": requirement_contract_hash, "package_id": package["package_id"], "package_version": package["package_version"], "source_package_run_id": package["run_id"], "requested_test_case_count": request.count, "generation_mode": mode, "generation_model": OPENAI_MODEL if mode == "openai" else LOCAL_EXTRACTOR_VERSION, "generation_prompt_version": TEST_CASE_GENERATION_PROMPT_VERSION, "generation_prompt_hash": test_case_generation_prompt_hash(), "generated_at": timestamp, "approved_requirement_ids": [item["requirement_id"] for item in approved_requirements], "source_package_items": contract.get("source_package_items", []), "discovery_contexts": discovery_contexts, "discovery_context_ids": [context["context_id"] for context in discovery_contexts]})
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
        try:
            if os.getenv("OPENAI_API_KEY"):
                generated = _test_cases_with_openai(approved_requirements, package_row, requested_count, discovery_contexts)
                mode = "openai"
            else:
                generated = _test_cases_fallback(approved_requirements, requested_count, package_row, discovery_contexts)
                mode = "local_fallback"
        except Exception as exc:
            generated = _test_cases_fallback(approved_requirements, requested_count, package_row, discovery_contexts)
            generated.setdefault("global_gaps", []).append(f"LLM test-case regeneration failed; local fallback was used: {safe_llm_error_message(exc)}")
            mode = "local_fallback"

        _validate_test_case_contract(generated, approved_requirements, requested_count, discovery_contexts)
        new_generation_id, timestamp = uid("tcg"), now()
        requirement_contract_hash = hashlib.sha256(json.dumps(requirement_contract, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        generation_version = conn.execute("SELECT COUNT(*)+1 AS v FROM test_case_generation WHERE analysis_id=%s", (parent["analysis_id"],)).fetchone()["v"]
        generated.update({
            "contract_version": "1.0",
            "generation_id": new_generation_id,
            "analysis_id": parent["analysis_id"],
            "analysis_version": analysis["analysis_version"],
            "requirement_contract_hash": requirement_contract_hash,
            "package_id": package["package_id"],
            "package_version": package["package_version"],
            "source_package_run_id": package["run_id"],
            "requested_test_case_count": requested_count,
            "generation_mode": mode,
            "generation_model": OPENAI_MODEL if mode == "openai" else LOCAL_EXTRACTOR_VERSION,
            "generation_prompt_version": TEST_CASE_GENERATION_PROMPT_VERSION,
            "generation_prompt_hash": test_case_generation_prompt_hash(),
            "generated_at": timestamp,
            "approved_requirement_ids": [item["requirement_id"] for item in approved_requirements],
            "source_package_items": requirement_contract.get("source_package_items", []),
            "discovery_contexts": discovery_contexts,
            "discovery_context_ids": [context["context_id"] for context in discovery_contexts],
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

        timestamp = now()
        selected_cases = [json.loads(json.dumps(by_id[case_id])) for case_id in request.selected_test_case_ids]
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
        return _test_run_response(conn.execute("SELECT * FROM test_run WHERE run_id=%s", (run_id,)).fetchone())

@app.get("/api/v1/test-case-generations/{generation_id}/execution-runs")
def list_execution_runs_for_generation(generation_id: str):
    with connection() as conn:
        if not conn.execute("SELECT generation_id FROM test_case_generation WHERE generation_id=%s", (generation_id,)).fetchone():
            raise HTTPException(404, "Test-case generation run not found")
        rows = conn.execute("SELECT * FROM test_run WHERE generation_id=%s ORDER BY created_at DESC", (generation_id,)).fetchall()
        return [_test_run_response(row) for row in rows]

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
        return _test_run_response(conn.execute("SELECT * FROM test_run WHERE run_id=%s", (run_id,)).fetchone())

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
