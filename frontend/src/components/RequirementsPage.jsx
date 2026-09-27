import { useEffect, useMemo, useState } from "react";
import { AlertCircle, Check, CheckCircle2, FileSearch, LoaderCircle, Pencil, Play, RefreshCw, RotateCcw, ShieldCheck, X } from "lucide-react";
import { analyzeRequirements, getProject, listManifests, listProjects, listRequirementAnalyses, reviewRequirementAnalysis } from "../api/client.js";

function statusText(status) {
  return String(status || "not_started").replaceAll("_", " ");
}

function textValue(value) {
  if (typeof value === "string") return value;
  if (value && typeof value === "object") return value.text || value.statement || value.description || "";
  return String(value ?? "");
}

function listText(value) {
  return Array.isArray(value) && value.length ? value.join(", ") : "Not specified";
}

export function RequirementsPage({ onContinueToGeneration }) {
  const [project, setProject] = useState(() => JSON.parse(localStorage.getItem("testiq_current_project") || "null"));
  const [projects, setProjects] = useState([]);
  const [packages, setPackages] = useState([]);
  const [packageId, setPackageId] = useState(() => JSON.parse(localStorage.getItem("testiq_current_package") || "null")?.package_id || "");
  const [analysis, setAnalysis] = useState(null);
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const [reviewing, setReviewing] = useState(false);
  const [reviewer, setReviewer] = useState("Product Owner");
  const [comments, setComments] = useState("");
  const [assumptions, setAssumptions] = useState("");
  const [selectedItems, setSelectedItems] = useState([]);
  const [expandedItems, setExpandedItems] = useState([]);
  const [editingItemId, setEditingItemId] = useState("");
  const [editingValue, setEditingValue] = useState("");
  const [reviewStatuses, setReviewStatuses] = useState({});
  const [reviewSaved, setReviewSaved] = useState(false);
  const [error, setError] = useState("");

  const selectedPackage = useMemo(() => packages.find((item) => item.package_id === packageId), [packages, packageId]);
  const contract = analysis?.contract;
  const requirements = contract?.requirements || [];
  const criteriaItems = requirements.flatMap((item) => (item.acceptance_criteria || []).map((criterion, index) => {
    const given = typeof criterion === "string" ? "The source-defined precondition is satisfied." : (Array.isArray(criterion.given) ? criterion.given.join("; ") : criterion.given) || "The source-defined precondition is satisfied.";
    const when = typeof criterion === "string" ? "The requirement flow runs." : criterion.when || "The requirement flow runs.";
    const then = typeof criterion === "string" ? criterion : criterion.then || criterion.text || "The expected behavior occurs.";
    const text = `Given: ${given}\nWhen: ${when}\nThen: ${then}`;
    const acceptanceId = typeof criterion === "string" ? `AC-${index + 1}` : criterion.acceptance_id || `AC-${index + 1}`;
    return { id: `${item.requirement_id}:${acceptanceId}`, title: acceptanceId, text, given, when, then };
  }));
  const outcomeItems = requirements.flatMap((item) => (item.expected_outcomes || []).map((text, index) => ({ id: `${item.requirement_id}:OUTCOME-${index + 1}`, title: `${item.requirement_id} · Outcome ${index + 1}`, text })));
  const ruleItems = requirements.flatMap((item) => (item.business_rules || []).map((rule, index) => { const ruleId = typeof rule === "string" ? `RULE-${index + 1}` : rule.rule_id || `RULE-${index + 1}`; return { id: `${item.requirement_id}:${ruleId}`, title: ruleId, text: typeof rule === "string" ? rule : rule.text }; }));
  outcomeItems.forEach((item) => { item.text = textValue(item.text); });
  ruleItems.forEach((item) => { item.text = textValue(item.text); });
  const ruleIds = requirements.flatMap((item) => (item.business_rules || []).map((rule, index) => `${item.requirement_id}:${rule.rule_id || `RULE-${index + 1}`}`));
  ruleItems.forEach((item, index) => { item.id = ruleIds[index] || item.id; });
  const findings = [
    ...(contract?.global_gaps || []).map((text) => ({ text, kind: "Gap" })),
    ...requirements.flatMap((item) => (item.ambiguities || []).map((text) => ({ text, kind: `${item.requirement_id} ambiguity` }))),
    ...(contract?.conflicts || []).map((item) => ({ text: item.description, kind: "Conflict" })),
  ];
  const selectableItems = [
    ...requirements.map((item) => ({ id: item.requirement_id, label: item.title })),
    ...ruleItems.map((item) => ({ id: item.id, label: item.text })),
    ...criteriaItems.map((item) => ({ id: item.id, label: item.text })),
    ...outcomeItems.map((item) => ({ id: item.id, label: item.text })),
  ];

  async function loadPackages(projectId = project?.project_id) {
    if (!projectId) return;
    setLoading(true); setError("");
    try {
      const items = await listManifests(projectId);
      setPackages(items);
      const saved = JSON.parse(localStorage.getItem("testiq_current_package") || "null");
      const next = items.find((item) => item.package_id === packageId) || items.find((item) => item.package_id === saved?.package_id) || items[0];
      setPackageId(next?.package_id || "");
      if (next) localStorage.setItem("testiq_current_package", JSON.stringify(next));
    } catch (err) { setError(err.message); } finally { setLoading(false); }
  }

  async function loadAnalysis(id = packageId) {
    if (!id) return;
    try {
      const items = await listRequirementAnalyses(id);
      const next = items[0] || null;
      setAnalysis(next);
      if (next) { setSelectedItems(next.contract?.approved_items?.length ? next.contract.approved_items : []); setReviewStatuses(getReviewStatuses(next.contract)); setReviewSaved(Boolean(next.contract?.approved_items?.length)); }
    } catch (err) { setError(err.message); }
  }

  async function selectProject(id) {
    if (!id) { setProject(null); setPackages([]); setPackageId(""); setAnalysis(null); return; }
    const selected = await getProject(id);
    setProject(selected); setAnalysis(null);
    localStorage.setItem("testiq_current_project", JSON.stringify(selected));
    await loadPackages(selected.project_id);
  }

  function selectPackage(id) {
    setPackageId(id); setAnalysis(null); setError("");
    const selected = packages.find((item) => item.package_id === id);
    if (selected) localStorage.setItem("testiq_current_package", JSON.stringify(selected));
    loadAnalysis(id);
  }

  async function runAnalysis() {
    if (!packageId) return;
    setRunning(true); setError("");
    try {
      const next = await analyzeRequirements(packageId);
      setAnalysis(next);
      setSelectedItems([]);
      setReviewStatuses(getReviewStatuses(next.contract));
      setReviewSaved(false);
    } catch (err) { setError(err.message); } finally { setRunning(false); }
  }

  async function approve() {
    if (!analysis?.analysis_id || !reviewer.trim()) return;
    setReviewing(true); setError("");
    try {
      const items = (analysis.contract?.requirements || []).flatMap((item) => [
        { item_id: item.requirement_id, approved: selectedItems.includes(item.requirement_id), review_status: reviewStatuses[item.requirement_id] || "pending", title: item.title, statement: item.statement },
        ...(item.business_rules || []).map((rule, index) => { const itemId = `${item.requirement_id}:${rule.rule_id || `RULE-${index + 1}`}`; return { item_id: itemId, approved: selectedItems.includes(itemId), review_status: reviewStatuses[item.requirement_id] || "pending", text: rule.text }; }),
        ...(item.acceptance_criteria || []).map((criterion, index) => { const itemId = `${item.requirement_id}:${criterion.acceptance_id || `AC-${index + 1}`}`; return { item_id: itemId, approved: selectedItems.includes(itemId), review_status: reviewStatuses[item.requirement_id] || "pending", text: criterion.text, scenario: criterion.scenario, given: criterion.given, when: criterion.when, then: criterion.then }; }),
      ]);
      const next = await reviewRequirementAnalysis(analysis.analysis_id, {
        reviewer: reviewer.trim(), decision: Object.values(reviewStatuses).some((status) => status === "rejected") ? "needs_changes" : "approved", comments: comments.trim() || null,
        accepted_assumptions: assumptions.split("\n").map((item) => item.trim()).filter(Boolean),
        approved_items: selectedItems,
        items,
      });
      setAnalysis(next);
      setReviewStatuses(getReviewStatuses(next.contract));
      setSelectedItems(next.contract?.approved_items || selectedItems);
      setReviewSaved(Boolean(next.contract?.approved_items?.length));
    } catch (err) { setError(err.message); } finally { setReviewing(false); }
  }

  function toggleItem(id) {
    setSelectedItems((items) => items.includes(id) ? items.filter((item) => item !== id) : [...items, id]);
  }

  function toggleRequirement(item) {
    const childIds = [...criteriaItems, ...ruleItems, ...outcomeItems]
      .filter((entry) => entry.id.startsWith(`${item.requirement_id}:`))
      .map((entry) => entry.id);
    const allIds = [item.requirement_id, ...childIds];
    setSelectedItems((items) => {
      const shouldClear = items.includes(item.requirement_id);
      return shouldClear ? items.filter((entry) => !allIds.includes(entry)) : [...new Set([...items, ...allIds])];
    });
  }

  function hasSelectedReviewItem(item) {
    return [...criteriaItems, ...ruleItems].some((entry) => entry.id.startsWith(`${item.requirement_id}:`) && selectedItems.includes(entry.id));
  }

  function toggleAll(checked) {
    setSelectedItems(checked ? selectableItems.map((item) => item.id) : []);
  }

  function toggleExpanded(id) {
    setExpandedItems((items) => items.includes(id) ? items.filter((item) => item !== id) : [...items, id]);
  }

  function getReviewStatuses(nextContract) {
    return Object.fromEntries((nextContract?.requirements || []).map((item) => [item.requirement_id, item.review_status || ((nextContract.rejected_items || []).includes(item.requirement_id) ? "rejected" : (nextContract.approved_items || []).includes(item.requirement_id) ? "approved" : "pending")]));
  }

  function moveSelectedRequirements(status) {
    const eligibleIds = requirements.filter((item) => selectedItems.includes(item.requirement_id) && hasSelectedReviewItem(item)).map((item) => item.requirement_id);
    setReviewStatuses((current) => ({ ...current, ...Object.fromEntries(eligibleIds.map((id) => [id, status])) }));
  }

  function setRequirementReviewStatus(id, status) {
    setReviewStatuses((current) => ({ ...current, [id]: status }));
  }

  function startEdit(id, text) {
    setEditingItemId(id);
    setEditingValue(String(text || ""));
  }

  function saveEdit(id) {
    setAnalysis((current) => {
      if (!current) return current;
      const next = structuredClone(current);
      for (const requirementItem of next.contract.requirements || []) {
        if (requirementItem.requirement_id === id) requirementItem.statement = editingValue.trim();
        for (const rule of requirementItem.business_rules || []) if (rule.rule_id === id) rule.text = editingValue.trim();
        for (const criterion of requirementItem.acceptance_criteria || []) if (criterion.acceptance_id === id) criterion.text = editingValue.trim();
      }
      return next;
    });
    setEditingItemId("");
    setEditingValue("");
  }

  function selectionRow(id, title, text) {
    const longText = String(text || "").length > 80;
    const expanded = expandedItems.includes(id);
    return <div className={`selection-item ${selectedItems.includes(id) ? "selected" : ""}`} key={id}>
      <input type="checkbox" checked={selectedItems.includes(id)} onChange={() => toggleItem(id)} />
      <span className="selection-item-content"><b>{title}</b>{editingItemId === id ? <textarea className="inline-edit" value={editingValue} onChange={(event) => setEditingValue(event.target.value)} /> : <small className={expanded ? "expanded" : ""}>{text}</small>}{editingItemId === id ? <span className="item-actions"><button type="button" className="secondary-button compact-button" onClick={() => saveEdit(id)}>Save</button><button type="button" className="text-button" onClick={() => setEditingItemId("")}>Cancel</button></span> : <>{longText && <button type="button" className="read-more-button" onClick={(event) => { event.preventDefault(); event.stopPropagation(); toggleExpanded(id); }}>{expanded ? "Show less" : "Read more"}</button>}<span className="item-actions"><button type="button" className="item-icon-button item-edit" title="Edit item" aria-label={`Edit ${title}`} onClick={(event) => { event.preventDefault(); event.stopPropagation(); startEdit(id, text); }}><Pencil size={14} /></button></span></>}</span>
    </div>;
  }

  function requirementGroup(item, bucketStatus = "pending") {
    const criteria = criteriaItems.filter((entry) => entry.id.startsWith(`${item.requirement_id}:`));
    const rules = ruleItems.filter((entry) => entry.id.startsWith(`${item.requirement_id}:`));
    const canReview = hasSelectedReviewItem(item);
    return <details className="requirement-group" open={expandedItems.includes(`requirement:${item.requirement_id}`)} onToggle={(event) => { const isOpen = event.currentTarget.open; setExpandedItems((items) => isOpen ? [...new Set([...items, `requirement:${item.requirement_id}`])] : items.filter((entry) => entry !== `requirement:${item.requirement_id}`)); }} key={item.requirement_id}>
      <summary><div className="requirement-group-summary"><input type="checkbox" checked={selectedItems.includes(item.requirement_id)} onClick={(event) => event.stopPropagation()} onChange={() => toggleRequirement(item)} /><span><div className="requirement-title-row"><b>{item.requirement_id} · {item.title}</b><span className="priority-inline">{item.priority || "Not specified"}</span></div><small>{item.statement}</small><div className="requirement-meta"><div className="meta-badges"><span className="meta-badge">{item.type || "Functional"}</span></div><div className="meta-fact"><strong>Actors</strong><span>{listText(item.actors)}</span></div><div className="meta-fact"><strong>Entities</strong><span>{listText(item.entities)}</span></div></div>{item.ambiguities?.length > 0 && <small className="requirement-warning">Ambiguity: {item.ambiguities.join(" ")}</small>}{item.assumptions?.length > 0 && <small className="requirement-assumption">Assumption: {item.assumptions.join(" ")}</small>}</span><span className="requirement-review-actions">{bucketStatus !== "approved" && <button type="button" className="item-icon-button item-approve" disabled={!canReview} title={canReview ? "Approve requirement" : "Select an acceptance criterion or business rule first"} aria-label={`Approve ${item.title}`} onClick={(event) => { event.preventDefault(); event.stopPropagation(); setRequirementReviewStatus(item.requirement_id, "approved"); }}><Check size={14} /></button>}{bucketStatus !== "rejected" && <button type="button" className="item-icon-button item-reject" disabled={!canReview} title={canReview ? "Reject requirement" : "Select an acceptance criterion or business rule first"} aria-label={`Reject ${item.title}`} onClick={(event) => { event.preventDefault(); event.stopPropagation(); setRequirementReviewStatus(item.requirement_id, "rejected"); }}><X size={14} /></button>}{bucketStatus !== "pending" && <button type="button" className="item-icon-button item-reset" title="Return requirement to pending review" aria-label={`Return ${item.title} to pending review`} onClick={(event) => { event.preventDefault(); event.stopPropagation(); setRequirementReviewStatus(item.requirement_id, "pending"); }}><RotateCcw size={14} /></button>}</span></div></summary>
      <div className="requirement-group-body"><div className="requirement-group-grid"><div><h4>Acceptance criteria</h4>{criteria.length ? criteria.map((entry) => selectionRow(entry.id, entry.title, entry.text)) : <p className="muted">No acceptance criteria extracted.</p>}</div><div className="requirement-rules"><h4>Business rules</h4>{rules.length ? rules.map((entry) => selectionRow(entry.id, entry.title, entry.text)) : <p className="muted">No business rules extracted.</p>}</div></div></div>
    </details>;
  }

  function requirementBucket(label, status, items) {
    return <section className={`review-bucket ${status}`}><div className="review-bucket-header"><h3>{label}</h3><span>{items.length}</span></div>{items.length ? items.map((item) => requirementGroup(item, status)) : <p className="muted">No requirements in this section.</p>}</section>;
  }

  useEffect(() => { listProjects().then((items) => { setProjects(items); if (!project?.project_id && items[0]) selectProject(items[0].project_id); }).catch((err) => setError(err.message)); }, []);
  useEffect(() => { if (project?.project_id) loadPackages(project.project_id); }, [project?.project_id]);
  useEffect(() => { if (packageId) loadAnalysis(packageId); }, [packageId]);

  return <div className="requirements-page">
    <div className="page-heading"><div><div className="eyebrow">REQUIREMENTS ANALYSIS</div><h1>Review your requirements</h1><p>Turn the fixed source package into a reviewed contract for test-case generation.</p></div><button className="secondary-button" onClick={() => loadPackages()} disabled={loading}><RefreshCw size={16} /> Refresh</button></div>
    <div className="requirements-toolbar"><div><label>Project</label><select value={project?.project_id || ""} onChange={(event) => selectProject(event.target.value)}><option value="">Select project</option>{projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.project_name}</option>)}</select></div><div><label>Source package</label><select value={packageId} onChange={(event) => selectPackage(event.target.value)} disabled={!packages.length}><option value="">Select package</option>{packages.map((item) => <option key={item.package_id} value={item.package_id}>v{item.package_version} - {item.scope || "MVP"} ({item.source_count} sources)</option>)}</select></div><button className="primary-button requirements-run-button" onClick={runAnalysis} disabled={!packageId || running}>{running ? <><LoaderCircle className="spin" size={16} /> Analyzing...</> : <><Play size={16} /> {analysis ? "Run new analysis" : "Analyze package"}</>}</button></div>
    {error && <div className="error-message"><AlertCircle size={17} /> {error}</div>}
    {!selectedPackage && <section className="requirements-empty"><div className="empty-state-icon"><FileSearch size={25} /></div><h2>Select an existing source package</h2><p>Your uploaded BRD and created package will appear here.</p></section>}
    {selectedPackage && !analysis && <section className="requirements-empty"><div className="empty-state-icon"><FileSearch size={25} /></div><h2>Ready to analyze package v{selectedPackage.package_version}</h2><p>Run Requirements Analysis directly from the source package. Knowledge Fabric is not required.</p></section>}
    {analysis && <>
      <section className="requirements-status-card"><div className="requirements-status-icon"><CheckCircle2 size={22} /></div><div><div className="eyebrow">ANALYSIS VERSION {analysis.analysis_version}</div><h4>Requirement contract</h4><p>{analysis.contract?.generation_mode === "source_package_fallback" ? "Generated directly from the frozen source package." : "Generated with grounded context."}</p></div><span className={`requirements-status ${analysis.status}`}>{statusText(analysis.status)}</span></section>
      <div className="requirements-summary-grid"><div><span>Requirements</span><strong>{requirements.length}</strong></div><div><span>Business rules</span><strong>{ruleItems.length}</strong></div><div><span>Acceptance criteria</span><strong>{criteriaItems.length}</strong></div><div><span>Selected for approval</span><strong>{selectedItems.length}</strong></div></div>
      {contract?.global_gaps?.length > 0 && <section className="requirements-panel warning-panel"><div className="panel-title-row"><h2><AlertCircle size={18} /> Package-level gaps</h2><span>{contract.global_gaps.length} found</span></div><ul>{contract.global_gaps.map((gap) => <li key={gap}>{gap}</li>)}</ul></section>}
      {contract?.global_assumptions?.length > 0 && <section className="requirements-panel assumptions-panel"><div className="panel-title-row"><h2>Package-level assumptions</h2><span>{contract.global_assumptions.length}</span></div><ul>{contract.global_assumptions.map((assumption) => <li key={assumption}>{assumption}</li>)}</ul></section>}
      <section className="requirements-panel nested-requirements-panel"><div className="panel-title-row"><div><h2>Requirements and related evidence</h2><p className="panel-helper">Select one or more requirements, then use the approve or reject icons. Edit remains available on each item.</p></div><span>{selectedItems.filter((id) => requirements.some((item) => item.requirement_id === id)).length} selected</span></div><div className="selection-actions"><label className="select-all"><input type="checkbox" checked={selectableItems.length > 0 && selectedItems.length === selectableItems.length} onChange={(event) => toggleAll(event.target.checked)} /> Select all</label><div className="review-move-actions"><button type="button" className="review-icon-action approved-action" disabled={!requirements.some((item) => selectedItems.includes(item.requirement_id) && hasSelectedReviewItem(item))} title="Approve selected requirements" aria-label="Approve selected requirements" onClick={() => moveSelectedRequirements("approved")}><Check size={16} /></button><button type="button" className="review-icon-action rejected-action" disabled={!requirements.some((item) => selectedItems.includes(item.requirement_id) && hasSelectedReviewItem(item))} title="Reject selected requirements" aria-label="Reject selected requirements" onClick={() => moveSelectedRequirements("rejected")}><X size={16} /></button><button className="text-button" onClick={() => toggleAll(false)}>Clear all</button></div></div><div className="review-buckets"><div className="pending-bucket">{requirementBucket("Pending review", "pending", requirements.filter((item) => (reviewStatuses[item.requirement_id] || "pending") === "pending"))}</div><div className="review-bucket-columns">{requirementBucket("Approved requirements", "approved", requirements.filter((item) => reviewStatuses[item.requirement_id] === "approved"))}{requirementBucket("Rejected requirements", "rejected", requirements.filter((item) => reviewStatuses[item.requirement_id] === "rejected"))}</div></div></section>
      <section className="requirements-panel requirement-scope-panel"><div className="panel-title-row"><div><h2>Requirements</h2></div></div>{requirements.map((item) => selectionRow(item.requirement_id, item.title, item.statement))}</section>
      <section className="requirements-panel business-rules-panel"><div className="panel-title-row"><h2>Business rules</h2></div>{ruleItems.map((item) => selectionRow(item.id, item.title, item.text))}</section>
      {findings.length > 0 && <section className="requirements-panel"><div className="panel-title-row"><h2><AlertCircle size={18} /> Review notes</h2><span>{findings.length} found</span></div><ul>{findings.map((item, index) => <li key={`${item.text}-${index}`}><strong>{item.kind}: </strong>{item.text}{item.source_reference && <small>Source: {item.source_reference}</small>}</li>)}</ul></section>}
      {!reviewSaved ? <section className="requirements-review-card"><div><div className="eyebrow">HUMAN REVIEW</div><h2>Finalize requirement review</h2><p>Save the review decision for the selected requirements. Only approved requirements will continue to test-case generation.</p></div><div className="review-fields"><label>Reviewer<input value={reviewer} onChange={(event) => setReviewer(event.target.value)} /></label><label>Accepted assumptions <span>(one per line)</span><textarea value={assumptions} onChange={(event) => setAssumptions(event.target.value)} placeholder="Example: SMS provider response is treated as the observable delivery outcome" /></label><label>Review comments<textarea value={comments} onChange={(event) => setComments(event.target.value)} placeholder="Add review notes" /></label><button className="primary-button" onClick={approve} disabled={reviewing || selectedItems.length === 0}>{reviewing ? <><LoaderCircle className="spin" size={16} /> Saving review...</> : <><ShieldCheck size={16} /> Save review</>}</button></div></section> : <section className="review-complete-card"><div className="review-complete-icon"><CheckCircle2 size={22} /></div><div><div className="eyebrow">REVIEW SAVED</div><h2>Approved requirements are ready</h2><p>{requirements.filter((item) => reviewStatuses[item.requirement_id] === "approved").length} requirement{requirements.filter((item) => reviewStatuses[item.requirement_id] === "approved").length === 1 ? "" : "s"} will be used for test-case generation. Pending and rejected requirements are excluded.</p></div><button className="primary-button" onClick={() => onContinueToGeneration?.(analysis)} disabled={!onContinueToGeneration}><Play size={16} /> Continue to Test Case Generation</button></section>}
    </>}
  </div>;
}
