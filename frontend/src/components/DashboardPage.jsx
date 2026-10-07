import { useCallback, useEffect, useMemo, useState } from "react";
import { CheckCircle2, CircleDashed, Clock3, FileStack, RefreshCw, Target } from "lucide-react";
import { getProjectDashboard, listProjects } from "../api/client.js";
import { LoadingSpinner } from "./LoadingSpinner.jsx";

function stored(key) { try { return JSON.parse(localStorage.getItem(key) || "null"); } catch { return null; } }
function dateLabel(value) { return value ? new Date(value).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "Not available"; }
const statusLabels = { delivered: "Delivered", on_track: "In progress", in_progress: "In progress", at_risk: "Needs attention" };

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

  return <div className="dashboard-page">
    <div className="page-heading"><div><div className="eyebrow">EXECUTIVE WORKSPACE SUMMARY</div><h1>Dashboard</h1><p>Shows the status, progress, and key details in one place.</p></div><button type="button" className="secondary-button" onClick={() => load(project?.project_id, packageId)} disabled={loading}><RefreshCw size={15} /> Refresh</button></div>
    <div className="dashboard-controls"><label><span>Project</span><select value={project?.project_id || ""} onChange={(event) => chooseProject(event.target.value)}><option value="">Choose project</option>{projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.project_name}</option>)}</select></label><label><span>Resource Package</span><select value={packageId} onChange={(event) => choosePackage(event.target.value)} disabled={!data?.packages.length}><option value="">Choose package</option>{(data?.packages || []).map((item) => <option key={item.package_id} value={item.package_id}>v{item.package_version} · {item.package_name || item.scope || "Resource Package"}</option>)}</select></label></div>
    {error && <div className="error-message">Unable to load dashboard: {error}</div>}
    {loading && !data && <LoadingSpinner variant="section" label="Loading executive summary" detail="Collecting the selected package's latest workflow results." />}
    {!loading && !error && !data?.packages.length && <section className="dashboard-empty"><div className="empty-state-icon"><FileStack size={24} /></div><h2>No package summaries yet</h2><p>Create a Resource Package from uploaded inputs to generate its own dashboard summary.</p><button type="button" className="primary-button" onClick={() => onNavigate("sources")}>Upload sources</button></section>}
    {summary && <>
      <section className={`dashboard-hero ${summary.status}`}><div><span className="dashboard-status"><span />{statusLabels[summary.status] || summary.status}</span><h2>{summary.package_name || `Resource Package v${summary.package_version}`}</h2><p>Project: {data.project.project_name} · Package v{summary.package_version} · Updated {dateLabel(summary.updated_at)}</p></div><div className="dashboard-next"><span>Next milestone</span><strong>{summary.next_milestone}</strong></div></section>
      <section className="dashboard-metrics" aria-label="Package delivery metrics">
        <article><span>Source documents</span><strong>{summary.counts.sources}</strong></article>
        <article><span>Requirements approved</span><strong>{summary.counts.approved_requirements}<small> / {summary.counts.requirements}</small></strong></article>
        <article><span>Test cases generation</span><strong>{summary.counts.approved_test_cases}<small> / {summary.counts.test_cases}</small></strong></article>
        <article><span>Test data generation</span><strong>{summary.counts.test_data_records}<small> records</small></strong></article>
      </section>
      <section className="dashboard-lower-grid dashboard-lower-grid-single">
        <article className="dashboard-card"><div className="dashboard-card-heading"><h2>WorkSpace Status</h2><Target size={18} /></div><ol className="dashboard-workflow">
          {[ ["Sources", summary.counts.sources > 0], ["Business rules", summary.counts.business_rules > 0 && summary.counts.pending_business_rules === 0], ["Knowledge Base", ["ready", "succeeded"].includes(summary.workflow.knowledge_base)], ["Requirements", summary.counts.approved_requirements > 0], ["Test cases", summary.counts.approved_test_cases > 0], ["Test data", summary.counts.test_data_records > 0] ].map(([label, done]) => <li key={label} className={done ? "done" : "pending"}><span>{done ? <CheckCircle2 size={16} /> : <CircleDashed size={16} />}</span><strong>{label}</strong><small>{done ? "Complete" : "Pending"}</small></li>)}
        </ol></article>
      </section>
      <div className="dashboard-footer"><Clock3 size={14} /> Historical summaries remain available by selecting another Resource Package.</div>
    </>}
  </div>;
}
