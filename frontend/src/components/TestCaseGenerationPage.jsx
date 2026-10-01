import { useEffect, useMemo, useState } from "react";
import { AlertCircle, ArrowLeft, Check, CheckCircle2, ClipboardList, Pencil, PlayCircle, RefreshCw, ShieldCheck, X } from "lucide-react";
import { createExecutionRun, generateTestCasesFromAnalysis, listExecutionRuns, listGeneratedTestCases, regenerateTestCasesFromGeneration, reviewTestCaseGeneration, updateExecutionRunLifecycle } from "../api/client.js";
import { LoadingSpinner } from "./LoadingSpinner.jsx";

function lines(value) {
  return String(value || "").split("\n").map((item) => item.trim()).filter(Boolean);
}

function caseContract(generation) {
  return generation?.contract || generation || {};
}

function shortCategory(value) {
  return String(value || "case").replaceAll("_", " ");
}

function terminalRunState(run) {
  const state = String(run?.lifecycle_state || run?.status || "").toLowerCase();
  return ["cancelled", "failed", "completed"].includes(state);
}

export function TestCaseGenerationPage({ analysis, onBack }) {
  const [generation, setGeneration] = useState(null);
  const [executionRuns, setExecutionRuns] = useState([]);
  const [selected, setSelected] = useState([]);
  const [executionSelected, setExecutionSelected] = useState([]);
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
  const [freezing, setFreezing] = useState(false);
  const [runActionId, setRunActionId] = useState("");
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  const requirements = analysis?.contract?.requirements || [];
  const approvedIds = new Set(analysis?.contract?.approved_items || []);
  const approvedRequirements = requirements.filter((item) => approvedIds.has(item.requirement_id) || item.review_status === "approved");
  const contract = caseContract(generation);
  const generationId = generation?.generation_id || contract.generation_id;
  const testCases = contract.test_cases || [];
  const generationMode = contract.generation_mode;
  const generationModel = contract.generation_model;
  const approvedCount = testCases.filter((item) => (statuses[item.test_case_id] || item.review_status || item.approval?.status || "pending") === "approved").length;
  const allApproved = testCases.length > 0 && approvedCount === testCases.length;
  const selectedAll = testCases.length > 0 && selected.length === testCases.length;

  function applyGeneration(next) {
    const nextContract = caseContract(next);
    const nextCases = nextContract.test_cases || [];
    const nextStatuses = Object.fromEntries(nextCases.map((item) => [item.test_case_id, item.review_status || item.approval?.status || "pending"]));
    const approvedCaseIds = nextCases.filter((item) => (nextStatuses[item.test_case_id] || "pending") === "approved").map((item) => item.test_case_id);
    setGeneration(next);
    setStatuses(nextStatuses);
    setExecutionSelected(approvedCaseIds);
  }

  async function refreshReviewDetails(id = generationId) {
    if (!id) return;
    try {
      const runs = await listExecutionRuns(id);
      setExecutionRuns(runs);
    } catch {
      // Older saved generations may not have execution runs yet; the page can
      // still display the generated cases.
    }
  }

  useEffect(() => {
    if (!analysis?.analysis_id) return;
    listGeneratedTestCases(analysis.analysis_id)
      .then((items) => {
        const next = items[0] || null;
        applyGeneration(next);
      })
      .catch(() => {});
  }, [analysis?.analysis_id]);

  useEffect(() => {
    if (!generationId) {
      setExecutionRuns([]);
      return;
    }
    refreshReviewDetails(generationId);
  }, [generationId]);

  async function generate() {
    if (!analysis?.analysis_id) return;
    setLoading(true);
    setError("");
    setSuccess("");
    try {
      const next = await generateTestCasesFromAnalysis(analysis.analysis_id);
      applyGeneration(next);
      setSelected([]);
      await refreshReviewDetails(next.generation_id);
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

  function toggleExecutionSelected(id) {
    setExecutionSelected((items) => items.includes(id) ? items.filter((item) => item !== id) : [...items, id]);
  }

  function startEdit(item) {
    setEditingId(item.test_case_id);
    setEditingTitle(item.title || "");
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
      setSuccess("Test-case review saved. Approved cases can now be frozen for execution.");
      await refreshReviewDetails(next.generation_id || generationId);
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
      await refreshReviewDetails(next.generation_id);
    } catch (err) {
      setError(err.message);
    } finally {
      setRegenerating(false);
    }
  }

  async function freezeExecutionRun() {
    if (!generationId) return;
    if (!executionSelected.length) {
      setError("Select at least one approved test case for the execution run.");
      return;
    }
    setFreezing(true);
    setError("");
    setSuccess("");
    try {
      const run = await createExecutionRun(generationId, {
        reviewer: reviewer.trim(),
        comments: comments.trim() || null,
        selected_test_case_ids: executionSelected,
      });
      setExecutionRuns((items) => [run, ...items.filter((item) => item.run_id !== run.run_id)]);
      setSuccess(`Execution run ${run.run_id} was frozen with ${executionSelected.length} approved case(s).`);
    } catch (err) {
      setError(err.message);
    } finally {
      setFreezing(false);
    }
  }

  async function updateRunLifecycle(run, lifecycleState) {
    const actionId = `${run.run_id}:${lifecycleState}`;
    setRunActionId(actionId);
    setError("");
    setSuccess("");
    try {
      const next = await updateExecutionRunLifecycle(run.run_id, {
        lifecycle_state: lifecycleState,
        reviewer: reviewer.trim() || null,
        comments: comments.trim() || null,
        error: lifecycleState === "failed" ? (comments.trim() || "Marked failed during coverage review.") : null,
      });
      setExecutionRuns((items) => items.map((item) => item.run_id === next.run_id ? next : item));
      setSuccess(`Execution run ${next.run_id} marked ${next.lifecycle_state || next.status}.`);
    } catch (err) {
      setError(err.message);
    } finally {
      setRunActionId("");
    }
  }

  const caseItems = useMemo(() => testCases.map((item) => ({ ...item, reviewStatus: statuses[item.test_case_id] || item.review_status || item.approval?.status || "pending" })), [testCases, statuses]);
  const approvedCaseItems = caseItems.filter((item) => item.reviewStatus === "approved");
  const executionAllSelected = approvedCaseItems.length > 0 && executionSelected.length === approvedCaseItems.length;

  function listBlock(title, items, empty = "Not specified") {
    return <div className="case-detail-block"><h4>{title}</h4>{Array.isArray(items) && items.length ? <ul>{items.map((item, index) => <li key={`${title}-${index}`}>{typeof item === "string" ? item : item.text || item.action || item.name || JSON.stringify(item)}</li>)}</ul> : <p>{empty}</p>}</div>;
  }

  function testDataSetupBlock(items) {
    return <div className="case-detail-block test-data-setup-block"><h4>Test data setup</h4>{Array.isArray(items) && items.length ? <div className="test-data-setup-list">{items.map((item, index) => <div className="test-data-setup-item" key={`test-data-${index}`}><strong>{item.name || `Data item ${index + 1}`}</strong><span>{item.setup_step || item.value_strategy || "Create required database test data before execution."}</span><small>{item.created_during_execution ? "Created during execution" : "Existing or selected data"}{item.source ? ` - Source: ${item.source}` : ""}</small></div>)}</div> : <p>Database test data setup is not specified.</p>}</div>;
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
          <h3>{item.title}</h3>
          <p className="case-scenario">{item.scenario}</p>
          <div className="test-case-chip-row">
            <span>{item.requirement_id}</span>
            <span>{shortCategory(item.type)}</span>
            <span>{item.priority || "medium"}</span>
          </div>
          <div className="case-detail-grid">
            {testDataSetupBlock(item.test_data_setup)}
            {listBlock("Steps", item.steps)}
            {listBlock("Expected results", item.expected_results)}
          </div>
          <div className="test-case-item-actions"><button type="button" className="item-icon-button item-edit" title="Edit test case" aria-label={`Edit ${item.test_case_id}`} onClick={() => startEdit(item)}><Pencil size={14} /></button></div>
        </>}
      </div>
    </article>;
  }

  if (!analysis) {
    return <div className="requirements-page test-generation-page"><div className="empty-state-panel"><div className="empty-state-icon"><ClipboardList size={25} /></div><h2>No requirement analysis selected</h2><p>Open Requirements, approve the scope, then continue to test-case generation.</p><button type="button" className="primary-button" onClick={onBack}>Back to requirements</button></div></div>;
  }

  return <div className="requirements-page test-generation-page">
    <div className="page-heading"><div><div className="eyebrow">TEST CASE GENERATION</div><h1>Generate, review, and approve test cases</h1><p>Generate grounded cases from approved requirements, review them, then freeze approved cases for execution.</p></div><button type="button" className="secondary-button" onClick={onBack}><ArrowLeft size={16} /> Back to requirements</button></div>
    {error && <div className="error-message"><AlertCircle size={17} /> {error}</div>}
    {success && <div className="toast-message"><CheckCircle2 size={17} /> {success}</div>}
    <section className="generation-ready-card"><div className="generation-ready-icon"><CheckCircle2 size={24} /></div><div><div className="eyebrow">APPROVED SCOPE</div><h2>{approvedRequirements.length} approved requirement{approvedRequirements.length === 1 ? "" : "s"} ready</h2><p>Pending and rejected requirements are excluded. {generation ? `${testCases.length} test cases generated using ${generationMode === "openai" ? generationModel : "the local fallback"}.` : "Generate grounded test cases from the approved functional requirements and acceptance criteria."}</p></div><span className={`requirements-status ${allApproved ? "approved" : ""}`}>{allApproved ? "Approved" : generation ? "Needs review" : "Ready"}</span></section>
    <section className="generation-next-card"><div><div className="eyebrow">LLM TEST DESIGN</div><h2>{generation ? "Generate another set" : "Generate test cases from approved scope"}</h2><p>The backend derives coverage from approved functional requirements, acceptance criteria, business requirements, source package evidence, and available Knowledge Fabric context.</p></div><div className="generation-controls"><button type="button" className="primary-button" disabled={!approvedRequirements.length || loading} onClick={generate}>{loading ? <LoadingSpinner variant="inline" label="Generating" /> : <><ShieldCheck size={16} /> Generate test cases</>}</button></div></section>
    {loading && <LoadingSpinner variant="section" label="Generating test cases" detail="Designing grounded scenarios from the approved functional requirements." />}
    {generation && <section className="requirements-panel generated-cases-panel"><div className="panel-title-row"><div><h2>Review generated test cases</h2><p className="panel-helper">Approve or reject each case. Edit title, steps, or expected result before saving.</p></div><span>{approvedCount}/{testCases.length} approved</span></div><div className="selection-actions"><label className="select-all"><input type="checkbox" checked={selectedAll} onChange={(event) => toggleAll(event.target.checked)} /> Select all</label><div className="review-move-actions"><button type="button" className="review-icon-action approved-action" disabled={!selected.length} title="Approve selected test cases" onClick={() => moveSelected("approved")}><Check size={16} /></button><button type="button" className="review-icon-action rejected-action" disabled={!selected.length} title="Reject selected test cases" onClick={() => moveSelected("rejected")}><X size={16} /></button><button type="button" className="secondary-button compact-button" disabled={regenerating || (!selected.length && !(contract.rejected_test_case_ids || []).length)} title="Create a new generation version from selected or rejected cases" onClick={regenerateSelected}>{regenerating ? <LoadingSpinner variant="inline" label="Regenerating" /> : <><RefreshCw size={14} /> Regenerate</>}</button><button type="button" className="text-button" onClick={() => toggleAll(false)}>Clear all</button></div></div><div className="test-case-review-list">{caseItems.map(testCaseCard)}</div></section>}
    {generation && <section className="requirements-review-card"><div><div className="eyebrow">HUMAN REVIEW</div><h2>{allApproved ? "Test cases approved" : "Save test-case review"}</h2><p>{allApproved ? "All generated test cases are approved and ready to be selected for an execution run." : "Save reviewer, comments, and approval decisions for the generated cases."}</p></div><div className="review-fields"><label>Reviewer<input value={reviewer} onChange={(event) => setReviewer(event.target.value)} /></label><label>Review comments<textarea value={comments} onChange={(event) => setComments(event.target.value)} placeholder="Add review notes" /></label><button className="primary-button" onClick={saveReview} disabled={saving || !testCases.length}>{saving ? <LoadingSpinner variant="inline" label="Saving review" /> : <><ShieldCheck size={16} /> Save test-case review</>}</button></div></section>}
    {generation && <section className="requirements-panel execution-baseline-panel"><div className="panel-title-row"><div><h2>Select approved scenarios for execution</h2><p className="panel-helper">Only saved, approved cases can be frozen into the execution baseline. The selected versions are stored immutably for the next workflow stage.</p></div><span>{executionSelected.length} selected</span></div>{approvedCaseItems.length ? <><div className="selection-actions"><label className="select-all"><input type="checkbox" checked={executionAllSelected} onChange={(event) => setExecutionSelected(event.target.checked ? approvedCaseItems.map((item) => item.test_case_id) : [])} /> Select all approved</label><button type="button" className="primary-button" disabled={freezing || !executionSelected.length} onClick={freezeExecutionRun}>{freezing ? <LoadingSpinner variant="inline" label="Freezing" /> : <><PlayCircle size={16} /> Freeze for execution</>}</button></div><div className="execution-case-list">{approvedCaseItems.map((item) => <label className="execution-case-item" key={item.test_case_id}><input type="checkbox" checked={executionSelected.includes(item.test_case_id)} onChange={() => toggleExecutionSelected(item.test_case_id)} /><span><strong>{item.test_case_id}</strong>{item.title}</span></label>)}</div></> : <div className="generation-empty"><ShieldCheck size={22} /><h3>No approved cases yet</h3><p>Approve cases and save the review before creating an execution baseline.</p></div>}{executionRuns.length > 0 && <div className="execution-run-list"><h3>Frozen execution runs</h3>{executionRuns.map((run) => { const state = run.lifecycle_state || run.status; const terminal = terminalRunState(run); return <article className={String(state || "").toLowerCase()} key={run.run_id}><div className="execution-run-head"><div><strong>{run.run_id}</strong><span>{run.selected_test_case_ids?.length || 0} case(s) - {state}</span></div><span className={`execution-run-status ${state}`}>{state}</span></div><small>Baseline hash: {run.baseline_hash}</small>{run.error && <small className="execution-run-error">Error: {run.error}</small>}{!terminal && <div className="execution-run-actions"><button type="button" className="secondary-button compact-button" disabled={runActionId === `${run.run_id}:cancelled`} onClick={() => updateRunLifecycle(run, "cancelled")}>{runActionId === `${run.run_id}:cancelled` ? <LoadingSpinner variant="inline" label="Cancelling" /> : <><X size={14} /> Cancel baseline</>}</button><button type="button" className="secondary-button compact-button danger-compact-button" disabled={runActionId === `${run.run_id}:failed`} onClick={() => updateRunLifecycle(run, "failed")}>{runActionId === `${run.run_id}:failed` ? <LoadingSpinner variant="inline" label="Updating" /> : <><AlertCircle size={14} /> Mark failed</>}</button></div>}</article>; })}</div>}</section>}
  </div>;
}
