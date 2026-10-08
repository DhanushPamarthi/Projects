import { useEffect, useState } from "react";
import { CheckCircle2, Database, FileText, FileUp, FolderOpen, LoaderCircle } from "lucide-react";
import { toast } from "react-toastify";
import { listProjects, listSources, uploadSource } from "../api/client.js";
import { AppSelect } from "./AppSelect.jsx";
import { Pagination } from "./Pagination.jsx";
import { WorkspacePageHero } from "./WorkspacePageHero.jsx";

const sourceTypes = [["campaign_rules", "Campaign rules"], ["api_spec", "API specification"], ["database_schema", "Database schema"], ["message_template", "Message template"], ["ui_description", "UI description"]];
function sourceStatus(source) { const status = source.extraction?.status || source.status || "processing"; return { label: status === "succeeded" ? "Ready" : status === "failed" || status === "extraction_failed" ? "Needs attention" : status === "processing" || status === "pending" ? "Processing" : status, tone: status === "failed" || status === "extraction_failed" ? "failed" : status === "processing" || status === "pending" ? "processing" : "ready" }; }

export function SourceDocumentsPage() {
  const [projects, setProjects] = useState([]); const [project, setProject] = useState(() => JSON.parse(localStorage.getItem("testiq_current_project") || "null"));
  const [sources, setSources] = useState([]); const [sourceType, setSourceType] = useState("api_spec"); const [file, setFile] = useState(null); const [busy, setBusy] = useState(false); const [page, setPage] = useState(1); const [error, setError] = useState("");
  async function refreshProjects() { const items = await listProjects({ cache: "no-store" }); setProjects(items); if (project) setProject(items.find((item) => item.project_id === project.project_id) || null); }
  async function refreshSources(id = project?.project_id) { if (id) setSources(await listSources(id)); }
  useEffect(() => { refreshProjects().catch((err) => setError(err.message)); }, []);
  useEffect(() => { refreshSources().catch(() => {}); setPage(1); if (project) localStorage.setItem("testiq_current_project", JSON.stringify(project)); }, [project?.project_id]);
  async function upload() { if (!project || !file) return; setBusy(true); setError(""); try { await uploadSource(project.project_id, file, sourceType, ""); setFile(null); await refreshSources(); await refreshProjects(); toast.success("Source uploaded. It will be included automatically when the Knowledge Base is updated."); } catch (err) { setError(err.message); } finally { setBusy(false); } }
  const pageSize = 5;
  const visibleSources = sources.slice((page - 1) * pageSize, page * pageSize);
  return <div className="source-page source-documents-page professional-workspace-page">
    <WorkspacePageHero productLabel="TestIQ · Source Documents" eyebrow="TECHNICAL SOURCES" title="Source Intelligence" context={project?.project_name} cues={["Controlled intake", "Automatic extraction", "Knowledge-ready"]} />
    {error && <div className="error-message">{error}</div>}
    <div className="source-workspace-grid">
      <section className="packages-card source-context-card">
        <div className="packages-card-head"><div><span className="card-kicker">PROJECT CONTEXT</span><h2>Choose a project</h2></div><span className="source-card-icon project"><FolderOpen size={20} /></span></div>
        <div className="source-project-select"><label htmlFor="source-project">Active project</label><AppSelect id="source-project" value={project?.project_id || ""} onChange={(event) => setProject(projects.find((item) => item.project_id === event.target.value) || null)}><option value="">Choose project</option>{projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.project_name}</option>)}</AppSelect>{project && <span className="context-status"><CheckCircle2 size={14} /> Active</span>}</div>
      </section>
      <section className={`packages-card source-upload-card ${!project ? "disabled" : ""}`}>
        <div className="packages-card-head"><div><span className="card-kicker">SOURCE INTAKE</span><h2>Add technical source</h2></div><span className="source-card-icon upload"><FileUp size={20} /></span></div>
        <div className="source-type-control"><label htmlFor="source-type">Document type</label><AppSelect id="source-type" value={sourceType} onChange={(event) => setSourceType(event.target.value)} disabled={!project}>{sourceTypes.map(([value, label]) => <option value={value} key={value}>{label}</option>)}</AppSelect></div>
        <div className="drop-zone-modern source-dropzone" onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); if (project) setFile(event.dataTransfer.files?.[0] || null); }}>
          <span className="source-drop-icon"><FileUp size={22} /></span><h3>{file ? file.name : "Drop a source document here"}</h3><p>{file ? "Ready to upload" : "or choose a file from your computer"}</p>
          <label className="choose-file">{file ? "Choose another file" : "Choose file"}<input type="file" accept=".docx,.pdf,.txt,.md,.json,.yaml,.yml,.sql,.csv" disabled={!project} onChange={(event) => setFile(event.target.files?.[0] || null)} /></label><span className="source-format-note">DOCX, PDF, TXT, MD, JSON, YAML, SQL or CSV · up to 25 MB</span>
        </div>
        <div className="source-upload-footer"><button className="primary-button" onClick={upload} disabled={busy || !file || !project}>{busy ? <LoaderCircle className="spin" size={16} /> : <FileUp size={16} />} Upload source</button></div>
      </section>
    </div>
    {!project ? <div className="packages-empty"><Database size={24} /><h2>Select a project</h2><p>Create a project first, then upload technical source documents.</p></div> : <>
      <section className="packages-card source-library-card"><div className="packages-card-head"><div><span className="card-kicker">PROJECT LIBRARY</span><h2>Project sources</h2><p>{sources.length} document{sources.length === 1 ? "" : "s"} attached.</p></div><span className="package-count">{sources.length}</span></div>{sources.length ? <><div className="source-library">{visibleSources.map((source) => { const status = sourceStatus(source); const processing = status.tone === "processing"; return <div className="source-library-row" key={source.source_id}><span className="source-library-icon"><FileText size={17} /></span><div className="source-library-main"><strong>{source.active_revision?.filename || source.logical_name}</strong><span>{source.source_type.replaceAll("_", " ")}</span></div><span className={`source-library-status ${status.tone}`} role={processing ? "status" : undefined} aria-live={processing ? "polite" : undefined}>{processing ? <LoaderCircle className="source-status-spinner" size={13} aria-hidden="true" /> : <i aria-hidden="true" />}{status.label}</span></div>; })}</div><Pagination page={page} pageSize={pageSize} totalItems={sources.length} onPageChange={setPage} label="source documents" /></> : <div className="source-library-empty"><Database size={22} /><p>No source documents uploaded yet.</p></div>}</section>
    </>}
  </div>;
}
