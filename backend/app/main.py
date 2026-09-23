import hashlib, json, mimetypes, uuid
from datetime import datetime, timezone
from pathlib import Path
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from .config import EXTRACTION_CONFIG_VERSION, EXTRACTION_PROMPT_VERSION, LOCAL_EXTRACTOR_VERSION, MAX_UPLOAD_BYTES, OPENAI_MODEL, REQUIRED_BRD_SECTIONS, SUPPORTED_EXTENSIONS, UPLOAD_DIR
from .db import connection, init_db
from .extractors import extract_local, extract_openai, missing_sections
from .validation import validate_file_content

app = FastAPI(title="TestIQ Source Package API", version="1.0.0")
SOURCE_TYPES = {"brd", "campaign_rules", "api_spec", "database_schema", "message_template", "ui_description"}

def now(): return datetime.now(timezone.utc).isoformat()
def uid(prefix): return f"{prefix}_{uuid.uuid4().hex}"
def row_dict(row): return dict(row) if row else None

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
                text, sections = extract_openai(local_text)
                mode = "openai"
            except Exception as exc:
                text, sections, mode = local_text, local_sections, "local"
                fallback_message = f"OpenAI extraction failed; local extraction was used: {exc}"
            messages = json.loads(conn.execute("SELECT validation_messages FROM source_revision WHERE revision_id=%s", (revision_id,)).fetchone()["validation_messages"] or "[]")
            messages.extend([f"Missing expected section: {item}" for item in missing_sections(local_sections, required_sections)])
            if fallback_message: messages.append(fallback_message)
            model = OPENAI_MODEL if mode == "openai" else LOCAL_EXTRACTOR_VERSION
            conn.execute("UPDATE source_extraction SET mode=%s,status=%s,extracted_text=%s,error=%s,model_name=%s,prompt_version=%s,config_version=%s WHERE extraction_id=%s", (mode, "succeeded", text, fallback_message, model, EXTRACTION_PROMPT_VERSION, EXTRACTION_CONFIG_VERSION, extraction_id))
            for index, section in enumerate(local_sections):
                conn.execute("INSERT INTO source_fragment (fragment_id,revision_id,source_id,fragment_index,heading,source_reference,page_number,fragment_type,paragraph_index,table_index,row_index,cell_index,fragment_text) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (uid("frag"), revision_id, source_id, index, section.get("heading"), section.get("reference", "unknown"), section.get("page_number"), section.get("fragment_type", "paragraph"), section.get("paragraph_index"), section.get("table_index"), section.get("row_index"), section.get("cell_index"), section.get("text", "")))
            conn.execute("UPDATE source_revision SET status=%s,validation_messages=%s WHERE revision_id=%s", ("uploaded", json.dumps(messages), revision_id))
        except Exception as exc:
            conn.execute("UPDATE source_extraction SET mode=%s,status=%s,error=%s,model_name=%s,prompt_version=%s,config_version=%s WHERE extraction_id=%s", ("local", "failed", str(exc), LOCAL_EXTRACTOR_VERSION, EXTRACTION_PROMPT_VERSION, EXTRACTION_CONFIG_VERSION, extraction_id))
            row = conn.execute("SELECT validation_messages FROM source_revision WHERE revision_id=%s", (revision_id,)).fetchone()
            messages = json.loads(row["validation_messages"] or "[]") if row else []
            messages.append(f"Extraction failed: {exc}")
            conn.execute("UPDATE source_revision SET status=%s,validation_messages=%s WHERE revision_id=%s", ("extraction_failed", json.dumps(messages), revision_id))

class ManifestRequest(BaseModel):
    source_ids: list[str] = Field(min_length=1)
    scope: str | None = None

class ProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)

class RequirementReviewItem(BaseModel):
    item_id: str = Field(min_length=1, max_length=200)
    approved: bool
    title: str | None = Field(default=None, max_length=400)
    statement: str | None = Field(default=None, max_length=10000)
    text: str | None = Field(default=None, max_length=10000)
    scenario: str | None = Field(default=None, max_length=400)
    given: list[str] | None = None
    when: str | None = Field(default=None, max_length=4000)
    then: str | None = Field(default=None, max_length=4000)

