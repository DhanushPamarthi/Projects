import { useEffect, useMemo, useState } from "react";
import { FileSearch } from "lucide-react";
import { LoadingSpinner } from "./LoadingSpinner.jsx";
import { getProject, getTestCaseCoverageReview, listGeneratedTestCases, listManifests, listProjects, listRequirementAnalyses } from "../api/client.js";
import { AppSelect } from "./AppSelect.jsx";
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

function textValue(item) {
  if (!item) return "";
  if (typeof item === "string") return item;
  if (item.text) return item.text;
  if (item.statement) return item.statement;
  if (item.description) return item.description;
  const givenWhenThen = [item.given, item.when, item.then].filter(Boolean).join(" ");
  return givenWhenThen || item.title || "";
}

function acceptanceText(item) {
  if (!item) return "";
  if (item.text) return item.text;
  return [item.given, item.when, item.then].filter(Boolean).join(" ");
}

function latestTimestamp(item) {
  return String(item?.created_at || item?.updated_at || item?.contract?.generated_at || item?.contract?.created_at || "");
}

export function TraceabilityPage() {
  const [projects, setProjects] = useState([]);
  const [project, setProject] = useState(() => readStoredJson("testiq_current_project"));
  const [packages, setPackages] = useState([]);
  const [packageId, setPackageId] = useState(() => readStoredJson("testiq_current_package")?.package_id || "");
  const [analysis, setAnalysis] = useState(null);
  const [testGeneration, setTestGeneration] = useState(null);
  const [coverageReview, setCoverageReview] = useState(null);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(1);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    async function load() {
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
        if (!cancelled) setError(err.message || "Unable to load traceability data.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function loadAnalysis() {
      setAnalysis(null);
      setTestGeneration(null);
      setCoverageReview(null);
      if (!packageId) { setLoading(false); return; }
      setLoading(true);
      setError("");
      try {
        const analyses = await listRequirementAnalyses(packageId);
        const functionalAnalyses = analyses.filter((item) => item.analysis_kind === "functional");
        const latestAnalysis = functionalAnalyses[0] || null;
        // A test run from an older FR version must never appear as coverage
        // for the latest Functional Requirements.
        const generations = latestAnalysis
          ? (await listGeneratedTestCases(latestAnalysis.analysis_id)).sort((left, right) => latestTimestamp(right).localeCompare(latestTimestamp(left)))
          : [];
        const latestGeneration = generations[0] || null;
        const review = latestGeneration?.generation_id
          ? await getTestCaseCoverageReview(latestGeneration.generation_id).catch(() => null)
          : null;
        if (!cancelled) {
          setAnalysis(latestAnalysis);
          setTestGeneration(latestGeneration);
          setCoverageReview(review);
        }
      } catch (err) {
        if (!cancelled) setError(err.message || "Unable to load traceability data.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    loadAnalysis();
    return () => { cancelled = true; };
  }, [packageId]);
  useEffect(() => { setPage(1); }, [packageId]);

  async function selectProject(projectId) {
    setProject(null); setPackages([]); setPackageId(""); setAnalysis(null); setTestGeneration(null); setCoverageReview(null);
    if (!projectId) { setLoading(false); localStorage.removeItem("testiq_current_project"); return; }
    setLoading(true); setError("");
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
      setError(err.message || "Unable to load source packages."); setLoading(false);
    }
  }

  function selectPackage(id) {
    setPackageId(id);
    const selectedPackage = packages.find((item) => item.package_id === id);
    if (selectedPackage) localStorage.setItem("testiq_current_package", JSON.stringify(selectedPackage));
  }

  const tree = useMemo(() => {
    const requirements = analysis?.contract?.requirements || [];
    const testCases = testGeneration?.contract?.test_cases || testGeneration?.test_cases || [];
    const businessMap = new Map();

    for (const requirement of requirements) {
      for (const rule of requirement.business_rules || []) {
        const brId = rule?.rule_id || rule?.business_requirement_id;
        const brText = textValue(rule);
        if (brId && !businessMap.has(brId)) businessMap.set(brId, { brId, brText, requirements: [] });
      }
    }

    for (const requirement of requirements) {
      const linkedBusinessIds = new Set([
        ...(requirement.business_requirement_ids || []),
        ...(requirement.business_rules || []).map((rule) => rule?.rule_id || rule?.business_requirement_id).filter(Boolean),
      ]);
      for (const brId of linkedBusinessIds) {
        if (!businessMap.has(brId)) businessMap.set(brId, { brId, brText: "", requirements: [] });
        const business = businessMap.get(brId);
        const acceptanceCriteria = (requirement.acceptance_criteria || []).map((criterion, index) => {
          const acId = criterion?.acceptance_id || `AC-${index + 1}`;
          const cases = testCases.filter((testCase) =>
            testCase.requirement_id === requirement.requirement_id &&
            ((testCase.acceptance_criteria_ids || []).includes(acId) || !(testCase.acceptance_criteria_ids || []).length)
          );
          return { acId, acText: acceptanceText(criterion), testCases: cases };
        });
        business.requirements.push({
          frId: requirement.requirement_id,
          frText: requirement.statement || requirement.title || "",
          acceptanceCriteria,
        });
      }
    }

    return [...businessMap.values()].sort((left, right) => left.brId.localeCompare(right.brId));
  }, [analysis, testGeneration]);

  const coverage = useMemo(() => {
    const review = coverageReview || testGeneration?.coverage_review || testGeneration?.contract?.coverage_review || {};
    const summary = review.summary || {};
    const testCases = testGeneration?.contract?.test_cases || testGeneration?.test_cases || [];
    const testCaseById = new Map(testCases.map((item) => [item.test_case_id, item]));
    const rows = (review.matrix || []).flatMap((requirement) => (requirement.business_rules || []).flatMap((rule) => {
      const ids = rule.test_case_ids || [];
      return (ids.length ? ids : [null]).map((id) => ({
        requirementId: requirement.requirement_id,
        requirementTitle: requirement.title || requirement.requirement_id,
        ruleId: rule.id || rule.label || "Business requirement",
        description: rule.label || rule.id || "",
        testCaseId: id,
        testCase: id ? testCaseById.get(id) : null,
        status: String(rule.status || (ids.length ? "covered" : "missing")).toLowerCase(),
      }));
    }));
    const total = Number(summary.business_rules_total ?? rows.length);
    const covered = Number(summary.business_rules_covered ?? rows.filter((row) => row.status === "covered").length);
    return { rows, total, covered, percent: total ? Math.round((covered / total) * 100) : 0 };
  }, [coverageReview, testGeneration]);

  function emptyBadge(text) {
    return <span className="traceability-gap">{text}</span>;
  }

  const functionalRows = (analysis?.contract?.requirements || []).map((requirement) => ({
    id: requirement.requirement_id,
    title: requirement.title || requirement.statement || "Functional Requirement",
    statement: requirement.statement || "",
    businessRules: requirement.business_rules || [],
    acceptanceCriteria: requirement.acceptance_criteria || [],
    testCases: (testGeneration?.contract?.test_cases || []).filter((item) => item.requirement_id === requirement.requirement_id),
  }));
  const pageSize = 8;
  const visibleCoverageRows = coverage.rows.slice((page - 1) * pageSize, page * pageSize);

  return <div className="requirements-page professional-workspace-page">
    <WorkspacePageHero productLabel="TestIQ · Traceability" eyebrow="TRACEABILITY MATRIX" title="Coverage Intelligence" context={project?.project_name} cues={["Requirement coverage", "Linked evidence", "Audit visibility"]} />
    <div className="requirements-toolbar"><div><label>Project</label><AppSelect value={project?.project_id || ""} onChange={(event) => selectProject(event.target.value)}><option value="">Select project</option>{projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.project_name}</option>)}</AppSelect></div><div><label>Source package</label><AppSelect value={packageId} onChange={(event) => selectPackage(event.target.value)} disabled={!packages.length}><option value="">Select package</option>{packages.map((item) => <option key={item.package_id} value={item.package_id}>{item.package_name || item.scope || "Source package"} - v{item.package_version || "?"}</option>)}</AppSelect></div></div>
    {error && <div className="error-message">{error}</div>}
    {loading ? <LoadingSpinner variant="page" label="Loading traceability" detail="Linking source evidence to requirements and generated artifacts." />
      : !analysis ? <section className="requirements-empty"><div className="empty-state-icon"><FileSearch size={25} /></div><h2>No analysis available yet</h2><p>Run Functional Requirements for a source package to view traceability links.</p></section>
        : <>
          <section className="traceability-coverage-card"><div className="traceability-coverage-header"><div><div className="eyebrow">FUNCTIONAL REQUIREMENTS</div><h2>All Functional Requirements</h2><p>Each FR is shown once, independently of its business-rule mappings.</p></div><strong>{functionalRows.length} FRs</strong></div>
            <div className="traceability-coverage-table-wrap"><table className="traceability-coverage-table"><thead><tr><th>FR</th><th>Requirement</th><th>Business rules</th><th>Acceptance criteria</th><th>Mapped test cases</th></tr></thead><tbody>{functionalRows.map((row) => <tr key={row.id}><td><strong>{row.id}</strong></td><td>{row.title}<small>{row.statement}</small></td><td>{row.businessRules.map((item) => item.rule_id).filter(Boolean).join(", ") || "—"}</td><td>{row.acceptanceCriteria.map((item) => item.acceptance_id).filter(Boolean).join(", ") || "—"}</td><td>{row.testCases.map((item) => item.test_case_id).join(", ") || emptyBadge("Needs a test case")}</td></tr>)}</tbody></table></div>
          </section>
          {tree.length > 0 && <>
          <section className="traceability-coverage-card">
            <div className="traceability-coverage-header"><div><div className="eyebrow">COVERAGE REVIEW</div><h2>Business requirement coverage</h2><p>Requirements mapped to generated test evidence.</p></div><div className={`traceability-coverage-score ${coverage.percent === 100 ? "complete" : ""}`}><strong>{coverage.percent}%</strong><span>covered</span></div></div>
            <div className="traceability-coverage-summary"><span><strong>{coverage.covered}</strong> of {coverage.total} requirements covered</span><span>{coverage.rows.filter((row) => row.testCaseId).length} linked test cases</span></div>
            <div className="traceability-coverage-table-wrap"><table className="traceability-coverage-table"><thead><tr><th>Status</th><th>Business requirement</th><th>Description</th><th>Test case ID</th><th>Test case description</th></tr></thead><tbody>{coverage.rows.length ? visibleCoverageRows.map((row) => <tr key={`${row.ruleId}-${row.testCaseId || "missing"}`}><td><span className={`traceability-coverage-status ${row.status === "covered" ? "covered" : "missing"}`}>{row.status === "covered" ? "Covered" : "Missing"}</span></td><td><strong>{row.ruleId}</strong><small>{row.requirementId}</small></td><td>{row.description || "No business requirement description"}</td><td>{row.testCaseId || emptyBadge("No test case linked")}</td><td>{row.testCase ? (row.testCase.title || row.testCase.scenario || "No description") : emptyBadge("Coverage required")}</td></tr>) : <tr><td colSpan="5">{emptyBadge("Coverage review is not available for this generation")}</td></tr>}</tbody></table></div><Pagination page={page} pageSize={pageSize} totalItems={coverage.rows.length} onPageChange={setPage} label="traceability records" />
          </section>
        </>}
        </>}
  </div>;
}
