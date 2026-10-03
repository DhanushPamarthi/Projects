import { useEffect, useMemo, useState } from "react";
import { CheckCircle2, FileCheck2, LoaderCircle, PlayCircle, XCircle } from "lucide-react";
import { executeExecutionRun, getProject, listGeneratedTestCases, listManifests, listProjects, listRequirementAnalyses, listExecutionRuns } from "../api/client.js";

function readStoredJson(key) {
  try {
    const saved = localStorage.getItem(key);
    return saved ? JSON.parse(saved) : null;
  } catch {
    localStorage.removeItem(key);
    return null;
  }
}

function formatDate(value) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function runDateLabel(run) {
  const value = run?.completed_at || run?.approved_at || run?.created_at;
  if (!value) return run?.run_id || "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const pad = (item) => String(item).padStart(2, "0");
  return `Run_${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}_${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`;
}

function runStatus(run) {
  return String(run?.lifecycle_state || run?.status || "selected").toLowerCase();
}

function testResultStatus(run) {
  const summaryStatus = run?.execution_summary?.status;
  if (summaryStatus === "pass") return "Pass";
  if (summaryStatus === "fail") return "Fail";
  const state = runStatus(run);
  if (state === "running") return "In progress";
  if (state === "cancelled") return "Cancelled";
  if (state === "failed") return "Baseline failed";
  if (state === "completed") return "No results recorded";
  return "Not executed";
}

function statusClass(status) {
  return status.toLowerCase().replaceAll(" ", "-");
}

function resultIcon(status) {
  if (status === "passed") return <CheckCircle2 size={16} />;
  if (status === "failed") return <XCircle size={16} />;
  return <FileCheck2 size={16} />;
}