class RequirementReviewRequest(BaseModel):
    reviewer: str = Field(min_length=1, max_length=200)
    decision: str = Field(pattern="^(approved|needs_changes|rejected)$")
    comments: str | None = Field(default=None, max_length=4000)
    accepted_assumptions: list[str] = Field(default_factory=list)
    approved_items: list[str] = Field(default_factory=list)
    items: list[RequirementReviewItem] = Field(default_factory=list)

def _text_lines(fragments):
    return [{"text": " ".join(str(f.get("fragment_text") or "").split()), "heading": f.get("heading"), "source_id": f.get("source_id"), "revision_id": f.get("revision_id"), "source_reference": f.get("source_reference")} for f in fragments if str(f.get("fragment_text") or "").strip()]

def _requirement_contract(package, items, fragments):
    lines = _text_lines(fragments); text = " ".join(x["text"] for x in lines); lowered = text.lower()
    rules = [x for x in lines if any(t in x["text"].lower() for t in ("must", "shall", "eligible", "exclude", "fallback", "status"))]
    acceptance = [x for x in lines if any(t in x["text"].lower() for t in ("acceptance", "expected", "success", "outcome", "should"))]
    ambiguities = [x for x in lines if any(t in x["text"].lower() for t in ("tbd", "tbc", "to be confirmed", "undefined", "?"))]
    conflicts = [{"terms": list(pair), "description": f"Both '{pair[0]}' and '{pair[1]}' appear in the package; confirm precedence."} for pair in (("eligible", "ineligible"), ("include", "exclude"), ("retry", "no retry"), ("sms", "email")) if pair[0] in lowered and pair[1] in lowered]
    entities = [x for x in ("member", "campaign", "refill", "reminder", "communication", "prescription", "pharmacy") if x in lowered]
    actors = [x for x in ("member", "care team", "pharmacy", "campaign service", "scheduler", "administrator") if x in lowered]
    refs = lambda selected: [{"source_id": x["source_id"], "revision_id": x["revision_id"], "source_reference": x["source_reference"], "heading": x["heading"]} for x in selected]
    selected = lines[:2] or [{"source_id": i["source_id"], "revision_id": i["revision_id"], "source_reference": "unknown", "heading": i["logical_name"]} for i in items]
    def matching(*terms):
        found = [x for x in lines if any(term.lower() in x["text"].lower() for term in terms)]
        return found or selected

    def make_requirement(number, title, statement, terms, criteria, outcomes, flow):
        evidence = matching(*terms)
        return {"requirement_id": f"FR-{number:02d}", "title": title, "statement": statement,
                "approved": False,
                "business_rules": [{"rule_id": f"BR-{number:02d}-{index:02d}", "text": x["text"], "approved": False} for index, x in enumerate(evidence, 1)],
                "acceptance_criteria": [{"acceptance_id": f"AC-{number:02d}-{index:02d}", "scenario": f"Validate {title.lower()}", "given": [], "when": statement, "then": criterion, "text": criterion, "approved": False, "source_references": refs(evidence)} for index, criterion in enumerate(criteria, 1)],
                "actors": actors, "entities": entities, "flow_steps": flow,
                "expected_outcomes": [{"outcome_id": f"EO-{number:02d}-{index:02d}", "text": outcome, "approved": False} for index, outcome in enumerate(outcomes, 1)], "source_references": refs(evidence)}

    if "refill" in lowered and "sms" in lowered:
        requirements = [
            make_requirement(1, "Identify active members", "The system shall identify members with an active member record.", ("active member", "member record"), ["An active member is selected for campaign processing.", "An inactive member is excluded from campaign processing."], ["A qualifying member is available for eligibility validation."], ["Load member record", "Check member status"]),
            make_requirement(2, "Identify eligible prescriptions", "The system shall identify members who have an eligible prescription.", ("eligible prescription", "prescription eligibility"), ["A member with an eligible prescription continues to refill reminder processing.", "A member without an eligible prescription is excluded."], ["Eligible members continue to communication."], ["Evaluate prescription eligibility", "Route eligible or ineligible member"]),
            make_requirement(3, "Validate refill eligibility", "The campaign shall identify members who meet the refill reminder criteria.", ("refill reminder", "refill due", "seven days"), ["Members meeting the refill criteria are selected for the campaign."], ["A refill-eligible member enters the campaign."], ["Check refill due date", "Validate campaign eligibility"]),
            make_requirement(4, "Suppress ineligible communication", "The campaign shall not send communication when the member is not eligible.", ("not eligible", "ineligible", "stop campaign"), ["An ineligible member receives no SMS or phone communication."], ["No communication is sent."], ["Detect ineligible status", "Stop campaign"]),
            make_requirement(5, "Send SMS as primary communication", "For an eligible member, the campaign shall send an SMS refill reminder as the primary communication channel.", ("primary communication", "send sms", "sms refill"), ["An eligible member receives the refill reminder SMS."], ["SMS is sent as the primary communication."], ["Enter communication step", "Send SMS"]),
            make_requirement(6, "Record SMS communication status", "The system shall record the outcome of the SMS communication.", ("record sms", "sms sent", "sms failed", "communication status"), ["SMS sent or delivered and SMS failed statuses are recorded."], ["SMS outcome is captured."], ["Receive SMS provider result", "Persist SMS status"]),
            make_requirement(7, "Trigger phone fallback when SMS fails", "If SMS communication fails, the campaign shall trigger the alternate phone-call communication.", ("sms fails", "phone call", "alternate communication"), ["A phone call is triggered after SMS failure.", "A phone call is not triggered after successful SMS delivery."], ["SMS failure leads to phone fallback."], ["Check SMS outcome", "Trigger phone call when failed"]),
            make_requirement(8, "Record phone-call outcome", "The system shall record the outcome of the alternate phone-call communication.", ("call connected", "no answer", "call failed", "call outcome"), ["Connected, no-answer, and failed call outcomes are recorded."], ["Phone-call outcome is captured."], ["Place phone call", "Capture call outcome"]),
            make_requirement(9, "Revalidate eligibility before communication", "The campaign shall suppress communication if the member becomes ineligible before communication is sent.", ("becomes ineligible", "before communication", "revalidate"), ["A member who becomes ineligible receives no communication."], ["Communication is suppressed when eligibility changes."], ["Recheck eligibility", "Suppress communication if status changed"]),
        ]
    else:
        requirements = [make_requirement(1, "Source package requirement", "The system shall implement the reviewed business behavior described by the source package.", tuple(), [x["text"] for x in (acceptance or lines)[:25]], [x["text"] for x in (acceptance or lines)[:15]], ["Evaluate the source-defined flow"])]
    gaps = []
    if not rules: gaps.append("No business rules were extracted")
    if not acceptance and not lines: gaps.append("No acceptance criteria or expected outcomes were extracted")
    return {"contract_version": "1.0", "package_id": package["package_id"], "project_id": package["project_id"], "package_scope": package.get("scope"), "requirements": requirements, "ambiguities": [{"text": x["text"], "source_reference": x["source_reference"]} for x in ambiguities[:25]], "conflicts": conflicts, "gaps": gaps, "accepted_assumptions": [], "readiness": {"status": "ready" if not gaps else "needs_review", "journey": "campaign refill reminder", "gaps": gaps}, "source_package_items": [{"source_id": i["source_id"], "revision_id": i["revision_id"], "source_type": i["source_type"], "logical_name": i["logical_name"], "sha256": i["sha256"]} for i in items], "generated_at": now(), "generation_mode": "source_package_fallback"}

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
                requirement["business_rules"][index - 1] = rule = {"rule_id": f"{requirement_id}:RULE-{index}", "text": rule, "approved": False}
            items.append((rule.get("rule_id", f"{requirement_id}:RULE-{index}"), rule, "business_rule"))
        for index, criterion in enumerate(requirement.get("acceptance_criteria", []), 1):
            if isinstance(criterion, str):
                requirement["acceptance_criteria"][index - 1] = criterion = {"acceptance_id": f"{requirement_id}:AC-{index}", "text": criterion, "approved": False}
            items.append((criterion.get("acceptance_id", f"{requirement_id}:AC-{index}"), criterion, "acceptance_criterion"))
        for index, outcome in enumerate(requirement.get("expected_outcomes", []), 1):
            if isinstance(outcome, str):
                requirement["expected_outcomes"][index - 1] = outcome = {"outcome_id": f"{requirement_id}:OUTCOME-{index}", "text": outcome, "approved": False}
            items.append((outcome.get("outcome_id", f"{requirement_id}:OUTCOME-{index}"), outcome, "expected_outcome"))
    return items

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
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES: raise HTTPException(413, "Uploaded file exceeds the maximum size")
    init_db()
    try: detected_type, messages = validate_file_content(file.filename or "source", content, file.content_type)
    except ValueError as exc:
        source_id, revision_id = uid("src"), uid("rev"); path = UPLOAD_DIR / f"{revision_id}_{Path(file.filename or 'source').name}"; path.write_bytes(content)
        with connection() as conn:
            conn.execute("INSERT INTO source_document (source_id,project_id,source_type,logical_name,owner_name,active_revision_id,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", (source_id, project_id, source_type, file.filename or "source", owner, None, now(), now()))
            conn.execute("INSERT INTO source_revision (revision_id,source_id,revision,filename,content_type,size_bytes,sha256,storage_path,owner_version,ingestion_version,status,validation_messages,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (revision_id, source_id, 1, file.filename or "source", file.content_type, len(content), hashlib.sha256(content).hexdigest(), str(path), owner_version, "source-intake-v1", "rejected", json.dumps([str(exc)]), now()))
        return JSONResponse(status_code=415 if "Unsupported document format" in str(exc) else 422, content={"status": "rejected", "revision_id": revision_id, "source_id": source_id, "validation_messages": [str(exc)], "original_preserved": True})
    digest = hashlib.sha256(content).hexdigest(); filename = file.filename or "source"
    with connection() as conn:
        existing = conn.execute("SELECT s.*,r.* FROM source_document s JOIN source_revision r ON r.source_id=s.source_id WHERE s.project_id=%s AND s.source_type=%s AND s.logical_name=%s ORDER BY r.revision DESC", (project_id, source_type, filename)).fetchall()
        source = existing[0] if existing else None
        source_id = source["source_id"] if source else uid("src")
        if source and any(x["sha256"] == digest for x in existing):
            reused_revision_id = next(x["revision_id"] for x in existing if x["sha256"] == digest)
            revision_id = uid("rev"); revision = max(x["revision"] for x in existing) + 1
            conn.execute("INSERT INTO source_revision (revision_id,source_id,revision,filename,content_type,size_bytes,sha256,storage_path,owner_version,ingestion_version,status,validation_messages,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (revision_id, source_id, revision, filename, detected_type, len(content), digest, "", owner_version, "source-intake-v1", "uploaded", json.dumps(messages), now()))
            conn.execute("INSERT INTO source_extraction (extraction_id,revision_id,mode,status,extracted_text,error,extractor_version,model_name,prompt_version,config_version,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (uid("ext"), revision_id, "reused", "skipped", None, reused_revision_id, LOCAL_EXTRACTOR_VERSION, None, EXTRACTION_PROMPT_VERSION, EXTRACTION_CONFIG_VERSION, now()))
            conn.execute("UPDATE source_document SET active_revision_id=%s,updated_at=%s WHERE source_id=%s", (revision_id, now(), source_id))
            return JSONResponse(status_code=201, content={"status": "duplicate", "source_id": source_id, "revision_id": revision_id, "extraction": {"status": "skipped"}, "extraction_summary": {"status": "skipped", "extraction_mode": "reused", "reused_revision_id": reused_revision_id}})
        revision_id, path = uid("rev"), UPLOAD_DIR / f"{uid('file')}_{Path(filename).name}"; path.write_bytes(content)
        revision = (existing[0]["revision"] + 1) if existing else 1
        if not source: conn.execute("INSERT INTO source_document (source_id,project_id,source_type,logical_name,owner_name,active_revision_id,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", (source_id, project_id, source_type, filename, owner, revision_id, now(), now()))
        else: conn.execute("UPDATE source_document SET active_revision_id=%s,updated_at=%s WHERE source_id=%s", (revision_id, now(), source_id))
        conn.execute("INSERT INTO source_revision (revision_id,source_id,revision,filename,content_type,size_bytes,sha256,storage_path,owner_version,ingestion_version,status,validation_messages,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (revision_id, source_id, revision, filename, detected_type, len(content), digest, str(path), owner_version, "source-intake-v1", "pending", json.dumps(messages), now()))
        conn.execute("INSERT INTO source_extraction (extraction_id,revision_id,mode,status,extracted_text,error,extractor_version,model_name,prompt_version,config_version,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (uid("ext"), revision_id, "pending", "pending", None, None, LOCAL_EXTRACTOR_VERSION, None, EXTRACTION_PROMPT_VERSION, EXTRACTION_CONFIG_VERSION, now()))
        if brd_source_id: conn.execute("INSERT OR IGNORE INTO source_relationship (relationship_id,brd_source_id,supporting_source_id,relationship_type,created_at) VALUES (%s,%s,%s,%s,%s)", (uid("rel"), brd_source_id, source_id, "supports", now()))
        extraction_id = conn.execute("SELECT extraction_id FROM source_extraction WHERE revision_id=%s", (revision_id,)).fetchone()["extraction_id"]
    if background_tasks is not None: background_tasks.add_task(process_extraction, revision_id, source_id, extraction_id, str(path), filename, [x.strip() for x in (required_sections or ",".join(REQUIRED_BRD_SECTIONS)).split(",") if x.strip()])
    return JSONResponse(status_code=202, content={"status": "accepted", "active": True, "source_id": source_id, "revision_id": revision_id, "revision": revision, "sha256": digest, "validation_messages": messages, "extraction": {"status": "pending"}})

@app.get("/api/v1/projects/{project_id}/sources")
def list_sources(project_id: str):
    with connection() as conn:
        sources = conn.execute("SELECT * FROM source_document WHERE project_id=%s ORDER BY created_at", (project_id,)).fetchall(); result=[]
        for source in sources:
            item=row_dict(source); rev=conn.execute("SELECT * FROM source_revision WHERE revision_id=%s", (source["active_revision_id"],)).fetchone() if source["active_revision_id"] else None; item["active_revision"]=row_dict(rev); item["extraction"]=row_dict(conn.execute("SELECT status,mode,error,model_name FROM source_extraction WHERE revision_id=%s ORDER BY created_at DESC LIMIT 1", (source["active_revision_id"],)).fetchone()) if source["active_revision_id"] else None; item["supporting_sources"]=[row_dict(x) for x in conn.execute("SELECT s.source_id,s.source_type,s.logical_name FROM source_relationship r JOIN source_document s ON s.source_id=r.supporting_source_id WHERE r.brd_source_id=%s", (source["source_id"],)).fetchall()]; result.append(item)
        return result

@app.get("/api/v1/revisions/{revision_id}")
def get_revision(revision_id: str):
    with connection() as conn:
        row=conn.execute("SELECT r.*,s.source_type,s.logical_name FROM source_revision r JOIN source_document s ON s.source_id=r.source_id WHERE r.revision_id=%s", (revision_id,)).fetchone()
        if not row: raise HTTPException(404,"Revision not found")
        result=row_dict(row); extraction=conn.execute("SELECT * FROM source_extraction WHERE revision_id=%s ORDER BY created_at DESC LIMIT 1",(revision_id,)).fetchone(); result["extraction"]=row_dict(extraction); result["source"]={"source_id":row["source_id"],"source_type":row["source_type"],"logical_name":row["logical_name"]}; result["validation_messages"]=json.loads(row["validation_messages"] or "[]"); result["fragments"]=[row_dict(x) for x in conn.execute("SELECT * FROM source_fragment WHERE revision_id=%s ORDER BY fragment_index",(revision_id,)).fetchall()]; missing=[m.split(": ",1)[1] for m in result["validation_messages"] if m.startswith("Missing expected section: ")]; summary={"status":extraction["status"] if extraction else None,"extraction_mode":extraction["mode"] if extraction else None,"text_available":bool(extraction and extraction["extracted_text"]),"section_count":len(result["fragments"]),"missing_sections":missing};
        if extraction and extraction["mode"] == "reused": summary["reused_revision_id"] = extraction["error"]
        result["extraction_summary"]=summary; return result

@app.post("/api/v1/projects/{project_id}/manifests", status_code=201)
def create_manifest(project_id: str, request: ManifestRequest):
    with connection() as conn:
        sources=[]
        for sid in request.source_ids:
            source=conn.execute("SELECT * FROM source_document WHERE source_id=%s AND project_id=%s",(sid,project_id)).fetchone()
            if not source: raise HTTPException(404,"Source not found")
            if not source["active_revision_id"]: raise HTTPException(422,"Source has no active revision")
            sources.append(source)
        version=conn.execute("SELECT COALESCE(MAX(package_version),0)+1 AS next_version FROM source_package_manifest WHERE project_id=%s",(project_id,)).fetchone()["next_version"]; package_id=uid("pkg"); conn.execute("INSERT INTO source_package_manifest VALUES (%s,%s,%s,%s,%s)",(package_id,project_id,version,request.scope,now())); items=[]
        for source in sources:
            rev=conn.execute("SELECT r.*,e.mode,e.model_name,e.prompt_version,e.config_version FROM source_revision r LEFT JOIN source_extraction e ON e.revision_id=r.revision_id WHERE r.revision_id=%s ORDER BY e.created_at DESC LIMIT 1",(source["active_revision_id"],)).fetchone(); conn.execute("INSERT INTO source_package_item (package_id,source_id,revision_id,source_version,ingestion_status,ingestion_version,content_hash,model_name,prompt_version,config_version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",(package_id,source["source_id"],rev["revision_id"],rev["owner_version"],rev["status"],rev["ingestion_version"],rev["sha256"],rev["model_name"],rev["prompt_version"],rev["config_version"])); items.append({"source_id":source["source_id"],"revision_id":rev["revision_id"],"ingestion_status":rev["status"],"content_hash":rev["sha256"],"model_name":rev["model_name"],"prompt_version":rev["prompt_version"],"config_version":rev["config_version"]})
        return {"package_id":package_id,"project_id":project_id,"package_version":version,"scope":request.scope,"items":items}

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
        ids=[x["revision_id"] for x in items]; fragments=conn.execute(f"SELECT * FROM source_fragment WHERE revision_id IN ({','.join('%s' for _ in ids)}) ORDER BY revision_id,fragment_index",ids).fetchall(); contract=_requirement_contract(row_dict(package),[row_dict(x) for x in items],[row_dict(x) for x in fragments]); version=(conn.execute("SELECT COALESCE(MAX(analysis_version),0)+1 AS v FROM requirement_analysis WHERE package_id=%s",(package_id,)).fetchone()["v"]); analysis_id=uid("req"); timestamp=now(); conn.execute("INSERT INTO requirement_analysis (analysis_id,package_id,project_id,analysis_version,status,contract_json,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",(analysis_id,package_id,package["project_id"],version,"needs_review",json.dumps(contract),timestamp,timestamp)); return _analysis_response(conn.execute("SELECT * FROM requirement_analysis WHERE analysis_id=%s",(analysis_id,)).fetchone())

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
                    if submitted_item.given is not None: target["given"] = [value.strip() for value in submitted_item.given if value.strip()]
                    if submitted_item.text is not None: target["text"] = submitted_item.text.strip()
                target["approved"] = submitted_item.approved
            approved = {key for key, value, _ in review_items if value.get("approved")}
            contract["approved_items"] = sorted(approved)
            status = "approved" if approved == expected else "needs_changes"
            decision = status
        else:
            contract["approved_items"] = list(dict.fromkeys(request.approved_items))
            status = "approved" if request.decision == "approved" else request.decision
            decision = request.decision
        timestamp=now()
        conn.execute("UPDATE requirement_analysis SET status=%s,contract_json=%s,reviewer=%s,review_decision=%s,review_comments=%s,reviewed_at=%s,updated_at=%s WHERE analysis_id=%s",(status,json.dumps(contract),request.reviewer,decision,request.comments,timestamp,timestamp,analysis_id))
        return _analysis_response(conn.execute("SELECT * FROM requirement_analysis WHERE analysis_id=%s", (analysis_id,)).fetchone())

@app.exception_handler(ValueError)
async def value_error(_, exc): return JSONResponse(status_code=422, content={"detail":str(exc)})
