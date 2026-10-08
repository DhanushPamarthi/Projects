import { useEffect, useMemo, useState } from "react";
import { BrainCircuit, CheckCircle2, FileCheck2, Gauge, LoaderCircle, PlayCircle, ShieldCheck, XCircle } from "lucide-react";
import { executeExecutionRun, getProject, listGeneratedTestCases, listManifests, listProjects, listRequirementAnalyses, listExecutionRuns } from "../api/client.js";
import { AppSelect } from "./AppSelect.jsx";
import { LoadingSpinner } from "./LoadingSpinner.jsx";
import { Pagination } from "./Pagination.jsx";
import { WorkspacePageHero } from "./WorkspacePageHero.jsx";

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
  if (summaryStatus === "waiting_for_cdp") return "Waiting for CDP";
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
  if (status === "pending") return <LoaderCircle size={16} />;
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
  const [runPage, setRunPage] = useState(1); const [resultPage, setResultPage] = useState(1); const [evidencePage, setEvidencePage] = useState(1);
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
        if (!cancelled) {
          setRuns(nextRuns);
        }
      } catch (err) {
        if (!cancelled) setError(err.message || "Unable to load test results.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    loadRuns();
    return () => { cancelled = true; };
  }, [packageId]);
  useEffect(() => { setRunPage(1); setResultPage(1); setEvidencePage(1); }, [packageId]);

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
    const waiting = runs.filter((run) => testResultStatus(run) === "Waiting for CDP").length;
    const executed = passed + failed;
    const total = runs.length;
    return { passed, failed, executed, waiting, total, progress: total ? Math.round((executed / total) * 100) : 0 };
  }, [runs]);

  const runsWithResults = runs.filter((run) => run.execution_results?.length);
  const executionResultRows = useMemo(() => runs.flatMap((run) => {
    const cases = run.generation?.contract?.test_cases || run.generation?.test_cases || [];
    const titles = new Map(cases.map((item) => [item.test_case_id, item.title || item.scenario || "Untitled test case"]));
    return (run.execution_results || []).map((result) => ({
      ...result,
      title: titles.get(result.test_case_id) || result.title || "Untitled test case",
      executedAt: result.completed_at || result.executed_at || run.completed_at || run.updated_at || run.created_at,
    }));
  }), [runs]);
  const pageSize = 5;
  const visibleRuns = runs.slice((runPage - 1) * pageSize, runPage * pageSize);
  const visibleResultRows = executionResultRows.slice((resultPage - 1) * pageSize, resultPage * pageSize);
  const visibleEvidenceRuns = runsWithResults.slice((evidencePage - 1) * pageSize, evidencePage * pageSize);
  const executionResultsTable = <section className="requirements-panel execution-results-table-panel"><div className="panel-title-row"><div><h2>Execution results</h2><p className="panel-helper">Latest validated outcomes.</p></div><span>{executionResultRows.length} result{executionResultRows.length === 1 ? "" : "s"}</span></div><div className="execution-results-table-wrap"><table className="execution-results-table"><thead><tr><th>TC#</th><th>Title</th><th>Status</th><th>Failure Details</th><th>Executed On</th></tr></thead><tbody>{executionResultRows.length ? visibleResultRows.map((result) => <tr key={result.result_id || `${result.test_case_id}-${result.executedAt}`}><td><strong>{result.test_case_id}</strong></td><td>{result.title}</td><td><span className={`execution-run-status ${statusClass(result.status || "pending")}`}>{({ passed: "Pass", failed: "Fail", pending: "Not Run", pass: "Pass", fail: "Fail" })[result.status] || "Not Run"}</span></td><td>{result.status === "failed" ? (result.message || (result.assertions || []).filter((item) => item.status === "failed").map((item) => item.message || item.name).join("; ") || "Execution failed") : "-"}</td><td>{formatDate(result.executedAt)}</td></tr>) : <tr><td colSpan="5" className="execution-results-empty">No execution results yet.</td></tr>}</tbody></table></div><Pagination page={resultPage} pageSize={pageSize} totalItems={executionResultRows.length} onPageChange={setResultPage} label="execution results" /></section>;

  return <div className="requirements-page professional-workspace-page test-results-page">
    <WorkspacePageHero productLabel="TestIQ · Test Execution" eyebrow="TEST EXECUTION" title="Execution Intelligence" context={project?.project_name} cues={["Layer validation", "Outcome evidence", "Release confidence"]} />
    <section className="execution-dashboard execution-health-dashboard">
      <div className="execution-dashboard-heading"><div><div className="eyebrow">EXECUTION HEALTH</div><h2>Delivery validation</h2></div><span className="execution-dashboard-count">{summary.total} run{summary.total === 1 ? "" : "s"}</span></div>
      <div className="execution-kpi-grid">
        <article className="execution-kpi-card progress">
          <span className="execution-kpi-icon"><Gauge size={18} /></span>
          <div className="execution-kpi-copy"><span>Execution progress</span><strong>{summary.progress}%</strong><small>{summary.total ? `${summary.executed} of ${summary.total} runs complete` : "No runs available"}</small></div>
          <div className="execution-kpi-track" aria-label={`${summary.progress}% execution progress`}><span style={{ width: `${summary.progress}%` }} /></div>
        </article>
        <article className="execution-kpi-card outcomes">
          <span className="execution-kpi-icon"><ShieldCheck size={18} /></span>
          <div className="execution-kpi-copy"><span>Validated outcomes</span><strong>{summary.passed} passed</strong><small>{summary.failed} failed · {summary.waiting} awaiting validation</small></div>
          <div className="execution-outcome-chips"><span className="passed">{summary.passed} pass</span><span className="failed">{summary.failed} fail</span><span className="waiting">{summary.waiting} waiting</span></div>
        </article>
        <article className="execution-kpi-card ai-usage">
          <span className="execution-kpi-icon"><BrainCircuit size={18} /></span>
          <div className="execution-kpi-copy"><span>AI model usage</span><strong>Usage telemetry</strong><small>Tokens, API calls, and cost are available in LLM Token Usage.</small></div>
          <span className="execution-kpi-availability">Detailed telemetry available</span>
        </article>
      </div>
    </section>
    <div className="requirements-toolbar"><div><label>Project</label><AppSelect value={project?.project_id || ""} onChange={(event) => selectProject(event.target.value)}><option value="">Select project</option>{projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.project_name}</option>)}</AppSelect></div><div><label>Source package</label><AppSelect value={packageId} onChange={(event) => selectPackage(event.target.value)} disabled={!packages.length}><option value="">Select package</option>{packages.map((item) => <option key={item.package_id} value={item.package_id}>{item.package_name || item.scope || "Source package"} - v{item.package_version || "?"}</option>)}</AppSelect></div></div>
    {error && <div className="error-message">{error}</div>}
    {loading ? <LoadingSpinner variant="section" label="Loading execution results" detail="Collecting the selected package's latest run evidence." />
      : !runs.length ? <>{executionResultsTable}<section className="requirements-empty"><div className="empty-state-icon"><FileCheck2 size={25} /></div><h2>No execution runs yet</h2><p>Mark reviewed test cases ready for execution from the Test Data page. TestIQ creates a run for the selected cases; validate the layer outputs here after processing completes.</p></section></>
        : <>
          <div className="requirements-summary-grid"><div><span>Total baselines</span><strong>{runs.length}</strong></div><div><span>Executed</span><strong>{summary.executed}</strong></div><div><span>Passed</span><strong>{summary.passed}</strong></div><div><span>Failed</span><strong>{summary.failed}</strong></div><div><span>Waiting for CDP</span><strong>{summary.waiting}</strong></div></div>
          {executionResultsTable}
          <section className="requirements-panel"><div className="panel-title-row"><div><h2>Execution runs</h2><p className="panel-helper">Validate layer evidence for each run.</p></div><span>{runs.length} run{runs.length === 1 ? "" : "s"}</span></div><div className="business-requirements-table-wrap"><table className="business-requirements-table test-results-table"><thead><tr><th>Run ID</th><th>Project Name</th><th>Selected cases</th><th>Data records</th><th>Status</th><th>Created On</th><th>Action</th></tr></thead><tbody>{visibleRuns.map((run) => { const status = testResultStatus(run); const state = runStatus(run); const blocked = state === "cancelled" || executingRunId === run.run_id; const hasResults = Boolean(run.execution_results?.length); const action = status === "Waiting for CDP" ? "Check layer results" : hasResults ? "Recheck layer results" : "Check layer results"; const selectedRecords = run.frozen_contract?.selected_test_data_record_ids || run.configuration_snapshot?.selected_test_data_record_ids || []; return <tr key={run.run_id}><td>{runDateLabel(run)}</td><td>{project?.project_name || "-"}</td><td>{run.selected_test_case_ids?.length || 0}</td><td>{selectedRecords.length || run.frozen_contract?.test_data_records?.length || 0}</td><td><span className={`execution-run-status ${statusClass(status)}`}>{status}</span></td><td>{formatDate(run.created_at)}</td><td><button type="button" className="secondary-button compact-button" disabled={blocked} onClick={() => executeRun(run)}>{executingRunId === run.run_id ? <><LoaderCircle className="spin" size={14} /> Checking...</> : <><PlayCircle size={14} /> {action}</>}</button></td></tr>; })}</tbody></table></div><Pagination page={runPage} pageSize={pageSize} totalItems={runs.length} onPageChange={setRunPage} label="execution runs" /></section>
          {runsWithResults.length > 0 && <section className="requirements-panel"><div className="panel-title-row"><div><h2>Execution evidence</h2><p className="panel-helper">Bronze-to-Transport validation evidence.</p></div><span>{runsWithResults.length} run{runsWithResults.length === 1 ? "" : "s"} with results</span></div><div className="execution-result-list">{visibleEvidenceRuns.map((run) => <article key={run.run_id} className="execution-result-run"><div className="execution-result-run-head"><div><strong>{runDateLabel(run)}</strong><span>{run.execution_summary?.passed || 0} passed / {run.execution_summary?.failed || 0} failed / {run.execution_summary?.pending || 0} waiting</span></div><span className={`execution-run-status ${statusClass(testResultStatus(run))}`}>{testResultStatus(run)}</span></div><div className="execution-result-cases">{run.execution_results.map((result) => { const failedAssertions = (result.assertions || []).filter((item) => item.status === "failed"); const pendingAssertions = (result.assertions || []).filter((item) => item.status === "pending"); const actual = result.actual || {}; const layers = [{ label: "Bronze", count: actual.bronze_rows?.length || 0 }, { label: "Silver", count: (actual.silver?.members?.length || 0) + (actual.silver?.prescriptions?.length || 0) }, { label: "Gold", count: (actual.gold || []).length }, { label: "Transport", count: actual.transport?.length || 0 }]; return <details key={result.result_id} className={`execution-result-case ${result.status}`}><summary>{resultIcon(result.status)} <strong>{result.test_case_id}</strong><span>{result.status}</span>{failedAssertions.length ? <em>{failedAssertions.length} failed assertion{failedAssertions.length === 1 ? "" : "s"}</em> : pendingAssertions.length ? <em>{pendingAssertions.length} waiting for CDP</em> : <em>{(result.assertions || []).length} assertions passed</em>}</summary><div className="execution-layer-summary">{layers.map((layer) => <span key={layer.label}><strong>{layer.label}</strong>{layer.count} row{layer.count === 1 ? "" : "s"}</span>)}</div><div className="execution-assertion-list">{(result.assertions || []).map((assertion, index) => <div key={`${result.result_id}-${index}`} className={`execution-assertion ${assertion.status}`}><span>{assertion.status === "passed" ? <CheckCircle2 size={14} /> : assertion.status === "failed" ? <XCircle size={14} /> : <LoaderCircle size={14} />}</span><div><strong>{assertion.name}</strong><small>{assertion.message}</small>{assertion.status === "failed" && <code>Expected: {JSON.stringify(assertion.expected)} | Actual: {JSON.stringify(assertion.actual)}</code>}</div></div>)}</div><div className="execution-bronze-refs"><strong>Bronze refs</strong><span>{(result.bronze_refs || []).map((ref) => `${ref.bronze_table}:${ref.bronze_id}`).join(", ") || "No Bronze refs"}</span></div></details>; })}</div></article>)}</div><Pagination page={evidencePage} pageSize={pageSize} totalItems={runsWithResults.length} onPageChange={setEvidencePage} label="evidence runs" /></section>}
        </>}
  </div>;
}
