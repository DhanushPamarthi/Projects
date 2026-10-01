import { useEffect, useMemo, useState } from "react";
import { FileCheck2 } from "lucide-react";
import { LoadingSpinner } from "./LoadingSpinner.jsx";
import { getProject, listGeneratedTestCases, listManifests, listProjects, listRequirementAnalyses, listExecutionRuns } from "../api/client.js";

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

function testCaseStatus(testCase) {
  const directStatus = String(testCase?.execution_status || testCase?.status || testCase?.result || "").toLowerCase();
  if (["fail", "failed", "error", "blocked"].includes(directStatus)) return "fail";
  if (["pass", "passed", "success", "completed"].includes(directStatus)) return "pass";
  const validationStatuses = (testCase?.validation_results || []).map((item) => String(item?.status || "").toLowerCase());
  if (validationStatuses.some((status) => ["fail", "failed", "error", "blocked"].includes(status))) return "fail";
  if (validationStatuses.length && validationStatuses.every((status) => ["pass", "passed", "success", "completed"].includes(status))) return "pass";
  return "";
}

function testResultStatus(run) {
  const state = runStatus(run);
  if (["failed", "cancelled"].includes(state)) return "Fail";
  const testCases = run?.frozen_contract?.test_cases || [];
  const caseStatuses = testCases.map(testCaseStatus).filter(Boolean);
  if (caseStatuses.some((status) => status === "fail")) return "Fail";
  if (caseStatuses.length && caseStatuses.length === testCases.length && caseStatuses.every((status) => status === "pass")) return "Pass";
  return state === "completed" ? "Pass" : "Fail";
}

export function TestResultsPage() {
  const [projects, setProjects] = useState([]);
  const [project, setProject] = useState(() => readStoredJson("testiq_current_project"));
  const [packages, setPackages] = useState([]);
  const [packageId, setPackageId] = useState(() => readStoredJson("testiq_current_package")?.package_id || "");
  const [runs, setRuns] = useState([]);
  const [loading, setLoading] = useState(true);
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

  const summary = useMemo(() => {
    const passed = runs.filter((run) => testResultStatus(run) === "Pass").length;
    const failed = runs.filter((run) => testResultStatus(run) === "Fail").length;
    return { passed, failed };
  }, [runs]);

  return <div className="requirements-page">
    <div className="page-heading"><div><div className="eyebrow">EXECUTION BASELINES</div><h1>Test Results</h1><p>Review frozen test execution runs created from approved test cases.</p></div></div>
    <div className="requirements-toolbar"><div><label>Project</label><select value={project?.project_id || ""} onChange={(event) => selectProject(event.target.value)}><option value="">Select project</option>{projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.project_name}</option>)}</select></div><div><label>Source package</label><select value={packageId} onChange={(event) => selectPackage(event.target.value)} disabled={!packages.length}><option value="">Select package</option>{packages.map((item) => <option key={item.package_id} value={item.package_id}>{item.package_name || item.scope || "Source package"} - v{item.package_version || "?"}</option>)}</select></div></div>
    {error && <div className="error-message">{error}</div>}
    {loading ? <LoadingSpinner variant="page" label="Loading test results" detail="Retrieving the selected package and execution history." />
      : !runs.length ? <section className="requirements-empty"><div className="empty-state-icon"><FileCheck2 size={25} /></div><h2>No test results available yet</h2><p>Freeze approved test cases into an execution baseline to populate this page.</p></section>
        : <>
          <div className="requirements-summary-grid"><div><span>Total runs</span><strong>{runs.length}</strong></div><div><span>Pass</span><strong>{summary.passed}</strong></div><div><span>Fail</span><strong>{summary.failed}</strong></div><div><span>Test cases</span><strong>{runs.reduce((total, run) => total + (run.selected_test_case_ids?.length || 0), 0)}</strong></div></div>
          <section className="requirements-panel"><div className="panel-title-row"><div><h2>Execution run results</h2><p className="panel-helper">Each row represents a frozen baseline created from reviewed test cases.</p></div><span>{runs.length} run{runs.length === 1 ? "" : "s"}</span></div><div className="business-requirements-table-wrap"><table className="business-requirements-table test-results-table"><thead><tr><th>Run ID</th><th>Project Name</th><th>Test cases</th><th>Status</th><th>Executed On</th></tr></thead><tbody>{runs.map((run) => { const status = testResultStatus(run); return <tr key={run.run_id}><td>{runDateLabel(run)}</td><td>{project?.project_name || "-"}</td><td>{run.selected_test_case_ids?.length || 0}</td><td><span className={`execution-run-status ${status.toLowerCase()}`}>{status}</span></td><td>{formatDate(run.completed_at || run.approved_at || run.created_at)}</td></tr>; })}</tbody></table></div></section>
        </>}
  </div>;
}
