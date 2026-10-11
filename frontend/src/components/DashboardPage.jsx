import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, ArrowRight, BookOpen, CheckCircle2, CircleDashed, Database, FileCheck2, FileStack, PlayCircle, RefreshCw, Route, ShieldCheck, Sparkles, Target } from "lucide-react";
import { getProjectDashboard, listProjects } from "../api/client.js";
import { LoadingSpinner } from "./LoadingSpinner.jsx";
import { AppSelect } from "./AppSelect.jsx";

function stored(key) { try { return JSON.parse(localStorage.getItem(key) || "null"); } catch { return null; } }
function dateLabel(value) { return value ? new Date(value).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "Not available"; }
const statusLabels = { delivered: "Delivered", on_track: "In progress", in_progress: "In progress", at_risk: "Needs attention" };
const workflowDestinations = {
  "Source documents": "sources",
  "Business use cases": "business-requirements",
  "Knowledge Base": "knowledge-base",
  "Functional Requirements": "requirements",
  "Test cases": "test-cases",
  "Test data": "test-data",
  "Test results": "test-results",
};

export function DashboardPage({ onNavigate }) {
  const [project, setProject] = useState(stored("testiq_current_project"));
  const [projects, setProjects] = useState([]);
  const [data, setData] = useState(null);
  const [packageId, setPackageId] = useState(stored("testiq_current_package")?.package_id || "");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async (projectId, preferredPackageId = "") => {
    if (!projectId) { setData(null); return; }
    setLoading(true);
    try {
      const result = await getProjectDashboard(projectId);
      setData(result);
      const nextId = result.packages.some((item) => item.package_id === preferredPackageId) ? preferredPackageId : result.packages[0]?.package_id || "";
      setPackageId(nextId);
      const selected = result.packages.find((item) => item.package_id === nextId);
      if (selected) localStorage.setItem("testiq_current_package", JSON.stringify(selected));
      else localStorage.removeItem("testiq_current_package");
      setError("");
    } catch (err) { setError(err.message || "Unable to load dashboard"); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => {
    let alive = true;
    listProjects().then((items) => {
      if (!alive) return;
      setProjects(items);
      const savedProject = stored("testiq_current_project");
      const chosen = items.find((item) => item.project_id === savedProject?.project_id) || items[0] || null;
      setProject(chosen);
      if (chosen) {
        localStorage.setItem("testiq_current_project", JSON.stringify(chosen));
        load(chosen.project_id, stored("testiq_current_package")?.package_id || "");
      } else setData(null);
    }).catch((err) => { if (alive) setError(err.message || "Unable to load projects"); });
    return () => { alive = false; };
  }, [load]);

  const summary = useMemo(() => data?.packages.find((item) => item.package_id === packageId) || null, [data, packageId]);
  function chooseProject(id) {
    const selected = projects.find((item) => item.project_id === id) || null;
    setProject(selected); setPackageId(""); setData(null);
    if (selected) { localStorage.setItem("testiq_current_project", JSON.stringify(selected)); localStorage.removeItem("testiq_current_package"); load(selected.project_id, ""); }
  }
  function choosePackage(id) {
    setPackageId(id);
    const selected = data?.packages.find((item) => item.package_id === id);
    if (selected) localStorage.setItem("testiq_current_package", JSON.stringify(selected));
  }

  const workflow = useMemo(() => summary ? [
    { label: "Source documents", detail: `${summary.counts.sources} captured`, done: summary.counts.sources > 0 },
    { label: "Business use cases", detail: summary.counts.business_rules ? `${summary.counts.approved_business_rules}/${summary.counts.business_rules} approved` : "Waiting", done: summary.counts.business_rules > 0 && summary.counts.approved_business_rules > 0 && summary.counts.pending_business_rules === 0 },
    { label: "Knowledge Base", detail: summary.knowledge_base?.chunk_count ? `${summary.knowledge_base.chunk_count} chunks indexed` : "Not indexed", done: ["ready", "succeeded"].includes(summary.workflow.knowledge_base) },
    { label: "Functional Requirements", detail: `${summary.counts.approved_requirements}/${summary.counts.requirements} auto-approved`, done: summary.counts.requirements > 0 && summary.counts.approved_requirements === summary.counts.requirements },
    { label: "Test cases", detail: `${summary.counts.approved_test_cases}/${summary.counts.test_cases} approved`, done: summary.counts.approved_test_cases > 0 },
    { label: "Test data", detail: `${summary.counts.test_data_records} records`, done: summary.counts.test_data_records > 0 },
    { label: "Test results", detail: `${summary.counts.test_runs} runs`, done: summary.counts.test_runs > 0 },
  ] : [], [summary]);
  const completedStages = workflow.filter((stage) => stage.done).length;
  const processPercent = workflow.length ? Math.round((completedStages / workflow.length) * 100) : 0;
  const nextStage = workflow.find((stage) => !stage.done) || workflow.at(-1);

  return <div className="dashboard-page">
    <section className="dashboard-titlebar"><div className="dashboard-titlebar-copy"><div className="dashboard-titlebar-meta"><div className="dashboard-product-mark"><Sparkles size={12} /> TestIQ · Process.BRD</div><div className="eyebrow">DELIVERY CONTROL CENTER</div></div><h1>Quality Delivery Intelligence</h1><div className="dashboard-audience-cues" aria-label="Dashboard focus areas"><span>Delivery health</span><span>Engineering traceability</span><span>Test readiness</span></div></div><div className="dashboard-titlebar-actions"><div className="dashboard-controls"><label><span>Project</span><AppSelect value={project?.project_id || ""} onChange={(event) => chooseProject(event.target.value)}><option value="">Choose project</option>{projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.project_name}</option>)}</AppSelect></label><label><span>Resource Package</span><AppSelect value={packageId} onChange={(event) => choosePackage(event.target.value)} disabled={!data?.packages.length}><option value="">Choose package</option>{(data?.packages || []).map((item) => <option key={item.package_id} value={item.package_id}>v{item.package_version} · {item.package_name || item.scope || "Resource Package"}</option>)}</AppSelect></label></div><button type="button" className="dashboard-refresh" aria-label="Refresh dashboard" title="Refresh dashboard" onClick={() => load(project?.project_id, packageId)} disabled={loading}><RefreshCw className={loading ? "spin" : ""} size={16} /></button></div></section>
    {error && <div className="error-message">Unable to load dashboard: {error}</div>}
    {loading && !data && <LoadingSpinner variant="section" label="Loading executive summary" detail="Collecting the selected package's latest workflow results." />}
    {!loading && !error && !data?.packages.length && <section className="dashboard-empty"><div className="empty-state-icon"><FileStack size={24} /></div><h2>No package summaries yet</h2><p>Create a Resource Package from uploaded inputs to generate its own dashboard summary.</p><button type="button" className="primary-button" onClick={() => onNavigate("sources")}>Upload sources</button></section>}
    {summary && <>
      <section className={`dashboard-hero ${summary.status}`}><div className="dashboard-hero-copy"><span className="dashboard-status"><span />{statusLabels[summary.status] || summary.status}</span><h2>{data.project.project_name}</h2><p>{summary.package_name || `Resource Package v${summary.package_version}`} · Version {summary.package_version} · Updated {dateLabel(summary.updated_at)}</p><button type="button" className="dashboard-hero-action" onClick={() => onNavigate(workflowDestinations[nextStage?.label] || "traceability")}>{summary.status === "delivered" ? "Open traceability" : "Continue workflow"}<ArrowRight size={15} /></button></div><div className="dashboard-progress-overview"><div className="dashboard-progress-ring" style={{ "--dashboard-progress": `${processPercent}%` }}><div><strong>{processPercent}%</strong><span>complete</span></div></div><div className="dashboard-next"><span>Next milestone</span><strong>{summary.next_milestone}</strong><small>{completedStages} of {workflow.length} stages complete</small></div></div></section>
      <section className="dashboard-metrics" aria-label="Package delivery metrics">
        <article><span className="dashboard-metric-icon coral"><FileStack size={16} /></span><div><span>Source documents</span><strong>{summary.counts.sources}</strong><small>Inputs captured</small></div></article>
        <article><span className="dashboard-metric-icon blue"><ShieldCheck size={16} /></span><div><span>Business use cases</span><strong>{summary.counts.approved_business_rules}<small> / {summary.counts.business_rules}</small></strong><small>Approved</small></div></article>
        <article><span className="dashboard-metric-icon amber"><Target size={16} /></span><div><span>Functional requirements</span><strong>{summary.counts.approved_requirements}<small> / {summary.counts.requirements}</small></strong><small>Auto-approved</small></div></article>
        <article><span className="dashboard-metric-icon violet"><FileCheck2 size={16} /></span><div><span>Approved test cases</span><strong>{summary.counts.approved_test_cases}<small> / {summary.counts.test_cases}</small></strong><small>Generation output</small></div></article>
        <article><span className="dashboard-metric-icon green"><Database size={16} /></span><div><span>Test data records</span><strong>{summary.counts.test_data_records}</strong><small>{summary.counts.saved_test_data_records} saved to Raw layer</small></div></article>
        <article><span className="dashboard-metric-icon cyan"><PlayCircle size={16} /></span><div><span>Test execution</span><strong>{summary.counts.test_runs}</strong><small>Recorded runs</small></div></article>
      </section>
      <section className="dashboard-insight-grid"><article className={`dashboard-card dashboard-health-card ${summary.blockers.length ? "has-alerts" : "is-clear"}`}><div className="dashboard-card-heading"><div><span>DELIVERY HEALTH</span><h2>{summary.blockers.length ? `${summary.blockers.length} item${summary.blockers.length === 1 ? "" : "s"} need attention` : "Ready to move forward"}</h2></div>{summary.blockers.length ? <AlertTriangle size={18} /> : <Target size={18} />}</div>{summary.blockers.length ? <ul className="dashboard-blockers">{summary.blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}</ul> : <p className="dashboard-clear"><CheckCircle2 size={15} /> No blockers detected for this package.</p>}</article>
        <article className="dashboard-card dashboard-knowledge-card"><div className="dashboard-card-heading"><div><span>KNOWLEDGE & TRACEABILITY</span><h2>{["ready", "succeeded"].includes(summary.workflow.knowledge_base) ? "Context is ready for generation" : "Context is still being prepared"}</h2></div><BookOpen size={18} /></div><div className="dashboard-knowledge-stats"><div><strong>{summary.knowledge_base?.chunk_count || 0}</strong><span>Indexed chunks</span></div><div><strong>{summary.knowledge_base?.node_count || 0}</strong><span>Traceability nodes</span></div><div><strong>{summary.knowledge_base?.edge_count || 0}</strong><span>Linked edges</span></div></div><button type="button" className="dashboard-text-action" onClick={() => onNavigate("knowledge-base")}>Inspect context <ArrowRight size={14} /></button></article></section>
      <article className="dashboard-card dashboard-process-card"><div className="dashboard-card-heading"><div><span>END-TO-END READINESS</span><h2>Process.BRD delivery journey</h2></div><div className="dashboard-process-meta"><strong>{completedStages}/{workflow.length}</strong><span>stages complete</span><Route size={18} /></div></div><ol className="dashboard-workflow">
        {workflow.map((stage, index) => <li key={stage.label} className={stage.done ? "done" : index === completedStages ? "current" : "pending"}><span>{stage.done ? <CheckCircle2 size={16} /> : <CircleDashed size={16} />}</span><button type="button" onClick={() => onNavigate(workflowDestinations[stage.label])}><strong>{stage.label}</strong><small>{stage.detail}</small></button></li>)}
      </ol></article>
    </>}
  </div>;
}
