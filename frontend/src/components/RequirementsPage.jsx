import { useEffect, useMemo, useState } from "react";
import { AlertCircle, CheckCircle2, ChevronLeft, ChevronRight, Download, FileSearch, Pencil, Play, RefreshCw, RotateCcw, ShieldCheck } from "lucide-react";
import { generateFunctionalRequirements, getLatestRequirementAnalyses, getRagReadiness, invalidateApiCache, listManifests, listProjects, reviewRequirementAnalysis } from "../api/client.js";
import { useActivities } from "../activity/activityStore.js";
import { LoadingSpinner } from "./LoadingSpinner.jsx";
import { AppSelect } from "./AppSelect.jsx";

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
  return item?.package_name?.trim() || item?.scope?.trim() || "Resource Package";
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

function evidenceLabel(ref) {
  const location = ref?.source_reference || ref?.fragment_id || "Source evidence";
  const heading = ref?.heading ? ` · ${ref.heading}` : "";
  return `${location}${heading}`;
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

const FUNCTIONAL_PAGE_SIZE = 5;
const FUNCTIONAL_PROGRESS_MESSAGES = ["Reading project documents", "Retrieving Knowledge Base evidence", "Writing testable behavior", "Checking traceability and readiness"];

export function RequirementsPage({ onContinueToGeneration, onOpenBusinessRequirements, onOpenKnowledgeBase }) {
  const [project, setProject] = useState(() => JSON.parse(localStorage.getItem("testiq_current_project") || "null"));
  const [projects, setProjects] = useState([]);
  const [packages, setPackages] = useState([]);
  const [packageId, setPackageId] = useState(() => JSON.parse(localStorage.getItem("testiq_current_package") || "null")?.package_id || "");
  const [analysis, setAnalysis] = useState(null);
  const [approvedBusinessRequirements, setApprovedBusinessRequirements] = useState([]);
  const [loading, setLoading] = useState(false);
  const [requestRunning, setRequestRunning] = useState(false);
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
  const [page, setPage] = useState(1);
  const [progressMessageIndex, setProgressMessageIndex] = useState(0);
  const [progressPercent, setProgressPercent] = useState(0);
  const [knowledgeReadiness, setKnowledgeReadiness] = useState(null);
  const activities = useActivities();
  const functionalActivity = activities.find((item) => item.section === "requirements");
  const running = requestRunning || functionalActivity?.status === "running";

  const selectedPackage = useMemo(() => packages.find((item) => item.package_id === packageId), [packages, packageId]);
  const contract = analysis?.contract;
  const requirements = contract?.requirements || [];
  const pageCount = Math.max(1, Math.ceil(requirements.length / FUNCTIONAL_PAGE_SIZE));
  const visibleRequirements = requirements.slice((page - 1) * FUNCTIONAL_PAGE_SIZE, page * FUNCTIONAL_PAGE_SIZE);
  const readiness = contract?.readiness || {};
  const readinessChecks = readiness.checks || [];
  const readinessGapItems = (readiness.gaps || []).filter((gap) => !isAnalysisWarningText(gap));
  const passedReadinessCount = readinessChecks.filter((check) => check.status === "passed").length;
  const conflictItems = (contract?.conflicting_requirements || contract?.conflicts || []).filter((conflict) => !isGenericFallbackConflict(conflict));
  const ragStatus = contract?.rag_status;
  const ragMetadata = contract?.rag_metadata;
  const knowledgeUsed = Boolean(ragStatus?.used ?? ragMetadata?.index_id);

  const criteriaItems = requirements.flatMap((item) => (item.acceptance_criteria || []).map((criterion, index) => {
    const given = typeof criterion === "string" ? "The source-defined precondition is satisfied." : (Array.isArray(criterion.given) ? criterion.given.join("; ") : criterion.given) || "The source-defined precondition is satisfied.";
    const when = typeof criterion === "string" ? "The requirement flow runs." : criterion.when || "The requirement flow runs.";
    const then = typeof criterion === "string" ? criterion : criterion.then || criterion.text || "The expected behavior occurs.";
    const text = `Given: ${given}\nWhen: ${when}\nThen: ${then}`;
    const acceptanceId = typeof criterion === "string" ? `AC-${index + 1}` : criterion.acceptance_id || `AC-${index + 1}`;
    const scenario = typeof criterion === "object" ? criterion.scenario : "";
    return { id: `${item.requirement_id}:${acceptanceId}`, title: scenario ? `${acceptanceId} · ${scenario}` : acceptanceId, text, source_references: sourceReferences(criterion?.source_references) };
  }));

  const outcomeItems = requirements.flatMap((item) => (item.expected_outcomes || []).map((outcome, index) => {
    const outcomeId = typeof outcome === "string" ? `EO-${index + 1}` : outcome.outcome_id || `EO-${index + 1}`;
    return { id: `${item.requirement_id}:${outcomeId}`, title: outcomeId, text: textValue(outcome), source_references: sourceReferences(outcome?.source_references) };
  }));

  const businessRuleCount = requirements.reduce((total, item) => total + (item.business_rules?.length || 0), 0);
  const sourceReferenceCount = new Set(requirements.flatMap((item) => sourceReferences(item.source_references).map((ref) => ref.source_reference || ref.fragment_id))).size;


  const findings = requirements.flatMap((item) => (item.ambiguities || []).map((text) => ({ text, kind: `${item.requirement_id} clarification` })));

  const selectableItems = [
    ...requirements.map((item) => ({ id: item.requirement_id, label: item.title })),
    ...criteriaItems.map((item) => ({ id: item.id, label: item.text })),
    ...outcomeItems.map((item) => ({ id: item.id, label: item.text })),
  ];

  useEffect(() => {
    if (!running) return undefined;
    const elapsedSeconds = functionalActivity?.startedAt ? Math.max(0, (Date.now() - new Date(functionalActivity.startedAt).getTime()) / 1000) : 0;
    setProgressPercent(Math.min(94, Math.max(4, Math.round(4 + elapsedSeconds))));
    setProgressMessageIndex(Math.floor(elapsedSeconds / 3) % FUNCTIONAL_PROGRESS_MESSAGES.length);
    const messageTimer = window.setInterval(() => setProgressMessageIndex((index) => (index + 1) % FUNCTIONAL_PROGRESS_MESSAGES.length), 1800);
    const progressTimer = window.setInterval(() => setProgressPercent((value) => Math.min(94, value + Math.max(1, Math.ceil((94 - value) / 10)))), 450);
    return () => { window.clearInterval(messageTimer); window.clearInterval(progressTimer); };
  }, [running, functionalActivity?.startedAt]);

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
    if (!id) { setAnalysis(null); setApprovedBusinessRequirements([]); setKnowledgeReadiness(null); setLoading(false); return; }
    setLoading(true);
    try {
      const [latest, nextReadiness] = await Promise.all([
        getLatestRequirementAnalyses(id),
        getRagReadiness(id).catch(() => ({ ready: false, message: "The Knowledge Base has not been built for this Resource Package." })),
      ]);
      setKnowledgeReadiness(nextReadiness);
      const generatedAnalysis = latest.functional || null;
      const next = generatedAnalysis;
      setApprovedBusinessRequirements(next?.contract?.approved_business_requirements || []);
      setAnalysis(next);
      if (next) {
        let savedDraft = null;
        try { savedDraft = JSON.parse(localStorage.getItem(`testiq_functional_review_draft:${next.analysis_id}`) || "null"); } catch { savedDraft = null; }
        const autoApproved = Boolean((next.contract?.auto_approved && next.contract?.review_metadata?.reviewer === "system:auto") || (next.contract?.functional_requirements_generation && !next.contract?.review_metadata));
        const autoApprovedItemIds = (next.contract?.requirements || []).flatMap((item) => [item.requirement_id, ...(item.acceptance_criteria || []).map((criterion, index) => `${item.requirement_id}:${criterion.acceptance_id || `AC-${index + 1}`}`), ...(item.expected_outcomes || []).map((outcome, index) => `${item.requirement_id}:${outcome.outcome_id || `EO-${index + 1}`}`)]);
        setSelectedItems(autoApproved ? (next.contract?.approved_items?.length ? next.contract.approved_items : autoApprovedItemIds) : savedDraft?.selectedItems ?? (next.contract?.approved_items?.length ? next.contract.approved_items : []));
        setReviewStatuses(autoApproved ? getReviewStatuses(next.contract) : savedDraft?.reviewStatuses ?? getReviewStatuses(next.contract));
        setReviewSaved(true);
        setAssumptions(savedDraft?.assumptions ?? (next.contract?.accepted_assumptions || []).join("\n"));
        setComments(savedDraft?.comments ?? "");
      }
    } catch (err) { setError(err.message); } finally { setLoading(false); }
  }

  async function selectProject(id) {
    setApprovedBusinessRequirements([]); setAnalysis(null);
    if (!id) { setProject(null); setPackages([]); setPackageId(""); return; }
    const selected = projects.find((item) => item.project_id === id);
    if (!selected) { setError("Selected project was not found."); return; }
    setProject(selected); setAnalysis(null);
    localStorage.setItem("testiq_current_project", JSON.stringify(selected));
    await loadPackages(selected.project_id);
  }

  function selectPackage(id) {
    setPackageId(id); setAnalysis(null); setError("");
    setKnowledgeReadiness(null);
    setPage(1);
    setApprovedBusinessRequirements([]); setLoading(Boolean(id));
    const selected = packages.find((item) => item.package_id === id);
    if (selected) localStorage.setItem("testiq_current_package", JSON.stringify(selected));
    else localStorage.removeItem("testiq_current_package");
  }

  async function refreshWorkspace() {
    invalidateApiCache();
    await Promise.all([loadPackages(project?.project_id), loadAnalysis(packageId)]);
  }

  async function runAnalysis() {
    if (!packageId || running || !knowledgeReadiness?.ready) return;
    setProgressMessageIndex(0);
    setProgressPercent(0);
    setRequestRunning(true); setError("");
    try {
      const next = await generateFunctionalRequirements(packageId);
      setAnalysis(next);
      setPage(1);
      setSelectedItems([]);
      setReviewStatuses(getReviewStatuses(next.contract));
      setReviewSaved(false);
      setAssumptions("");
      setProgressPercent(100);
      await new Promise((resolve) => window.setTimeout(resolve, 300));
    } catch (err) { setError(err.message); } finally { setRequestRunning(false); }
  }

  function downloadJson() {
    if (!contract) return;
    const selectedRequirementIds = new Set(requirements.filter((item) => selectedItems.includes(item.requirement_id)).map((item) => item.requirement_id));
    if (!selectedRequirementIds.size) return;
    const exported = structuredClone(contract);
    exported.requirements = (exported.requirements || []).filter((item) => selectedRequirementIds.has(item.requirement_id)).map((item) => ({
      ...item,
      acceptance_criteria: (item.acceptance_criteria || []).filter((criterion, index) => selectedItems.includes(`${item.requirement_id}:${criterion.acceptance_id || `AC-${index + 1}`}`)),
      expected_outcomes: (item.expected_outcomes || []).filter((outcome, index) => selectedItems.includes(`${item.requirement_id}:${outcome.outcome_id || `EO-${index + 1}`}`)),
    }));
    const selectedBusinessIds = new Set(exported.requirements.flatMap((item) => item.business_requirement_ids || (item.business_rules || []).map((rule) => rule.rule_id)));
    exported.approved_business_requirements = (exported.approved_business_requirements || []).filter((item) => selectedBusinessIds.has(item.business_requirement_id));
    exported.export_selection = { requirement_ids: [...selectedRequirementIds], exported_at: new Date().toISOString() };
    const blob = new Blob([JSON.stringify(exported, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = window.document.createElement("a");
    anchor.href = url;
    anchor.download = `functional-requirements-v${analysis.analysis_version}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
  }

  async function approve() {
    if (!analysis?.analysis_id || !reviewer.trim()) return;
    setReviewing(true); setError("");
    try {
      const items = (analysis.contract?.requirements || []).flatMap((item) => {
        const parentStatus = reviewStatuses[item.requirement_id] || "pending";
        const childStatus = (itemId) => parentStatus === "approved" ? (selectedItems.includes(itemId) ? "approved" : "rejected") : parentStatus;
        return [
          { item_id: item.requirement_id, approved: parentStatus === "approved", review_status: parentStatus, title: item.title, statement: item.statement },
          ...(item.business_rules || []).map((rule, index) => { const itemId = `${item.requirement_id}:${rule.rule_id || `RULE-${index + 1}`}`; return { item_id: itemId, approved: parentStatus === "approved", review_status: parentStatus, text: textValue(rule) }; }),
          ...(item.acceptance_criteria || []).map((criterion, index) => { const itemId = `${item.requirement_id}:${criterion.acceptance_id || `AC-${index + 1}`}`; const status = childStatus(itemId); return { item_id: itemId, approved: status === "approved", review_status: status, text: criterion.text, scenario: criterion.scenario, given: criterion.given, when: criterion.when, then: criterion.then }; }),
          ...(item.expected_outcomes || []).map((outcome, index) => { const itemId = `${item.requirement_id}:${outcome.outcome_id || `EO-${index + 1}`}`; const status = childStatus(itemId); return { item_id: itemId, approved: status === "approved", review_status: status, text: textValue(outcome) }; }),
        ];
      });
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
      .concat(outcomeItems.filter((entry) => entry.id.startsWith(`${item.requirement_id}:`)))
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
    const autoApproved = Boolean(nextContract?.functional_requirements_generation && !nextContract?.review_metadata);
    return Object.fromEntries((nextContract?.requirements || []).map((item) => {
      const rejected = (nextContract.rejected_items || []).includes(item.requirement_id);
      const status = autoApproved ? (rejected ? "rejected" : "approved") : item.review_status || (rejected ? "rejected" : (nextContract.approved_items || []).includes(item.requirement_id) ? "approved" : "pending");
      return [item.requirement_id, status];
    }));
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
        for (const criterion of requirementItem.acceptance_criteria || []) if (`${requirementItem.requirement_id}:${criterion.acceptance_id}` === id || criterion.acceptance_id === id) { criterion.text = editingValue.trim(); criterion.then = editingValue.trim(); }
        for (const outcome of requirementItem.expected_outcomes || []) if (`${requirementItem.requirement_id}:${outcome.outcome_id}` === id || outcome.outcome_id === id) outcome.text = editingValue.trim();
      }
      return next;
    });
    setEditingItemId("");
    setEditingValue("");
  }

  function selectionRow(id, title, text, references = []) {
    return <div className={`selection-item ${selectedItems.includes(id) ? "selected" : ""}`} key={id}>
      <input type="checkbox" checked={selectedItems.includes(id)} onChange={() => toggleItem(id)} />
      <span className="selection-item-content"><b>{title}</b>{editingItemId === id ? <textarea className="inline-edit" value={editingValue} onChange={(event) => setEditingValue(event.target.value)} /> : <><small className="expanded">{text}</small>{references.length > 0 && <small className="source-reference-line">Evidence: {references.map(evidenceLabel).join("; ")}</small>}</>}{editingItemId === id ? <span className="item-actions"><button type="button" className="secondary-button compact-button" onClick={() => saveEdit(id)}>Save</button><button type="button" className="text-button" onClick={() => setEditingItemId("")}>Cancel</button></span> : <span className="item-actions"><button type="button" className="item-icon-button item-edit" title="Edit item" aria-label={`Edit ${title}`} onClick={(event) => { event.preventDefault(); event.stopPropagation(); startEdit(id, text); }}><Pencil size={14} /></button></span>}</span>
    </div>;
  }

  function requirementGroup(item, bucketStatus = "pending") {
    const criteria = criteriaItems.filter((entry) => entry.id.startsWith(`${item.requirement_id}:`));
    const outcomes = outcomeItems.filter((entry) => entry.id.startsWith(`${item.requirement_id}:`));
    const canReview = hasSelectedReviewItem(item);
    const groupKey = `requirement:${item.requirement_id}`;
    const expanded = expandedItems.includes(groupKey);
    return <div className={`requirement-group${expanded ? " open" : ""}`} key={item.requirement_id}>
      <div className="requirement-group-row">
        <input className="requirement-group-select" type="checkbox" checked={selectedItems.includes(item.requirement_id)} aria-label={`Select ${item.requirement_id}`} onChange={() => toggleRequirement(item)} />
        <details className="requirement-group-disclosure" open={expanded} onToggle={(event) => { const isOpen = event.currentTarget.open; setExpandedItems((items) => isOpen ? [...new Set([...items, groupKey])] : items.filter((entry) => entry !== groupKey)); }}>
          <summary><div className="requirement-group-summary"><span><div className="requirement-title-row"><b>{item.requirement_id} - {item.title}</b><span className="priority-inline">{item.priority || "Not specified"}</span></div><small>{item.statement}</small><div className="requirement-meta"><div className="meta-badges"><span className="meta-badge">{item.type || "Functional"}</span></div><div className="meta-fact"><strong>Actors</strong><span>{listText(item.actors)}</span></div><div className="meta-fact"><strong>Entities</strong><span>{listText(item.entities)}</span></div></div>{item.ambiguities?.length > 0 && <small className="requirement-warning">Ambiguity: {item.ambiguities.join(" ")}</small>}{item.assumptions?.length > 0 && <small className="requirement-assumption">Assumption: {item.assumptions.join(" ")}</small>}</span></div></summary>
          <div className="requirement-group-body"><div className="requirement-group-grid"><div><h4>Acceptance criteria</h4>{criteria.length ? criteria.map((entry) => selectionRow(entry.id, entry.title, entry.text, entry.source_references)) : <p className="muted">No acceptance criteria extracted.</p>}</div><div className="requirement-outcomes"><h4>Expected outcomes</h4>{outcomes.length ? outcomes.map((entry) => selectionRow(entry.id, entry.title, entry.text, entry.source_references)) : <p className="muted">No expected outcomes extracted.</p>}</div><div className="requirement-rules"><h4>Source rules</h4>{(item.business_rules || []).length ? <div className="trace-detail-list">{item.business_rules.map((rule, index) => <div className="trace-detail-item" key={`${item.requirement_id}-${rule.rule_id || index}`}><strong>{rule.rule_id || `BR-${index + 1}`}</strong><p>{textValue(rule)}</p>{rule.mapping_basis && <small>{rule.mapping_basis}</small>}{sourceReferences(rule.source_references).length > 0 && <small className="source-reference-line">Evidence: {sourceReferences(rule.source_references).map(evidenceLabel).join("; ")}</small>}</div>)}</div> : <p className="muted">No source rule was mapped.</p>}</div><div><h4>Functional flow</h4>{(item.flow_steps || []).length ? <ol>{item.flow_steps.map((step, index) => <li key={`${item.requirement_id}-step-${index}`}>{step}</li>)}</ol> : <p className="muted">No flow steps extracted.</p>}</div><div><h4>Requirement evidence</h4>{sourceReferences(item.source_references).length ? <ul>{sourceReferences(item.source_references).map((ref, index) => <li key={`${ref.fragment_id || ref.source_reference}-${index}`}>{evidenceLabel(ref)}</li>)}</ul> : <p className="muted">No source evidence attached.</p>}</div></div></div>
        </details>
        <span className="requirement-review-actions">{bucketStatus !== "approved" && <button type="button" className="item-icon-button item-approve review-label-button" disabled={!canReview} title={canReview ? "Mark requirement as approved" : "Select an acceptance criterion first"} aria-label={`Mark ${item.title} as approved`} onClick={() => setRequirementReviewStatus(item.requirement_id, "approved")}>Approve</button>}{bucketStatus !== "rejected" && <button type="button" className="item-icon-button item-reject review-label-button" disabled={!canReview} title={canReview ? "Mark requirement as rejected" : "Select an acceptance criterion first"} aria-label={`Mark ${item.title} as rejected`} onClick={() => setRequirementReviewStatus(item.requirement_id, "rejected")}>Reject</button>}{bucketStatus !== "pending" && <button type="button" className="item-icon-button item-reset" title="Return requirement to pending review" aria-label={`Return ${item.title} to pending review`} onClick={() => setRequirementReviewStatus(item.requirement_id, "pending")}><RotateCcw size={14} /></button>}</span>
      </div>
    </div>;
  }

  function requirementBucket(label, status, items) {
    return <section className={`review-bucket ${status}`}><div className="review-bucket-header"><h3>{label}</h3><span>{items.length}</span></div>{items.length ? items.map((item) => requirementGroup(item, status)) : <p className="muted">No requirements in this section.</p>}</section>;
  }

  useEffect(() => {
    let active = true;
    (async () => {
      setLoading(true);
      try {
        const items = await listProjects();
        if (!active) return;
        setProjects(items);
        const selectedProject = items.find((item) => item.project_id === project?.project_id) || items[0] || null;
        setProject(selectedProject);
        if (!selectedProject) { setPackages([]); setPackageId(""); return; }
        localStorage.setItem("testiq_current_project", JSON.stringify(selectedProject));
        const packageItems = await listManifests(selectedProject.project_id);
        if (!active) return;
        setPackages(packageItems);
        const saved = JSON.parse(localStorage.getItem("testiq_current_package") || "null");
        const selectedPackage = packageItems.find((item) => item.package_id === packageId) || packageItems.find((item) => item.package_id === saved?.package_id) || packageItems[0] || null;
        setPackageId(selectedPackage?.package_id || "");
        if (selectedPackage) localStorage.setItem("testiq_current_package", JSON.stringify(selectedPackage));
      } catch (err) { if (active) setError(err.message); }
      finally { if (active) setLoading(false); }
    })();
    return () => { active = false; };
  }, []);
  useEffect(() => { if (packageId && packages.some((item) => item.package_id === packageId)) loadAnalysis(packageId); }, [packageId, packages]);
  useEffect(() => {
    if (!analysis?.analysis_id) return;
    localStorage.setItem(`testiq_functional_review_draft:${analysis.analysis_id}`, JSON.stringify({
      selectedItems, reviewStatuses, reviewSaved, reviewer, comments, assumptions,
    }));
  }, [analysis?.analysis_id, selectedItems, reviewStatuses, reviewSaved, reviewer, comments, assumptions]);

  return <div className="requirements-page">
    <div className="page-heading"><div><div className="eyebrow">FUNCTIONAL REQUIREMENTS</div><h1>Functional Requirements</h1><p>Traceable behavior context is generated from the BRD, technical sources, and Knowledge Base. Functional Requirements are auto-approved for now; test cases remain the reviewed artifact.</p></div><button className="secondary-button" onClick={refreshWorkspace} disabled={loading}>{loading ? <LoadingSpinner variant="inline" label="Refreshing" /> : <><RefreshCw size={16} /> Refresh</>}</button></div>
    <div className="requirements-toolbar"><div><label>Project</label><AppSelect value={project?.project_id || ""} onChange={(event) => selectProject(event.target.value)} disabled={running}><option value="">Select project</option>{projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.project_name}</option>)}</AppSelect></div><div><label>Resource Package</label><AppSelect value={packageId} onChange={(event) => selectPackage(event.target.value)} disabled={!packages.length || running}><option value="">Select Resource Package</option>{packages.map((item) => <option key={item.package_id} value={item.package_id}>{packageOptionLabel(item)}</option>)}</AppSelect></div>{running ? <section className="generation-progress generation-progress--toolbar" role="status" aria-live="polite"><div className="generation-progress-track" role="progressbar" aria-label="Project context generation progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow={progressPercent}><span style={{ width: `${progressPercent}%` }} /></div><b className="generation-progress-value">{progressPercent}%</b><div className="generation-progress-copy"><strong>Preparing test-generation context</strong><span>{FUNCTIONAL_PROGRESS_MESSAGES[progressMessageIndex]}...</span></div></section> : <button className="primary-button requirements-run-button" onClick={runAnalysis} disabled={!packageId || loading || !knowledgeReadiness?.ready}>{analysis ? <><RefreshCw size={16} /> Refresh generation context</> : <><Play size={16} /> Prepare test-generation context</>}</button>}</div>
    {error && <div className="error-message"><AlertCircle size={17} /> {error}</div>}
    {loading && !running && <LoadingSpinner variant="page" label="Loading project context" detail="Checking project documents, Knowledge Base readiness, and the latest generation context." />}
    {!loading && !selectedPackage && <section className="requirements-empty"><div className="empty-state-icon"><FileSearch size={25} /></div><h2>Select a Resource Package</h2><p>Select the package containing the BRD you want to analyze.</p></section>}
    {!loading && selectedPackage && !analysis && <section className="requirements-empty"><div className="empty-state-icon"><FileSearch size={25} /></div><h2>{!knowledgeReadiness?.ready ? "Build the Knowledge Base first" : "Ready to prepare test generation"}</h2><p>{!knowledgeReadiness?.ready ? "The Knowledge Base is built automatically from the BRD and any technical sources. Generation will be available when it is ready." : "The project documents and Knowledge Base are ready. Prepare the internal generation context, then generate and review test cases."}</p>{!knowledgeReadiness?.ready ? <button type="button" className="primary-button business-back-button" onClick={() => onOpenKnowledgeBase?.()} disabled={!onOpenKnowledgeBase}>Open Knowledge Base</button> : null}</section>}
    {!loading && analysis && <>
      <section className="requirements-status-card"><div className="requirements-status-icon"><CheckCircle2 size={22} /></div><div><div className="eyebrow">INTERNAL CONTEXT · VERSION {analysis.analysis_version}</div><h4>Generated from project documents</h4><p>{knowledgeUsed ? "The context uses BRD and technical evidence retrieved from the Knowledge Base." : "This version does not contain Knowledge Base evidence. Generate a new version after the Knowledge Base is ready."}</p></div><div className="requirements-document-actions"><span className={`requirements-status ${analysis.status}`}>{statusText(analysis.status)}</span><button type="button" className="secondary-button compact-button" onClick={downloadJson} disabled={!requirements.some((item) => selectedItems.includes(item.requirement_id))}><Download size={14} /> Export context JSON</button></div></section>
      <div className="requirements-summary-grid requirement-review-summary"><div><span>Functional requirements</span><strong>{requirements.length}</strong></div><div><span>Approved rule mappings</span><strong>{businessRuleCount}</strong></div><div><span>Acceptance criteria</span><strong>{criteriaItems.length}</strong></div><div><span>Expected outcomes</span><strong>{outcomeItems.length}</strong></div><div><span>Source citations</span><strong>{sourceReferenceCount}</strong></div></div>
      {readinessChecks.length > 0 && <details className="requirements-panel readiness-panel readiness-dropdown-panel" open={(readiness.status || "needs_review") !== "ready"}><summary className="readiness-summary"><div><h2>Readiness for test generation</h2><p className="panel-helper">Checks whether the approved requirement detail is enough for reliable test design.</p></div><span className="readiness-summary-meta"><span className={`readiness-status ${readiness.status || "needs_review"}`}>{statusText(readiness.status || "needs_review")}</span>{passedReadinessCount} of {readinessChecks.length} passed · View details</span></summary><div className="readiness-list">{readinessChecks.map((check) => <div className={`readiness-check-row ${check.status}`} key={check.check_id}><span className="readiness-check-icon"><CheckCircle2 size={15} /></span><div><strong>{check.label}</strong><p>{check.details}</p></div><span>{statusText(check.status)}</span></div>)}</div>{readinessGapItems.length > 0 && <ul className="readiness-gaps">{readinessGapItems.map((gap) => <li key={gap}>{gap}</li>)}</ul>}</details>}
      {conflictItems.length > 0 && <details className="requirements-panel clarification-panel"><summary className="clarification-summary"><div><h2><AlertCircle size={18} /> Review questions detected</h2><p className="panel-helper">Some terms may need confirmation during requirement review. These are not package errors.</p></div><span>{conflictItems.length} questions · View details</span></summary><ul className="clarification-list">{conflictItems.map((conflict) => <li key={conflict.conflict_id || conflict.description}><strong>{conflict.conflict_id || "Question"}: </strong>{conflictDescriptionText(conflict)}<small>{conflict.severity ? `Severity: ${conflict.severity}` : ""}</small></li>)}</ul></details>}
      {contract?.global_assumptions?.length > 0 && <section className="requirements-panel assumptions-panel"><div className="panel-title-row"><h2>Package-level assumptions</h2><span>{contract.global_assumptions.length}</span></div><ul>{contract.global_assumptions.map((assumption) => <li key={assumption}>{assumption}</li>)}</ul></section>}
      {contract?.technical_gaps?.length > 0 && <details className="requirements-panel warning-panel"><summary className="clarification-summary"><div><h2><AlertCircle size={18} /> Technical gaps</h2><p className="panel-helper">Implementation details that need clarification before complete test coverage is possible.</p></div><span>{contract.technical_gaps.length} gaps · View details</span></summary><ul>{contract.technical_gaps.map((gap, index) => <li key={gap.gap_id || index}><strong>{gap.gap_id || `Technical gap ${index + 1}`}: </strong>{gap.text}<small>Evidence: {sourceReferences(gap.source_references).map(evidenceLabel).join("; ") || gap.technical_reference}</small></li>)}</ul></details>}
      <section className="requirements-panel nested-requirements-panel"><div className="panel-title-row"><div><h2>Functional Requirements and related evidence</h2><p className="panel-helper">Functional Requirements are auto-approved for now. Review and change a decision only when a correction is needed.</p></div><span>{selectedItems.filter((id) => requirements.some((item) => item.requirement_id === id)).length} selected</span></div><div className="selection-actions"><label className="select-all"><input type="checkbox" checked={selectableItems.length > 0 && selectableItems.every((item) => selectedItems.includes(item.id))} onChange={(event) => toggleAll(event.target.checked)} /> Select all</label><div className="review-move-actions"><button type="button" className="review-icon-action review-label-action approved-action" disabled={!requirements.some((item) => selectedItems.includes(item.requirement_id) && hasSelectedReviewItem(item))} title="Mark selected requirements as approved" aria-label="Mark selected requirements as approved" onClick={() => moveSelectedRequirements("approved")}>Approve</button><button type="button" className="review-icon-action review-label-action rejected-action" disabled={!requirements.some((item) => selectedItems.includes(item.requirement_id) && hasSelectedReviewItem(item))} title="Mark selected requirements as rejected" aria-label="Mark selected requirements as rejected" onClick={() => moveSelectedRequirements("rejected")}>Reject</button><button className="text-button" onClick={() => toggleAll(false)}>Clear all</button></div></div><div className="review-buckets"><div className="pending-bucket">{requirementBucket("Pending review", "pending", visibleRequirements.filter((item) => (reviewStatuses[item.requirement_id] || "pending") === "pending"))}</div><div className="review-bucket-columns">{requirementBucket("Approved Functional Requirements", "approved", visibleRequirements.filter((item) => reviewStatuses[item.requirement_id] === "approved"))}{requirementBucket("Rejected Functional Requirements", "rejected", visibleRequirements.filter((item) => reviewStatuses[item.requirement_id] === "rejected"))}</div></div><div className="requirements-pagination"><span>Showing {(page - 1) * FUNCTIONAL_PAGE_SIZE + 1}-{Math.min(page * FUNCTIONAL_PAGE_SIZE, requirements.length)} of {requirements.length}</span><div><button type="button" className="secondary-button" aria-label="Previous functional requirements page" disabled={page === 1} onClick={() => setPage((current) => current - 1)}><ChevronLeft size={15} /> Previous</button><span>Page {page} of {pageCount}</span><button type="button" className="secondary-button" aria-label="Next functional requirements page" disabled={page === pageCount} onClick={() => setPage((current) => current + 1)}>Next <ChevronRight size={15} /></button></div></div></section>
      {findings.length > 0 && <section className="requirements-panel"><div className="panel-title-row"><h2><AlertCircle size={18} /> Review notes</h2><span>{findings.length} found</span></div><ul>{findings.map((item, index) => <li key={`${item.text}-${index}`}><strong>{item.kind}: </strong>{item.text}</li>)}</ul></section>}
      <section className="review-complete-card"><div className="review-complete-icon"><CheckCircle2 size={22} /></div><div><div className="eyebrow">GENERATION CONTEXT READY</div><h2>Ready to generate test cases</h2><p>{requirements.length} internal requirement{requirements.length === 1 ? "" : "s"} were derived from the project documents. Review and approval are required only for the generated test cases.</p></div><button className="primary-button" onClick={() => onContinueToGeneration?.(analysis)} disabled={!onContinueToGeneration}><Play size={16} /> Generate Test Cases</button></section>
    </>}
  </div>;
}





