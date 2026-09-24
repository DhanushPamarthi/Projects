import { useEffect, useMemo, useState } from "react";
import { AlertCircle, CheckCircle2, FileSearch, LoaderCircle, Play, RefreshCw, ShieldCheck } from "lucide-react";
import { analyzeRequirements, getProject, listManifests, listProjects, listRequirementAnalyses, reviewRequirementAnalysis } from "../api/client.js";

function statusText(status) {
  return String(status || "not_started").replaceAll("_", " ");
}

function textValue(value) {
  if (typeof value === "string") return value;
  if (value && typeof value === "object") return value.text || value.statement || value.description || "";
  return String(value ?? "");
}

export function RequirementsPage() {
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
  const [error, setError] = useState("");

  const selectedPackage = useMemo(() => packages.find((item) => item.package_id === packageId), [packages, packageId]);
  const contract = analysis?.contract;
  const requirements = contract?.requirements || [];
  const requirement = requirements[0];
  const criteriaItems = requirements.flatMap((item) => (item.acceptance_criteria || []).map((criterion, index) => {
    const text = typeof criterion === "string" ? criterion : `${criterion.scenario || "Acceptance scenario"} — Given: ${(criterion.given || []).join("; ") || "source condition"} When: ${criterion.when || "the journey executes"} Then: ${criterion.then || criterion.text || "expected behavior"}`;
    return { id: `${item.requirement_id}:${criterion.acceptance_id || `AC-${index + 1}`}`, title: `${item.requirement_id} · ${criterion.acceptance_id || `AC-${index + 1}`}`, text };
  }));
  const outcomeItems = requirements.flatMap((item) => (item.expected_outcomes || []).map((text, index) => ({ id: `${item.requirement_id}:OUTCOME-${index + 1}`, title: `${item.requirement_id} · Outcome ${index + 1}`, text })));
  const ruleItems = requirements.flatMap((item) => (item.business_rules || []).map((text, index) => ({ id: `${item.requirement_id}:RULE-${index + 1}`, title: `${item.requirement_id} · Rule ${index + 1}`, text })));
  outcomeItems.forEach((item) => { item.text = textValue(item.text); });
  ruleItems.forEach((item) => { item.text = textValue(item.text); });
  const findings = [...(contract?.ambiguities || []), ...(contract?.conflicts || []).map((item) => ({ text: item.description }))];
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
      if (next) setSelectedItems(next.contract?.approved_items?.length ? next.contract.approved_items : getItemIds(next.contract));
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
      setSelectedItems(getItemIds(next.contract));
    } catch (err) { setError(err.message); } finally { setRunning(false); }
  }

  async function approve() {
    if (!analysis?.analysis_id || !reviewer.trim()) return;
    setReviewing(true); setError("");
    try {
      setAnalysis(await reviewRequirementAnalysis(analysis.analysis_id, {
        reviewer: reviewer.trim(), decision: "approved", comments: comments.trim() || null,
        accepted_assumptions: assumptions.split("\n").map((item) => item.trim()).filter(Boolean),
        approved_items: selectedItems,
      }));
    } catch (err) { setError(err.message); } finally { setReviewing(false); }
  }

  function getItemIds(nextContract) {
    return (nextContract?.requirements || []).flatMap((item) => [
      item.requirement_id,
      ...(item.business_rules || []).map((_, index) => `${item.requirement_id}:RULE-${index + 1}`),
      ...(item.acceptance_criteria || []).map((criterion, index) => `${item.requirement_id}:${criterion.acceptance_id || `AC-${index + 1}`}`),
      ...(item.expected_outcomes || []).map((_, index) => `${item.requirement_id}:OUTCOME-${index + 1}`),
    ]);
  }

  function toggleItem(id) {
    setSelectedItems((items) => items.includes(id) ? items.filter((item) => item !== id) : [...items, id]);
  }

  function toggleAll(checked) {
    setSelectedItems(checked ? selectableItems.map((item) => item.id) : []);
  }

  function toggleExpanded(id) {
    setExpandedItems((items) => items.includes(id) ? items.filter((item) => item !== id) : [...items, id]);
  }

  function selectionRow(id, title, text) {
    const longText = String(text || "").length > 180;
    const expanded = expandedItems.includes(id);
    return <div className={`selection-item ${selectedItems.includes(id) ? "selected" : ""}`} key={id}>
      <input type="checkbox" checked={selectedItems.includes(id)} onChange={() => toggleItem(id)} />
      <span><b>{title}</b><small className={expanded ? "expanded" : ""}>{text}</small>{longText && <button type="button" className="read-more-button" onClick={(event) => { event.preventDefault(); event.stopPropagation(); toggleExpanded(id); }}>{expanded ? "Show less" : "Read more"}</button>}</span>
    </div>;
  }

  function requirementGroup(item, index) {
    const criteria = criteriaItems.filter((entry) => entry.id.startsWith(`${item.requirement_id}:`));
    const outcomes = outcomeItems.filter((entry) => entry.id.startsWith(`${item.requirement_id}:`));
    const rules = ruleItems.filter((entry) => entry.id.startsWith(`${item.requirement_id}:`));
    return <details className="requirement-group" open={index === 0} key={item.requirement_id}>
      <summary><div className="requirement-group-summary"><input type="checkbox" checked={selectedItems.includes(item.requirement_id)} onClick={(event) => event.stopPropagation()} onChange={() => toggleItem(item.requirement_id)} /><span><b>{item.requirement_id} · {item.title}</b><small>{item.statement}</small></span><em>{selectedItems.includes(item.requirement_id) ? "Selected" : "Not selected"}</em></div></summary>
      <div className="requirement-group-body"><div className="requirement-group-grid"><div><h4>Acceptance criteria</h4>{criteria.length ? criteria.map((entry) => selectionRow(entry.id, entry.title, entry.text)) : <p className="muted">No acceptance criteria extracted.</p>}</div><div><h4>Expected outcomes</h4>{outcomes.length ? outcomes.map((entry) => selectionRow(entry.id, entry.title, entry.text)) : <p className="muted">No expected outcomes extracted.</p>}</div></div><div className="requirement-rules"><h4>Business rules</h4>{rules.length ? rules.map((entry) => selectionRow(entry.id, entry.title, entry.text)) : <p className="muted">No business rules extracted.</p>}</div></div>
    </details>;
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
      {contract?.readiness?.gaps?.length > 0 && <section className="requirements-panel warning-panel"><div className="panel-title-row"><h2><AlertCircle size={18} /> Readiness gaps</h2><span>{contract.readiness.gaps.length} found</span></div><ul>{contract.readiness.gaps.map((gap) => <li key={gap}>{gap}</li>)}</ul></section>}
      <section className="requirements-panel nested-requirements-panel"><div className="panel-title-row"><div><h2>Requirements and related evidence</h2><p className="panel-helper">Expand a requirement to review only the acceptance criteria, rules, and outcomes that belong to it.</p></div><span>{selectedItems.filter((id) => requirements.some((item) => item.requirement_id === id)).length} selected</span></div><div className="selection-actions"><label className="select-all"><input type="checkbox" checked={selectableItems.length > 0 && selectedItems.length === selectableItems.length} onChange={(event) => toggleAll(event.target.checked)} /> Select all</label><button className="text-button" onClick={() => toggleAll(false)}>Clear all</button></div>{requirements.map((item, index) => requirementGroup(item, index))}</section>
      <section className="requirements-panel requirement-scope-panel"><div className="panel-title-row"><div><h2>Requirements</h2></div></div>{requirements.map((item) => selectionRow(item.requirement_id, item.title, item.statement))}</section>
      <section className="requirements-panel business-rules-panel"><div className="panel-title-row"><h2>Business rules</h2></div>{ruleItems.map((item) => selectionRow(item.id, item.title, item.text))}</section>
      {findings.length > 0 && <section className="requirements-panel"><div className="panel-title-row"><h2><AlertCircle size={18} /> Ambiguities and conflicts</h2><span>{findings.length} found</span></div><ul>{findings.map((item, index) => <li key={`${item.text}-${index}`}>{item.text}{item.source_reference && <small>Source: {item.source_reference}</small>}</li>)}</ul></section>}
      <section className="requirements-review-card"><div><div className="eyebrow">HUMAN REVIEW</div><h2>Approve selected items</h2><p>{selectedItems.length} item{selectedItems.length === 1 ? "" : "s"} selected for Test Case Generation. Unchecked items remain available for later review.</p></div><div className="review-fields"><label>Reviewer<input value={reviewer} onChange={(event) => setReviewer(event.target.value)} /></label><label>Accepted assumptions <span>(one per line)</span><textarea value={assumptions} onChange={(event) => setAssumptions(event.target.value)} placeholder="Example: SMS provider response is treated as the observable delivery outcome" /></label><label>Review comments<textarea value={comments} onChange={(event) => setComments(event.target.value)} placeholder="Add review notes" /></label><button className="primary-button" onClick={approve} disabled={reviewing || analysis.status === "approved" || selectedItems.length === 0}>{reviewing ? <><LoaderCircle className="spin" size={16} /> Saving review...</> : <><ShieldCheck size={16} /> {analysis.status === "approved" ? "Approved" : "Approve selected"}</>}</button></div></section>
    </>}
  </div>;
}
