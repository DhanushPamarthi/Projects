import { useEffect, useMemo, useRef, useState } from "react";
import { CheckCircle2, ChevronLeft, ChevronRight, ClipboardCheck, CloudUpload, FileClock, FileStack, FolderPlus, History, LoaderCircle, Plus, RotateCcw, Search, Square } from "lucide-react";
import { toast } from "react-toastify";
import { createProject, generateFunctionalRequirements, getProjectWorkspaceSummary, getRevision, listManifests, listSourceRevisions, retryPackageAutomation, resumeProjectPackageCreation, selectActiveRevision, stopPackageAutomation, stopProjectPackageCreation, uploadSource } from "../api/client.js";
import { AppSelect } from "./AppSelect.jsx";

function extractionLabel(source) {
  const status = source?.extraction?.status || source?.status || "missing";
  if (["succeeded", "uploaded", "skipped"].includes(status)) return "Ready";
  if (["pending", "processing"].includes(status)) return "Processing";
  if (status === "missing") return "Not uploaded";
  return "Needs attention";
}

function projectBrdStatus(brds) {
  if (!brds.length) return "Not uploaded";
  const statuses = brds.map(extractionLabel);
  if (statuses.includes("Processing")) return "Processing";
  if (statuses.includes("Needs attention")) return "Needs attention";
  return statuses.every((status) => status === "Ready") ? "Ready" : "Processing";
}

function knowledgeLabel(summary, brdStatus = "Not uploaded") {
  if (!summary) return brdStatus === "Ready" || brdStatus === "Processing" ? "Building" : "Waiting for BRD";
  const status = summary.knowledge_base?.status || summary.workflow?.knowledge_base;
  if (["ready", "succeeded"].includes(status)) return "Ready";
  if (["building", "processing"].includes(status)) return "Building";
  if (["failed", "error"].includes(status)) return "Needs attention";
  return "Waiting for documents";
}

function progressDetails(brdStatus, packageSummary, stage, error) {
  const knowledgeBase = packageSummary?.knowledge_base || {};
  if (stage === "complete") return { percent: 100, label: "Process completed" };
  if (stage === "cancelled") return { percent: 0, label: "Process stopped" };
  if (stage === "failed") {
    const failedStage = /business use cases failed/i.test(error || "")
      ? "Business Use Case creation failed"
      : /knowledge base failed/i.test(error || "") ? "Knowledge Base building failed" : "Package processing failed";
    const percent = failedStage.startsWith("Business") ? 38 : Math.min(97, 62 + Math.round((Number(knowledgeBase.progress_percent) || 0) * 0.35));
    return { percent, label: failedStage };
  }
  if (!packageSummary) {
    return brdStatus === "Ready"
      ? { percent: 8, label: "Creating package" }
      : brdStatus === "Processing" ? { percent: 3, label: "Extracting BRD" } : { percent: 0, label: "Waiting for BRD" };
  }
  if (stage === "queued" || stage === "not_started") return { percent: 24, label: "Preparing Business Use Cases" };
  if (stage === "business_use_cases") return { percent: 42, label: "Creating Business Use Cases" };
  if (stage === "knowledge_base") {
    return { percent: Math.min(97, 62 + Math.round((Number(knowledgeBase.progress_percent) || 0) * 0.35)), label: "Building Knowledge Base" };
  }
  return { percent: 0, label: "Waiting for processing" };
}

