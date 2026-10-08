import { useEffect, useMemo, useRef, useState } from "react";
import { CheckCircle2, ClipboardCheck, CloudUpload, Database, FileStack, FolderPlus, LoaderCircle, Plus, RefreshCw, Search } from "lucide-react";
import { toast } from "react-toastify";
import { createProject, generateFunctionalRequirements, generateTestCasesFromAnalysis, getProjectDashboard, getRevision, listManifests, listProjects, listSources, uploadSource } from "../api/client.js";

function extractionLabel(source) {
  const status = source?.extraction?.status || source?.status || "missing";
  if (["succeeded", "uploaded", "skipped"].includes(status)) return "Ready";
  if (["pending", "processing"].includes(status)) return "Processing";
  if (status === "missing") return "Not uploaded";
  return "Needs attention";
}

function knowledgeLabel(summary, brdStatus = "Not uploaded") {
  if (!summary) return brdStatus === "Ready" || brdStatus === "Processing" ? "Building" : "Waiting for BRD";
  const status = summary.knowledge_base?.status || summary.workflow?.knowledge_base;
  if (["ready", "succeeded"].includes(status)) return "Ready";
  if (["building", "processing"].includes(status)) return "Building";
  if (["failed", "error"].includes(status)) return "Needs attention";
  return "Waiting for documents";
}

