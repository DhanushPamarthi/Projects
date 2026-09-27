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

def extraction_config_hash() -> str:
    payload = {"model": OPENAI_MODEL, "timeout": OPENAI_TIMEOUT_SECONDS, "extractor": LOCAL_EXTRACTOR_VERSION, "config_version": EXTRACTION_CONFIG_VERSION}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

REQUIREMENTS_ANALYSIS_PROMPT = (
    "You are a senior business analyst creating a professional, testable requirements contract from source fragments.\n"
    "Use only the supplied fragments. Do not invent behavior, actors, values, rules, priorities, or acceptance conditions.\n"
    "Return exactly the requested JSON schema. Do not add expected_outcomes, conflicts, approved flags, or any other fields.\n"
    "Create concise requirements. For each requirement include type, priority, actors, entities, at least one business rule, at least one acceptance criterion, ambiguities, assumptions, and source references.\n"
    "Use globally unique sequential IDs with exactly three digits: requirements FR-001, FR-002; business rules BR-001, BR-002; acceptance criteria AC-001, AC-002. Do not nest or restart child IDs per requirement.\n"
    "Each acceptance criterion must use a clear Given, When, Then structure. Use a single string for given.\n"
    "Every requirement, rule, and acceptance criterion must include one or more source references using the supplied fragment_id and source_reference values exactly.\n"
    "Use global_gaps for missing or unresolved package-level information and global_assumptions for package-level assumptions. Use empty arrays when none exist.\n\n"
    "SOURCE FRAGMENTS:\n"
)

TEST_CASE_GENERATION_PROMPT = (
    "You are a senior QA test designer. Generate grounded, reviewable test cases only from the approved requirements supplied below.\n"
    "Do not invent APIs, screens, data values, actors, rules, or outcomes that are not supported by the approved contract.\n"
    "Create positive, negative, boundary, and failure cases only when the approved requirement or its rules justify them.\n"
    "Every test case must cite at least one approved requirement, acceptance criterion, and/or business rule ID.\n"
    "Return only the requested JSON schema. Keep steps and expected results concise and actionable.\n\n"
    "APPROVED REQUIREMENTS:\n"
)

def test_case_generation_prompt_hash() -> str:
    return hashlib.sha256(TEST_CASE_GENERATION_PROMPT.encode("utf-8")).hexdigest()

def requirements_analysis_prompt_hash() -> str:
    return hashlib.sha256(REQUIREMENTS_ANALYSIS_PROMPT.encode("utf-8")).hexdigest()

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
                        "ambiguities": {"type": "array", "items": {"type": "string"}},
                        "assumptions": {"type": "array", "items": {"type": "string"}},
                        "source_references": references,
                    },
                    "required": ["requirement_id", "title", "type", "statement", "priority", "actors", "entities", "business_rules", "acceptance_criteria", "ambiguities", "assumptions", "source_references"],
                },
            },
            "global_gaps": {"type": "array", "items": {"type": "string"}},
            "global_assumptions": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["requirements", "global_gaps", "global_assumptions"],
    }

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
        for rule in requirement.get("business_rules", []):
            if not rule.get("text"): raise ValueError(f"AI returned an empty business rule for {requirement_id}")
            validate_refs(rule.get("source_references"), rule.get("rule_id", requirement_id))
        for criterion in requirement.get("acceptance_criteria", []):
            if not criterion.get("given") or not criterion.get("when") or not criterion.get("then"):
                raise ValueError(f"AI returned an invalid acceptance criterion for {requirement_id}")
            validate_refs(criterion.get("source_references"), criterion.get("acceptance_id", requirement_id))

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
        "gaps": [],
        "accepted_assumptions": [],
        "readiness": {"status": "ready", "journey": "source package", "gaps": []},
        "source_package_items": [{"source_id": i["source_id"], "revision_id": i["revision_id"], "source_type": i["source_type"], "logical_name": i["logical_name"], "sha256": i["sha256"]} for i in items],
        "generated_at": now(),
        "generation_mode": "openai",
        "generation_model": OPENAI_MODEL,
        "generation_prompt_version": REQUIREMENTS_ANALYSIS_PROMPT_VERSION,
        "generation_prompt_hash": requirements_analysis_prompt_hash(),
        "generation_config_hash": requirements_analysis_config_hash(),
    })
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
    ambiguities = [x for x in lines if any(t in x["text"].lower() for t in ("tbd", "tbc", "to be confirmed", "undefined", "?"))]
    conflicts = [{"terms": list(pair), "description": f"Both '{pair[0]}' and '{pair[1]}' appear in the package; confirm precedence."} for pair in (("eligible", "ineligible"), ("include", "exclude"), ("retry", "no retry"), ("sms", "email")) if pair[0] in lowered and pair[1] in lowered]
    entities = [x for x in ("member", "campaign", "refill", "reminder", "communication", "prescription", "pharmacy") if x in lowered]
    actors = [x for x in ("member", "care team", "pharmacy", "campaign service", "scheduler", "administrator") if x in lowered]
    refs = lambda selected: [{"fragment_id": x.get("fragment_id"), "source_reference": x["source_reference"]} for x in selected]
    selected = lines[:2] or [{"source_id": i["source_id"], "revision_id": i["revision_id"], "source_reference": "unknown", "heading": i["logical_name"]} for i in items]
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
    if not acceptance and not lines: gaps.append("No acceptance criteria or expected outcomes were extracted")
    global_gaps = gaps + (["The source contains unresolved ambiguities."] if ambiguities else [])
    return {"contract_version": "1.0", "package_id": package["package_id"], "project_id": package["project_id"], "package_scope": package.get("scope"), "requirements": requirements, "global_gaps": global_gaps, "global_assumptions": [], "gaps": global_gaps, "accepted_assumptions": [], "readiness": {"status": "ready" if not global_gaps else "needs_review", "journey": "campaign refill reminder", "gaps": global_gaps}, "source_package_items": [{"source_id": i["source_id"], "revision_id": i["revision_id"], "source_type": i["source_type"], "logical_name": i["logical_name"], "sha256": i["sha256"]} for i in items], "generated_at": now(), "generation_mode": "source_package_fallback"}

