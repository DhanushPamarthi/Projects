import { useEffect, useMemo, useState } from "react";
import { Activity, BarChart3, CheckCircle2, ChevronDown, ChevronLeft, ChevronRight, ChevronUp, CircleDollarSign, Coins, Gauge, Layers3, RefreshCw, Sparkles, Zap } from "lucide-react";
import { getTokenUsage, listManifests, listProjects } from "../api/client.js";
import { AppSelect } from "./AppSelect.jsx";

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
const formatDateTime = (value) => value ? new Date(value).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "Not Available";

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
  const [showCycleHistory, setShowCycleHistory] = useState(true);
  const [cyclePage, setCyclePage] = useState(1);
  const [updatedAt, setUpdatedAt] = useState(null);

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
        if (active) { setUsage(result); setUpdatedAt(new Date()); setError(""); }
      } catch (err) { if (active) setError(err.message || "Could not load token usage."); }
      finally { if (active) setLoading(false); }
    }
    refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, [projectId, packageId]);

  const totals = usage?.totals || {};
  const steps = useMemo(() => usage?.steps || [], [usage]);
  const cycles = useMemo(() => usage?.cycles || [], [usage]);
  const cycleSummary = usage?.cycle_summary || {};

  function selectProject(id) {
    setProjectId(id);
    setUsage(null);
    const selected = projects.find((item) => item.project_id === id);
    if (selected) localStorage.setItem("testiq_current_project", JSON.stringify(selected));
    else localStorage.removeItem("testiq_current_project");
    localStorage.removeItem("testiq_current_package");
    setPackageId("");
    setCyclePage(1);
  }

  function selectPackage(id) {
    setPackageId(id);
    const selected = packages.find((item) => item.package_id === id);
    if (selected) localStorage.setItem("testiq_current_package", JSON.stringify(selected));
    else localStorage.removeItem("testiq_current_package");
    setCyclePage(1);
  }

  async function refreshNow() {
    if (!projectId) return;
    setRefreshing(true);
    try { setUsage(await getTokenUsage({ projectId, packageId: packageId || undefined })); setUpdatedAt(new Date()); setError(""); }
    catch (err) { setError(err.message || "Could not load token usage."); }
    finally { setRefreshing(false); setLoading(false); }
  }

  const totalTokens = Number(totals.total_tokens || 0);
  const inputTokens = Number(totals.input_tokens || 0);
  const outputTokens = Number(totals.output_tokens || 0);
  const inputShare = totalTokens ? Math.round((inputTokens / totalTokens) * 100) : 0;
  const outputShare = totalTokens ? Math.round((outputTokens / totalTokens) * 100) : 0;
  const averageTokens = Number(totals.calls || 0) ? Math.round(totalTokens / Number(totals.calls)) : 0;
  const costPerMillion = totalTokens ? (Number(totals.estimated_cost_usd || 0) / totalTokens) * 1_000_000 : 0;
  const maxStepTokens = Math.max(...steps.map((step) => Number(step.total_tokens || 0)), 1);
  const selectedProject = projects.find((item) => item.project_id === projectId);
  const selectedPackage = packages.find((item) => item.package_id === packageId);
  const cyclePageSize = 5;
  const cyclePageCount = Math.max(1, Math.ceil(cycles.length / cyclePageSize));
  const currentCyclePage = Math.min(cyclePage, cyclePageCount);
  const visibleCycles = cycles.slice((currentCyclePage - 1) * cyclePageSize, currentCyclePage * cyclePageSize);

  return <div className="token-usage-page">
    <section className="token-usage-hero">
      <div className="token-usage-hero-top"><div><div className="token-usage-hero-meta"><span className="token-usage-product-label"><Sparkles size={12} /> TestIQ AI FinOps</span><div className="eyebrow">Model Consumption &amp; Cost Governance</div></div><h1>LLM Usage Intelligence</h1><p>Track token usage and cost across each delivery cycle.</p></div><div className="token-usage-live"><span><i /> Live Metric</span><small>{updatedAt ? `Updated ${updatedAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : "Waiting for Usage Data"}</small><button type="button" onClick={refreshNow} disabled={!projectId || refreshing}>{<RefreshCw className={refreshing ? "spin" : ""} size={15} />} Refresh Data</button></div></div>
      <div className="token-usage-hero-lower"><div className="token-usage-cost-spotlight"><span><CircleDollarSign size={16} /> Estimated model spend</span><strong>{formatCost(totals.estimated_cost_usd)}</strong><small>{packageId ? "Selected Resource Package" : "All packages in this project"}</small></div><div className="token-usage-filters"><label><span>Project</span><AppSelect value={projectId} onChange={(event) => selectProject(event.target.value)}><option value="">Select project</option>{projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.project_name}</option>)}</AppSelect></label><label><span>Resource Package</span><AppSelect value={packageId} onChange={(event) => selectPackage(event.target.value)} disabled={!projectId}><option value="">All packages</option>{packages.map((item) => <option key={item.package_id} value={item.package_id}>v{item.package_version} · {item.package_name || item.scope || "Resource Package"}</option>)}</AppSelect></label></div></div>
    </section>
    {error && <div className="error-message">{error}</div>}
    {!projectId && !loading ? <section className="token-usage-empty-state"><Activity size={23} /><h2>Select a project</h2><p>Choose a project to view its recorded LLM token usage.</p></section> : <>
      <section className="token-usage-summary"><article><span className="token-kpi-icon coral"><Layers3 size={17} /></span><div><span>Total Tokens</span><strong>{formatCount(totalTokens)}</strong><small>Across The Selected Scope</small></div></article><article><span className="token-kpi-icon blue"><Zap size={17} /></span><div><span>Model Requests</span><strong>{formatCount(totals.calls)}</strong><small>{formatCount(averageTokens)} Tokens Per Request</small></div></article><article><span className="token-kpi-icon violet"><Gauge size={17} /></span><div><span>Completed Cycles</span><strong>{formatCount(cycleSummary.completed_cycles)}</strong><small>{formatCount(cycleSummary.average_tokens_per_cycle)} Average Tokens</small></div></article><article><span className="token-kpi-icon green"><BarChart3 size={17} /></span><div><span>Average Cycle Cost</span><strong>{formatCost(cycleSummary.average_cost_per_cycle_usd)}</strong><small>{formatCount(cycleSummary.cycle_count)} Recorded Cycles</small></div></article></section>
      <section className="token-usage-insights"><article className="token-usage-card"><div className="token-card-heading"><div><span>Consumption Mix</span><h2>Input Vs. Generated Output</h2></div><Coins size={19} /></div><div className="token-mix-total"><strong>{formatCount(totalTokens)}</strong><span>Total Tokens</span></div><div className="token-mix-bar" aria-label={`${inputShare}% input tokens and ${outputShare}% output tokens`}><span style={{ width: `${inputShare}%` }} /><i style={{ width: `${outputShare}%` }} /></div><div className="token-mix-legend"><div><span className="input-dot" /><p>Input Tokens<strong>{formatCount(inputTokens)}</strong><small>{inputShare}% Of Consumption</small></p></div><div><span className="output-dot" /><p>Output Tokens<strong>{formatCount(outputTokens)}</strong><small>{outputShare}% Of Consumption</small></p></div></div></article>
        <article className="token-usage-card token-efficiency-card"><div className="token-card-heading"><div><span>Efficiency Signals</span><h2>Commercial Context</h2></div><Gauge size={19} /></div><dl><div><dt>Cost Per 1M Tokens</dt><dd>{formatCost(costPerMillion)}</dd></div><div><dt>Average Request Size</dt><dd>{formatCount(averageTokens)} <small>Tokens</small></dd></div><div><dt>Current Scope</dt><dd title={selectedPackage?.package_name || selectedProject?.project_name}>{selectedPackage ? `Package v${selectedPackage.package_version}` : selectedProject?.project_name || "Project"}</dd></div></dl>{totals.cost_pricing_complete === false && <p className="token-usage-pricing-note">Estimate Excludes Calls Made With Unpriced Models.</p>}</article></section>
      <section className={`token-cycle-history-card${showCycleHistory ? " is-expanded" : ""}`}>
        <div className="token-cycle-history-heading">
          <div className="token-cycle-heading-copy"><span>Cycle Economics</span><h2>End-To-End Cycle History</h2><p>Tokens and estimated cost for every recorded project delivery cycle.</p></div>
          <div className="token-cycle-heading-actions"><div className="token-cycle-summary"><span><CheckCircle2 size={14} /> {formatCount(cycleSummary.completed_cycles)} Complete</span><strong>{formatCost(cycleSummary.average_cost_per_cycle_usd)} Average</strong></div><button type="button" className="token-cycle-collapse-button" onClick={() => setShowCycleHistory((value) => !value)} aria-expanded={showCycleHistory} aria-controls="token-cycle-history-content" aria-label={showCycleHistory ? "Collapse End-To-End Cycle History" : "Expand End-To-End Cycle History"}>{showCycleHistory ? <ChevronUp size={19} /> : <ChevronDown size={19} />}</button></div>
        </div>
        {showCycleHistory && <div id="token-cycle-history-content" className="token-cycle-history-content">{visibleCycles.length ? <div className="token-cycle-table-wrap"><table className="token-cycle-table"><thead><tr><th>Cycle</th><th>Resource Package</th><th>Status</th><th>Workflow Coverage</th><th>Requests</th><th>Total Tokens</th><th>Estimated Cost</th><th>Last Activity</th></tr></thead><tbody>{visibleCycles.map((cycle, index) => <tr key={cycle.cycle_id}><td><div className="token-cycle-identity"><span>{String((currentCyclePage - 1) * cyclePageSize + index + 1).padStart(2, "0")}</span><div><strong>Cycle {String((currentCyclePage - 1) * cyclePageSize + index + 1).padStart(2, "0")}</strong><small>{cycle.generation_id}</small></div></div></td><td><strong>Version {cycle.package_version}</strong><small>{cycle.package_name}</small></td><td><span className={`token-cycle-status ${cycle.lifecycle_status}`}>{cycle.lifecycle_status === "complete" ? "Complete" : "Test Design Complete"}</span></td><td><div className="token-cycle-stages">{(cycle.stages || []).map((item) => <span key={item.stage}>{STEP_LABELS[item.stage] || item.stage}</span>)}</div></td><td><span className="token-cycle-metric">{formatCount(cycle.calls)}</span></td><td><strong className="token-cycle-metric primary">{formatCount(cycle.total_tokens)}</strong></td><td><strong className="token-cycle-metric cost">{formatCost(cycle.estimated_cost_usd)}</strong>{!cycle.cost_pricing_complete && <small>Partial Estimate</small>}</td><td><span className="token-cycle-date">{formatDateTime(cycle.completed_at || cycle.updated_at)}</span></td></tr>)}</tbody></table></div> : <div className="token-usage-empty-state compact"><Activity size={23} /><h2>No Cycle History Available</h2><p>A cycle appears after test cases are generated for the selected project.</p></div>}{cycles.length > cyclePageSize && <nav className="app-pagination token-cycle-pagination" aria-label="Token usage cycle history pages"><span>Showing {(currentCyclePage - 1) * cyclePageSize + 1}–{Math.min(currentCyclePage * cyclePageSize, cycles.length)} Of {cycles.length}</span><div><button type="button" aria-label="Previous Cycle Page" disabled={currentCyclePage === 1} onClick={() => setCyclePage(currentCyclePage - 1)}><ChevronLeft size={15} /></button><span>Page {currentCyclePage} Of {cyclePageCount}</span><button type="button" aria-label="Next Cycle Page" disabled={currentCyclePage === cyclePageCount} onClick={() => setCyclePage(currentCyclePage + 1)}><ChevronRight size={15} /></button></div></nav>}</div>}
      </section>
      <section className="token-usage-table-card"><div className="token-usage-table-heading"><div><span>Workflow Attribution</span><h2>Usage By Process.BRD Step</h2><p>See where model capacity is being invested across the delivery lifecycle.</p></div><button type="button" className="secondary-button token-usage-details-toggle" onClick={() => setShowDetails((value) => !value)} aria-expanded={showDetails} aria-controls="token-usage-details">{showDetails ? <ChevronUp size={16} /> : <ChevronDown size={16} />}{showDetails ? "Hide Ledger" : "View Detailed Ledger"}</button></div>
        {steps.length ? <div className="token-stage-list">{steps.map((step) => <article key={step.stage}><div className="token-stage-identity"><span>{String(steps.indexOf(step) + 1).padStart(2, "0")}</span><div><strong>{STEP_LABELS[step.stage] || step.stage}</strong><small>{formatCount(step.calls)} request{Number(step.calls) === 1 ? "" : "s"}</small></div></div><div className="token-stage-track"><span style={{ width: `${(Number(step.total_tokens || 0) / maxStepTokens) * 100}%` }} /></div><div className="token-stage-value"><strong>{formatCount(step.total_tokens)}</strong><small>{formatCost(step.estimated_cost_usd)}</small></div></article>)}</div> : <div className="token-usage-empty-state compact"><Activity size={23} /><h2>No token usage recorded yet</h2><p>Usage will appear after an AI-powered workflow step makes a model request.</p></div>}
        {showDetails && <div id="token-usage-details">{totals.cost_pricing_complete === false && <p className="token-usage-pricing-note">Some calls use an unpriced model and are excluded from the estimate.</p>}{steps.length ? <div className="token-usage-table-wrap"><table className="token-usage-table"><thead><tr><th>Step</th><th>Input tokens</th><th>Output tokens</th><th>Total tokens</th><th>Estimated cost</th><th>Calls</th><th>Cached input</th></tr></thead><tbody>{steps.map((step) => <tr key={step.stage}><td><strong>{STEP_LABELS[step.stage] || step.stage}</strong></td><td>{formatCount(step.input_tokens)}</td><td>{formatCount(step.output_tokens)}</td><td><strong>{formatCount(step.total_tokens)}</strong></td><td><strong>{formatCost(step.estimated_cost_usd)}</strong>{step.unpriced_calls > 0 && <small>{formatCount(step.unpriced_calls)} call(s) not priced</small>}</td><td>{formatCount(step.calls)}</td><td>{formatCount(step.cached_input_tokens)}</td></tr>)}</tbody></table></div> : <div className="token-usage-empty-state"><Activity size={23} /><h2>No token usage recorded yet</h2><p>Usage will appear here after an AI-powered workflow step makes a model request.</p></div>}</div>}
      </section>
    </>}
  </div>;
}