export function ProjectsPage({ onOpenTestCases }) {
  const [projects, setProjects] = useState([]); const [sourceMap, setSourceMap] = useState({}); const [dashboardMap, setDashboardMap] = useState({});
  const [name, setName] = useState(""); const [query, setQuery] = useState(""); const [statusFilter, setStatusFilter] = useState("all");
  const [projectFilter, setProjectFilter] = useState(() => {
    try { return JSON.parse(localStorage.getItem("testiq_current_project") || "null")?.project_id || ""; }
    catch { return ""; }
  });
  const [busy, setBusy] = useState(false); const [loading, setLoading] = useState(true); const [processingProjectId, setProcessingProjectId] = useState(""); const [replacingBrdProjectId, setReplacingBrdProjectId] = useState(""); const [previousPackageId, setPreviousPackageId] = useState(""); const [generatingProjectId, setGeneratingProjectId] = useState(""); const [stoppingPackageId, setStoppingPackageId] = useState(""); const [stopRequestedProjectId, setStopRequestedProjectId] = useState(""); const [page, setPage] = useState(1); const [error, setError] = useState(""); const [expandedSourceId, setExpandedSourceId] = useState(""); const [versionHistory, setVersionHistory] = useState({}); const [versionBusySourceId, setVersionBusySourceId] = useState(""); const brdInput = useRef(null); const uploadTarget = useRef(null);

  async function refresh() {
    setLoading((current) => current || projects.length === 0); setError("");
    try {
      const workspace = await getProjectWorkspaceSummary();
      const items = workspace.map((item) => ({ project_id: item.project_id, project_name: item.project_name, created_at: item.created_at, source_count: item.source_count }));
      setProjects(items);
      setSourceMap(Object.fromEntries(workspace.map((item) => [item.project_id, item.brds || (item.brd ? [item.brd] : [])])));
      setDashboardMap(Object.fromEntries(workspace.map((item) => [item.project_id, { packages: item.latest_package ? [item.latest_package] : [] }])));
      const refreshedPackage = workspace.find((item) => item.project_id === replacingBrdProjectId)?.latest_package;
      if (replacingBrdProjectId && refreshedPackage?.package_id && refreshedPackage.package_id !== previousPackageId) {
        setReplacingBrdProjectId(""); setPreviousPackageId("");
      }
      setProjectFilter((current) => items.some((item) => item.project_id === current)
        ? current
        : (items.find((item) => item.project_id === projectFilter)?.project_id || items[0]?.project_id || ""));
    } catch (err) { setError(err.message); } finally { setLoading(false); }
  }

  useEffect(() => { refresh(); }, []);

  const needsWorkspacePolling = projects.some((project) => {
    const brdStatus = projectBrdStatus((sourceMap[project.project_id] || []).filter((source) => source.source_type === "brd"));
    const packageSummary = dashboardMap[project.project_id]?.packages?.[0];
    const stage = packageSummary?.automation?.stage || packageSummary?.workflow?.package_processing || "not_started";
    return brdStatus === "Processing" || (brdStatus === "Ready" && !packageSummary) || replacingBrdProjectId === project.project_id || ["queued", "business_use_cases", "knowledge_base"].includes(stage);
  });

  useEffect(() => {
    if (!projects.length || !needsWorkspacePolling) return undefined;
    const timer = window.setInterval(async () => {
      try {
        const workspace = await getProjectWorkspaceSummary();
        setSourceMap(Object.fromEntries(workspace.map((item) => [item.project_id, item.brds || (item.brd ? [item.brd] : [])])));
        setDashboardMap(Object.fromEntries(workspace.map((item) => [item.project_id, { packages: item.latest_package ? [item.latest_package] : [] }])));
        const refreshedPackage = workspace.find((item) => item.project_id === replacingBrdProjectId)?.latest_package;
        if (replacingBrdProjectId && refreshedPackage?.package_id && refreshedPackage.package_id !== previousPackageId) {
          setReplacingBrdProjectId(""); setPreviousPackageId("");
        }
      } catch { /* The next refresh will retry without interrupting the workspace. */ }
    }, 4000);
    return () => window.clearInterval(timer);
  }, [projects.length, needsWorkspacePolling, replacingBrdProjectId, previousPackageId]);

  async function create() {
    if (!name.trim()) return;
    setBusy(true); setError("");
    try { const project = await createProject(name.trim()); setName(""); localStorage.setItem("testiq_current_project", JSON.stringify(project)); setProjectFilter(project.project_id); await refresh(); toast.success("Project created. Add the BRD or technical sources when ready."); }
    catch (err) { setError(err.message); } finally { setBusy(false); }
  }

  function chooseBrd(project, sourceId = "") { uploadTarget.current = { project, sourceId }; brdInput.current?.click(); }
  async function uploadBrd(event) {
    const target = uploadTarget.current;
    const project = target?.project;
    const sourceId = target?.sourceId || "";
    const files = Array.from(event.target.files || []);
    event.target.value = "";
    if (!project || !files.length) return;
    if (sourceId && files.length > 1) {
      setError("Choose one file when uploading a new version of an existing BRD.");
      return;
    }
    const currentPackageId = dashboardMap[project.project_id]?.packages?.[0]?.package_id || "";
    setBusy(true); setError(""); setProcessingProjectId(project.project_id); setStopRequestedProjectId("");
    setReplacingBrdProjectId(project.project_id); setPreviousPackageId(currentPackageId);
    try {
      const uploads = [];
      for (const file of files) {
        const upload = await uploadSource(project.project_id, file, "brd", "", "", sourceId);
        uploads.push({ file, upload });
      }
      if (sourceId) setVersionHistory((current) => { const next = { ...current }; delete next[sourceId]; return next; });
      localStorage.setItem("testiq_current_project", JSON.stringify(project));
      await refresh();
      const results = await Promise.all(uploads.map(async ({ file, upload }) => {
        let revision;
        for (let attempt = 0; attempt < 40; attempt += 1) {
          await new Promise((resolve) => setTimeout(resolve, 2000));
          revision = await getRevision(upload.revision_id);
          if (["succeeded", "skipped", "failed"].includes(revision.extraction?.status || "pending")) break;
        }
        return { file, revision, status: revision?.extraction?.status || "pending" };
      }));
      await refresh();
      const failed = results.find((result) => result.status === "failed");
      if (failed) setError(`${failed.file.name}: ${failed.revision.extraction?.error || "BRD extraction failed."}`);
      else if (results.some((result) => result.status === "pending")) setError("One or more BRDs are still processing. Refresh to check their status.");
      else toast.success(sourceId ? "New BRD version uploaded." : `${results.length} BRD${results.length === 1 ? "" : "s"} added to the project.`);
    }
    catch (err) { setError(err.message); setReplacingBrdProjectId(""); setPreviousPackageId(""); refresh().catch(() => {}); }
    finally { setBusy(false); setProcessingProjectId(""); }
  }

  async function toggleVersionHistory(sourceId) {
    if (expandedSourceId === sourceId) { setExpandedSourceId(""); return; }
    setExpandedSourceId(sourceId); setError("");
    if (versionHistory[sourceId]) return;
    setVersionBusySourceId(sourceId);
    try {
      const result = await listSourceRevisions(sourceId);
      setVersionHistory((current) => ({ ...current, [sourceId]: result.revisions || [] }));
    } catch (err) { setError(err.message || "Could not load BRD versions."); }
    finally { setVersionBusySourceId(""); }
  }

  async function chooseActiveVersion(project, sourceId, revisionId) {
    if (!revisionId || versionBusySourceId) return;
    const currentPackageId = dashboardMap[project.project_id]?.packages?.[0]?.package_id || "";
    setVersionBusySourceId(sourceId); setReplacingBrdProjectId(project.project_id); setPreviousPackageId(currentPackageId); setError("");
    try {
      await selectActiveRevision(sourceId, revisionId);
      toast.success("Active BRD version updated. A new Resource Package is being prepared.");
      await refresh();
    } catch (err) { setError(err.message || "Could not activate the selected BRD version."); setReplacingBrdProjectId(""); setPreviousPackageId(""); }
    finally { setVersionBusySourceId(""); }
  }

  function generateProjectTestCases(project) {
    const packageItem = dashboardMap[project?.project_id]?.packages?.[0];
    if (!project || !packageItem?.package_id || generatingProjectId) return;
    localStorage.setItem("testiq_current_project", JSON.stringify(project));
    localStorage.setItem("testiq_current_package", JSON.stringify(packageItem));
    onOpenTestCases?.({
      package_id: packageItem.package_id,
      project_id: project.project_id,
      project_name: project.project_name,
      generation_pipeline: true,
    });
  }

  async function retryProjectProcessing(packageId) {
    if (!packageId || busy) return;
    setStopRequestedProjectId("");
    setBusy(true); setError("");
    try {
      await retryPackageAutomation(packageId);
      toast.success("Processing resumed.");
      await refresh();
    } catch (err) { setError(err.message || "Could not restart package processing."); }
    finally { setBusy(false); }
  }

  async function resumeProjectProcessing(projectId, packageId) {
    if (packageId) return retryProjectProcessing(packageId);
    if (!projectId || stoppingPackageId) return;
    setStoppingPackageId(projectId); setError("");
    try {
      await resumeProjectPackageCreation(projectId);
      setStopRequestedProjectId("");
      toast.success("Processing resumed.");
      await refresh();
    } catch (err) { setError(err.message || "Could not resume project processing."); }
    finally { setStoppingPackageId(""); }
  }

  async function stopProjectProcessing(projectId, packageId) {
    if (!projectId || stoppingPackageId) return;
    const operationId = packageId || projectId;
    setStoppingPackageId(operationId); setError("");
    try {
      if (packageId) await stopPackageAutomation(packageId);
      else await stopProjectPackageCreation(projectId);
      setStopRequestedProjectId(projectId);
      toast.success("Process stopped.");
      await refresh();
    } catch (err) {
      if (err.status === 409) {
        setStopRequestedProjectId(projectId);
        setError("");
        toast.success("Process stopped.");
      } else setError(err.message || "Could not stop package processing.");
    }
    finally { setStoppingPackageId(""); }
  }

  const visibleProjects = useMemo(() => projects.filter((project) => {
    const sources = sourceMap[project.project_id] || []; const brdStatus = projectBrdStatus(sources.filter((source) => source.source_type === "brd")); const kbStatus = knowledgeLabel(dashboardMap[project.project_id]?.packages?.[0], brdStatus);
    const haystack = `${project.project_name} ${brdStatus} ${kbStatus} ${sources.filter((source) => source.source_type === "brd").map((brd) => brd.active_revision?.filename || brd.logical_name || "").join(" ")}`.toLowerCase();
    return (projectFilter === "all" || project.project_id === projectFilter)
      && haystack.includes(query.trim().toLowerCase())
      && (statusFilter === "all" || (statusFilter === "brd" ? brdStatus === "Ready" : statusFilter === "kb" ? kbStatus === "Ready" : brdStatus === "Not uploaded"));
  }), [projects, sourceMap, dashboardMap, query, statusFilter, projectFilter]);

  const projectsPerPage = 2;
  const totalPages = projectFilter === "all" ? Math.max(1, Math.ceil(visibleProjects.length / projectsPerPage)) : 1;
  const pagedProjects = projectFilter === "all"
    ? visibleProjects.slice((page - 1) * projectsPerPage, page * projectsPerPage)
    : visibleProjects;
  const paginationStart = Math.max(1, Math.min(page - 2, totalPages - 4));
  const paginationPages = Array.from({ length: Math.min(5, totalPages) }, (_, index) => paginationStart + index);
  useEffect(() => { setPage(1); }, [projectFilter, query, statusFilter]);
  useEffect(() => { setPage((current) => Math.min(current, totalPages)); }, [totalPages]);

  return <div className="projects-page">
    <input ref={brdInput} type="file" hidden multiple accept=".docx,.pdf,.txt,.md,.json,.yaml,.yml,.sql,.csv" onChange={uploadBrd} />
    <section className="project-intake-hero"><div className="project-intake-copy"><div className="eyebrow">PROCESS.BRD INTAKE</div><h1>Manage Projects &amp; Upload BRD</h1><p>Upload the project BRD and manage its complete version history.</p></div><div className="project-create-form"><label htmlFor="project-name">New project</label><div><input id="project-name" value={name} onChange={(event) => setName(event.target.value)} placeholder="Enter project name" onKeyDown={(event) => { if (event.key === "Enter") create(); }} /><button className="primary-button" onClick={create} disabled={busy || !name.trim()}>{busy ? <LoaderCircle className="spin" size={16} /> : <Plus size={16} />} Create project</button></div></div></section>
    <div className="project-list-toolbar">{projectFilter === "all" && <div className="project-search"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search projects" /></div>}<div className="project-filter-controls"><label className="project-filter-label">Project<AppSelect aria-label="Select project" value={projectFilter} onChange={(event) => { const value = event.target.value; setProjectFilter(value); if (value !== "all") setQuery(""); const selected = projects.find((item) => item.project_id === value); if (selected) localStorage.setItem("testiq_current_project", JSON.stringify(selected)); }}><option value="all">All projects</option>{projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.project_name}</option>)}</AppSelect></label><label className="project-filter-label">Status<AppSelect aria-label="Filter projects by status" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option value="all">All Projects</option><option value="brd">BRD ready</option><option value="kb">Knowledge Base ready</option><option value="missing">Needs BRD</option></AppSelect></label><span>{visibleProjects.length} project{visibleProjects.length === 1 ? "" : "s"}</span></div></div>
    {error && <div className="error-message">{error}</div>}
    {loading ? <div className="projects-loading"><LoaderCircle className="spin" size={22} /> Loading projects</div> : !visibleProjects.length ? <div className="projects-empty"><FolderPlus size={25} /><h2>{projects.length ? "No matching projects" : "No projects yet"}</h2><p>{projects.length ? "Try a different search or filter." : "Create a project to begin."}</p></div> : <><div className="project-card-list">{pagedProjects.map((project) => {
      const sources = sourceMap[project.project_id] || [];
      const brds = sources.filter((source) => source.source_type === "brd");
      const brdStatus = projectBrdStatus(brds);
      const latestPackageSummary = dashboardMap[project.project_id]?.packages?.[0];
      const isWaitingForReplacementPackage = replacingBrdProjectId === project.project_id
        && (latestPackageSummary?.package_id || "") === previousPackageId;
      const packageSummary = isWaitingForReplacementPackage ? null : latestPackageSummary;
      const automation = packageSummary?.automation || {};
      const automationStage = automation.stage || packageSummary?.workflow?.package_processing || "not_started";
      const automationError = automation.error || packageSummary?.automation_error || "";
      const isProcessing = processingProjectId === project.project_id || replacingBrdProjectId === project.project_id || brdStatus === "Processing" || ["queued", "business_use_cases", "knowledge_base"].includes(automationStage);
      const isGenerating = generatingProjectId === project.project_id;
      const canGenerateUseCases = brdStatus === "Ready" && automationStage === "complete";
      const isComplete = automationStage === "complete";
      const isFailed = automationStage === "failed";
      const isStopped = automationStage === "cancelled" || stopRequestedProjectId === project.project_id;
      const generationHint = !packageSummary ? "Package creation starts after BRD extraction completes." : "Use case generation becomes available when package processing completes.";
      const progress = isStopped && !packageSummary ? { percent: 0, label: "Process stopped" } : progressDetails(brdStatus, packageSummary, automationStage, automationError);
      const canStop = Boolean(packageSummary?.package_id && ["not_started", "queued", "business_use_cases", "knowledge_base"].includes(automationStage))
        || (!packageSummary && brdStatus === "Ready" && !isStopped);
      const cardState = isComplete ? { label: "Ready for test design", tone: "ready" }
        : isFailed ? { label: "Needs attention", tone: "attention" }
          : isStopped ? { label: "Paused", tone: "paused" }
            : isProcessing ? { label: "Processing", tone: "processing" }
              : brdStatus === "Not uploaded" ? { label: "BRD required", tone: "waiting" }
                : { label: "Preparing", tone: "waiting" };
      return <article className="project-workspace-card" key={project.project_id}>
        <div className="project-card-top"><div className="project-card-identity"><span className="project-card-icon"><FolderPlus size={20} /></span><div><h2>{project.project_name}</h2><p>Created {project.created_at ? new Date(project.created_at).toLocaleDateString() : "recently"}</p></div></div><span className={`project-state-badge ${cardState.tone}`}>{cardState.tone === "ready" && <CheckCircle2 size={13} />}{cardState.label}</span></div>
        <section className="project-brd-collection" aria-label={`${project.project_name} BRDs`}>
          <div className="project-brd-collection-head"><div><span className="project-status-label"><FileStack size={14} /> Business requirements</span><strong>{brds.length} BRD{brds.length === 1 ? "" : "s"}</strong></div><button type="button" className="secondary-button compact-button" onClick={() => chooseBrd(project)} disabled={busy || isProcessing || isGenerating}><CloudUpload size={14} /> Add BRD</button></div>
          {brds.length ? <div className="project-brd-list">{brds.map((brd) => {
            const activeRevision = brd.active_revision || {};
            const status = extractionLabel(brd);
            const revisions = versionHistory[brd.source_id] || [];
            const successfulRevisions = revisions.filter((revision) => revision.extraction_status === "succeeded");
            const expanded = expandedSourceId === brd.source_id;
            return <div className={`project-brd-item ${expanded ? "expanded" : ""}`} key={brd.source_id}>
              <div className="project-brd-item-main"><span className="project-brd-file-icon"><FileClock size={17} /></span><div><strong title={activeRevision.filename || brd.logical_name}>{activeRevision.filename || brd.logical_name}</strong><span>Active version v{activeRevision.revision || 1}{activeRevision.owner_version ? ` · ${activeRevision.owner_version}` : ""}</span></div></div>
              <span className={`project-brd-version-count ${status.toLowerCase().replaceAll(" ", "-")}`}>{status === "Processing" && <LoaderCircle className="spin" size={12} />} v{activeRevision.revision || 1} · {brd.revision_count || 1} version{(brd.revision_count || 1) === 1 ? "" : "s"}</span>
              <div className="project-brd-actions"><button type="button" className="secondary-button compact-button" onClick={() => toggleVersionHistory(brd.source_id)} disabled={versionBusySourceId === brd.source_id}><History size={14} /> {versionBusySourceId === brd.source_id ? "Loading..." : expanded ? "Hide versions" : "Versions"}</button><button type="button" className="secondary-button compact-button" onClick={() => chooseBrd(project, brd.source_id)} disabled={busy || isProcessing || isGenerating}><CloudUpload size={14} /> Upload new version</button></div>
              {expanded && <div className="project-brd-version-panel"><div><strong>Version history</strong><span>Select the revision used by the next Resource Package.</span></div>{versionBusySourceId === brd.source_id && !revisions.length ? <LoaderCircle className="spin" size={17} /> : <AppSelect aria-label={`Active version for ${brd.logical_name}`} value={activeRevision.revision_id || ""} disabled={versionBusySourceId === brd.source_id || !successfulRevisions.length} onChange={(event) => chooseActiveVersion(project, brd.source_id, event.target.value)}>{successfulRevisions.map((revision) => <option value={revision.revision_id} key={revision.revision_id}>v{revision.revision} · {revision.filename}{revision.owner_version ? ` · ${revision.owner_version}` : ""}</option>)}</AppSelect>}</div>}
            </div>;
          })}</div> : <div className="project-brd-empty"><div><FileStack size={18} /><span>No BRD uploaded</span></div><button type="button" className="primary-button compact-button" onClick={() => chooseBrd(project)} disabled={busy}><CloudUpload size={14} /> Upload first BRD</button></div>}
        </section>
        {(brds.length > 0 || packageSummary || processingProjectId === project.project_id || stopRequestedProjectId === project.project_id) && <section className="project-processing" aria-label="Processing progress"><div className="project-processing-heading"><strong>{isComplete ? "Process completed" : "Processing"}</strong><div className="project-processing-actions">{isGenerating && <span>Generating use cases…</span>}{canStop && <button type="button" className="secondary-button compact-button processing-icon-button stop-processing-button" aria-label="Stop processing" title="Stop processing" disabled={stoppingPackageId === (packageSummary?.package_id || project.project_id)} onClick={() => stopProjectProcessing(project.project_id, packageSummary?.package_id)}>{stoppingPackageId === (packageSummary?.package_id || project.project_id) ? <LoaderCircle className="spin" size={14} /> : <Square size={14} />}</button>}{isStopped && <button type="button" className="secondary-button compact-button processing-icon-button resume-processing-button" aria-label="Resume processing" title="Resume processing" disabled={busy || stoppingPackageId === (packageSummary?.package_id || project.project_id)} onClick={() => resumeProjectProcessing(project.project_id, packageSummary?.package_id)}>{stoppingPackageId === (packageSummary?.package_id || project.project_id) ? <LoaderCircle className="spin" size={14} /> : <RotateCcw size={15} />}</button>}</div></div><div className={`project-progress-track ${isComplete ? "completed" : ""} ${isFailed ? "failed" : ""} ${progress.percent > 0 && progress.percent < 100 && !isFailed && !isStopped ? "running" : ""}`} role="progressbar" aria-label="Project processing" aria-valuemin="0" aria-valuemax="100" aria-valuenow={progress.percent}><span style={{ width: `${progress.percent}%` }} /></div><div className={`project-progress-meta ${isComplete ? "completed" : ""}`}><strong>{progress.percent}%</strong><span>{progress.label}</span></div>{(isFailed || isStopped) && <div className="project-processing-error"><span>{isStopped ? "Process stopped." : automationError || "Processing failed."}</span>{isFailed && <button type="button" className="secondary-button compact-button" disabled={busy} onClick={() => retryProjectProcessing(packageSummary?.package_id)}>Retry processing</button>}</div>}</section>}
        <div className="project-card-footer"><div><button className="primary-button compact-button project-generate-button" onClick={() => generateProjectTestCases(project)} disabled={!canGenerateUseCases || busy || isProcessing || isGenerating} title={!canGenerateUseCases ? generationHint : "Continue to the Test Cases page to generate and review test cases"}>{isGenerating ? <LoaderCircle className="spin" size={15} /> : <ClipboardCheck size={15} />} {isGenerating ? "Preparing..." : "Continue to Generate Test Cases"}</button></div></div>
      </article>;
    })}</div>{projectFilter === "all" && totalPages > 1 && <nav className="project-pagination" aria-label="Project pages"><span>Showing {(page - 1) * projectsPerPage + 1}–{Math.min(page * projectsPerPage, visibleProjects.length)} of {visibleProjects.length}</span><div><button type="button" aria-label="Previous project page" disabled={page === 1} onClick={() => setPage((current) => current - 1)}><ChevronLeft size={16} /></button>{paginationPages.map((pageNumber) => <button type="button" className={page === pageNumber ? "active" : ""} aria-current={page === pageNumber ? "page" : undefined} key={pageNumber} onClick={() => setPage(pageNumber)}>{pageNumber}</button>)}<button type="button" aria-label="Next project page" disabled={page === totalPages} onClick={() => setPage((current) => current + 1)}><ChevronRight size={16} /></button></div></nav>}</>}
  </div>;
}