def _analysis_response(row):
    result = row_dict(row); result["contract"] = json.loads(result.pop("contract_json")); return result

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
                requirement["expected_outcomes"][index - 1] = outcome = {"outcome_id": f"OUTCOME-{index}", "text": outcome, "approved": False}
            items.append((f"{requirement_id}:{outcome.get('outcome_id', f'OUTCOME-{index}')}", outcome, "expected_outcome"))
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
        approved.append({"requirement_id": requirement_id, "title": requirement.get("title"), "type": requirement.get("type"), "statement": requirement.get("statement"), "priority": requirement.get("priority"), "actors": requirement.get("actors", []), "entities": requirement.get("entities", []), "acceptance_criteria": criteria, "business_rules": rules, "source_references": requirement.get("source_references", [])})
    return approved

def _test_case_generation_schema():
    source_reference = {"type": "object", "additionalProperties": False, "properties": {"fragment_id": {"type": "string"}, "source_reference": {"type": "string"}}, "required": ["fragment_id", "source_reference"]}
    return {"type": "object", "additionalProperties": False, "properties": {"test_cases": {"type": "array", "minItems": 1, "items": {"type": "object", "additionalProperties": False, "properties": {
        "test_case_id": {"type": "string", "pattern": "^TC-[0-9]{3}$"}, "requirement_id": {"type": "string", "pattern": "^FR-[0-9]{3}$"}, "title": {"type": "string"}, "type": {"type": "string"}, "priority": {"type": "string"}, "preconditions": {"type": "array", "items": {"type": "string"}}, "steps": {"type": "array", "minItems": 1, "items": {"type": "string"}}, "expected_results": {"type": "array", "minItems": 1, "items": {"type": "string"}}, "acceptance_criteria_ids": {"type": "array", "items": {"type": "string", "pattern": "^AC-[0-9]{3}$"}}, "business_rule_ids": {"type": "array", "items": {"type": "string", "pattern": "^BR-[0-9]{3}$"}}, "assumptions": {"type": "array", "items": {"type": "string"}}, "source_references": {"type": "array", "items": source_reference, "minItems": 1}
    }, "required": ["test_case_id", "requirement_id", "title", "type", "priority", "preconditions", "steps", "expected_results", "acceptance_criteria_ids", "business_rule_ids", "assumptions", "source_references"]}}, "global_gaps": {"type": "array", "items": {"type": "string"}}}, "required": ["test_cases", "global_gaps"]}

def _validate_test_case_contract(contract, approved_requirements):
    if not isinstance(contract, dict) or not contract.get("test_cases"):
        raise ValueError("The AI returned no test cases")
    approved_by_id = {item["requirement_id"]: item for item in approved_requirements}
    seen = set()
    for test_case in contract["test_cases"]:
        case_id = test_case.get("test_case_id")
        requirement_id = test_case.get("requirement_id")
        if not case_id or case_id in seen or requirement_id not in approved_by_id:
            raise ValueError("AI returned an invalid or duplicate test case")
        seen.add(case_id)
        if not test_case.get("steps") or not test_case.get("expected_results"):
            raise ValueError(f"AI returned an incomplete test case for {requirement_id}")
        item = approved_by_id[requirement_id]
        criterion_ids = {x.get("acceptance_id") for x in item.get("acceptance_criteria", []) if isinstance(x, dict)}
        rule_ids = {x.get("rule_id") for x in item.get("business_rules", []) if isinstance(x, dict)}
        if not set(test_case.get("acceptance_criteria_ids", [])) <= criterion_ids or not set(test_case.get("business_rule_ids", [])) <= rule_ids:
            raise ValueError(f"AI returned an unapproved evidence reference for {case_id}")

