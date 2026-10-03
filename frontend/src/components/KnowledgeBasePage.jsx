import { useEffect, useMemo, useState } from "react";
import { BookOpen, CheckCircle2, Database, LoaderCircle, RefreshCw, Search } from "lucide-react";
import { buildKnowledgeIndex, evaluateKnowledgeIndex, getRagReadiness, listKnowledgeIndexes, listManifests, listProjects, retrieveKnowledge } from "../api/client.js";
import { LoadingSpinner } from "./LoadingSpinner.jsx";

export function KnowledgeBasePage({ onContinueToBusinessRequirements }) {
  const [projects, setProjects] = useState([]);
  const [project, setProject] = useState(() => JSON.parse(localStorage.getItem("testiq_current_project") || "null"));
  const [packages, setPackages] = useState([]);
  const [packageId, setPackageId] = useState(() => JSON.parse(localStorage.getItem("testiq_current_package") || "null")?.package_id || "");
  const [indexes, setIndexes] = useState([]);
  const [readiness, setReadiness] = useState(null);
  const [selectedIndex, setSelectedIndex] = useState(null);
  const [query, setQuery] = useState("");
  const [retrieval, setRetrieval] = useState(null);
  const [evaluation, setEvaluation] = useState(null);
  const [working, setWorking] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  function clearSelectedPackage() {
    setPackageId("");
    setIndexes([]);
    setReadiness(null);
    setSelectedIndex(null);
    setRetrieval(null);
    setEvaluation(null);
    localStorage.removeItem("testiq_current_package");
  }

  async function loadPackage(id = packageId) {
    if (!id) {
      setIndexes([]);
      setReadiness(null);
      setSelectedIndex(null);
      setWorking("");
      return;
    }
    setWorking("load");
    setError("");
    try {
      const [nextIndexes, nextReadiness] = await Promise.all([listKnowledgeIndexes(id), getRagReadiness(id)]);
      const list = Array.isArray(nextIndexes) ? nextIndexes : nextIndexes?.indexes || [];
      setIndexes(list);
      setReadiness(nextReadiness);
      setSelectedIndex(list[0] || null);
      setRetrieval(null);
      setEvaluation(null);
    } catch (err) {
      if (err.status === 404) {
        clearSelectedPackage();
        setError("Selected source package was not found. Choose an available source package or create a new one.");
      } else {
        setError(err.message);
      }
    } finally {
      setWorking("");
    }
  }

  useEffect(() => {
    let active = true;
    async function initialize() {
      setWorking("projects");
      setError("");
      try {
        const allProjects = await listProjects();
        if (!active) return;
        setProjects(allProjects);
        const storedProject = JSON.parse(localStorage.getItem("testiq_current_project") || "null");
        const storedPackage = JSON.parse(localStorage.getItem("testiq_current_package") || "null");
        const chosenProject = allProjects.find((item) => item.project_id === project?.project_id)
          || allProjects.find((item) => item.project_id === storedProject?.project_id)
          || allProjects[0]
          || null;
        setProject(chosenProject);
        if (!chosenProject) {
          setPackages([]);
          clearSelectedPackage();
          localStorage.removeItem("testiq_current_project");
          return;
        }
        localStorage.setItem("testiq_current_project", JSON.stringify(chosenProject));
        const projectPackages = await listManifests(chosenProject.project_id);
        if (!active) return;
        setPackages(projectPackages);
        const chosenPackage = projectPackages.find((item) => item.package_id === packageId)
          || projectPackages.find((item) => item.package_id === storedPackage?.package_id)
          || projectPackages[0]
          || null;
        setPackageId(chosenPackage?.package_id || "");
        if (chosenPackage) {
          localStorage.setItem("testiq_current_package", JSON.stringify(chosenPackage));
        } else {
          clearSelectedPackage();
        }
      } catch (err) {
        if (active) setError(err.message);
      } finally {
        if (active) setWorking("");
      }
    }
    initialize();
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (packageId && packages.some((item) => item.package_id === packageId)) loadPackage(packageId);
  }, [packageId, packages]);

  const chosenPackage = useMemo(() => packages.find((item) => item.package_id === packageId), [packages, packageId]);
  const statusLabel = readiness?.ready ? "Ready" : selectedIndex?.status || readiness?.status || "Not built";
  const hasReadyIndex = selectedIndex?.status === "ready" && readiness?.ready !== false;
  const isLoadingWorkspace = working === "projects";
  const isLoadingKnowledge = working === "load" && Boolean(chosenPackage);

  async function selectProject(id) {
    setWorking("projects");
    setError("");
    const chosen = projects.find((item) => item.project_id === id);
    const storedPackage = JSON.parse(localStorage.getItem("testiq_current_package") || "null");
    setProject(chosen || null);
    setPackages([]);
    clearSelectedPackage();
    try {
      if (chosen) {
        localStorage.setItem("testiq_current_project", JSON.stringify(chosen));
        const rows = await listManifests(id);
        setPackages(rows);
        const chosenPackage = rows.find((item) => item.package_id === storedPackage?.package_id) || rows[0] || null;
        setPackageId(chosenPackage?.package_id || "");
        if (chosenPackage) localStorage.setItem("testiq_current_package", JSON.stringify(chosenPackage));
        else clearSelectedPackage();
      } else {
        localStorage.removeItem("testiq_current_project");
        clearSelectedPackage();
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setWorking("");
    }
  }

  async function buildIndex() {
    if (!packageId) return;
    setWorking("build");
    setError("");
    setNotice("");
    try {
      const built = await buildKnowledgeIndex(packageId);
      setNotice(built.reused ? "Knowledge Base is already ready for this package." : "Knowledge Base is ready for this package.");
      await loadPackage(packageId);
      if (built.index_id) setSelectedIndex(built);
    } catch (err) {
      setError(err.message);
    } finally {
      setWorking("");
    }
  }

  async function runRetrieval(event) {
    event.preventDefault();
    if (!selectedIndex || !query.trim()) return;
    setWorking("retrieve");
    setError("");
    setRetrieval(null);
    try {
      setRetrieval(await retrieveKnowledge(selectedIndex.index_id, { query: query.trim(), requested_by: "TestIQ user", purpose: "workspace_exploration" }));
    } catch (err) {
      setError(err.message);
    } finally {
      setWorking("");
    }
  }

  async function runEvaluation() {
    if (!selectedIndex) return;
    setWorking("evaluate");
    setError("");
    setEvaluation(null);
    try {
      setEvaluation(await evaluateKnowledgeIndex(selectedIndex.index_id));
    } catch (err) {
      setError(err.message);
    } finally {
      setWorking("");
    }
  }

  return <div className="packages-page knowledge-page">
    <div className="page-heading">
      <div>
        <div className="eyebrow">WORKSPACE TOOLS</div>
        <h1>Knowledge Base</h1>
        <p>Prepare package evidence in the background so generation can use it when it is available.</p>
      </div>
      <button className="secondary-button" onClick={() => loadPackage()} disabled={!chosenPackage || Boolean(working)}>
        {isLoadingKnowledge ? <LoaderCircle className="spin" size={16} /> : <RefreshCw size={16} />} Refresh
      </button>
    </div>

    <section className="knowledge-toolbar">
      <label>
        <span>Project</span>
        <select value={project?.project_id || ""} onChange={(event) => selectProject(event.target.value)} disabled={isLoadingWorkspace}>
          <option value="">Choose project</option>
          {projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.project_name}</option>)}
        </select>
      </label>
      <label>
        <span>Source package</span>
        <select value={packageId} onChange={(event) => {
          const id = event.target.value;
          const chosen = packages.find((item) => item.package_id === id);
          setPackageId(id);
          if (chosen) localStorage.setItem("testiq_current_package", JSON.stringify(chosen));
          else clearSelectedPackage();
        }} disabled={!packages.length || isLoadingWorkspace}>
          <option value="">Choose source package</option>
          {packages.map((item) => <option key={item.package_id} value={item.package_id}>v{item.package_version} · {item.package_name || item.scope || "Source package"}</option>)}
        </select>
      </label>
    </section>

    {error && <div className="error-message">{error}</div>}
    {notice && <div className="toast-message"><CheckCircle2 size={16} />{notice}</div>}

    {isLoadingWorkspace && <LoadingSpinner variant="section" label="Loading workspace" detail="Checking projects and source packages." />}
    {!isLoadingWorkspace && !project && <div className="packages-empty"><BookOpen size={24} /><h2>Select a project</h2><p>Choose the project that contains the source package to prepare its Knowledge Base.</p></div>}
    {!isLoadingWorkspace && project && !packages.length && <div className="packages-empty"><Database size={24} /><h2>No source packages</h2><p>Create a source package before building its Knowledge Base.</p></div>}

    {chosenPackage && <>
      <section className="packages-card">
        <div className="packages-card-head">
          <div>
            <h2>Automatic Knowledge Base</h2>
            <p>The source package documents are indexed automatically. Main flow generation uses this context when it is ready and continues with the current stage input when it is not ready.</p>
          </div>
          <span className={`health-badge ${hasReadyIndex ? "" : "pending"}`}>{statusLabel}</span>
        </div>
        <div className="source-list-card">
          <strong>{chosenPackage.package_name || chosenPackage.scope || "Source package"}</strong>
          <span> · v{chosenPackage.package_version} · {chosenPackage.source_count || chosenPackage.items?.length || 0} source(s)</span>
          <p>{readiness?.message || "Knowledge Base status is checked from the latest package index."}</p>
        </div>
        <div className="review-actions">
          <label className="package-selector">
            <span>Index version</span>
            <select value={selectedIndex?.index_id || ""} onChange={(event) => setSelectedIndex(indexes.find((item) => item.index_id === event.target.value) || null)}>
              <option value="">No index</option>
              {indexes.map((item) => <option key={item.index_id} value={item.index_id}>{item.status} · {item.created_at ? new Date(item.created_at).toLocaleString() : item.index_id}</option>)}
            </select>
          </label>
          <button className="primary-button" onClick={buildIndex} disabled={!packageId || Boolean(working)}>
            {working === "build" ? <LoaderCircle className="spin" size={16} /> : <BookOpen size={16} />} {indexes.length ? "Rebuild Knowledge Base" : "Build Knowledge Base"}
          </button>
        </div>
        {selectedIndex && <div className="source-list-card">
          <strong>{selectedIndex.chunk_count ?? 0} evidence chunks</strong>
          <span> · {selectedIndex.node_count ?? 0} knowledge nodes · {selectedIndex.edge_count ?? 0} relationships</span>
          <p>Index: {selectedIndex.index_id}</p>
          {selectedIndex.error && <div className="error-message">{selectedIndex.error}</div>}
        </div>}
      </section>

      {selectedIndex?.status === "ready" && <section className="packages-card">
        <div className="packages-card-head">
          <div>
            <h2>Explore indexed evidence</h2>
            <p>Search package context and inspect source citations.</p>
          </div>
        </div>
        <form className="review-actions" onSubmit={runRetrieval}>
          <input className="search-input" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Ask about requirements, eligibility, schema, or API behavior" />
          <button className="secondary-button" type="submit" disabled={Boolean(working) || !query.trim()}>
            {working === "retrieve" ? <LoaderCircle className="spin" size={16} /> : <Search size={16} />} Retrieve
          </button>
          <button className="secondary-button" type="button" onClick={runEvaluation} disabled={Boolean(working)}>
            {working === "evaluate" ? <LoaderCircle className="spin" size={16} /> : null} Evaluate
          </button>
        </form>
        {retrieval && <div className="knowledge-results">
          <h3>{retrieval.hits?.length || 0} evidence matches</h3>
          {retrieval.hits?.map((hit) => <article className="source-list-card" key={hit.chunk_id}>
            <strong>{hit.source_reference}</strong>
            <p>{hit.chunk_text}</p>
            <small>{hit.source_type} · score {Number(hit.retrieval_score || 0).toFixed(3)} · {hit.chunk_id}</small>
          </article>)}
        </div>}
        {evaluation && <div className="toast-message">Evaluation {evaluation.passed ? "passed" : "needs review"}: {evaluation.results?.filter((item) => item.passed).length || 0} of {evaluation.results?.length || 0} questions passed.</div>}
      </section>}

      {hasReadyIndex && <div className="success-actions knowledge-next-step">
        <span>Knowledge Base is ready to support Business Use Cases, Functional Requirements, Test Cases, and Test Data generation.</span>
        <button className="primary-button" onClick={() => onContinueToBusinessRequirements?.()} disabled={!onContinueToBusinessRequirements}>Open Business Use Cases</button>
      </div>}
    </>}

    {isLoadingKnowledge && <LoadingSpinner variant="section" label="Loading Knowledge Base" detail="Checking package index status." />}
  </div>;
}
