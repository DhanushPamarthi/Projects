import { useEffect, useMemo, useState } from "react";
import { AlertCircle, ArrowLeft, Check, CheckCircle2, FileText, LoaderCircle, Pencil, ShieldCheck, X } from "lucide-react";
import { generateTestCasesFromAnalysis, listGeneratedTestCases, reviewTestCaseGeneration } from "../api/client.js";

function listText(value) { return Array.isArray(value) && value.length ? value.join(", ") : "Not specified"; }

export function TestCaseGenerationPage({ analysis, onBack }) {
  const [generation, setGeneration] = useState(null);
  const [requestedCount, setRequestedCount] = useState(5);
  const [selected, setSelected] = useState([]);
  const [statuses, setStatuses] = useState({});
  const [editingId, setEditingId] = useState("");
  const [editingTitle, setEditingTitle] = useState("");
  const [editingExpected, setEditingExpected] = useState("");
  const [reviewer, setReviewer] = useState("Product Owner");
  const [comments, setComments] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const requirements = analysis?.contract?.requirements || [];
  const approvedIds = new Set(analysis?.contract?.approved_items || []);
  const approvedRequirements = requirements.filter((item) => approvedIds.has(item.requirement_id) || item.review_status === "approved");
  const testCases = generation?.contract?.test_cases || generation?.test_cases || [];
  const generationMode = generation?.contract?.generation_mode || generation?.generation_mode;
  const generationModel = generation?.contract?.generation_model || generation?.generation_model;
  const approvedCount = testCases.filter((item) => (statuses[item.test_case_id] || item.review_status || "pending") === "approved").length;
  const allApproved = testCases.length > 0 && approvedCount === testCases.length;
  const selectedAll = testCases.length > 0 && selected.length === testCases.length;

  useEffect(() => {
    if (!analysis?.analysis_id) return;
    listGeneratedTestCases(analysis.analysis_id).then((items) => { const next = items[0] || null; setGeneration(next); setStatuses(Object.fromEntries((next?.contract?.test_cases || []).map((item) => [item.test_case_id, item.review_status || "pending"]))); }).catch(() => {});
  }, [analysis?.analysis_id]);

  async function generate() {
    if (!analysis?.analysis_id) return;
    setLoading(true); setError("");
    try { const next = await generateTestCasesFromAnalysis(analysis.analysis_id, requestedCount); setGeneration(next); setStatuses(Object.fromEntries((next.test_cases || []).map((item) => [item.test_case_id, item.review_status || "pending"]))); setSelected([]); }
    catch (err) { setError(err.message); }
    finally { setLoading(false); }
  }

  function moveSelected(status) { setStatuses((current) => ({ ...current, ...Object.fromEntries(selected.map((id) => [id, status])) })); }
  function toggleSelected(id) { setSelected((items) => items.includes(id) ? items.filter((item) => item !== id) : [...items, id]); }
  function toggleAll(checked) { setSelected(checked ? testCases.map((item) => item.test_case_id) : []); }
  function startEdit(item) { setEditingId(item.test_case_id); setEditingTitle(item.title || ""); setEditingExpected((item.expected_results || []).join("\n")); }
  function saveEdit(id) { setGeneration((current) => { const next = structuredClone(current); const cases = next.contract?.test_cases || next.test_cases || []; const item = cases.find((entry) => entry.test_case_id === id); if (item) { item.title = editingTitle.trim(); item.expected_results = editingExpected.split("\n").map((value) => value.trim()).filter(Boolean); } return next; }); setEditingId(""); }
  async function saveReview() {
    if (!generation?.generation_id) return;
    setSaving(true); setError("");
    try { const next = await reviewTestCaseGeneration(generation.generation_id, { reviewer: reviewer.trim(), comments: comments.trim() || null, items: testCases.map((item) => ({ test_case_id: item.test_case_id, review_status: statuses[item.test_case_id] || item.review_status || "pending", title: item.title, steps: item.steps, expected_results: item.expected_results })) }); setGeneration(next); setStatuses(Object.fromEntries((next.contract?.test_cases || []).map((item) => [item.test_case_id, item.review_status || "pending"]))); }
    catch (err) { setError(err.message); }
    finally { setSaving(false); }
  }

  const caseItems = useMemo(() => testCases.map((item) => ({ ...item, reviewStatus: statuses[item.test_case_id] || item.review_status || "pending" })), [testCases, statuses]);

  return <div className="requirements-page test-generation-page">
    <div className="page-heading"><div><div className="eyebrow">TEST CASE GENERATION</div><h1>Generate and review test cases</h1><p>Generate cases from approved requirements, then review each case before continuing.</p></div><button type="button" className="secondary-button" onClick={onBack}><ArrowLeft size={16} /> Back to requirements</button></div>
    {error && <div className="error-message"><AlertCircle size={17} /> {error}</div>}
    <section className="generation-ready-card"><div className="generation-ready-icon"><CheckCircle2 size={24} /></div><div><div className="eyebrow">APPROVED SCOPE</div><h2>{approvedRequirements.length} approved requirement{approvedRequirements.length === 1 ? "" : "s"} ready</h2><p>Pending and rejected requirements are excluded. {generation ? `${testCases.length} test cases generated using ${generationMode === "openai" ? generationModel : "the local fallback"}.` : "Choose a count and generate test cases with the LLM."}</p></div><span className={`requirements-status ${allApproved ? "approved" : ""}`}>{allApproved ? "Approved" : generation ? "Needs review" : "Ready"}</span></section>
    <section className="requirements-panel generation-scope-panel"><div className="panel-title-row"><div><h2>Requirements included in generation</h2><p className="panel-helper">Generated cases retain requirement, evidence, and source-package provenance.</p></div><span>{approvedRequirements.length} included</span></div>{approvedRequirements.length ? <div className="generation-requirement-list">{approvedRequirements.map((item) => <article className="generation-requirement" key={item.requirement_id}><div className="generation-requirement-icon"><FileText size={17} /></div><div><h3>{item.requirement_id} · {item.title}</h3><p>{item.statement}</p><div className="generation-meta"><span>{item.type || "Functional"}</span><span>Priority: {item.priority || "Not specified"}</span><span>Actors: {listText(item.actors)}</span><span>Entities: {listText(item.entities)}</span></div></div></article>)}</div> : <div className="generation-empty"><ShieldCheck size={22} /><h3>No approved requirements</h3><p>Return to the requirements review and approve at least one requirement.</p></div>}</section>
    <section className="generation-next-card"><div><div className="eyebrow">LLM TEST DESIGN</div><h2>{generation ? "Generate another set" : "Choose the number of cases"}</h2><p>The backend validates the structured response and stores every generation run.</p></div><div className="generation-controls"><label>Number of test cases<input type="number" min="1" max="100" value={requestedCount} onChange={(event) => setRequestedCount(Math.min(100, Math.max(1, Number(event.target.value) || 1)))} /></label><button type="button" className="primary-button" disabled={!approvedRequirements.length || loading} onClick={generate}>{loading ? <><LoaderCircle className="spin" size={16} /> Generating...</> : <><ShieldCheck size={16} /> Generate test cases</>}</button></div></section>
    {generation && <section className="requirements-panel generated-cases-panel"><div className="panel-title-row"><div><h2>Review generated test cases</h2><p className="panel-helper">Approve or reject each case. Edit the title or expected result before saving.</p></div><span>{approvedCount}/{testCases.length} approved</span></div><div className="selection-actions"><label className="select-all"><input type="checkbox" checked={selectedAll} onChange={(event) => toggleAll(event.target.checked)} /> Select all</label><div className="review-move-actions"><button type="button" className="review-icon-action approved-action" disabled={!selected.length} title="Approve selected test cases" onClick={() => moveSelected("approved")}><Check size={16} /></button><button type="button" className="review-icon-action rejected-action" disabled={!selected.length} title="Reject selected test cases" onClick={() => moveSelected("rejected")}><X size={16} /></button><button type="button" className="text-button" onClick={() => toggleAll(false)}>Clear all</button></div></div><div className="test-case-review-list">{caseItems.map((item) => <article className={`test-case-review-item ${item.reviewStatus}`} key={item.test_case_id}><input type="checkbox" checked={selected.includes(item.test_case_id)} onChange={() => toggleSelected(item.test_case_id)} /><div className="test-case-review-content"><div className="test-case-review-heading"><strong>{item.test_case_id}</strong><span className={`test-case-status ${item.reviewStatus}`}>{item.reviewStatus}</span></div>{editingId === item.test_case_id ? <><input className="test-case-edit-title" value={editingTitle} onChange={(event) => setEditingTitle(event.target.value)} /><textarea className="test-case-edit-text" value={editingExpected} onChange={(event) => setEditingExpected(event.target.value)} /><div className="item-actions-static"><button type="button" className="secondary-button compact-button" onClick={() => saveEdit(item.test_case_id)}>Save</button><button type="button" className="text-button" onClick={() => setEditingId("")}>Cancel</button></div></> : <><h3>{item.title}</h3><p><b>Requirement:</b> {item.requirement_id} · <b>Type:</b> {item.type} · <b>Priority:</b> {item.priority}</p><p><b>Steps:</b> {(item.steps || []).join("; ")}</p><p><b>Expected:</b> {(item.expected_results || []).join("; ")}</p><div className="test-case-item-actions"><button type="button" className="item-icon-button item-edit" title="Edit test case" aria-label={`Edit ${item.test_case_id}`} onClick={() => startEdit(item)}><Pencil size={14} /></button></div></>}</div></article>)}</div></section>}
    {generation && <section className="requirements-review-card"><div><div className="eyebrow">HUMAN REVIEW</div><h2>{allApproved ? "Test cases approved" : "Save test-case review"}</h2><p>{allApproved ? "All generated test cases are approved and ready for the next data-generation stage." : "Save the review decision after approving or rejecting the generated cases."}</p></div><div className="review-fields"><label>Reviewer<input value={reviewer} onChange={(event) => setReviewer(event.target.value)} /></label><label>Review comments<textarea value={comments} onChange={(event) => setComments(event.target.value)} placeholder="Add review notes" /></label><button className="primary-button" onClick={saveReview} disabled={saving || !testCases.length}>{saving ? <><LoaderCircle className="spin" size={16} /> Saving review...</> : <><ShieldCheck size={16} /> Save test-case review</>}</button></div></section>}
  </div>;
}