export function TestResultsPage() {
  const [projects, setProjects] = useState([]);
  const [project, setProject] = useState(() => readStoredJson("testiq_current_project"));
  const [packages, setPackages] = useState([]);
  const [packageId, setPackageId] = useState(() => readStoredJson("testiq_current_package")?.package_id || "");
  const [runs, setRuns] = useState([]);
  const [loading, setLoading] = useState(true);
  const [executingRunId, setExecutingRunId] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setError("");
      try {
        const projectItems = await listProjects();
        if (cancelled) return;
        setProjects(projectItems);
        const savedProject = readStoredJson("testiq_current_project");
        const selectedProject = projectItems.find((item) => item.project_id === savedProject?.project_id) || projectItems[0];
        if (!selectedProject) return;
        const projectDetails = await getProject(selectedProject.project_id);
        if (cancelled) return;
        setProject(projectDetails);
        localStorage.setItem("testiq_current_project", JSON.stringify(projectDetails));
        const packageItems = await listManifests(projectDetails.project_id);
        if (cancelled) return;
        setPackages(packageItems);
        const savedPackage = readStoredJson("testiq_current_package");
        const selectedPackage = packageItems.find((item) => item.package_id === savedPackage?.package_id) || packageItems[0];
        if (selectedPackage) {
          setPackageId(selectedPackage.package_id);
          localStorage.setItem("testiq_current_package", JSON.stringify(selectedPackage));
        }
      } catch (err) {
        if (!cancelled) setError(err.message || "Unable to load test results.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function loadRuns() {
      setRuns([]);
      if (!packageId) { setLoading(false); return; }
      setLoading(true);
      setError("");
      try {
        const analyses = await listRequirementAnalyses(packageId);
        const generationGroups = await Promise.all(analyses.map((analysis) => listGeneratedTestCases(analysis.analysis_id).catch(() => [])));
        const generations = generationGroups.flat();
        const runGroups = await Promise.all(generations.map((generation) => listExecutionRuns(generation.generation_id).catch(() => [])));
        const generationById = new Map(generations.map((generation) => [generation.generation_id, generation]));
        const nextRuns = runGroups.flat().map((run) => ({
          ...run,
          generation: generationById.get(run.generation_id),
        })).sort((left, right) => String(right.created_at || "").localeCompare(String(left.created_at || "")));
        if (!cancelled) setRuns(nextRuns);
      } catch (err) {
        if (!cancelled) setError(err.message || "Unable to load test results.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    loadRuns();
    return () => { cancelled = true; };
  }, [packageId]);

  async function selectProject(projectId) {
    setProject(null);
    setPackages([]);
    setPackageId("");
    setRuns([]);
    if (!projectId) { setLoading(false); localStorage.removeItem("testiq_current_project"); return; }
    setLoading(true);
    setError("");
    try {
      const selectedProject = await getProject(projectId);
      setProject(selectedProject);
      localStorage.setItem("testiq_current_project", JSON.stringify(selectedProject));
      const items = await listManifests(projectId);
      setPackages(items);
      const selectedPackage = items[0];
      if (selectedPackage) {
        setPackageId(selectedPackage.package_id);
        localStorage.setItem("testiq_current_package", JSON.stringify(selectedPackage));
      }
    } catch (err) {
      setError(err.message || "Unable to load source packages.");
      setLoading(false);
    }
  }

  function selectPackage(id) {
    setPackageId(id);
    const selectedPackage = packages.find((item) => item.package_id === id);
    if (selectedPackage) localStorage.setItem("testiq_current_package", JSON.stringify(selectedPackage));
  }

  async function executeRun(run) {
    setExecutingRunId(run.run_id);
    setError("");
    try {
      const updated = await executeExecutionRun(run.run_id, { reviewer: "Product Owner" });
      setRuns((items) => items.map((item) => item.run_id === run.run_id ? { ...updated, generation: item.generation } : item));
    } catch (err) {
      setError(err.message || "Unable to execute this baseline.");
    } finally {
      setExecutingRunId("");
    }
  }

  const summary = useMemo(() => {
    const passed = runs.filter((run) => testResultStatus(run) === "Pass").length;
    const failed = runs.filter((run) => testResultStatus(run) === "Fail").length;
    const executed = passed + failed;
    return { passed, failed, executed };
  }, [runs]);

  const runsWithResults = runs.filter((run) => run.execution_results?.length);

  return <div className="requirements-page">
    <div className="page-heading"><div><div className="eyebrow">TEST EXECUTION</div><h1>Test Results</h1><p>Execute frozen baselines and review the saved Bronze validation result for every selected test case.</p></div></div>
    <div className="requirements-toolbar"><div><label>Project</label><select value={project?.project_id || ""} onChange={(event) => selectProject(event.target.value)}><option value="">Select project</option>{projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.project_name}</option>)}</select></div><div><label>Source package</label><select value={packageId} onChange={(event) => selectPackage(event.target.value)} disabled={!packages.length}><option value="">Select package</option>{packages.map((item) => <option key={item.package_id} value={item.package_id}>{item.package_name || item.scope || "Source package"} - v{item.package_version || "?"}</option>)}</select></div></div>
    {error && <div className="error-message">{error}</div>}
    {loading ? <section className="requirements-panel"><LoaderCircle className="spin" size={18} /> Loading execution baselines...</section>
      : !runs.length ? <section className="requirements-empty"><div className="empty-state-icon"><FileCheck2 size={25} /></div><h2>No execution baselines yet</h2><p>Freeze approved test cases after saving their test data to Bronze. Then execute the baseline here.</p></section>
        : <>
          <div className="requirements-summary-grid"><div><span>Total baselines</span><strong>{runs.length}</strong></div><div><span>Executed</span><strong>{summary.executed}</strong></div><div><span>Passed</span><strong>{summary.passed}</strong></div><div><span>Failed</span><strong>{summary.failed}</strong></div></div>
          <section className="requirements-panel"><div className="panel-title-row"><div><h2>Execution baselines</h2><p className="panel-helper">Run a baseline after its test data is saved to Bronze. Results are stored per test case with Bronze row evidence.</p></div><span>{runs.length} baseline{runs.length === 1 ? "" : "s"}</span></div><div className="business-requirements-table-wrap"><table className="business-requirements-table test-results-table"><thead><tr><th>Baseline ID</th><th>Project Name</th><th>Selected cases</th><th>Status</th><th>Created On</th><th>Action</th></tr></thead><tbody>{runs.map((run) => { const status = testResultStatus(run); const state = runStatus(run); const blocked = state === "cancelled" || executingRunId === run.run_id; return <tr key={run.run_id}><td>{runDateLabel(run)}</td><td>{project?.project_name || "-"}</td><td>{run.selected_test_case_ids?.length || 0}</td><td><span className={`execution-run-status ${statusClass(status)}`}>{status}</span></td><td>{formatDate(run.created_at)}</td><td><button type="button" className="secondary-button compact-button" disabled={blocked} onClick={() => executeRun(run)}>{executingRunId === run.run_id ? <><LoaderCircle className="spin" size={14} /> Running...</> : <><PlayCircle size={14} /> {run.execution_results?.length ? "Re-run" : "Execute"}</>}</button></td></tr>; })}</tbody></table></div></section>
          {runsWithResults.length > 0 && <section className="requirements-panel"><div className="panel-title-row"><div><h2>Execution evidence</h2><p className="panel-helper">Each result shows the generated setup data compared with the Bronze rows inserted for that test case.</p></div><span>{runsWithResults.length} run{runsWithResults.length === 1 ? "" : "s"} with results</span></div><div className="execution-result-list">{runsWithResults.map((run) => <article key={run.run_id} className="execution-result-run"><div className="execution-result-run-head"><div><strong>{runDateLabel(run)}</strong><span>{run.execution_summary?.passed || 0} passed / {run.execution_summary?.failed || 0} failed</span></div><span className={`execution-run-status ${statusClass(testResultStatus(run))}`}>{testResultStatus(run)}</span></div><div className="execution-result-cases">{run.execution_results.map((result) => { const failedAssertions = (result.assertions || []).filter((item) => item.status === "failed"); return <details key={result.result_id} className={`execution-result-case ${result.status}`}><summary>{resultIcon(result.status)} <strong>{result.test_case_id}</strong><span>{result.status}</span>{failedAssertions.length ? <em>{failedAssertions.length} failed assertion{failedAssertions.length === 1 ? "" : "s"}</em> : <em>{(result.assertions || []).length} assertion{(result.assertions || []).length === 1 ? "" : "s"} passed</em>}</summary><div className="execution-assertion-list">{(result.assertions || []).map((assertion, index) => <div key={`${result.result_id}-${index}`} className={`execution-assertion ${assertion.status}`}><span>{assertion.status === "passed" ? <CheckCircle2 size={14} /> : <XCircle size={14} />}</span><div><strong>{assertion.name}</strong><small>{assertion.message}</small>{assertion.status === "failed" && <code>Expected: {JSON.stringify(assertion.expected)} | Actual: {JSON.stringify(assertion.actual)}</code>}</div></div>)}</div><div className="execution-bronze-refs"><strong>Bronze refs</strong><span>{(result.bronze_refs || []).map((ref) => `${ref.bronze_table}:${ref.bronze_id}`).join(", ") || "No Bronze refs"}</span></div></details>; })}</div></article>)}</div></section>}
        </>}
  </div>;
}
