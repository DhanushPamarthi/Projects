import { useEffect, useMemo, useState } from "react";
import { ArrowRight, BookOpen, FileSearch, Play, RefreshCw } from "lucide-react";
import { generateBusinessRequirements, getLatestRequirementAnalyses, listManifests, listProjects, reviewRequirementAnalysis } from "../api/client.js";
import { useActivities } from "../activity/activityStore.js";
import { LoadingSpinner } from "./LoadingSpinner.jsx";

function ruleText(rule) {
  if (typeof rule === "string") return rule;
  return rule?.text || rule?.statement || rule?.description || "";
}

const BUSINESS_PROGRESS_MESSAGES = ["Reading the BRD", "Identifying business rules", "Linking rules to source evidence", "Preparing Business Use Cases for review"];

export function BusinessRequirementsPage({ onContinueToKnowledgeBase }) {
  const [projects, setProjects] = useState([]);
  const [project, setProject] = useState(() => JSON.parse(localStorage.getItem("testiq_current_project") || "null"));
  const [packages, setPackages] = useState([]);
  const [packageId, setPackageId] = useState(() => JSON.parse(localStorage.getItem("testiq_current_package") || "null")?.package_id || "");
  const [analysis, setAnalysis] = useState(null);
  const [packageName, setPackageName] = useState("");
  const [loading, setLoading] = useState(true);
  const [requestRunning, setRequestRunning] = useState(false);
  const [reviewing, setReviewing] = useState(false);
  const [selectedRuleIds, setSelectedRuleIds] = useState([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [progressMessageIndex, setProgressMessageIndex] = useState(0);
  const [progressPercent, setProgressPercent] = useState(0);
  const activities = useActivities();
  const businessActivity = activities.find((item) => item.section === "business-requirements");
  const running = requestRunning || businessActivity?.status === "running";

  useEffect(() => {
    if (!running) return undefined;
    const elapsedSeconds = businessActivity?.startedAt ? Math.max(0, (Date.now() - new Date(businessActivity.startedAt).getTime()) / 1000) : 0;
    setProgressPercent(Math.min(94, Math.max(4, Math.round(4 + elapsedSeconds * 1.5))));
    setProgressMessageIndex(Math.floor(elapsedSeconds / 3) % BUSINESS_PROGRESS_MESSAGES.length);
    const messageTimer = window.setInterval(() => setProgressMessageIndex((index) => (index + 1) % BUSINESS_PROGRESS_MESSAGES.length), 1800);
    const progressTimer = window.setInterval(() => setProgressPercent((value) => Math.min(94, value + Math.max(1, Math.ceil((94 - value) / 10)))), 450);
    return () => { window.clearInterval(messageTimer); window.clearInterval(progressTimer); };
  }, [running, businessActivity?.startedAt]);

  useEffect(() => {
    if (!analysis?.analysis_id) { setSelectedRuleIds([]); return; }
    try {
      const stored = JSON.parse(localStorage.getItem(`testiq_business_rule_selection:${analysis.analysis_id}`) || "[]");
      setSelectedRuleIds(Array.isArray(stored) ? stored : []);
    } catch { setSelectedRuleIds([]); }
  }, [analysis?.analysis_id]);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const items = await listProjects();
        if (cancelled) return;
        setProjects(items);
        const savedProject = JSON.parse(localStorage.getItem("testiq_current_project") || "null");
        const selected = items.find((item) => item.project_id === savedProject?.project_id) || items[0];
        if (!selected) return;
        const selectedProject = selected;
        setProject(selectedProject);
        localStorage.setItem("testiq_current_project", JSON.stringify(selectedProject));
        const packageItems = await listManifests(selectedProject.project_id);
        if (cancelled) return;
        setPackages(packageItems);
        const savedPackage = JSON.parse(localStorage.getItem("testiq_current_package") || "null");
        const selectedPackage = packageItems.find((item) => item.package_id === savedPackage?.package_id) || packageItems[0];
        if (!selectedPackage) return;
        if (cancelled) return;
        setPackageId(selectedPackage.package_id);
        setPackageName(selectedPackage.package_name || selectedPackage.scope || "Resource Package");
        localStorage.setItem("testiq_current_package", JSON.stringify(selectedPackage));
      } catch (err) {
        if (!cancelled) setError(err.message || "Unable to load Business Use Cases.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function loadAnalysis() {
      setAnalysis(null);
      if (!packageId || !packages.some((item) => item.package_id === packageId)) { setPackageName(""); setLoading(false); return; }
      setLoading(true);
      setError("");
      try {
        const selectedPackage = packages.find((item) => item.package_id === packageId);
        if (selectedPackage) setPackageName(selectedPackage.package_name || selectedPackage.scope || "Resource Package");
        const latest = await getLatestRequirementAnalyses(packageId);
        if (!cancelled) setAnalysis(latest.business || null);
      } catch (err) {
        if (!cancelled) setError(err.message || "Unable to load Business Use Cases.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    loadAnalysis();
    return () => { cancelled = true; };
  }, [packageId, packages]);

  async function selectProject(projectId) {
    setAnalysis(null);
    setPackageId("");
    setSelectedRuleIds([]);
    setPackages([]);
    if (!projectId) { setProject(null); setLoading(false); localStorage.removeItem("testiq_current_project"); return; }
    setLoading(true);
    setError("");
    try {
      const selectedProject = projects.find((item) => item.project_id === projectId);
      if (!selectedProject) throw new Error("Selected project was not found.");
      setProject(selectedProject);
      localStorage.setItem("testiq_current_project", JSON.stringify(selectedProject));
      const items = await listManifests(projectId);
      setPackages(items);
      const savedPackage = JSON.parse(localStorage.getItem("testiq_current_package") || "null");
      const selectedPackage = items.find((item) => item.package_id === savedPackage?.package_id) || items[0];
      if (selectedPackage) {
        setPackageId(selectedPackage.package_id);
        localStorage.setItem("testiq_current_package", JSON.stringify(selectedPackage));
      }
    } catch (err) {
      setError(err.message || "Unable to load Resource Packages.");
      setLoading(false);
    }
  }

  function selectPackage(id) {
    setPackageId(id);
    setSelectedRuleIds([]);
    const selected = packages.find((item) => item.package_id === id);
    if (selected) localStorage.setItem("testiq_current_package", JSON.stringify(selected));
  }

  async function runAnalysis() {
    if (!packageId || running) return;
    setProgressMessageIndex(0);
    setProgressPercent(0);
    setRequestRunning(true);
    setError("");
    setNotice("");
    try {
      const next = await generateBusinessRequirements(packageId);
      setAnalysis(next);
      localStorage.removeItem(`testiq_business_rule_selection:${next.analysis_id}`);
      setSelectedRuleIds([]);
      setProgressPercent(100);
      await new Promise((resolve) => window.setTimeout(resolve, 300));
    } catch (err) {
      setError(err.message || "Unable to generate Business Use Cases.");
    } finally {
      setRequestRunning(false);
    }
  }

  function updateRuleSelection(value) {
    setSelectedRuleIds((current) => {
      const next = typeof value === "function" ? value(current) : value;
      if (analysis?.analysis_id) localStorage.setItem(`testiq_business_rule_selection:${analysis.analysis_id}`, JSON.stringify(next));
      return next;
    });
  }

  function toggleRule(id) {
    updateRuleSelection((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id]);
  }

  function renderRuleRow(rule) {
    return <tr key={rule.itemId}><td><input type="checkbox" aria-label={`Select ${rule.id}`} checked={selectedRuleIds.includes(rule.itemId)} onChange={() => toggleRule(rule.itemId)} /></td><td>{rule.id}</td><td>{rule.text}</td><td><span className={`requirements-status ${rule.status}`}>{rule.status.replaceAll("_", " ")}</span></td></tr>;
  }

  async function reviewSelectedRules(status, targetRuleIds = selectedRuleIds) {
    if (!analysis?.analysis_id || targetRuleIds.length === 0 || reviewing) return;
    setReviewing(true);
    setError("");
    setNotice("");
    const contract = analysis.contract || {};
    const approved = new Set(contract.approved_items || []);
    const rejected = new Set(contract.rejected_items || []);
    const selectedLinkedRuleIds = new Set(rules.filter((rule) => targetRuleIds.includes(rule.itemId)).flatMap((rule) => rule.linkedItemIds || [rule.itemId]));
    const items = (contract.requirements || []).flatMap((requirement) => [
      { item_id: requirement.requirement_id, approved: (requirement.review_status || (approved.has(requirement.requirement_id) ? "approved" : rejected.has(requirement.requirement_id) ? "rejected" : "pending")) === "approved", title: requirement.title, statement: requirement.statement, review_status: requirement.review_status || (approved.has(requirement.requirement_id) ? "approved" : rejected.has(requirement.requirement_id) ? "rejected" : "pending") },
      ...(requirement.business_rules || []).map((rule, index) => {
        const ruleId = typeof rule === "object" && rule?.rule_id ? rule.rule_id : `RULE-${index + 1}`;
        const itemId = `${requirement.requirement_id}:${ruleId}`;
        const reviewStatus = selectedLinkedRuleIds.has(itemId) ? status : rule?.review_status || (approved.has(itemId) ? "approved" : rejected.has(itemId) ? "rejected" : "pending");
        return { item_id: itemId, approved: reviewStatus === "approved", text: ruleText(rule), review_status: reviewStatus };
      }),
      ...(requirement.acceptance_criteria || []).map((criterion, index) => {
        const criterionId = typeof criterion === "object" && criterion?.acceptance_id ? criterion.acceptance_id : `AC-${index + 1}`;
        const itemId = `${requirement.requirement_id}:${criterionId}`;
        const reviewStatus = criterion?.review_status || (approved.has(itemId) ? "approved" : rejected.has(itemId) ? "rejected" : "pending");
        return { item_id: itemId, approved: reviewStatus === "approved", text: typeof criterion === "string" ? criterion : criterion.text, scenario: criterion?.scenario, given: criterion?.given, when: criterion?.when, then: criterion?.then, review_status: reviewStatus };
      }),
    ]);
    try {
      const next = await reviewRequirementAnalysis(analysis.analysis_id, {
        reviewer: "Business Use Case Reviewer",
        decision: status === "approved" ? "approved" : "needs_changes",
        approved_items: contract.approved_items || [],
        accepted_assumptions: contract.accepted_assumptions || [],
        items,
      });
      setAnalysis(next);
      localStorage.removeItem(`testiq_business_rule_selection:${analysis.analysis_id}`);
      setSelectedRuleIds([]);
      const counts = next.contract?.knowledge_base_rule_status?.counts;
      setNotice(counts ? `Decisions saved: ${counts.approved || 0} approved, ${counts.rejected || 0} rejected, ${counts.pending || 0} pending. Approved rules are ready for the Knowledge Base.` : "Business Use Case decisions were saved.");
    } catch (err) {
      setError(err.message || "Unable to save Business Use Case decisions.");
    } finally {
      setReviewing(false);
    }
  }

  const rules = useMemo(() => {
    const contract = analysis?.contract || {};
    const approved = new Set(contract.approved_items || []);
    const rejected = new Set(contract.rejected_items || []);
    if (Array.isArray(contract.approved_business_requirements)) {
      return contract.approved_business_requirements.map((item) => ({
        id: item.business_requirement_id,
        itemId: `${item.source_requirement_id || "approved"}:${item.business_requirement_id}`,
        linkedItemIds: [`${item.source_requirement_id || "approved"}:${item.business_requirement_id}`],
        text: item.text,
        status: "approved",
      })).filter((rule) => rule.text);
    }
    const rawRules = (contract.requirements || []).flatMap((requirement) =>
      (requirement.business_rules || []).map((rule, index) => {
        const id = typeof rule === "object" && rule?.rule_id ? rule.rule_id : `RULE-${index + 1}`;
        const itemId = `${requirement.requirement_id}:${id}`;
        return {
          id,
          itemId,
          linkedItemIds: [itemId],
          text: ruleText(rule),
          status: rule?.review_status || (approved.has(itemId) ? "approved" : rejected.has(itemId) ? "rejected" : "pending"),
        };
      }).filter((rule) => rule.text)
    );
    return rawRules;
  }, [analysis]);

  const approvedRuleCount = rules.filter((rule) => rule.status === "approved").length;
  const pendingRuleCount = rules.filter((rule) => rule.status === "pending").length;
  const ruleReviewComplete = approvedRuleCount > 0 && pendingRuleCount === 0;

  return <div className="requirements-page">
    <div className="page-heading"><div><div className="eyebrow">BUSINESS USE CASES</div><h1>Business Use Cases</h1><p>AI extracts business rules from the BRD as Business Use Cases. Review every use case before building the Knowledge Base.</p></div></div>
    <div className="requirements-toolbar"><div><label>Project</label><select value={project?.project_id || ""} onChange={(event) => selectProject(event.target.value)} disabled={running}><option value="">Select project</option>{projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.project_name}</option>)}</select></div><div><label>Resource Package</label><select value={packageId} onChange={(event) => selectPackage(event.target.value)} disabled={!packages.length || running}><option value="">Select Resource Package</option>{packages.map((item) => <option key={item.package_id} value={item.package_id}>{item.package_name || item.scope || "Resource Package"} - v{item.package_version || "?"}</option>)}</select></div>{running ? <section className="generation-progress generation-progress--toolbar" role="status" aria-live="polite"><div className="generation-progress-track" role="progressbar" aria-label="Business use case generation progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow={progressPercent}><span style={{ width: `${progressPercent}%` }} /></div><b className="generation-progress-value">{progressPercent}%</b><div className="generation-progress-copy"><strong>Generating Business Use Cases</strong><span>{BUSINESS_PROGRESS_MESSAGES[progressMessageIndex]}...</span></div></section> : <button type="button" className="primary-button requirements-run-button" onClick={runAnalysis} disabled={!packageId}>{analysis ? <><RefreshCw size={16} /> Regenerate Business Use Cases</> : <><Play size={16} /> Generate Business Use Cases</>}</button>}</div>
    {error && <div className="error-message">{error}</div>}
    {notice && <div className="toast-message">{notice}</div>}
    {loading ? <LoadingSpinner variant="page" label="Loading Business Use Cases" detail="Retrieving the selected Resource Package and its latest generated version." />
      : error ? <section className="requirements-panel"><p className="error-text">{error}</p></section>
      : !analysis ? <section className="requirements-empty"><div className="empty-state-icon"><FileSearch size={25} /></div><h2>No Business Use Cases yet</h2><p>Generate Business Use Cases from the selected Resource Package to begin the review.</p></section>
      : <>
        <section className="requirements-status-card"><div className="requirements-status-icon"><BookOpen size={22} /></div><div><div className="eyebrow">{packageName}</div><h4>Business Use Cases · Version {analysis.analysis_version}</h4><p>{rules.length} business rule{rules.length === 1 ? "" : "s"} extracted from the BRD. Only approved rules are added to the Knowledge Base.</p></div></section>
        {rules.length ? <section className="requirements-panel"><div className="panel-title-row"><div><h2>Business use cases</h2><p className="panel-helper">Select distinct BRD business rules to approve or reject.</p></div><span>{rules.length}</span></div><div className="business-requirements-actions"><label className="select-all"><input type="checkbox" checked={rules.length > 0 && rules.every((rule) => selectedRuleIds.includes(rule.itemId))} onChange={(event) => updateRuleSelection(event.target.checked ? rules.map((rule) => rule.itemId) : [])} /> Select all</label><button type="button" className="review-icon-action approved-action business-review-action" aria-label="Approve selected business use cases" disabled={!selectedRuleIds.length || reviewing} onClick={() => reviewSelectedRules("approved")}>{reviewing ? <LoadingSpinner variant="inline" label="Saving" /> : "Approve selected"}</button><button type="button" className="review-icon-action approved-action business-review-action" aria-label="Approve all business use cases" disabled={!rules.length || reviewing} onClick={() => reviewSelectedRules("approved", rules.map((rule) => rule.itemId))}>Approve all</button><button type="button" className="review-icon-action rejected-action business-review-action" aria-label="Reject selected business use cases" disabled={!selectedRuleIds.length || reviewing} onClick={() => reviewSelectedRules("rejected")}>{reviewing ? <LoadingSpinner variant="inline" label="Saving" /> : "Reject selected"}</button></div><div className="business-requirements-table-wrap"><table className="business-requirements-table"><thead><tr><th aria-label="Select use case" /><th>Use Case ID</th><th>Description</th><th>Status</th></tr></thead><tbody>{rules.map(renderRuleRow)}</tbody></table></div></section>
          : <section className="requirements-empty"><div className="empty-state-icon"><BookOpen size={25} /></div><h2>No business use cases found</h2><p>The latest analysis for this package did not extract any distinct business use cases.</p></section>}
        {rules.length > 0 && <section className="workflow-next-card"><div><div className="eyebrow">NEXT STEP</div><h2>Build the Knowledge Base</h2><p>Complete every rule decision. Approved rules are indexed with source evidence; rejected rules remain auditable but are excluded.</p></div><button type="button" className="primary-button" disabled={!ruleReviewComplete || !onContinueToKnowledgeBase} onClick={() => onContinueToKnowledgeBase?.()}>{ruleReviewComplete ? `Continue with ${approvedRuleCount} approved` : `${pendingRuleCount} decision${pendingRuleCount === 1 ? "" : "s"} remaining`} <ArrowRight size={16} /></button></section>}
      </>}
  </div>;
}
