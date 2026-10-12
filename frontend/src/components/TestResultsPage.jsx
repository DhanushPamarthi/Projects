import { useEffect, useMemo, useState } from "react";
import { Activity, AlertTriangle, CheckCircle2, Clock3, FileCheck2, Gauge, History, LoaderCircle, PlayCircle, Search, ShieldCheck, X, XCircle } from "lucide-react";
import { executeExecutionRun, getPackageExecutionReport, getProject, listManifests, listProjects } from "../api/client.js";
import { AppSelect } from "./AppSelect.jsx";
import { LoadingSpinner } from "./LoadingSpinner.jsx";
import { Pagination } from "./Pagination.jsx";
import { WorkspacePageHero } from "./WorkspacePageHero.jsx";

function readStoredJson(key) {
  try { return JSON.parse(localStorage.getItem(key) || "null"); }
  catch { localStorage.removeItem(key); return null; }
}

function formatDate(value) {
  if (!value) return "Not available";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function runDateLabel(run) {
  const value = run?.completed_at || run?.started_at || run?.approved_at || run?.created_at;
  if (!value) return run?.run_id || "Execution run";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const pad = (item) => String(item).padStart(2, "0");
  return `RUN-${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}`;
}

function runState(run) { return String(run?.lifecycle_state || run?.status || "selected").toLowerCase(); }
function normalizedStatus(value) {
  const status = String(value || "pending").toLowerCase();
  if (["pass", "passed"].includes(status)) return "passed";
  if (["fail", "failed"].includes(status)) return "failed";
  if (["pending", "waiting_for_cdp", "running"].includes(status)) return "pending";
  return "not_run";
}
function statusLabel(status) { return ({ passed: "Passed", failed: "Failed", pending: "Waiting", not_run: "Not run" })[normalizedStatus(status)]; }
function statusClass(status) { return normalizedStatus(status).replaceAll("_", "-"); }
function runStatus(run) {
  const summary = run?.execution_summary?.status;
  if (summary === "pass") return "Passed";
  if (summary === "fail") return "Failed";
  if (summary === "waiting_for_cdp") return "Waiting";
  const state = runState(run);
  return ({ running: "In progress", cancelled: "Cancelled", failed: "Failed", completed: "Completed", selected: "Ready" })[state] || "Ready";
}
function friendlyTerm(value) {
  return String(value || "required information")
    .replaceAll("_", " ")
    .replace(/\bbronze\b|\braw layer\b/gi, "submitted source data")
    .replace(/\bsilver\b|\bcurated layer\b/gi, "processed data")
    .replace(/\bgold\b|\banalytics\b/gi, "business outcome")
    .replace(/\btransport\b/gi, "delivery output")
    .replace(/\bCDP\b/gi, "target system")
    .replace(/\brows?\b/gi, "record")
    .replace(/\s+/g, " ").trim();
}
function friendlyValue(value) {
  if (value === null || value === undefined || value === "") return "no value";
  const text = typeof value === "object" ? JSON.stringify(value) : String(value);
  return text.length > 120 ? `${text.slice(0, 117)}...` : text;
}
function failureReason(result, testCaseTitle = "This test case") {
  if (normalizedStatus(result?.status) !== "failed") return "";
  const failed = (result.assertions || []).filter((item) => normalizedStatus(item.status) === "failed");
  if (result.error?.includes(" failed because ")) return result.error;
  const primary = failed[0];
  if (primary) {
    const actual = primary.actual;
    const missing = actual === null || actual === undefined || actual === "" || (Array.isArray(actual) && actual.length === 0) || (typeof actual === "object" && !Array.isArray(actual) && Object.keys(actual || {}).length === 0) || ["none", "null", "missing", "not found", "not found yet"].includes(String(actual).toLowerCase());
    const reason = missing
      ? `${testCaseTitle} failed because ${friendlyTerm(primary.name).toLowerCase()} was missing. Expected ${friendlyValue(primary.expected)}.`
      : `${testCaseTitle} failed because ${friendlyTerm(primary.name).toLowerCase()} did not match. Expected ${friendlyValue(primary.expected)}, but found ${friendlyValue(actual)}.`;
    return failed.length > 1 ? `${reason} ${failed.length - 1} additional validation check${failed.length === 2 ? "" : "s"} also failed.` : reason;
  }
  return friendlyTerm(result.error || result.message || "The actual result did not match the expected outcome.");
}
function statusIcon(status) {
  const normalized = normalizedStatus(status);
  if (normalized === "passed") return <CheckCircle2 size={15} />;
  if (normalized === "failed") return <XCircle size={15} />;
  if (normalized === "pending") return <Clock3 size={15} />;
  return <FileCheck2 size={15} />;
}

export function TestResultsPage() {
  const [projects, setProjects] = useState([]);
  const [project, setProject] = useState(() => readStoredJson("testiq_current_project"));
  const [packages, setPackages] = useState([]);
  const [packageId, setPackageId] = useState(() => readStoredJson("testiq_current_package")?.package_id || "");
  const [runs, setRuns] = useState([]);
  const [loading, setLoading] = useState(true);
  const [executingRunId, setExecutingRunId] = useState("");
  const [historyCaseId, setHistoryCaseId] = useState("");
  const [selectedCaseIds, setSelectedCaseIds] = useState([]);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [casePage, setCasePage] = useState(1);
  const [runPage, setRunPage] = useState(1);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    async function loadWorkspace() {
      setLoading(true); setError("");
      try {
        const projectItems = await listProjects();
        if (cancelled) return;
        setProjects(projectItems);
        const saved = readStoredJson("testiq_current_project");
        const selected = projectItems.find((item) => item.project_id === saved?.project_id) || projectItems[0] || null;
        if (!selected) { setProject(null); return; }
        const details = await getProject(selected.project_id);
        if (cancelled) return;
        setProject(details); localStorage.setItem("testiq_current_project", JSON.stringify(details));
        const packageItems = await listManifests(details.project_id);
        if (cancelled) return;
        setPackages(packageItems);
        const savedPackage = readStoredJson("testiq_current_package");
        const selectedPackage = packageItems.find((item) => item.package_id === savedPackage?.package_id) || packageItems[0];
        setPackageId(selectedPackage?.package_id || "");
        if (selectedPackage) localStorage.setItem("testiq_current_package", JSON.stringify(selectedPackage));
      } catch (err) { if (!cancelled) setError(err.message || "Unable to load test results."); }
      finally { if (!cancelled) setLoading(false); }
    }
    loadWorkspace();
    return () => { cancelled = true; };
  }, []);

  async function loadRuns(selectedPackageId) {
    setRuns([]);
    if (!selectedPackageId) return;
    setLoading(true); setError("");
    try {
      const report = await getPackageExecutionReport(selectedPackageId);
      const generations = report.generations || [];
      const generationById = new Map(generations.map((generation) => [generation.generation_id, generation]));
      setRuns((report.runs || []).map((run) => ({ ...run, generation: generationById.get(run.generation_id) }))
        .sort((left, right) => String(right.created_at || "").localeCompare(String(left.created_at || ""))));
    } catch (err) { setError(err.message || "Unable to load execution history."); }
    finally { setLoading(false); }
  }

  useEffect(() => { loadRuns(packageId); setCasePage(1); setRunPage(1); setHistoryCaseId(""); setSelectedCaseIds([]); }, [packageId]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!historyCaseId) return undefined;
    const closeOnEscape = (event) => { if (event.key === "Escape") setHistoryCaseId(""); };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [historyCaseId]);

  async function selectProject(projectId) {
    setProject(null); setPackages([]); setPackageId(""); setRuns([]); setError("");
    if (!projectId) { localStorage.removeItem("testiq_current_project"); return; }
    setLoading(true);
    try {
      const selected = await getProject(projectId);
      setProject(selected); localStorage.setItem("testiq_current_project", JSON.stringify(selected));
      const items = await listManifests(projectId); setPackages(items);
      const selectedPackage = items[0]; setPackageId(selectedPackage?.package_id || "");
      if (selectedPackage) localStorage.setItem("testiq_current_package", JSON.stringify(selectedPackage));
    } catch (err) { setError(err.message || "Unable to load resource packages."); setLoading(false); }
  }

  function selectPackage(id) {
    setPackageId(id); setQuery(""); setStatusFilter("all");
    const selected = packages.find((item) => item.package_id === id);
    if (selected) localStorage.setItem("testiq_current_package", JSON.stringify(selected));
  }

  async function executeRun(run) {
    setExecutingRunId(run.run_id); setError("");
    try {
      await executeExecutionRun(run.run_id, { reviewer: "QA Engineer" });
      await loadRuns(packageId);
    } catch (err) { setError(err.message || "Unable to execute the selected test cases."); }
    finally { setExecutingRunId(""); }
  }

  async function executeSelectedCases() {
    if (!selectedCaseIds.length) return;
    const groups = new Map();
    for (const item of caseHistory.filter((testCase) => selectedCaseIds.includes(testCase.test_case_id))) {
      if (!item.baseline_run_id) continue;
      groups.set(item.baseline_run_id, [...(groups.get(item.baseline_run_id) || []), item.test_case_id]);
    }
    if (!groups.size) { setError("The selected test cases do not have an executable baseline."); return; }
    setExecutingRunId("selected"); setError("");
    try {
      for (const [runId, testCaseIds] of groups) {
        await executeExecutionRun(runId, { reviewer: "QA Engineer", selected_test_case_ids: testCaseIds });
      }
      setSelectedCaseIds([]);
      await loadRuns(packageId);
    } catch (err) { setError(err.message || "Unable to execute the selected test cases."); }
    finally { setExecutingRunId(""); }
  }

  const caseHistory = useMemo(() => {
    const cases = new Map();
    for (const run of runs) {
      const definitions = run.generation?.contract?.test_cases || run.generation?.test_cases || run.frozen_contract?.test_cases || [];
      const titleById = new Map(definitions.map((item) => [item.test_case_id, item.title || item.scenario || "Untitled test case"]));
      for (const testCaseId of run.selected_test_case_ids || []) {
        if (!cases.has(testCaseId)) cases.set(testCaseId, { test_case_id: testCaseId, title: titleById.get(testCaseId) || "Untitled test case", attempts: [], baseline_run_id: runState(run) === "cancelled" ? "" : run.run_id });
      }
      const history = run.execution_history?.length ? run.execution_history : (run.execution_results || []).map((item) => ({ ...item, attempt_number: 1 }));
      for (const attempt of history) {
        const row = cases.get(attempt.test_case_id) || { test_case_id: attempt.test_case_id, title: titleById.get(attempt.test_case_id) || "Untitled test case", attempts: [], baseline_run_id: runState(run) === "cancelled" ? "" : run.run_id };
        if (!row.baseline_run_id && runState(run) !== "cancelled") row.baseline_run_id = run.run_id;
        row.attempts.push({ ...attempt, run_id: run.run_id, run_label: runDateLabel(run) });
        cases.set(attempt.test_case_id, row);
      }
    }
    return [...cases.values()].map((item) => {
      item.attempts.sort((left, right) => String(right.completed_at || "").localeCompare(String(left.completed_at || "")));
      return { ...item, latest: item.attempts[0] || null, run_count: item.attempts.length };
    }).sort((left, right) => String(right.latest?.completed_at || "").localeCompare(String(left.latest?.completed_at || "")) || left.test_case_id.localeCompare(right.test_case_id));
  }, [runs]);

  const filteredCases = useMemo(() => caseHistory.filter((item) => {
    const text = `${item.test_case_id} ${item.title}`.toLowerCase();
    return (!query.trim() || text.includes(query.trim().toLowerCase())) && (statusFilter === "all" || normalizedStatus(item.latest?.status) === statusFilter);
  }), [caseHistory, query, statusFilter]);

  useEffect(() => { setCasePage(1); }, [query, statusFilter]);
  const summary = useMemo(() => {
    const completed = caseHistory.filter((item) => ["passed", "failed"].includes(normalizedStatus(item.latest?.status)));
    const passed = completed.filter((item) => normalizedStatus(item.latest?.status) === "passed").length;
    const failed = completed.length - passed;
    return { total: caseHistory.length, attempts: caseHistory.reduce((total, item) => total + item.run_count, 0), passed, failed, passRate: completed.length ? Math.round((passed / completed.length) * 100) : 0 };
  }, [caseHistory]);

  const pageSize = 6;
  const visibleCases = filteredCases.slice((casePage - 1) * pageSize, casePage * pageSize);
  const visibleRuns = runs.slice((runPage - 1) * pageSize, runPage * pageSize);
  const historyCase = caseHistory.find((item) => item.test_case_id === historyCaseId) || null;
  const allVisibleSelected = visibleCases.length > 0 && visibleCases.every((item) => selectedCaseIds.includes(item.test_case_id));
  const selectedHaveHistory = caseHistory.filter((item) => selectedCaseIds.includes(item.test_case_id)).some((item) => item.run_count > 0);
  function toggleVisibleCases(checked) {
    const visibleIds = visibleCases.map((item) => item.test_case_id);
    setSelectedCaseIds((current) => checked ? [...new Set([...current, ...visibleIds])] : current.filter((id) => !visibleIds.includes(id)));
  }

  return <div className="requirements-page professional-workspace-page test-results-page test-results-modern">
    <WorkspacePageHero productLabel="TestIQ · Test Execution" eyebrow="QUALITY ASSURANCE" title="Test Execution Intelligence" context={project?.project_name} cues={["Release confidence", "Failure diagnostics", "Run history"]} />
    <section className={`test-results-overview ${summary.failed ? "has-risk" : summary.total && summary.passRate === 100 ? "is-healthy" : "is-monitoring"}`}>
      <div className="test-results-overview-title"><div><span>EXECUTION OVERVIEW</span><h2>Execution Summary</h2><p>Current release quality.</p></div><div className="execution-overview-context"><span>{packages.find((item) => item.package_id === packageId)?.package_name || "Select a Resource Package"}</span><strong><i />{summary.failed ? "Action required" : summary.total && summary.passRate === 100 ? "Release healthy" : "Monitoring quality"}</strong></div></div>
      <div className="test-results-metrics">
        <article><span className="metric-icon neutral"><FileCheck2 size={17} /></span><div><small>Test cases</small><strong>{summary.total}</strong><span>In selected package</span></div></article>
        <article><span className="metric-icon blue"><Activity size={17} /></span><div><small>Executions</small><strong>{summary.attempts}</strong><span>Across all recorded attempts</span></div></article>
        <article><span className="metric-icon green"><ShieldCheck size={17} /></span><div><small>Latest pass rate</small><strong>{summary.passRate}%</strong><span>{summary.passed} currently passing</span></div></article>
        <article className={summary.failed ? "attention" : ""}><span className="metric-icon red"><AlertTriangle size={17} /></span><div><small>Action required</small><strong>{summary.failed}</strong><span>Latest failed outcomes</span></div></article>
      </div>
      <div className="execution-readiness-track" aria-label={`Latest pass rate ${summary.passRate}%`}><div><span>Release confidence</span><strong>{summary.passRate}%</strong></div><div className="execution-readiness-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow={summary.passRate}><span style={{ width: `${summary.passRate}%` }} /></div><small>{summary.failed ? `${summary.failed} failed outcome${summary.failed === 1 ? "" : "s"} require review before release.` : summary.total ? "All current outcomes support release readiness." : "Execute test cases to establish a release signal."}</small></div>
    </section>

    <section className="test-results-context-bar"><label><span>Project</span><AppSelect value={project?.project_id || ""} onChange={(event) => selectProject(event.target.value)}><option value="">Select project</option>{projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.project_name}</option>)}</AppSelect></label><label><span>Resource Package</span><AppSelect value={packageId} onChange={(event) => selectPackage(event.target.value)} disabled={!packages.length}><option value="">Select package</option>{packages.map((item) => <option key={item.package_id} value={item.package_id}>v{item.package_version} · {item.package_name || item.scope || "Resource Package"}</option>)}</AppSelect></label></section>
    {error && <div className="error-message">{error}</div>}
    {loading ? <LoadingSpinner variant="page" label="Loading Test Results" detail="Preparing outcomes, failure explanations, and execution history." /> : !runs.length ? <section className="requirements-empty test-results-empty-state"><div className="empty-state-icon"><Gauge size={25} /></div><h2>No execution runs available</h2><p>Create an execution baseline from approved test cases and saved test data to begin validation.</p></section> : <>
      <section className="requirements-panel test-case-history-panel">
        <div className="test-results-panel-header"><div><span>TEST CASE STATUS</span><h2>Test Results</h2><p>Latest test case outcomes.</p></div><span>{filteredCases.length} test case{filteredCases.length === 1 ? "" : "s"}</span></div>
        <div className="test-results-actionbar"><div className="selection-summary"><strong>{selectedCaseIds.length}</strong><span>selected</span>{selectedCaseIds.length > 0 && <button type="button" onClick={() => setSelectedCaseIds([])}>Clear</button>}</div><div className="test-results-inline-tools"><label className="test-results-search"><Search size={15} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search test case ID or title" aria-label="Search test case ID or title" /></label><AppSelect aria-label="Filter test cases by status" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option value="all">All outcomes</option><option value="failed">Failed</option><option value="passed">Passed</option><option value="pending">Waiting</option><option value="not_run">Not run</option></AppSelect><button type="button" className="primary-button run-selected-button" disabled={!selectedCaseIds.length || Boolean(executingRunId)} onClick={executeSelectedCases}>{executingRunId === "selected" ? <><LoaderCircle className="spin" size={15} /> Executing selected...</> : <><PlayCircle size={15} /> {selectedHaveHistory ? "Re-Execute" : "Execute"}</>}</button></div></div>
        <div className="execution-results-table-wrap"><table className="execution-results-table test-case-history-table"><thead><tr><th className="test-case-select"><input type="checkbox" aria-label="Select visible test cases" checked={allVisibleSelected} onChange={(event) => toggleVisibleCases(event.target.checked)} /></th><th>Test case</th><th>Latest outcome</th><th>Executions</th><th>Last executed</th><th>Failure reason</th><th aria-label="Actions" /></tr></thead><tbody>{visibleCases.map((item) => {
          const latestStatus = normalizedStatus(item.latest?.status); const selected = selectedCaseIds.includes(item.test_case_id);
          return <tr key={item.test_case_id} className={`${latestStatus === "failed" ? "failed-row" : ""} ${selected ? "selected-row" : ""}`}><td className="test-case-select"><input type="checkbox" aria-label={`Select ${item.test_case_id}`} checked={selected} onChange={() => setSelectedCaseIds((current) => current.includes(item.test_case_id) ? current.filter((id) => id !== item.test_case_id) : [...current, item.test_case_id])} /></td><td><div className="test-case-identity"><strong>{item.test_case_id}</strong><span>{item.title}</span></div></td><td><span className={`test-outcome ${statusClass(latestStatus)}`}>{statusIcon(latestStatus)} {statusLabel(latestStatus)}</span></td><td><strong className="execution-count">{item.run_count}</strong></td><td>{item.latest ? formatDate(item.latest.completed_at) : "Never"}</td><td><span className={latestStatus === "failed" ? "failure-reason" : "no-failure-detail"}>{latestStatus === "failed" ? failureReason(item.latest, item.title) : "—"}</span></td><td><button type="button" className="history-toggle" onClick={() => setHistoryCaseId(item.test_case_id)}><History size={14} /> History</button></td></tr>;
        })}</tbody></table></div>
        {!filteredCases.length && <div className="execution-results-empty">No test cases match the selected filters.</div>}
        <Pagination page={casePage} pageSize={pageSize} totalItems={filteredCases.length} onPageChange={setCasePage} label="test cases" />
      </section>

      <section className="requirements-panel execution-runs-panel">
        <div className="test-results-panel-header"><div><span>EXECUTION BATCHES</span><h2>Execution Runs</h2><p>Run approved test cases.</p></div><span>{runs.length} run{runs.length === 1 ? "" : "s"}</span></div>
        <div className="execution-results-table-wrap"><table className="execution-results-table execution-batches-table"><thead><tr><th>Run</th><th>Test cases</th><th>Data records</th><th>Status</th><th>Last activity</th><th>Action</th></tr></thead><tbody>{visibleRuns.map((run) => { const selectedRecords = run.frozen_contract?.selected_test_data_record_ids || run.configuration_snapshot?.selected_test_data_record_ids || []; const hasResults = Boolean(run.execution_results?.length); const blocked = runState(run) === "cancelled" || Boolean(executingRunId); return <tr key={run.run_id}><td><div className="run-identity"><strong>{runDateLabel(run)}</strong><span>{run.run_id}</span></div></td><td>{run.selected_test_case_ids?.length || 0}</td><td>{selectedRecords.length || run.frozen_contract?.test_data_records?.length || 0}</td><td><span className={`execution-run-status ${runStatus(run).toLowerCase().replaceAll(" ", "-")}`}>{runStatus(run)}</span></td><td>{formatDate(run.completed_at || run.started_at || run.created_at)}</td><td><button type="button" className={hasResults ? "secondary-button compact-button" : "primary-button compact-button"} disabled={blocked} onClick={() => executeRun(run)}>{executingRunId === run.run_id ? <><LoaderCircle className="spin" size={14} /> Executing...</> : <><PlayCircle size={14} /> {hasResults ? "Re-Execute" : "Execute"}</>}</button></td></tr>; })}</tbody></table></div>
        <Pagination page={runPage} pageSize={pageSize} totalItems={runs.length} onPageChange={setRunPage} label="execution runs" />
      </section>
    </>}
    {historyCase && <div className="test-history-drawer-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setHistoryCaseId(""); }}><aside className="test-history-drawer" role="dialog" aria-modal="true" aria-labelledby="test-history-title"><header><div><span>EXECUTION HISTORY</span><h2 id="test-history-title">{historyCase.test_case_id}</h2><p>{historyCase.title}</p></div><button type="button" aria-label="Close execution history" onClick={() => setHistoryCaseId("")}><X size={18} /></button></header><div className="test-history-drawer-summary"><div><span>Recorded attempts</span><strong>{historyCase.run_count}</strong></div><div><span>Latest outcome</span><strong><span className={`test-outcome ${statusClass(historyCase.latest?.status)}`}>{statusIcon(historyCase.latest?.status)} {statusLabel(historyCase.latest?.status)}</span></strong></div><div><span>Last executed</span><strong>{historyCase.latest ? formatDate(historyCase.latest.completed_at) : "Never"}</strong></div></div><div className="test-history-drawer-scroll">{historyCase.attempts.map((attempt, index) => <article key={attempt.attempt_id || `${attempt.run_id}-${attempt.attempt_number}-${index}`}><span className={`history-marker ${statusClass(attempt.status)}`}>{statusIcon(attempt.status)}</span><div><div><strong>Attempt {historyCase.run_count - index}</strong><span>{attempt.run_label}</span></div><time>{formatDate(attempt.completed_at)}</time>{normalizedStatus(attempt.status) === "failed" && <p>{failureReason(attempt, historyCase.title)}</p>}</div><span className={`test-outcome ${statusClass(attempt.status)}`}>{statusLabel(attempt.status)}</span></article>)}</div></aside></div>}
  </div>;
}