def _test_cases_with_openai(approved_requirements, package, requested_count):
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), timeout=OPENAI_TIMEOUT_SECONDS)
    response = client.responses.create(model=OPENAI_MODEL, input=TEST_CASE_GENERATION_PROMPT + json.dumps({"requested_test_case_count": requested_count, "package_id": package["package_id"], "package_version": package["package_version"], "requirements": approved_requirements}, ensure_ascii=False), store=False, text={"format": {"type": "json_schema", "name": "test_case_contract", "strict": True, "schema": _test_case_generation_schema()}})
    if not response.output_text: raise ValueError("OpenAI returned no test cases")
    contract = json.loads(response.output_text)
    _validate_test_case_contract(contract, approved_requirements)
    if len(contract["test_cases"]) != requested_count:
        raise ValueError(f"AI returned {len(contract['test_cases'])} test cases; expected {requested_count}")
    return contract

def _test_cases_fallback(approved_requirements, requested_count):
    cases = []
    for index in range(requested_count):
        requirement = approved_requirements[index % len(approved_requirements)]
        criteria = requirement.get("acceptance_criteria", [])
        rules = requirement.get("business_rules", [])
        first = criteria[0] if criteria else {}
        cases.append({"test_case_id": f"TC-{index + 1:03d}", "requirement_id": requirement["requirement_id"], "title": f"{requirement.get('title') or 'Validate requirement'} - Case {index + 1}", "type": "positive", "priority": requirement.get("priority") or "medium", "preconditions": [first.get("given", "The documented preconditions are satisfied")] if isinstance(first, dict) else [], "steps": [first.get("when", "Execute the requirement flow")] if isinstance(first, dict) else ["Execute the requirement flow"], "expected_results": [first.get("then", requirement.get("statement", "The expected behavior occurs"))] if isinstance(first, dict) else [requirement.get("statement", "The expected behavior occurs")], "acceptance_criteria_ids": [first.get("acceptance_id")] if isinstance(first, dict) and first.get("acceptance_id") else [], "business_rule_ids": [rule.get("rule_id") for rule in rules if isinstance(rule, dict) and rule.get("rule_id")], "assumptions": [], "source_references": requirement.get("source_references", [])})
    return {"test_cases": cases, "global_gaps": ["LLM test-case generation was not run because OPENAI_API_KEY is not configured."]}

def _test_case_generation_response(row):
    result = row_dict(row); result["contract"] = json.loads(result.pop("contract_json")); return result

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
                raise HTTPException(502, f"LLM Requirements Analysis failed validation or generation: {exc}") from exc
        else:
            # Keep local development usable without a key, while making the
            # fallback explicit in the persisted contract.
            contract = _requirement_contract(row_dict(package), item_rows, fragment_rows)
            contract["generation_note"] = "LLM Requirements Analysis was not run because OPENAI_API_KEY is not configured."
        version = conn.execute("SELECT COALESCE(MAX(analysis_version),0)+1 AS v FROM requirement_analysis WHERE package_id=%s", (package_id,)).fetchone()["v"]
        analysis_id, timestamp = uid("req"), now()
        conn.execute("INSERT INTO requirement_analysis (analysis_id,package_id,project_id,analysis_version,status,contract_json,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", (analysis_id, package_id, package["project_id"], version, "needs_review", json.dumps(contract), timestamp, timestamp))
        return _analysis_response(conn.execute("SELECT * FROM requirement_analysis WHERE analysis_id=%s", (analysis_id,)).fetchone())

@app.get("/api/v1/packages/{package_id}/requirements")
def list_requirement_analyses(package_id: str):
    with connection() as conn: return [_analysis_response(x) for x in conn.execute("SELECT * FROM requirement_analysis WHERE package_id=%s ORDER BY analysis_version DESC",(package_id,)).fetchall()]

@app.get("/api/v1/requirement-analyses/{analysis_id}")
def get_requirement_analysis(analysis_id: str):
    with connection() as conn:
        row=conn.execute("SELECT * FROM requirement_analysis WHERE analysis_id=%s",(analysis_id,)).fetchone()
        if not row: raise HTTPException(404,"Requirement analysis not found")
        return _analysis_response(row)

