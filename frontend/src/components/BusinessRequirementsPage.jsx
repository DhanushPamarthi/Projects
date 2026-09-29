import { useEffect, useMemo, useState } from "react";
import { BookOpen, FileSearch, LoaderCircle, Play } from "lucide-react";
import { analyzeRequirements, getProject, listManifests, listProjects, listRequirementAnalyses, reviewRequirementAnalysis } from "../api/client.js";

function ruleText(rule) {
  if (typeof rule === "string") return rule;
  return rule?.text || rule?.statement || rule?.description || "";
}

export function BusinessRequirementsPage() {
  const [projects, setProjects] = useState([]);
  const [project, setProject] = useState(() => JSON.parse(localStorage.getItem("testiq_current_project") || "null"));
  const [packages, setPackages] = useState([]);
  const [packageId, setPackageId] = useState(() => JSON.parse(localStorage.getItem("testiq_current_package") || "null")?.package_id || "");
  const [analysis, setAnalysis] = useState(null);
  const [packageName, setPackageName] = useState("");
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [reviewing, setReviewing] = useState(false);
  const [selectedRuleIds, setSelectedRuleIds] = useState([]);
  const [error, setError] = useState("");

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
        const selectedProject = await getProject(selected.project_id);
        if (cancelled) return;
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
        setPackageName(selectedPackage.package_name || selectedPackage.scope || "Source package");
        localStorage.setItem("testiq_current_package", JSON.stringify(selectedPackage));
        const analyses = await listRequirementAnalyses(selectedPackage.package_id);
        const businessAnalysis = analyses.find((item) => !item.contract?.functional_requirements_generation) || analyses[0] || null;
        if (!cancelled) setAnalysis(businessAnalysis);
      } catch (err) {
        if (!cancelled) setError(err.message || "Unable to load business requirements.");
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
      if (!packageId) { setPackageName(""); setLoading(false); return; }
      setLoading(true);
      setError("");
      try {
        const selectedPackage = packages.find((item) => item.package_id === packageId);
        if (selectedPackage) setPackageName(selectedPackage.package_name || selectedPackage.scope || "Source package");
        const analyses = await listRequirementAnalyses(packageId);
        const businessAnalysis = analyses.find((item) => !item.contract?.functional_requirements_generation) || analyses[0] || null;
        if (!cancelled) setAnalysis(businessAnalysis);
      } catch (err) {
        if (!cancelled) setError(err.message || "Unable to load business requirements.");
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
      const selectedProject = await getProject(projectId);
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
      setError(err.message || "Unable to load source packages.");
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
    setRunning(true);
    setError("");
    try {
      const next = await analyzeRequirements(packageId);
      setAnalysis(next);
      setSelectedRuleIds([]);
    } catch (err) {
      setError(err.message || "Unable to generate Business Requirements.");
    } finally {
      setRunning(false);
    }
  }

  function toggleRule(id) {
    setSelectedRuleIds((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id]);
  }

  async function reviewSelectedRules(status) {
    if (!analysis?.analysis_id || selectedRuleIds.length === 0 || reviewing) return;
    setReviewing(true);
    setError("");
    const contract = analysis.contract || {};
    const approved = new Set(contract.approved_items || []);
    const rejected = new Set(contract.rejected_items || []);
    const items = (contract.requirements || []).flatMap((requirement) => [
      { item_id: requirement.requirement_id, approved: (requirement.review_status || (approved.has(requirement.requirement_id) ? "approved" : rejected.has(requirement.requirement_id) ? "rejected" : "pending")) === "approved", title: requirement.title, statement: requirement.statement, review_status: requirement.review_status || (approved.has(requirement.requirement_id) ? "approved" : rejected.has(requirement.requirement_id) ? "rejected" : "pending") },
      ...(requirement.business_rules || []).map((rule, index) => {
        const ruleId = typeof rule === "object" && rule?.rule_id ? rule.rule_id : `RULE-${index + 1}`;
        const itemId = `${requirement.requirement_id}:${ruleId}`;
        const reviewStatus = selectedRuleIds.includes(itemId) ? status : rule?.review_status || (approved.has(itemId) ? "approved" : rejected.has(itemId) ? "rejected" : "pending");
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
        reviewer: "Business Requirements Reviewer",
        decision: status === "approved" ? "approved" : "needs_changes",
        approved_items: contract.approved_items || [],
        accepted_assumptions: contract.accepted_assumptions || [],
        items,
      });
      setAnalysis(next);
      setSelectedRuleIds([]);
    } catch (err) {
      setError(err.message || "Unable to save business requirement review.");
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
        text: item.text,
        status: "approved",
      })).filter((rule) => rule.text);
    }
    return (contract.requirements || []).flatMap((requirement) =>
      (requirement.business_rules || []).map((rule, index) => {
        const id = typeof rule === "object" && rule?.rule_id ? rule.rule_id : `RULE-${index + 1}`;
        const itemId = `${requirement.requirement_id}:${id}`;
        return {
          id,
          itemId,
          text: ruleText(rule),
          status: rule?.review_status || (approved.has(itemId) ? "approved" : rejected.has(itemId) ? "rejected" : "pending"),
        };
      }).filter((rule) => rule.text)
    );
  }, [analysis]);

  return <div className="requirements-page">
    <div className="page-heading"><div><div className="eyebrow">BRD ANALYSIS</div><h1>Business Requirements</h1><p>Generate business rules from the source package, then approve them before Functional Requirements are generated.</p></div></div>
    <div className="requirements-toolbar"><div><label>Project</label><select value={project?.project_id || ""} onChange={(event) => selectProject(event.target.value)}><option value="">Select project</option>{projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.project_name}</option>)}</select></div><div><label>Source package</label><select value={packageId} onChange={(event) => selectPackage(event.target.value)} disabled={!packages.length}><option value="">Select package</option>{packages.map((item) => <option key={item.package_id} value={item.package_id}>{item.package_name || item.scope || "Source package"} - v{item.package_version || "?"}</option>)}</select></div><button type="button" className="primary-button requirements-run-button" onClick={runAnalysis} disabled={!packageId || running}>{running ? <><LoaderCircle className="spin" size={16} /> Generating...</> : <><Play size={16} /> Generate Business Requirements</>}</button></div>
    {error && <div className="error-message">{error}</div>}
    {loading ? <section className="requirements-panel"><LoaderCircle className="spin" size={18} /> Loading business requirements...</section>
      : error ? <section className="requirements-panel"><p className="error-text">{error}</p></section>
      : !analysis ? <section className="requirements-empty"><div className="empty-state-icon"><FileSearch size={25} /></div><h2>No Business Requirements yet</h2><p>Generate Business Requirements from the selected source package to begin review.</p></section>
      : <>
        <section className="requirements-status-card"><div className="requirements-status-icon"><BookOpen size={22} /></div><div><div className="eyebrow">{packageName}</div><h4>Business Requirements · Analysis version {analysis.analysis_version}</h4><p>{rules.length} business requirement{rules.length === 1 ? "" : "s"} ready for review</p></div></section>
        {rules.length ? <section className="requirements-panel"><div className="panel-title-row"><div><h2>Business requirements</h2><p className="panel-helper">Select requirements to approve or reject.</p></div><span>{rules.length}</span></div><div className="business-requirements-actions"><label className="select-all"><input type="checkbox" checked={rules.length > 0 && selectedRuleIds.length === rules.length} onChange={(event) => setSelectedRuleIds(event.target.checked ? rules.map((rule) => rule.itemId) : [])} /> Select all</label><button type="button" className="review-icon-action approved-action business-review-action" aria-label="Approve selected business requirements" disabled={!selectedRuleIds.length || reviewing} onClick={() => reviewSelectedRules("approved")}>{reviewing ? <LoaderCircle className="spin" size={16} /> : "Approve"}</button><button type="button" className="review-icon-action rejected-action business-review-action" aria-label="Reject selected business requirements" disabled={!selectedRuleIds.length || reviewing} onClick={() => reviewSelectedRules("rejected")}>{reviewing ? <LoaderCircle className="spin" size={16} /> : "Reject"}</button></div><div className="business-requirements-table-wrap"><table className="business-requirements-table"><thead><tr><th aria-label="Select" /><th>BRD ID</th><th>Description</th><th>Status</th></tr></thead><tbody>{rules.map((rule) => <tr key={rule.itemId}><td><input type="checkbox" aria-label={`Select ${rule.id}`} checked={selectedRuleIds.includes(rule.itemId)} onChange={() => toggleRule(rule.itemId)} /></td><td>{rule.id}</td><td>{rule.text}</td><td><span className={`requirements-status ${rule.status}`}>{rule.status.replaceAll("_", " ")}</span></td></tr>)}</tbody></table></div></section>
          : <section className="requirements-empty"><div className="empty-state-icon"><BookOpen size={25} /></div><h2>No business rules found</h2><p>The latest analysis for this package did not extract any business rules.</p></section>}
      </>}
  </div>;
}
