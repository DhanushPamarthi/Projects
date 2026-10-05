import { useEffect, useMemo, useState } from "react";
import { AlertCircle, ArrowLeft, Check, CheckCircle2, ClipboardList, LoaderCircle, Pencil, RefreshCw, ShieldCheck, X } from "lucide-react";
import { generateTestCasesFromAnalysis, getRequirementAnalysis, listGeneratedTestCases, regenerateTestCasesFromGeneration, reviewTestCaseGeneration } from "../api/client.js";

function lines(value) {
  return String(value || "").split("\n").map((item) => item.trim()).filter(Boolean);
}

function caseContract(generation) {
  return generation?.contract || generation || {};
}

function shortCategory(value) {
  return String(value || "case").replaceAll("_", " ");
}

function caseDescription(item) {
  const explicit = displayText(item.description);
  if (explicit && !/^requirement behavior is validated from the approved contract\.?$/i.test(explicit)) return explicit;
  const expected = Array.isArray(item.expected_results) ? item.expected_results.find((value) => typeof value === "string" && value.trim()) : "";
  return buildCaseDescription(item, expected);
}

function displayText(value) {
  return String(value || "")
    .replace(/customer data platform\s*\(\s*cdp\s*\)/gi, "")
    .replace(/customer data platform/gi, "")
    .replace(/\bCDP\b/gi, "")
    .replace(/\s+in\s+the\s*([.!?]|$)/gi, "$1")
    .replace(/\s+/g, " ")
    .trim();
}

function buildCaseDescription(item, expectedResult) {
  const rawScenario = displayText(item.scenario || item.title || "the documented behavior");
  const scenario = (/^requirement behavior is validated from the approved contract\.?$/i.test(rawScenario) ? caseBaseTitle(item) : rawScenario).replace(/[.!?]+$/, "");
  const setupFields = [...new Set((item.test_data_setup || []).map((setup) => displayText(setup.name)).filter(Boolean))];
  const setupSummary = setupFields.length ? `It prepares ${setupFields.join(" and ")} for this condition` : "It prepares the documented preconditions";
  const outcome = displayText(expectedResult || "the expected behavior from the approved requirement").replace(/[.!?]+$/, "");
  return `This case covers the scenario where ${scenario[0].toLowerCase()}${scenario.slice(1)}. ${setupSummary} and checks for the expected outcome: ${outcome}.`;
}

function caseBaseTitle(item) {
  let title = displayText(item.title);
  title = title.replace(/^verify the objective is to test\s+/i, "Verify ")
    .replace(/^verify the campaign should identify eligible members who are due for a prescription refill and send a refill reminder communication\.?$/i, "Verify refill eligibility and reminder delivery")
    .replace(/^verify the campaign should identify members who meet the refill reminder criteria\.?$/i, "Verify refill reminder eligibility")
    .replace(/^verify the campaign should\s+/i, "Verify campaign ")
    .replace(/^verify for an eligible member,?\s*/i, "Verify eligible-member ")
    .replace(/\s+/g, " ")
    .trim();
  if (!/^(?:requirement validation|functional behavior for\b|AC-\d+\b|BR-\d+\b)/i.test(title)) {
    return title || item.test_case_id;
  }
  const expected = Array.isArray(item.expected_results) ? item.expected_results.find((value) => typeof value === "string" && value.trim()) : "";
  const behavior = displayText(expected || item.scenario).replace(/[.!?]+$/, "");
  if (!behavior) return title;
  const formatted = behavior[0].toLowerCase() + behavior.slice(1);
  return `Verify ${formatted}`;
}

function caseDisplayTitle(item, allCases = []) {
  const title = caseBaseTitle(item);
  const duplicates = allCases.filter((candidate) => caseBaseTitle(candidate).toLocaleLowerCase() === title.toLocaleLowerCase());
  if (duplicates.length < 2) return title;
  const sourceId = item.acceptance_criteria_ids?.[0] || item.business_rule_ids?.[0] || item.requirement_id || item.test_case_id;
  const sameSource = duplicates.filter((candidate) => (candidate.acceptance_criteria_ids?.[0] || candidate.business_rule_ids?.[0] || candidate.requirement_id || candidate.test_case_id) === sourceId);
  return `${title} · ${sameSource.length > 1 ? item.test_case_id : sourceId}`;
}

