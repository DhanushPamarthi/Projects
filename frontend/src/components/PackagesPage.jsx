import { useEffect, useState } from "react";
import { CheckCircle2, FileStack, PackageOpen, RefreshCw } from "lucide-react";
import { getProject, listManifests, listProjects } from "../api/client.js";

export function PackagesPage() {
  const [project, setProject] = useState(() => JSON.parse(localStorage.getItem("testiq_current_project") || "null"));
  const [projects, setProjects] = useState([]); const [packages, setPackages] = useState([]); const [loading, setLoading] = useState(false); const [error, setError] = useState("");
  async function loadPackages(id = project?.project_id) { if (!id) return; setLoading(true); try { setPackages(await listManifests(id)); setError(""); } catch (err) { setError(err.message); } finally { setLoading(false); } }
  useEffect(() => { listProjects().then((items) => { setProjects(items); if (project?.project_id && !items.some((item) => item.project_id === project.project_id)) { localStorage.removeItem("testiq_current_project"); setProject(null); setPackages([]); } }).catch(() => {}); }, []);
  useEffect(() => { if (project?.project_id) loadPackages(project.project_id); }, [project?.project_id]);
  async function selectProject(id) { const selected = await getProject(id); setProject(selected); localStorage.setItem("testiq_current_project", JSON.stringify(selected)); }
  return <div className="packages-page"><div className="page-heading"><div><div className="eyebrow">TRACEABLE SNAPSHOTS</div><h1>Packages</h1><p>Immutable source snapshots ready for test runs.</p></div><button className="secondary-button" onClick={() => loadPackages()}><RefreshCw size={16} /> Refresh</button></div>
    <div className="package-project-bar"><div><span className="muted-label">Project</span><strong>{project?.project_name || "Select a project"}</strong></div><select value={project?.project_id || ""} onChange={(event) => selectProject(event.target.value)}><option value="">Choose project</option>{projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.project_name}</option>)}</select></div>
    {error && <div className="error-message">Unable to load packages: {error}</div>}
    {!project && <div className="packages-empty"><div className="empty-state-icon"><PackageOpen size={25} /></div><h2>Select a project to view packages</h2><p>Choose a project above to see its immutable source snapshots.</p></div>}
    {project && !loading && !packages.length && <div className="packages-empty"><div className="empty-state-icon"><PackageOpen size={25} /></div><h2>No packages yet</h2><p>Create a source package from the Sources page once your documents are ready.</p></div>}
    {project && packages.length > 0 && <section className="packages-card"><div className="packages-card-head"><div><h2>Package history</h2><p>{packages.length} immutable snapshot{packages.length === 1 ? "" : "s"} for this project.</p></div><span className="package-count">{packages.length} total</span></div><div className="packages-list">{packages.map((item) => <div className="package-detail-row" key={item.package_id}><span className="package-version large">v{item.package_version}</span><div className="package-main"><strong>{item.package_name || item.scope || "Source package"}</strong><span>{item.source_count} source{Number(item.source_count) === 1 ? "" : "s"} · Scope: {item.scope || "MVP"} · Created {item.created_at ? new Date(item.created_at).toLocaleString() : "recently"}</span></div><div className="package-detail-meta"><span><CheckCircle2 size={14} /> Ready</span><small><FileStack size={13} /> Fixed source revisions</small></div></div>)}</div></section>}
  </div>;
}
