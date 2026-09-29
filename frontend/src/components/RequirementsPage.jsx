import { useEffect, useMemo, useState } from "react";
import { AlertCircle, Check, CheckCircle2, FileSearch, LoaderCircle, Pencil, Play, RefreshCw, RotateCcw, ShieldCheck, X } from "lucide-react";
import { generateFunctionalRequirements, getProject, listManifests, listProjects, listRequirementAnalyses, reviewRequirementAnalysis } from "../api/client.js";

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

function packageDisplayName(item) {
  return item?.package_name?.trim() || item?.scope?.trim() || "Source package";
}

function sourceCountText(count) {
  return `${count || 0} source${Number(count) === 1 ? "" : "s"}`;
}

function packageOptionLabel(item) {
  const version = item?.package_version ? `v${item.package_version}` : "Unversioned";
  const scope = item?.scope || "MVP";
  return `${packageDisplayName(item)} - ${version} - ${scope} (${sourceCountText(item.source_count)})`;
}

function sourceReferences(value) {
  return Array.isArray(value) ? value.filter((item) => item?.source_reference || item?.fragment_id) : [];
}

function splitLines(value) {
  return String(value || "").split("\n").map((item) => item.trim()).filter(Boolean);
}

const HIDDEN_ANALYSIS_NOTICE_TEXTS = new Set([
  "The source contains unresolved ambiguities.",
  "Requirements analysis detected source statements that need reviewer clarification.",
]);

function reviewWarningText(text) {
  return text;
}

function isAnalysisWarningText(text) {
  const normalized = reviewWarningText(text);
  return [
    ...HIDDEN_ANALYSIS_NOTICE_TEXTS,
    "LLM Requirements Analysis was unavailable, so this contract was generated with the local fallback.",
  ].includes(normalized);
}

function conflictDescriptionText(conflict) {
  const terms = conflict?.terms;
  if (Array.isArray(terms) && terms.length >= 2) {
    return `The analyzed content mentions both '${terms[0]}' and '${terms[1]}'. Confirm the intended business rule.`;
  }
  return String(conflict?.description || "").replace("appear in the package; confirm precedence.", "appear in the analyzed content. Confirm the intended business rule.");
}

const GENERIC_FALLBACK_CONFLICT_PAIRS = [["eligible", "ineligible"], ["include", "exclude"], ["retry", "no retry"], ["sms", "email"]];

function isGenericFallbackConflict(conflict) {
  const hasRequirementLinks = Array.isArray(conflict?.requirement_ids) && conflict.requirement_ids.length > 0;
  if (hasRequirementLinks) return false;
  const terms = Array.isArray(conflict?.terms) ? conflict.terms.map((term) => String(term).toLowerCase()) : [];
  const description = String(conflict?.description || "").toLowerCase();
  if (!description.includes("confirm the intended business rule") && !description.includes("confirm precedence")) {
    return false;
  }
  return GENERIC_FALLBACK_CONFLICT_PAIRS.some(([left, right]) => {
    const hasTerms = terms.includes(left) && terms.includes(right);
    const hasDescriptionPair = description.includes(`'${left}'`) && description.includes(`'${right}'`);
    return hasTerms || hasDescriptionPair;
  });
}

