import { useEffect, useMemo, useState } from "react";
import { BookOpen, CheckCircle2, Database, History, LoaderCircle, Search } from "lucide-react";
import {
  evaluateKnowledgeIndex, getKnowledgeSummary, listManifests,
  listProjects, retrieveKnowledge,
} from "../api/client.js";
import { LoadingSpinner } from "./LoadingSpinner.jsx";
import { AppSelect } from "./AppSelect.jsx";
import { Pagination } from "./Pagination.jsx";
import { WorkspacePageHero } from "./WorkspacePageHero.jsx";

function editableRules(payload) {
  return (payload?.rules || []).map((rule) => ({ ...rule, decision: rule.status || rule.decision || "pending", review_comment: rule.review_comment || "" }));
}

export function KnowledgeBasePage({ onContinueToFunctional }) {
  const [projects, setProjects] = useState([]);
  const [project, setProject] = useState(() => JSON.parse(localStorage.getItem("testiq_current_project") || "null"));
  const [packages, setPackages] = useState([]);
  const [packageId, setPackageId] = useState(() => JSON.parse(localStorage.getItem("testiq_current_package") || "null")?.package_id || "");
  const [indexes, setIndexes] = useState([]);
  const [versions, setVersions] = useState([]);
  const [readiness, setReadiness] = useState(null);
  const [selectedIndex, setSelectedIndex] = useState(null);
  const [ruleDrafts, setRuleDrafts] = useState([]);
  const [query, setQuery] = useState("");
  const [retrieval, setRetrieval] = useState(null);
  const [evaluation, setEvaluation] = useState(null);
  const [working, setWorking] = useState("");
  const [versionPage, setVersionPage] = useState(1); const [hitPage, setHitPage] = useState(1);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  function clearSelectedPackage() {
    setPackageId(""); setIndexes([]); setReadiness(null); setSelectedIndex(null); setRuleDrafts([]);
    setRetrieval(null); setEvaluation(null); localStorage.removeItem("testiq_current_package");
  }

  async function loadPackage(id = packageId, options = {}) {
    const silent = Boolean(options.silent);
    if (!id) return;
    if (!silent) { setWorking("load"); setError(""); }
    try {
      const summary = await getKnowledgeSummary(id, silent || options.refresh ? { cache: "no-store" } : {});
      const nextIndexes = summary.indexes || [];
      const nextReadiness = summary.readiness || null;
      const nextRules = summary.business_rules || null;
      const nextVersions = summary.versions || [];
      const list = Array.isArray(nextIndexes) ? nextIndexes : nextIndexes?.indexes || [];
      setIndexes(list); setVersions(Array.isArray(nextVersions) ? nextVersions : []); setReadiness(nextReadiness);
      setRuleDrafts(editableRules(nextRules));
      setSelectedIndex((current) => list.find((item) => item.index_id === current?.index_id)
        || list.find((item) => item.index_id === nextReadiness?.index_id) || list[0] || null);
      if (!silent) { setRetrieval(null); setEvaluation(null); }
    } catch (err) {
      if (err.status === 404) { clearSelectedPackage(); setError("Selected Resource Package was not found."); }
      else if (!silent) setError(err.message);
    } finally { if (!silent) setWorking(""); }
  }

  useEffect(() => {
    let active = true;
    async function initialize() {
      setWorking("projects"); setError("");
      try {
        const allProjects = await listProjects();
        if (!active) return;
        setProjects(allProjects);
        const storedProject = JSON.parse(localStorage.getItem("testiq_current_project") || "null");
        const storedPackage = JSON.parse(localStorage.getItem("testiq_current_package") || "null");
        const chosenProject = allProjects.find((item) => item.project_id === project?.project_id)
          || allProjects.find((item) => item.project_id === storedProject?.project_id) || allProjects[0] || null;
        setProject(chosenProject);
        if (!chosenProject) { setPackages([]); clearSelectedPackage(); return; }
        localStorage.setItem("testiq_current_project", JSON.stringify(chosenProject));
        const projectPackages = await listManifests(chosenProject.project_id);
        if (!active) return;
        setPackages(projectPackages);
        const chosenPackage = projectPackages.find((item) => item.package_id === packageId)
          || projectPackages.find((item) => item.package_id === storedPackage?.package_id) || projectPackages[0] || null;
        setPackageId(chosenPackage?.package_id || "");
        if (chosenPackage) localStorage.setItem("testiq_current_package", JSON.stringify(chosenPackage));
      } catch (err) { if (active) setError(err.message); }
      finally { if (active) setWorking(""); }
    }
    initialize();
    return () => { active = false; };
  }, []);

  useEffect(() => { if (packageId && packages.some((item) => item.package_id === packageId)) loadPackage(packageId); setVersionPage(1); setHitPage(1); }, [packageId, packages]);

  const chosenPackage = useMemo(() => packages.find((item) => item.package_id === packageId), [packages, packageId]);
  const latestIndex = useMemo(() => indexes.find((item) => item.index_id === readiness?.index_id) || indexes[0] || null, [indexes, readiness?.index_id]);
  const progressIndex = latestIndex || selectedIndex;
  const hasReadyIndex = Boolean(readiness?.ready && indexes.some((item) => item.index_id === readiness.index_id));
  const storedProgress = Number(readiness?.progress_percent ?? progressIndex?.progress_percent ?? 0);
  const knowledgeProgress = Math.max(0, Math.min(100, working === "build" ? Math.max(5, storedProgress) : storedProgress));
  const knowledgeStage = readiness?.progress_stage || progressIndex?.progress_stage || progressIndex?.status || "Not built";
  const knowledgeMessage = readiness?.progress_message || progressIndex?.progress_message || readiness?.message || "The Knowledge Base will be created automatically when the BRD and available source documents finish processing.";
  const isKnowledgeBuilding = working === "build" || readiness?.index_status === "building" || progressIndex?.status === "building";
  const isLoadingWorkspace = working === "projects";
  const isLoadingKnowledge = working === "load" && Boolean(chosenPackage);

  useEffect(() => {
    if (!packageId || !isKnowledgeBuilding) return undefined;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") loadPackage(packageId, { silent: true });
    }, 5000);
    return () => window.clearInterval(timer);
  }, [packageId, isKnowledgeBuilding]);

  async function selectProject(id) {
    setWorking("projects"); setError(""); setNotice("");
    const chosen = projects.find((item) => item.project_id === id) || null;
    setProject(chosen); setPackages([]); setVersions([]); clearSelectedPackage();
    try {
      if (!chosen) return;
      localStorage.setItem("testiq_current_project", JSON.stringify(chosen));
      const rows = await listManifests(id); setPackages(rows);
      const first = rows[0] || null; setPackageId(first?.package_id || "");
      if (first) localStorage.setItem("testiq_current_package", JSON.stringify(first));
    } catch (err) { setError(err.message); }
    finally { setWorking(""); }
  }

  async function runRetrieval(event) {
    event.preventDefault(); if (!selectedIndex || !query.trim()) return;
    setWorking("retrieve"); setError(""); setRetrieval(null);
    try { setRetrieval(await retrieveKnowledge(selectedIndex.index_id, { query: query.trim(), requested_by: "TestIQ user", purpose: "workspace_exploration" })); setHitPage(1); }
    catch (err) { setError(err.message); } finally { setWorking(""); }
  }

  async function runEvaluation() {
    if (!selectedIndex) return; setWorking("evaluate"); setError(""); setEvaluation(null);
    try { setEvaluation(await evaluateKnowledgeIndex(selectedIndex.index_id)); } catch (err) { setError(err.message); } finally { setWorking(""); }
  }

  const pageSize = 5;
  const visibleVersions = versions.slice((versionPage - 1) * pageSize, versionPage * pageSize);
  const retrievalHits = retrieval?.hits || [];
  const visibleHits = retrievalHits.slice((hitPage - 1) * pageSize, hitPage * pageSize);
  return <div className="packages-page knowledge-page professional-workspace-page">
    <WorkspacePageHero productLabel="TestIQ · Knowledge Base" eyebrow="PROJECT KNOWLEDGE" title="Knowledge Intelligence" context={project?.project_name} cues={["Automatic indexing", "Version lineage", "Grounded retrieval"]} />
    <section className="knowledge-toolbar">
      <label><span>Project</span><AppSelect value={project?.project_id || ""} onChange={(event) => selectProject(event.target.value)} disabled={isLoadingWorkspace}><option value="">Choose project</option>{projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.project_name}</option>)}</AppSelect></label>
      <label><span>Resource Package</span><AppSelect value={packageId} onChange={(event) => { const id = event.target.value; const chosen = packages.find((item) => item.package_id === id); setPackageId(id); if (chosen) localStorage.setItem("testiq_current_package", JSON.stringify(chosen)); }} disabled={!packages.length || isLoadingWorkspace}><option value="">Choose a Resource Package</option>{packages.map((item) => <option key={item.package_id} value={item.package_id}>v{item.package_version} · {item.package_name || item.scope || "Resource Package"}</option>)}</AppSelect></label>
    </section>
    {error && <div className="error-message">{error}</div>}
    {notice && <div className="toast-message"><CheckCircle2 size={16} />{notice}</div>}
    {isLoadingWorkspace && <LoadingSpinner variant="section" label="Loading workspace" detail="Checking projects and Resource Packages." />}
    {!isLoadingWorkspace && !project && <div className="packages-empty"><BookOpen size={24} /><h2>Select a project</h2><p>Choose the project whose Knowledge Base you want to update.</p></div>}
    {!isLoadingWorkspace && project && !packages.length && <div className="packages-empty"><Database size={24} /><h2>No Resource Packages</h2><p>Create a Resource Package before updating project knowledge.</p></div>}

    {chosenPackage && <>
      <section className="packages-card knowledge-rule-summary">
        <div className="packages-card-head"><div><h2>Automatic indexing</h2><p>Successfully extracted project documents are indexed automatically.</p></div><span className="health-badge">Automatic</span></div>
        <div className="success-actions"><span>{ruleDrafts.length ? `${ruleDrafts.length} BRD rules were captured as traceable Knowledge Base context.` : "The BRD and source documents will be indexed automatically."}</span></div>
      </section>

      <section className="packages-card">
        <div className="packages-card-head"><div><h2>Knowledge Base version</h2><p>Traceable index status and reuse metrics.</p></div><span className={`health-badge ${hasReadyIndex ? "" : "pending"}`}>{hasReadyIndex ? "Ready" : isKnowledgeBuilding ? "Building" : "Waiting for processing"}</span></div>
        {(progressIndex || readiness) && <div className={`knowledge-progress-card ${isKnowledgeBuilding ? "building" : hasReadyIndex ? "ready" : "pending"}`}><div className="knowledge-progress-head"><div><strong>{isKnowledgeBuilding ? "Updating Knowledge Base" : hasReadyIndex ? `Knowledge Base version ${progressIndex?.knowledge_version || 1} ready` : "Knowledge Base status"}</strong><span>{knowledgeStage}</span></div><b>{knowledgeProgress}%</b></div><div className="knowledge-progress-track" role="progressbar" aria-label="Knowledge Base build progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow={knowledgeProgress}><span style={{ width: `${knowledgeProgress}%` }} /></div><p>{knowledgeMessage}</p></div>}
        <div className="review-actions"><label className="package-selector"><span>Knowledge Base version</span><AppSelect value={selectedIndex?.index_id || ""} onChange={(event) => setSelectedIndex(indexes.find((item) => item.index_id === event.target.value) || null)}><option value="">Not built yet</option>{indexes.map((item) => <option key={item.index_id} value={item.index_id}>Version {item.knowledge_version || 1} · {item.status}</option>)}</AppSelect></label><span className={`health-badge ${hasReadyIndex ? "" : "pending"}`}>{hasReadyIndex ? "Ready" : isKnowledgeBuilding ? "Building automatically" : "Waiting for document processing"}</span></div>
        {selectedIndex && <div className="knowledge-summary-grid"><div><span>Version</span><strong>{selectedIndex.knowledge_version || 1}</strong></div><div><span>Reused source sections</span><strong>{selectedIndex.reused_chunk_count || 0}</strong></div><div><span>New or changed</span><strong>{selectedIndex.new_chunk_count ?? selectedIndex.chunk_count ?? 0}</strong></div><div><span>Removed</span><strong>{selectedIndex.retired_chunk_count || 0}</strong></div></div>}
        {selectedIndex && <div className="source-list-card"><strong>{selectedIndex.chunk_count ?? 0} indexed source sections</strong><span> · {selectedIndex.node_count ?? 0} knowledge items · {selectedIndex.edge_count ?? 0} relationships</span><p>{selectedIndex.build_mode === "incremental" ? `Updated from Knowledge Base version ${versions.find((item) => item.index_id === selectedIndex.parent_index_id)?.knowledge_version || "previous"}.` : "Initial Knowledge Base version."}</p>{selectedIndex.error && <div className="error-message">{selectedIndex.error}</div>}</div>}
      </section>

      {versions.length > 0 && <section className="packages-card"><div className="packages-card-head"><div><h2>Knowledge Base history</h2><p>Version lineage for audit and traceability.</p></div><History size={20} /></div><div className="knowledge-version-list">{visibleVersions.map((item) => <div className="knowledge-version-row" key={item.index_id}><span className="package-version large">v{item.knowledge_version || 1}</span><div><strong>{item.package_name || `Resource Package ${item.package_version}`}</strong><small>{item.status} · {item.chunk_count || 0} source sections</small></div><span>{item.created_at ? new Date(item.created_at).toLocaleString() : ""}</span></div>)}</div><Pagination page={versionPage} pageSize={pageSize} totalItems={versions.length} onPageChange={setVersionPage} label="knowledge versions" /></section>}

      {selectedIndex?.status === "ready" && <section className="packages-card"><div className="packages-card-head"><div><h2>Search the Knowledge Base</h2><p>Retrieve grounded source evidence.</p></div></div><form className="review-actions" onSubmit={runRetrieval}><input className="search-input" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Ask about requirements, eligibility, database fields, or API behavior" /><button className="secondary-button" type="submit" disabled={Boolean(working) || !query.trim()}>{working === "retrieve" ? <LoaderCircle className="spin" size={16} /> : <Search size={16} />} Search</button><button className="secondary-button" type="button" onClick={runEvaluation} disabled={Boolean(working)}>{working === "evaluate" ? <LoaderCircle className="spin" size={16} /> : null} Run quality check</button></form>{retrieval && <div className="knowledge-results"><h3>{retrievalHits.length} matching source sections</h3>{visibleHits.map((hit) => <article className="source-list-card" key={hit.chunk_id}><strong>{hit.source_reference}</strong><p>{hit.chunk_text}</p><small>{hit.source_type} · match {Number(hit.retrieval_score || 0).toFixed(3)}</small></article>)}<Pagination page={hitPage} pageSize={pageSize} totalItems={retrievalHits.length} onPageChange={setHitPage} label="knowledge results" /></div>}{evaluation && <div className="toast-message">Quality check {evaluation.passed ? "passed" : "needs review"}: {evaluation.results?.filter((item) => item.passed).length || 0} of {evaluation.results?.length || 0} questions passed.</div>}</section>}
      {hasReadyIndex && <div className="success-actions knowledge-next-step"><span>Knowledge Base version {progressIndex?.knowledge_version || 1} is ready to support Functional Requirement generation.</span><button className="primary-button" onClick={() => onContinueToFunctional?.()} disabled={!onContinueToFunctional}>Open Functional Requirements</button></div>}
    </>}
    {isLoadingKnowledge && <LoadingSpinner variant="section" label="Loading Knowledge Base" detail="Checking rules, version history, and index status." />}
  </div>;
}