export function TestCaseGenerationPage({ analysis, onBack, onContinueToTestData, onReviewSaved, onAnalysisUnavailable }) {
  const [verifiedAnalysis, setVerifiedAnalysis] = useState(null);
  const [analysisLoading, setAnalysisLoading] = useState(Boolean(analysis?.analysis_id));
  const [analysisError, setAnalysisError] = useState("");
  const [generation, setGeneration] = useState(null);
  const [selected, setSelected] = useState([]);
  const [statuses, setStatuses] = useState({});
  const [editingId, setEditingId] = useState("");
  const [editingTitle, setEditingTitle] = useState("");
  const [editingSteps, setEditingSteps] = useState("");
  const [editingExpected, setEditingExpected] = useState("");
  const [reviewer, setReviewer] = useState("Product Owner");
  const [comments, setComments] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  const currentAnalysis = verifiedAnalysis?.analysis_id === analysis?.analysis_id ? verifiedAnalysis : null;
  const isAnalysisLoading = analysisLoading || Boolean(analysis?.analysis_id && !currentAnalysis && !analysisError);
  const requirements = currentAnalysis?.contract?.requirements || [];
  const approvedIds = new Set(currentAnalysis?.contract?.approved_items || []);
  const approvedRequirements = requirements.filter((item) => approvedIds.has(item.requirement_id) || item.review_status === "approved");
  const contract = caseContract(generation);
  const generationId = generation?.generation_id || contract.generation_id;
  const testCases = contract.test_cases || [];
  const generationMode = contract.generation_mode;
  const generationModel = contract.generation_model;
  const ragStatus = contract.rag_status;
  const ragMetadata = contract.rag_metadata;
  const knowledgeUsed = Boolean(ragStatus?.used ?? ragMetadata?.index_id);
  const knowledgeContextMessage = generation
    ? (knowledgeUsed ? `Knowledge Base used${ragMetadata?.index_id ? ` · ${ragMetadata.index_id}` : ""}` : "Knowledge Base not used for this run")
    : "Knowledge Base evidence is included when ready; generation can proceed without it.";
  const approvedCount = testCases.filter((item) => (statuses[item.test_case_id] || item.review_status || item.approval?.status || "pending") === "approved").length;
  const allApproved = testCases.length > 0 && approvedCount === testCases.length;
  const selectedAll = testCases.length > 0 && selected.length === testCases.length;

  function applyGeneration(next) {
    const nextContract = caseContract(next);
    const nextCases = nextContract.test_cases || [];
    const nextStatuses = Object.fromEntries(nextCases.map((item) => [item.test_case_id, item.review_status || item.approval?.status || "pending"]));
    setGeneration(next);
    setStatuses(nextStatuses);
  }

  useEffect(() => {
    let active = true;
    setVerifiedAnalysis(null);
    setAnalysisError("");
    if (!analysis?.analysis_id) {
      setAnalysisLoading(false);
      setGeneration(null);
      return () => { active = false; };
    }
    setAnalysisLoading(true);
    getRequirementAnalysis(analysis.analysis_id)
      .then((items) => {
        if (active) setVerifiedAnalysis(items);
      })
      .catch((err) => {
        if (!active) return;
        setAnalysisError(err.status === 404 ? "not-found" : err.message || "load-failed");
        setGeneration(null);
        if (err.status === 404) onAnalysisUnavailable?.();
      })
      .finally(() => { if (active) setAnalysisLoading(false); });
    return () => { active = false; };
  }, [analysis?.analysis_id, onAnalysisUnavailable]);

  useEffect(() => {
    if (isAnalysisLoading || !currentAnalysis?.analysis_id) return;
    listGeneratedTestCases(currentAnalysis.analysis_id)
      .then((items) => applyGeneration(items[0] || null))
      .catch((err) => setError(err.message || "Could not load generated test cases."));
  }, [currentAnalysis?.analysis_id, isAnalysisLoading]);

  async function generate() {
    if (!currentAnalysis?.analysis_id) return;
    setLoading(true);
    setError("");
    setSuccess("");
    try {
      const next = await generateTestCasesFromAnalysis(currentAnalysis.analysis_id);
      applyGeneration(next);
      setSelected([]);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  function moveSelected(status) {
    setStatuses((current) => ({ ...current, ...Object.fromEntries(selected.map((id) => [id, status])) }));
  }

  function toggleSelected(id) {
    setSelected((items) => items.includes(id) ? items.filter((item) => item !== id) : [...items, id]);
  }

  function toggleAll(checked) {
    setSelected(checked ? testCases.map((item) => item.test_case_id) : []);
  }

  function startEdit(item) {
    setEditingId(item.test_case_id);
    setEditingTitle(caseDisplayTitle(item, testCases));
    setEditingSteps((item.steps || []).join("\n"));
    setEditingExpected((item.expected_results || []).join("\n"));
  }

  function saveEdit(id) {
    setGeneration((current) => {
      const next = structuredClone(current);
      const nextContract = next.contract || next;
      const item = (nextContract.test_cases || []).find((entry) => entry.test_case_id === id);
      if (item) {
        item.title = editingTitle.trim();
        item.steps = lines(editingSteps);
        item.expected_results = lines(editingExpected);
      }
      return next;
    });
    setEditingId("");
  }

  async function saveReview() {
    if (!generationId) return;
    setSaving(true);
    setError("");
    setSuccess("");
    try {
      const next = await reviewTestCaseGeneration(generationId, {
        reviewer: reviewer.trim(),
        comments: comments.trim() || null,
        items: testCases.map((item) => ({
          test_case_id: item.test_case_id,
          review_status: statuses[item.test_case_id] || item.review_status || item.approval?.status || "pending",
          title: item.title,
          steps: item.steps,
          expected_results: item.expected_results,
        })),
      });
      applyGeneration(next);
      if (onReviewSaved) onReviewSaved();
      else setSuccess("Test-case review saved. Approved cases are ready for test data generation.");
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  async function regenerateSelected() {
    if (!generationId) return;
    const ids = selected.length ? selected : (contract.rejected_test_case_ids || []);
    if (!ids.length) {
      setError("Select at least one test case, or reject cases before requesting regeneration.");
      return;
    }
    setRegenerating(true);
    setError("");
    setSuccess("");
    try {
      const next = await regenerateTestCasesFromGeneration(generationId, {
        reviewer: reviewer.trim() || null,
        reason: comments.trim() || "Reviewer requested regeneration.",
        test_case_ids: ids,
      });
      applyGeneration(next);
      setSelected([]);
      setSuccess("A new regenerated test-case version was created.");
    } catch (err) {
      setError(err.message);
    } finally {
      setRegenerating(false);
    }
  }

  const caseItems = useMemo(() => testCases.map((item) => ({ ...item, reviewStatus: statuses[item.test_case_id] || item.review_status || item.approval?.status || "pending" })), [testCases, statuses]);
  const approvedCaseItems = caseItems.filter((item) => item.reviewStatus === "approved");

  function listBlock(title, items, empty = "Not specified") {
    return <div className="case-detail-block"><h4>{title}</h4>{Array.isArray(items) && items.length ? <ul>{items.map((item, index) => <li key={`${title}-${index}`}>{displayText(typeof item === "string" ? item : item.text || item.action || item.name || JSON.stringify(item))}</li>)}</ul> : <p>{empty}</p>}</div>;
  }

  function testDataSetupBlock(items) {
    return <div className="case-detail-block test-data-setup-block"><h4>Test data setup</h4>{Array.isArray(items) && items.length ? <div className="test-data-setup-list">{items.map((item, index) => <div className="test-data-setup-item" key={`test-data-${index}`}><strong>{displayText(item.name || `Data item ${index + 1}`)}</strong><span>{displayText(item.setup_step || item.value_strategy || "Create required database test data before execution.")}</span><small>{item.created_during_execution ? "Created during execution" : "Existing or selected data"}{item.source ? ` - Source: ${displayText(item.source)}` : ""}</small></div>)}</div> : <p>Database test data setup is not specified.</p>}</div>;
  }

  function testCaseCard(item) {
    const editing = editingId === item.test_case_id;
    return <article className={`test-case-review-item rich-test-case ${item.reviewStatus}`} key={item.test_case_id}>
      <input type="checkbox" checked={selected.includes(item.test_case_id)} onChange={() => toggleSelected(item.test_case_id)} />
      <div className="test-case-review-content">
        <div className="test-case-review-heading">
          <div><strong>{item.test_case_id}</strong><span className="case-category">{shortCategory(item.category)}</span></div>
          <span className={`test-case-status ${item.reviewStatus}`}>{item.reviewStatus}</span>
        </div>
        {editing ? <>
          <input className="test-case-edit-title" value={editingTitle} onChange={(event) => setEditingTitle(event.target.value)} />
          <label className="test-case-edit-label">Steps<textarea className="test-case-edit-text" value={editingSteps} onChange={(event) => setEditingSteps(event.target.value)} /></label>
          <label className="test-case-edit-label">Expected results<textarea className="test-case-edit-text" value={editingExpected} onChange={(event) => setEditingExpected(event.target.value)} /></label>
          <div className="item-actions-static"><button type="button" className="secondary-button compact-button" onClick={() => saveEdit(item.test_case_id)}>Save</button><button type="button" className="text-button" onClick={() => setEditingId("")}>Cancel</button></div>
        </> : <>
          <h3>{caseDisplayTitle(item, testCases)}</h3>
          <p className="case-description-label">Description</p>
          <p className="case-scenario">{caseDescription(item)}</p>
          <div className="test-case-chip-row">
            <span>{item.requirement_id}</span>
            <span>{shortCategory(item.type)}</span>
            <span>{item.priority || "medium"}</span>
          </div>
          <div className="case-detail-grid">
            {testDataSetupBlock(item.test_data_setup)}
            <div className="case-detail-side">
              {listBlock("Steps", item.steps)}
              {listBlock("Expected results", item.expected_results)}
            </div>
          </div>
          <div className="test-case-item-actions"><button type="button" className="item-icon-button item-edit" title="Edit test case" aria-label={`Edit ${item.test_case_id}`} onClick={() => startEdit(item)}><Pencil size={14} /></button></div>
        </>}
      </div>
    </article>;
  }

  if (isAnalysisLoading) {
    return <div className="requirements-page test-generation-page"><section className="generation-empty" role="status"><LoaderCircle className="spin" size={22} /><h3>Checking approved requirements</h3></section></div>;
  }
  if (!currentAnalysis) {
    return <div className="requirements-page test-generation-page"><div className="empty-state-panel"><div className="empty-state-icon"><ClipboardList size={25} /></div><h2>{analysisError === "not-found" ? "No saved requirement analysis" : analysisError ? "Could not verify requirement analysis" : "No requirement analysis selected"}</h2><p>{analysisError === "not-found" ? "This saved analysis is not in the database. Return to Requirements to load current data before generating test cases." : analysisError ? "The saved analysis could not be loaded. Check the database connection, then return to Requirements and try again." : "Open Requirements, approve the scope, then continue to test-case generation."}</p><button type="button" className="primary-button" onClick={onBack}>Back to requirements</button></div></div>;
  }

  return <div className="requirements-page test-generation-page">
    <div className="page-heading"><div><div className="eyebrow">TEST CASE GENERATION</div><h1>Generate, review, and approve test cases</h1><p>Generate grounded cases from approved requirements, then review and approve them before creating their test data.</p></div><div className="generation-page-actions">{generation && approvedCaseItems.length > 0 && <button type="button" className="primary-button" onClick={onContinueToTestData}>Continue to test data</button>}<button type="button" className="secondary-button" onClick={onBack}><ArrowLeft size={16} /> Back to requirements</button></div></div>
    {error && <div className="error-message"><AlertCircle size={17} /> {error}</div>}
    {success && <div className="toast-message"><CheckCircle2 size={17} /> {success}</div>}
    <section className="generation-ready-card"><div className="generation-ready-icon"><CheckCircle2 size={24} /></div><div><div className="eyebrow">APPROVED SCOPE</div><h2>{approvedRequirements.length} approved requirement{approvedRequirements.length === 1 ? "" : "s"} ready</h2><p>Pending and rejected requirements are excluded. {generation ? `${testCases.length} test cases generated using ${generationMode === "openai" ? generationModel : "the local fallback"}. ${knowledgeContextMessage}.` : "Generate test cases from approved Functional Requirements. Knowledge Base evidence is included when ready."}</p></div><span className={`requirements-status ${allApproved ? "approved" : ""}`}>{allApproved ? "Approved" : generation ? "Needs review" : "Ready"}</span></section>
    <section className="generation-next-card"><div><div className="eyebrow">LLM TEST DESIGN</div><h2>{generation ? "Generate another set" : "Generate test cases from approved scope"}</h2><p>Generate cases from approved Functional Requirements and acceptance criteria, grounded in retrieved package evidence and approved business use cases.</p></div><div className="generation-controls"><button type="button" className="primary-button" disabled={!approvedRequirements.length || loading} onClick={generate}>{loading ? <><LoaderCircle className="spin" size={16} /> Generating...</> : <><ShieldCheck size={16} /> Generate test cases</>}</button></div></section>
    {generation && <section className="requirements-panel generated-cases-panel"><div className="panel-title-row"><div><h2>Review generated test cases</h2><p className="panel-helper">Approve or reject each case. Edit title, steps, or expected result before saving.</p></div><span>{approvedCount}/{testCases.length} approved</span></div><div className="selection-actions"><label className="select-all"><input type="checkbox" checked={selectedAll} onChange={(event) => toggleAll(event.target.checked)} /> Select all</label><div className="review-move-actions"><button type="button" className="review-icon-action approved-action" disabled={!selected.length} title="Approve selected test cases" onClick={() => moveSelected("approved")}><Check size={16} /></button><button type="button" className="review-icon-action rejected-action" disabled={!selected.length} title="Reject selected test cases" onClick={() => moveSelected("rejected")}><X size={16} /></button><button type="button" className="secondary-button compact-button" disabled={regenerating || (!selected.length && !(contract.rejected_test_case_ids || []).length)} title="Create a new generation version from selected or rejected cases" onClick={regenerateSelected}>{regenerating ? <><LoaderCircle className="spin" size={14} /> Regenerating</> : <><RefreshCw size={14} /> Regenerate</>}</button><button type="button" className="text-button" onClick={() => toggleAll(false)}>Clear all</button></div></div><div className="test-case-review-list">{caseItems.map(testCaseCard)}</div></section>}
    {generation && <section className="requirements-review-card"><div><div className="eyebrow">HUMAN REVIEW</div><h2>{allApproved ? "Test cases approved" : "Save test-case review"}</h2><p>{allApproved ? "Approved cases can continue to test data generation." : "Save reviewer, comments, and approval decisions for the generated cases."}</p></div><div className="review-fields"><label>Reviewer<input value={reviewer} onChange={(event) => setReviewer(event.target.value)} /></label><label>Review comments<textarea value={comments} onChange={(event) => setComments(event.target.value)} placeholder="Add review notes" /></label><button className="primary-button" onClick={saveReview} disabled={saving || !testCases.length}>{saving ? <><LoaderCircle className="spin" size={16} /> Saving review...</> : <><ShieldCheck size={16} /> Save test-case review</>}</button></div></section>}
  </div>;
}