export function RequirementsPage({ onContinueToGeneration }) {
  const [project, setProject] = useState(() => JSON.parse(localStorage.getItem("testiq_current_project") || "null"));
  const [projects, setProjects] = useState([]);
  const [packages, setPackages] = useState([]);
  const [packageId, setPackageId] = useState(() => JSON.parse(localStorage.getItem("testiq_current_package") || "null")?.package_id || "");
  const [analysis, setAnalysis] = useState(null);
  const [approvedBusinessRequirements, setApprovedBusinessRequirements] = useState([]);
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
  const readiness = contract?.readiness || {};
  const readinessChecks = readiness.checks || [];
  const readinessGapItems = (readiness.gaps || []).filter((gap) => !isAnalysisWarningText(gap));
  const passedReadinessCount = readinessChecks.filter((check) => check.status === "passed").length;
  const analysisWarningItems = Array.from(new Set((contract?.global_gaps || []).map(reviewWarningText).filter((item) => !HIDDEN_ANALYSIS_NOTICE_TEXTS.has(item))));
  const conflictItems = (contract?.conflicting_requirements || contract?.conflicts || []).filter((conflict) => !isGenericFallbackConflict(conflict));

  const criteriaItems = requirements.flatMap((item) => (item.acceptance_criteria || []).map((criterion, index) => {
    const given = typeof criterion === "string" ? "The source-defined precondition is satisfied." : (Array.isArray(criterion.given) ? criterion.given.join("; ") : criterion.given) || "The source-defined precondition is satisfied.";
    const when = typeof criterion === "string" ? "The requirement flow runs." : criterion.when || "The requirement flow runs.";
    const then = typeof criterion === "string" ? criterion : criterion.then || criterion.text || "The expected behavior occurs.";
    const text = `Given: ${given}\nWhen: ${when}\nThen: ${then}`;
    const acceptanceId = typeof criterion === "string" ? `AC-${index + 1}` : criterion.acceptance_id || `AC-${index + 1}`;
    return { id: `${item.requirement_id}:${acceptanceId}`, title: acceptanceId, text, source_references: sourceReferences(criterion?.source_references) };
  }));


  const findings = requirements.flatMap((item) => (item.ambiguities || []).map((text) => ({ text, kind: `${item.requirement_id} clarification` })));

  const selectableItems = [
    ...requirements.map((item) => ({ id: item.requirement_id, label: item.title })),
    ...criteriaItems.map((item) => ({ id: item.id, label: item.text })),
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
    if (!id) { setAnalysis(null); setApprovedBusinessRequirements([]); setLoading(false); return; }
    setLoading(true);
    try {
      const items = await listRequirementAnalyses(id);
      const generatedAnalysis = items.find((item) => item.contract?.functional_requirements_generation) || null;
      const sourceAnalysis = items.find((item) => !item.contract?.functional_requirements_generation) || null;
      const approvedFromSource = sourceAnalysis?.contract?.approved_business_requirements;
      const approvedFromHistory = approvedFromSource?.length ? approvedFromSource : sourceAnalysis ? (() => {
        const contract = sourceAnalysis?.contract || {};
        const approvedIds = new Set(contract.approved_items || []);
        return (contract.requirements || []).flatMap((requirement) => (requirement.business_rules || []).flatMap((rule, index) => {
          const ruleId = rule?.rule_id || `RULE-${index + 1}`;
          const itemId = `${requirement.requirement_id}:${ruleId}`;
          const status = rule?.review_status || (approvedIds.has(itemId) ? "approved" : "pending");
          return status === "approved" ? [{ business_requirement_id: ruleId, text: typeof rule === "string" ? rule : rule?.text || "", source_references: rule?.source_references || [] }] : [];
        }));
      })() : generatedAnalysis?.contract?.approved_business_requirements || [];
      setApprovedBusinessRequirements(approvedFromHistory);
      const signature = (items) => (items || []).map((item) => `${item.business_requirement_id}|${item.text}`).sort();
      const generatedIds = signature(generatedAnalysis?.contract?.approved_business_requirements);
      const approvedIds = signature(approvedFromHistory);
      const matchesApprovedScope = generatedIds.length > 0 && generatedIds.length === approvedIds.length && generatedIds.every((id, index) => id === approvedIds[index]);
      const next = generatedAnalysis && matchesApprovedScope ? generatedAnalysis : null;
      setAnalysis(next);
      if (next) {
        setSelectedItems(next.contract?.approved_items?.length ? next.contract.approved_items : []);
        setReviewStatuses(getReviewStatuses(next.contract));
        setReviewSaved(Boolean(next.contract?.approved_items?.length));
        setAssumptions((next.contract?.accepted_assumptions || []).join("\n"));
      }
    } catch (err) { setError(err.message); } finally { setLoading(false); }
  }

  async function selectProject(id) {
    setApprovedBusinessRequirements([]); setAnalysis(null);
    if (!id) { setProject(null); setPackages([]); setPackageId(""); return; }
    const selected = await getProject(id);
    setProject(selected); setAnalysis(null);
    localStorage.setItem("testiq_current_project", JSON.stringify(selected));
    await loadPackages(selected.project_id);
  }

  function selectPackage(id) {
    setPackageId(id); setAnalysis(null); setError("");
    setApprovedBusinessRequirements([]); setLoading(Boolean(id));
    const selected = packages.find((item) => item.package_id === id);
    if (selected) localStorage.setItem("testiq_current_package", JSON.stringify(selected));
    else localStorage.removeItem("testiq_current_package");
  }

  async function runAnalysis() {
    if (!packageId) return;
    setRunning(true); setError("");
    try {
      const next = await generateFunctionalRequirements(packageId);
      setAnalysis(next);
      setSelectedItems([]);
      setReviewStatuses(getReviewStatuses(next.contract));
      setReviewSaved(false);
      setAssumptions("");
    } catch (err) { setError(err.message); } finally { setRunning(false); }
  }

  async function approve() {
    if (!analysis?.analysis_id || !reviewer.trim()) return;
    setReviewing(true); setError("");
    try {
      const items = (analysis.contract?.requirements || []).flatMap((item) => [
        { item_id: item.requirement_id, approved: selectedItems.includes(item.requirement_id), review_status: reviewStatuses[item.requirement_id] || "pending", title: item.title, statement: item.statement },
        ...(item.business_rules || []).map((rule, index) => { const itemId = `${item.requirement_id}:${rule.rule_id || `RULE-${index + 1}`}`; const parentStatus = reviewStatuses[item.requirement_id] || "pending"; return { item_id: itemId, approved: selectedItems.includes(item.requirement_id) && parentStatus === "approved", review_status: parentStatus, text: textValue(rule) }; }),
        ...(item.acceptance_criteria || []).map((criterion, index) => { const itemId = `${item.requirement_id}:${criterion.acceptance_id || `AC-${index + 1}`}`; return { item_id: itemId, approved: selectedItems.includes(itemId), review_status: reviewStatuses[item.requirement_id] || "pending", text: criterion.text, scenario: criterion.scenario, given: criterion.given, when: criterion.when, then: criterion.then }; }),
      ]);
      const next = await reviewRequirementAnalysis(analysis.analysis_id, {
        reviewer: reviewer.trim(),
        decision: Object.values(reviewStatuses).some((status) => status === "rejected") ? "needs_changes" : "approved",
        comments: comments.trim() || null,
        accepted_assumptions: splitLines(assumptions),
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
    const childIds = criteriaItems
      .filter((entry) => entry.id.startsWith(`${item.requirement_id}:`))
      .map((entry) => entry.id);
    const allIds = [item.requirement_id, ...childIds];
    setSelectedItems((items) => {
      const shouldClear = items.includes(item.requirement_id);
      return shouldClear ? items.filter((entry) => !allIds.includes(entry)) : [...new Set([...items, ...allIds])];
    });
  }

  function hasSelectedReviewItem(item) {
    return criteriaItems.some((entry) => entry.id.startsWith(`${item.requirement_id}:`) && selectedItems.includes(entry.id));
  }

  function toggleAll(checked) {
    setSelectedItems(checked ? selectableItems.map((item) => item.id) : []);
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
        for (const rule of requirementItem.business_rules || []) if (`${requirementItem.requirement_id}:${rule.rule_id}` === id || rule.rule_id === id) rule.text = editingValue.trim();
        for (const criterion of requirementItem.acceptance_criteria || []) if (`${requirementItem.requirement_id}:${criterion.acceptance_id}` === id || criterion.acceptance_id === id) criterion.text = editingValue.trim();
      }
      return next;
    });
    setEditingItemId("");
    setEditingValue("");
  }

  function selectionRow(id, title, text) {
    return <div className={`selection-item ${selectedItems.includes(id) ? "selected" : ""}`} key={id}>
      <input type="checkbox" checked={selectedItems.includes(id)} onChange={() => toggleItem(id)} />
      <span className="selection-item-content"><b>{title}</b>{editingItemId === id ? <textarea className="inline-edit" value={editingValue} onChange={(event) => setEditingValue(event.target.value)} /> : <small className="expanded">{text}</small>}{editingItemId === id ? <span className="item-actions"><button type="button" className="secondary-button compact-button" onClick={() => saveEdit(id)}>Save</button><button type="button" className="text-button" onClick={() => setEditingItemId("")}>Cancel</button></span> : <span className="item-actions"><button type="button" className="item-icon-button item-edit" title="Edit item" aria-label={`Edit ${title}`} onClick={(event) => { event.preventDefault(); event.stopPropagation(); startEdit(id, text); }}><Pencil size={14} /></button></span>}</span>
    </div>;
  }

  function requirementGroup(item, bucketStatus = "pending") {
    const criteria = criteriaItems.filter((entry) => entry.id.startsWith(`${item.requirement_id}:`));
    const canReview = hasSelectedReviewItem(item);
    return <details className="requirement-group" open={expandedItems.includes(`requirement:${item.requirement_id}`)} onToggle={(event) => { const isOpen = event.currentTarget.open; setExpandedItems((items) => isOpen ? [...new Set([...items, `requirement:${item.requirement_id}`])] : items.filter((entry) => entry !== `requirement:${item.requirement_id}`)); }} key={item.requirement_id}>
      <summary><div className="requirement-group-summary"><input type="checkbox" checked={selectedItems.includes(item.requirement_id)} onClick={(event) => event.stopPropagation()} onChange={() => toggleRequirement(item)} /><span><div className="requirement-title-row"><b>{item.requirement_id} - {item.title}</b><span className="priority-inline">{item.priority || "Not specified"}</span></div><small>{item.statement}</small><div className="requirement-meta"><div className="meta-badges"><span className="meta-badge">{item.type || "Functional"}</span></div><div className="meta-fact"><strong>Actors</strong><span>{listText(item.actors)}</span></div><div className="meta-fact"><strong>Entities</strong><span>{listText(item.entities)}</span></div></div>{item.ambiguities?.length > 0 && <small className="requirement-warning">Ambiguity: {item.ambiguities.join(" ")}</small>}{item.assumptions?.length > 0 && <small className="requirement-assumption">Assumption: {item.assumptions.join(" ")}</small>}</span><span className="requirement-review-actions">{bucketStatus !== "approved" && <button type="button" className="item-icon-button item-approve review-label-button" disabled={!canReview} title={canReview ? "Mark requirement as approved" : "Select an acceptance criterion first"} aria-label={`Mark ${item.title} as approved`} onClick={(event) => { event.preventDefault(); event.stopPropagation(); setRequirementReviewStatus(item.requirement_id, "approved"); }}>Approve</button>}{bucketStatus !== "rejected" && <button type="button" className="item-icon-button item-reject review-label-button" disabled={!canReview} title={canReview ? "Mark requirement as rejected" : "Select an acceptance criterion first"} aria-label={`Mark ${item.title} as rejected`} onClick={(event) => { event.preventDefault(); event.stopPropagation(); setRequirementReviewStatus(item.requirement_id, "rejected"); }}>Reject</button>}{bucketStatus !== "pending" && <button type="button" className="item-icon-button item-reset" title="Return requirement to pending review" aria-label={`Return ${item.title} to pending review`} onClick={(event) => { event.preventDefault(); event.stopPropagation(); setRequirementReviewStatus(item.requirement_id, "pending"); }}><RotateCcw size={14} /></button>}</span></div></summary>
      <div className="requirement-group-body">
        <div className="requirement-group-grid">
          <div><h4>Acceptance criteria</h4>{criteria.length ? criteria.map((entry) => selectionRow(entry.id, entry.title, entry.text)) : <p className="muted">No acceptance criteria extracted.</p>}</div>
        </div>
      </div>
    </details>;
  }

  function requirementBucket(label, status, items) {
    return <section className={`review-bucket ${status}`}><div className="review-bucket-header"><h3>{label}</h3><span>{items.length}</span></div>{items.length ? items.map((item) => requirementGroup(item, status)) : <p className="muted">No requirements in this section.</p>}</section>;
  }

  useEffect(() => { listProjects().then((items) => { setProjects(items); if (!project?.project_id && items[0]) selectProject(items[0].project_id); }).catch((err) => setError(err.message)); }, []);
  useEffect(() => { if (project?.project_id) loadPackages(project.project_id); }, [project?.project_id]);
  useEffect(() => { if (packageId) loadAnalysis(packageId); }, [packageId]);

  return <div className="requirements-page">
    <div className="page-heading"><div><div className="eyebrow">APPROVED BUSINESS SCOPE</div><h1>Functional Requirements</h1><p>Generate testable system behavior from approved Business Requirements and the selected source package.</p></div><button className="secondary-button" onClick={() => loadPackages()} disabled={loading}><RefreshCw size={16} /> Refresh</button></div>
    <div className="requirements-toolbar"><div><label>Project</label><select value={project?.project_id || ""} onChange={(event) => selectProject(event.target.value)}><option value="">Select project</option>{projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.project_name}</option>)}</select></div><div><label>Source package</label><select value={packageId} onChange={(event) => selectPackage(event.target.value)} disabled={!packages.length}><option value="">Select package</option>{packages.map((item) => <option key={item.package_id} value={item.package_id}>{packageOptionLabel(item)}</option>)}</select></div><button className="primary-button requirements-run-button" onClick={runAnalysis} disabled={!packageId || running || loading || !approvedBusinessRequirements.length}>{running ? <><LoaderCircle className="spin" size={16} /> Generating...</> : <><Play size={16} /> {analysis ? "Generate new version" : "Generate Functional Requirements"}</>}</button></div>
    {error && <div className="error-message"><AlertCircle size={17} /> {error}</div>}
    {!selectedPackage && <section className="requirements-empty"><div className="empty-state-icon"><FileSearch size={25} /></div><h2>Select an existing source package</h2><p>Your uploaded BRD and created package will appear here.</p></section>}
    {selectedPackage && !analysis && <section className="requirements-empty"><div className="empty-state-icon"><FileSearch size={25} /></div><h2>{approvedBusinessRequirements.length ? "Approved business requirements are ready" : "Approve business requirements first"}</h2><p>{approvedBusinessRequirements.length ? `${approvedBusinessRequirements.length} approved business requirement(s) will be used with the selected source package and any saved project knowledge context.` : "Generate Business Requirements, review them, and approve at least one before generating Functional Requirements."}</p></section>}
    {analysis && <>
      <section className="requirements-status-card"><div className="requirements-status-icon"><CheckCircle2 size={22} /></div><div><div className="eyebrow">FUNCTIONAL REQUIREMENTS · VERSION {analysis.analysis_version}</div><h4>Generated from approved business scope</h4><p>{analysis.contract?.generation_mode === "approved_business_requirement_fallback" ? "Mapped from approved Business Requirements and cited package sources using the local fallback." : `Generated from approved Business Requirements and package sources${analysis.contract?.knowledge_contexts?.length ? ` with ${analysis.contract.knowledge_contexts.length} saved project knowledge context(s)` : "; no saved project knowledge context was attached"}.`}</p></div><span className={`requirements-status ${analysis.status}`}>{statusText(analysis.status)}</span></section>
      <div className="requirements-summary-grid"><div><span>Functional requirements</span><strong>{requirements.length}</strong></div><div><span>Acceptance criteria</span><strong>{criteriaItems.length}</strong></div></div>
      {readinessChecks.length > 0 && <details className="requirements-panel readiness-panel readiness-dropdown-panel" open={(readiness.status || "needs_review") !== "ready"}><summary className="readiness-summary"><div><h2>Readiness for test generation</h2><p className="panel-helper">Checks whether the approved requirement detail is enough for reliable test design.</p></div><span className="readiness-summary-meta"><span className={`readiness-status ${readiness.status || "needs_review"}`}>{statusText(readiness.status || "needs_review")}</span>{passedReadinessCount} of {readinessChecks.length} passed · View details</span></summary><div className="readiness-list">{readinessChecks.map((check) => <div className={`readiness-check-row ${check.status}`} key={check.check_id}><span className="readiness-check-icon"><CheckCircle2 size={15} /></span><div><strong>{check.label}</strong><p>{check.details}</p></div><span>{statusText(check.status)}</span></div>)}</div>{readinessGapItems.length > 0 && <ul className="readiness-gaps">{readinessGapItems.map((gap) => <li key={gap}>{gap}</li>)}</ul>}</details>}
      {analysisWarningItems.length > 0 && <details className="requirements-panel warning-panel compact-warning-panel"><summary className="warning-summary"><div><h2><AlertCircle size={18} /> Analysis notices</h2><p className="panel-helper">Package created successfully. Open this only if you want to review non-blocking analysis notes.</p></div><span>{analysisWarningItems.length} notice{analysisWarningItems.length === 1 ? "" : "s"} · View details</span></summary><ul>{analysisWarningItems.map((gap) => <li key={gap}>{gap}</li>)}</ul></details>}
      {conflictItems.length > 0 && <details className="requirements-panel clarification-panel"><summary className="clarification-summary"><div><h2><AlertCircle size={18} /> Review questions detected</h2><p className="panel-helper">Some terms may need confirmation during requirement review. These are not package errors.</p></div><span>{conflictItems.length} questions · View details</span></summary><ul className="clarification-list">{conflictItems.map((conflict) => <li key={conflict.conflict_id || conflict.description}><strong>{conflict.conflict_id || "Question"}: </strong>{conflictDescriptionText(conflict)}<small>{conflict.severity ? `Severity: ${conflict.severity}` : ""}</small></li>)}</ul></details>}
      {contract?.global_assumptions?.length > 0 && <section className="requirements-panel assumptions-panel"><div className="panel-title-row"><h2>Package-level assumptions</h2><span>{contract.global_assumptions.length}</span></div><ul>{contract.global_assumptions.map((assumption) => <li key={assumption}>{assumption}</li>)}</ul></section>}
      <section className="requirements-panel nested-requirements-panel"><div className="panel-title-row"><div><h2>Requirements and related evidence</h2><p className="panel-helper">Select requirements and their acceptance criteria before approving or rejecting them.</p></div><span>{selectedItems.filter((id) => requirements.some((item) => item.requirement_id === id)).length} selected</span></div><div className="selection-actions"><label className="select-all"><input type="checkbox" checked={selectableItems.length > 0 && selectedItems.length === selectableItems.length} onChange={(event) => toggleAll(event.target.checked)} /> Select all</label><div className="review-move-actions"><button type="button" className="review-icon-action review-label-action approved-action" disabled={!requirements.some((item) => selectedItems.includes(item.requirement_id) && hasSelectedReviewItem(item))} title="Mark selected requirements as approved" aria-label="Mark selected requirements as approved" onClick={() => moveSelectedRequirements("approved")}>Approve</button><button type="button" className="review-icon-action review-label-action rejected-action" disabled={!requirements.some((item) => selectedItems.includes(item.requirement_id) && hasSelectedReviewItem(item))} title="Mark selected requirements as rejected" aria-label="Mark selected requirements as rejected" onClick={() => moveSelectedRequirements("rejected")}>Reject</button><button className="text-button" onClick={() => toggleAll(false)}>Clear all</button></div></div><div className="review-buckets"><div className="pending-bucket">{requirementBucket("Pending review", "pending", requirements.filter((item) => (reviewStatuses[item.requirement_id] || "pending") === "pending"))}</div><div className="review-bucket-columns">{requirementBucket("Approved requirements", "approved", requirements.filter((item) => reviewStatuses[item.requirement_id] === "approved"))}{requirementBucket("Rejected requirements", "rejected", requirements.filter((item) => reviewStatuses[item.requirement_id] === "rejected"))}</div></div></section>
      {findings.length > 0 && <section className="requirements-panel"><div className="panel-title-row"><h2><AlertCircle size={18} /> Review notes</h2><span>{findings.length} found</span></div><ul>{findings.map((item, index) => <li key={`${item.text}-${index}`}><strong>{item.kind}: </strong>{item.text}</li>)}</ul></section>}
      {!reviewSaved ? <section className="requirements-review-card"><div><div className="eyebrow">HUMAN REVIEW</div><h2>Finalize requirement review</h2><p>Save the review decision and notes. Only approved requirements continue to test-case generation.</p></div><div className="review-fields"><label>Reviewer<input value={reviewer} onChange={(event) => setReviewer(event.target.value)} /></label><label>Accepted assumptions <span>(optional, one per line)</span><textarea value={assumptions} onChange={(event) => setAssumptions(event.target.value)} placeholder="Example: SMS provider response is treated as the observable delivery outcome" /></label><label>Review comments<textarea value={comments} onChange={(event) => setComments(event.target.value)} placeholder="Add review notes" /></label><button className="primary-button" onClick={approve} disabled={reviewing || selectedItems.length === 0}>{reviewing ? <><LoaderCircle className="spin" size={16} /> Saving review...</> : <><ShieldCheck size={16} /> Save review</>}</button></div></section> : <section className="review-complete-card"><div className="review-complete-icon"><CheckCircle2 size={22} /></div><div><div className="eyebrow">REVIEW SAVED</div><h2>Approved requirements are ready</h2><p>{requirements.filter((item) => reviewStatuses[item.requirement_id] === "approved").length} requirement{requirements.filter((item) => reviewStatuses[item.requirement_id] === "approved").length === 1 ? "" : "s"} will be used for test-case generation. Pending and rejected requirements are excluded.</p></div><button className="primary-button" onClick={() => onContinueToGeneration?.(analysis)} disabled={!onContinueToGeneration}><Play size={16} /> Continue to Test Case Generation</button></section>}
    </>}
  </div>;
}