export function ProjectsPage({ onOpenTestCases }) {
  const [projects, setProjects] = useState([]); const [sourceMap, setSourceMap] = useState({}); const [dashboardMap, setDashboardMap] = useState({});
  const [name, setName] = useState(""); const [query, setQuery] = useState(""); const [statusFilter, setStatusFilter] = useState("all");
  const [busy, setBusy] = useState(false); const [loading, setLoading] = useState(true); const [processingProjectId, setProcessingProjectId] = useState(""); const [generatingProjectId, setGeneratingProjectId] = useState(""); const [error, setError] = useState(""); const brdInput = useRef(null); const uploadProject = useRef(null);

  async function refresh() {
    setLoading(true); setError("");
    try {
      const items = await listProjects({ cache: "no-store" });
      const [sources, dashboards] = await Promise.all([
        Promise.all(items.map(async (item) => [item.project_id, await listSources(item.project_id)])),
        Promise.all(items.map(async (item) => [item.project_id, await getProjectDashboard(item.project_id)])),
      ]);
      setProjects(items); setSourceMap(Object.fromEntries(sources)); setDashboardMap(Object.fromEntries(dashboards));
    } catch (err) { setError(err.message); } finally { setLoading(false); }
  }

  useEffect(() => { refresh(); }, []);

  async function create() {
    if (!name.trim()) return;
    setBusy(true); setError("");
    try { const project = await createProject(name.trim()); setName(""); await refresh(); localStorage.setItem("testiq_current_project", JSON.stringify(project)); toast.success("Project created. Add the BRD or technical sources when ready."); }
    catch (err) { setError(err.message); } finally { setBusy(false); }
  }

  function chooseBrd(project) { uploadProject.current = project; brdInput.current?.click(); }
  async function uploadBrd(event) {
    const project = uploadProject.current; const file = event.target.files?.[0]; event.target.value = "";
    if (!project || !file) return;
    setBusy(true); setError("");
    try {
      const uploaded = await uploadSource(project.project_id, file, "brd", "");
      localStorage.setItem("testiq_current_project", JSON.stringify(project));
      toast.success("BRD uploaded successfully.");
      setProcessingProjectId(project.project_id);
      await refresh();
      for (let attempt = 0; attempt < 40; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 2000));
        const revision = await getRevision(uploaded.revision_id);
        const status = revision.extraction?.status || "pending";
        if (["succeeded", "skipped", "failed"].includes(status)) {
          await refresh();
          if (status === "failed") setError(revision.extraction?.error || "BRD extraction failed.");
          break;
        }
      }
    }
    catch (err) { setError(err.message); } finally { setBusy(false); }
    setProcessingProjectId("");
  }

  async function generateProjectTestCases(project) {
    if (!project || generatingProjectId) return;
    setGeneratingProjectId(project.project_id); setError("");
    try {
      const packages = await listManifests(project.project_id, { cache: "no-store" });
      const packageItem = packages[0];
      if (!packageItem) throw new Error("No Resource Package is ready for this project yet.");
      localStorage.setItem("testiq_current_project", JSON.stringify(project));
      localStorage.setItem("testiq_current_package", JSON.stringify(packageItem));
      const analysis = await generateFunctionalRequirements(packageItem.package_id);
      await generateTestCasesFromAnalysis(analysis.analysis_id);
      toast.success("Test cases generated successfully.");
      onOpenTestCases?.(analysis);
    } catch (err) { setError(err.message || "Could not generate test cases."); }
    finally { setGeneratingProjectId(""); }
  }

  const visibleProjects = useMemo(() => projects.filter((project) => {
    const sources = sourceMap[project.project_id] || []; const brdStatus = extractionLabel(sources.find((source) => source.source_type === "brd")); const kbStatus = knowledgeLabel(dashboardMap[project.project_id]?.packages?.[0], brdStatus);
    const haystack = `${project.project_name} ${brdStatus} ${kbStatus}`.toLowerCase();
    return haystack.includes(query.trim().toLowerCase()) && (statusFilter === "all" || (statusFilter === "brd" ? brdStatus === "Ready" : statusFilter === "kb" ? kbStatus === "Ready" : brdStatus === "Not uploaded"));
  }), [projects, sourceMap, dashboardMap, query, statusFilter]);

  return <div className="projects-page">
    <input ref={brdInput} type="file" hidden accept=".docx,.pdf,.txt,.md,.json,.yaml,.yml,.sql,.csv" onChange={uploadBrd} />
    <div className="page-heading"><div><div className="eyebrow">PROJECT WORKSPACE</div><h1>Projects</h1><p>Track each project’s BRD, technical sources, and automatic Knowledge Base status.</p></div><button className="secondary-button" onClick={refresh} disabled={loading || busy}><RefreshCw size={16} /> Refresh</button></div>
    <section className="project-create-card"><div><h2>Create a project</h2><p>Projects can be created empty. Documents can be added in any order.</p></div><div className="project-create-form"><input value={name} onChange={(event) => setName(event.target.value)} placeholder="Project name" onKeyDown={(event) => { if (event.key === "Enter") create(); }} /><button className="primary-button" onClick={create} disabled={busy || !name.trim()}>{busy ? <LoaderCircle className="spin" size={16} /> : <Plus size={16} />} Create project</button></div></section>
    <div className="project-list-toolbar"><div className="project-search"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search projects" /></div><select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option value="all">All projects</option><option value="brd">BRD ready</option><option value="kb">Knowledge Base ready</option><option value="missing">Needs BRD</option></select><span>{visibleProjects.length} project{visibleProjects.length === 1 ? "" : "s"}</span></div>
    {error && <div className="error-message">{error}</div>}
    {loading ? <div className="projects-loading"><LoaderCircle className="spin" size={22} /> Loading projects</div> : !visibleProjects.length ? <div className="projects-empty"><FolderPlus size={25} /><h2>{projects.length ? "No matching projects" : "No projects yet"}</h2><p>{projects.length ? "Try a different search or filter." : "Create a project to begin."}</p></div> : <div className="project-card-list">{visibleProjects.map((project) => {
      const sources = sourceMap[project.project_id] || []; const brdStatus = extractionLabel(sources.find((source) => source.source_type === "brd")); const kbStatus = knowledgeLabel(dashboardMap[project.project_id]?.packages?.[0], brdStatus);
      const isProcessing = processingProjectId === project.project_id || brdStatus === "Processing";
      const isGenerating = generatingProjectId === project.project_id;
      const canGenerateTestCases = brdStatus === "Ready" && kbStatus === "Ready";
      const generationHint = brdStatus !== "Ready"
        ? "Generate Test Cases becomes available when the BRD is ready."
        : "Generate Test Cases becomes available when the Knowledge Base is ready.";
      return <article className="project-workspace-card" key={project.project_id}><div className="project-card-top"><div className="project-card-identity"><span className="project-card-icon"><FolderPlus size={20} /></span><div><h2>{project.project_name}</h2><p>Created {project.created_at ? new Date(project.created_at).toLocaleDateString() : "recently"}</p></div></div></div><div className="project-status-grid"><div><span className="project-status-label"><FileStack size={14} /> BRD</span><strong className={brdStatus === "Ready" ? "status-ready" : brdStatus === "Needs attention" ? "status-danger" : "status-pending"}>{isProcessing ? <><LoaderCircle className="spin status-spinner" size={13} /> Processing</> : brdStatus}</strong></div><div><span className="project-status-label"><Database size={14} /> Source documents</span><strong>{sources.filter((source) => source.source_type !== "brd").length} attached</strong></div><div><span className="project-status-label"><CheckCircle2 size={14} /> Knowledge Base</span><strong className={kbStatus === "Ready" ? "status-ready" : kbStatus === "Needs attention" ? "status-danger" : "status-pending"}>{kbStatus}</strong></div></div><div className="project-card-footer"><span>{isGenerating ? "Generating test cases from the Knowledge Base" : isProcessing ? "Extracting BRD content and preparing the Knowledge Base" : brdStatus === "Not uploaded" ? "Next step: upload the BRD" : kbStatus === "Ready" ? "Ready for downstream workflow" : "Documents are processed automatically"}</span><div><button className="secondary-button compact-button" onClick={() => chooseBrd(project)} disabled={busy || isProcessing || isGenerating}><CloudUpload size={15} /> {isProcessing ? "Processing..." : brdStatus === "Ready" ? "Replace BRD" : "Upload BRD"}</button><button className="primary-button compact-button project-generate-button" onClick={() => generateProjectTestCases(project)} disabled={!canGenerateTestCases || busy || isProcessing || isGenerating} title={!canGenerateTestCases ? generationHint : "Generate test cases from this project's Knowledge Base"}>{isGenerating ? <LoaderCircle className="spin" size={15} /> : <ClipboardCheck size={15} />} {isGenerating ? "Generating..." : "Generate Test Cases"}</button></div></div></article>;
    })}</div>}
  </div>;
}
