import { useEffect, useMemo, useState } from "react";
import { Activity, ChevronDown, ChevronUp, RefreshCw } from "lucide-react";
import { getTokenUsage, listManifests, listProjects } from "../api/client.js";

const STEP_LABELS = {
  source_extraction: "BRD and supporting source extraction",
  business_use_cases: "Business Use Cases",
  knowledge_base: "Knowledge Base and RAG retrieval",
  functional_requirements: "Functional Requirements",
  requirements_analysis: "Requirements analysis",
  test_cases: "Test case generation",
  test_data: "Test data generation",
};
const formatCount = (value) => Number(value || 0).toLocaleString();
const formatCost = (value) => new Intl.NumberFormat("en-US", {
  style: "currency", currency: "USD", minimumFractionDigits: 4, maximumFractionDigits: 6,
}).format(Number(value || 0));

function readStored(key) {
  try { return JSON.parse(localStorage.getItem(key) || "null"); } catch { return null; }
}

export function TokenUsagePage() {
  const [projects, setProjects] = useState([]);
  const [projectId, setProjectId] = useState(readStored("testiq_current_project")?.project_id || "");
  const [packages, setPackages] = useState([]);
  const [packageId, setPackageId] = useState(readStored("testiq_current_package")?.package_id || "");
  const [usage, setUsage] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [showDetails, setShowDetails] = useState(false);

  useEffect(() => {
    let active = true;
    listProjects().then((items) => {
      if (!active) return;
      setProjects(items);
      const saved = readStored("testiq_current_project");
      const selected = items.find((item) => item.project_id === projectId) || items.find((item) => item.project_id === saved?.project_id) || items[0];
      setProjectId(selected?.project_id || "");
      if (selected) localStorage.setItem("testiq_current_project", JSON.stringify(selected));
    }).catch((err) => { if (active) setError(err.message || "Could not load projects."); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    let active = true;
    async function loadPackages() {
      if (!projectId) { setPackages([]); setPackageId(""); setUsage(null); return; }
      try {
        const items = await listManifests(projectId, { cache: "no-store" });
        if (!active) return;
        setPackages(items);
        const saved = readStored("testiq_current_package");
        const selected = items.find((item) => item.package_id === packageId) || items.find((item) => item.package_id === saved?.package_id);
        setPackageId(selected?.package_id || "");
        if (selected) localStorage.setItem("testiq_current_package", JSON.stringify(selected));
      } catch (err) { if (active) setError(err.message || "Could not load Resource Packages."); }
    }
    loadPackages();
    return () => { active = false; };
  }, [projectId]);

  useEffect(() => {
    if (!projectId) return undefined;
    let active = true;
    async function refresh() {
      try {
        const result = await getTokenUsage({ projectId, packageId: packageId || undefined });
        if (active) { setUsage(result); setError(""); }
      } catch (err) { if (active) setError(err.message || "Could not load token usage."); }
      finally { if (active) setLoading(false); }
    }
    refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, [projectId, packageId]);

  const totals = usage?.totals || {};
  const steps = useMemo(() => usage?.steps || [], [usage]);

  function selectProject(id) {
    setProjectId(id);
    setUsage(null);
    const selected = projects.find((item) => item.project_id === id);
    if (selected) localStorage.setItem("testiq_current_project", JSON.stringify(selected));
    else localStorage.removeItem("testiq_current_project");
    localStorage.removeItem("testiq_current_package");
    setPackageId("");
  }

  function selectPackage(id) {
    setPackageId(id);
    const selected = packages.find((item) => item.package_id === id);
    if (selected) localStorage.setItem("testiq_current_package", JSON.stringify(selected));
    else localStorage.removeItem("testiq_current_package");
  }

  async function refreshNow() {
    if (!projectId) return;
    setRefreshing(true);
    try { setUsage(await getTokenUsage({ projectId, packageId: packageId || undefined })); setError(""); }
    catch (err) { setError(err.message || "Could not load token usage."); }
    finally { setRefreshing(false); setLoading(false); }
  }

  return <div className="token-usage-page">
    <div className="page-heading"><div><div className="eyebrow">WORKSPACE TOOLS</div><h1>LLM Token Usage</h1><p>Review input and output tokens reported by the provider for each TestIQ workflow step.</p></div><button type="button" className="secondary-button" onClick={refreshNow} disabled={!projectId || refreshing}>{refreshing ? <RefreshCw className="spin" size={15} /> : <RefreshCw size={15} />} Refresh</button></div>
    <section className="token-usage-filters"><label><span>Project</span><select value={projectId} onChange={(event) => selectProject(event.target.value)}><option value="">Select project</option>{projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.project_name}</option>)}</select></label><label><span>Resource Package</span><select value={packageId} onChange={(event) => selectPackage(event.target.value)} disabled={!projectId}><option value="">All packages</option>{packages.map((item) => <option key={item.package_id} value={item.package_id}>v{item.package_version} · {item.package_name || item.scope || "Resource Package"}</option>)}</select></label></section>
    {error && <div className="error-message">{error}</div>}
    {!projectId && !loading ? <section className="token-usage-empty-state"><Activity size={23} /><h2>Select a project</h2><p>Choose a project to view its recorded LLM token usage.</p></section> : <>
      <section className="token-usage-summary"><div><span>Input tokens</span><strong>{formatCount(totals.input_tokens)}</strong></div><div><span>Output tokens</span><strong>{formatCount(totals.output_tokens)}</strong></div><div><span>Total tokens</span><strong>{formatCount(totals.total_tokens)}</strong></div><div><span>API calls</span><strong>{formatCount(totals.calls)}</strong></div><div><span>Estimated cost</span><strong>{formatCost(totals.estimated_cost_usd)}</strong></div></section>
      <section className="token-usage-table-card"><div className="token-usage-table-heading"><h2>Usage by workflow step</h2><button type="button" className="secondary-button token-usage-details-toggle" onClick={() => setShowDetails((value) => !value)} aria-expanded={showDetails} aria-controls="token-usage-details">{showDetails ? <ChevronUp size={16} /> : <ChevronDown size={16} />}{showDetails ? "Hide details" : "Show details"}</button></div>
        {showDetails && <div id="token-usage-details">{totals.cost_pricing_complete === false && <p className="token-usage-pricing-note">Some calls use an unpriced model and are excluded from the estimate.</p>}{steps.length ? <div className="token-usage-table-wrap"><table className="token-usage-table"><thead><tr><th>Step</th><th>Input tokens</th><th>Output tokens</th><th>Total tokens</th><th>Estimated cost</th><th>Calls</th><th>Cached input</th></tr></thead><tbody>{steps.map((step) => <tr key={step.stage}><td><strong>{STEP_LABELS[step.stage] || step.stage}</strong></td><td>{formatCount(step.input_tokens)}</td><td>{formatCount(step.output_tokens)}</td><td><strong>{formatCount(step.total_tokens)}</strong></td><td><strong>{formatCost(step.estimated_cost_usd)}</strong>{step.unpriced_calls > 0 && <small>{formatCount(step.unpriced_calls)} call(s) not priced</small>}</td><td>{formatCount(step.calls)}</td><td>{formatCount(step.cached_input_tokens)}</td></tr>)}</tbody></table></div> : <div className="token-usage-empty-state"><Activity size={23} /><h2>No token usage recorded yet</h2><p>Usage will appear here after an AI-powered workflow step makes a model request.</p></div>}</div>}
      </section>
    </>}
  </div>;
}