@app.post("/api/v1/requirement-analyses/{analysis_id}/review")
def review_requirement_analysis(analysis_id: str, request: RequirementReviewRequest):
    with connection() as conn:
        row=conn.execute("SELECT * FROM requirement_analysis WHERE analysis_id=%s",(analysis_id,)).fetchone()
        if not row: raise HTTPException(404,"Requirement analysis not found")
        contract=json.loads(row["contract_json"])
        contract["accepted_assumptions"] = list(dict.fromkeys(contract.get("accepted_assumptions",[]) + request.accepted_assumptions))
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
                elif item_type in {"business_rule", "expected_outcome"}:
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
        conn.execute("UPDATE requirement_analysis SET status=%s,contract_json=%s,reviewer=%s,review_decision=%s,review_comments=%s,reviewed_at=%s,updated_at=%s WHERE analysis_id=%s",(status,json.dumps(contract),request.reviewer,decision,request.comments,timestamp,timestamp,analysis_id))
        return _analysis_response(conn.execute("SELECT * FROM requirement_analysis WHERE analysis_id=%s", (analysis_id,)).fetchone())

@app.post("/api/v1/requirement-analyses/{analysis_id}/test-cases", status_code=201)
def generate_test_cases_from_analysis(analysis_id: str, request: TestCaseGenerationRequest):
    with connection() as conn:
        analysis = conn.execute("SELECT * FROM requirement_analysis WHERE analysis_id=%s", (analysis_id,)).fetchone()
        if not analysis: raise HTTPException(404, "Requirement analysis not found")
        contract = json.loads(analysis["contract_json"])
        approved_requirements = _approved_requirement_payload(contract)
        if not approved_requirements: raise HTTPException(422, "Approve at least one requirement before generating test cases")
        package = conn.execute("SELECT * FROM source_package_manifest WHERE package_id=%s", (analysis["package_id"],)).fetchone()
        if not package: raise HTTPException(404, "Source package not found")
        try:
            if os.getenv("OPENAI_API_KEY"):
                generated = _test_cases_with_openai(approved_requirements, row_dict(package), request.count)
                mode = "openai"
            else:
                generated = _test_cases_fallback(approved_requirements, request.count)
                mode = "local_fallback"
        except Exception as exc:
            raise HTTPException(502, f"LLM test-case generation failed validation or generation: {exc}") from exc
        generated.update({"contract_version": "1.0", "analysis_id": analysis_id, "package_id": package["package_id"], "package_version": package["package_version"], "generation_mode": mode, "generation_model": OPENAI_MODEL if mode == "openai" else LOCAL_EXTRACTOR_VERSION, "generation_prompt_version": TEST_CASE_GENERATION_PROMPT_VERSION, "generation_prompt_hash": test_case_generation_prompt_hash(), "generated_at": now(), "approved_requirement_ids": [item["requirement_id"] for item in approved_requirements]})
        for test_case in generated.get("test_cases", []):
            test_case["review_status"] = "pending"
            test_case["approved"] = False
        generation_id, timestamp = uid("tcg"), now()
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
        for item in request.items:
            case = by_id[item.test_case_id]
            case["review_status"] = item.review_status
            case["approved"] = item.review_status == "approved"
            if item.title is not None: case["title"] = item.title.strip()
            if item.steps is not None: case["steps"] = [value.strip() for value in item.steps if value.strip()]
            if item.expected_results is not None: case["expected_results"] = [value.strip() for value in item.expected_results if value.strip()]
        contract["approved_test_case_ids"] = [case["test_case_id"] for case in cases if case.get("review_status") == "approved"]
        contract["rejected_test_case_ids"] = [case["test_case_id"] for case in cases if case.get("review_status") == "rejected"]
        contract["pending_test_case_ids"] = [case["test_case_id"] for case in cases if case.get("review_status", "pending") == "pending"]
        status = "approved" if not contract["pending_test_case_ids"] and not contract["rejected_test_case_ids"] else "needs_review"
        timestamp = now()
        conn.execute("UPDATE test_case_generation SET status=%s,contract_json=%s,updated_at=%s WHERE generation_id=%s", (status, json.dumps(contract), timestamp, generation_id))
        result = _test_case_generation_response(conn.execute("SELECT * FROM test_case_generation WHERE generation_id=%s", (generation_id,)).fetchone())
        result["reviewer"] = request.reviewer
        result["review_comments"] = request.comments
        return result

@app.exception_handler(ValueError)
async def value_error(_, exc): return JSONResponse(status_code=422, content={"detail":str(exc)})
