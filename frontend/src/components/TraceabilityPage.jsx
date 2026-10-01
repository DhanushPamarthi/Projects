import { useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronRight, FileSearch } from "lucide-react";
import { LoadingSpinner } from "./LoadingSpinner.jsx";
import { getProject, listGeneratedTestCases, listManifests, listProjects, listRequirementAnalyses } from "../api/client.js";

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

function nodeKey(...parts) {
  return parts.filter(Boolean).join("::");
}

export function TraceabilityPage() {
  const [projects, setProjects] = useState([]);
  const [project, setProject] = useState(() => readStoredJson("testiq_current_project"));
  const [packages, setPackages] = useState([]);
  const [packageId, setPackageId] = useState(() => readStoredJson("testiq_current_package")?.package_id || "");
  const [analysis, setAnalysis] = useState(null);
  const [testGeneration, setTestGeneration] = useState(null);
  const [expanded, setExpanded] = useState([]);
  const [loading, setLoading] = useState(true);
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
      if (!packageId) { setLoading(false); return; }
      setLoading(true);
      setError("");
      try {
        const analyses = await listRequirementAnalyses(packageId);
        const latestAnalysis = analyses[0] || null;
        const generationGroups = await Promise.all(analyses.map((item) => listGeneratedTestCases(item.analysis_id).catch(() => [])));
        const generations = generationGroups.flat().sort((left, right) => latestTimestamp(right).localeCompare(latestTimestamp(left)));
        if (!cancelled) {
          setAnalysis(latestAnalysis);
          setTestGeneration(generations[0] || null);
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

  async function selectProject(projectId) {
    setProject(null); setPackages([]); setPackageId(""); setAnalysis(null); setTestGeneration(null);
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

  function toggle(key) {
    setExpanded((items) => items.includes(key) ? items.filter((item) => item !== key) : [...items, key]);
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

  const allExpandableKeys = useMemo(() => tree.flatMap((business) => [
    nodeKey("br", business.brId),
    ...business.requirements.map((requirement) => nodeKey("fr", business.brId, requirement.frId)),
    ...business.requirements.flatMap((requirement) => requirement.acceptanceCriteria.map((criterion) => nodeKey("ac", business.brId, requirement.frId, criterion.acId))),
  ]), [tree]);

  useEffect(() => {
    setExpanded(allExpandableKeys.filter((_, index) => index < 20));
  }, [allExpandableKeys]);

  function expandAll() {
    setExpanded(allExpandableKeys);
  }

  function collapseAll() {
    setExpanded([]);
  }

  function emptyBadge(text) {
    return <span className="traceability-gap">{text}</span>;
  }

  function expander(key, label) {
    const isOpen = expanded.includes(key);
    return <button type="button" className="traceability-toggle" aria-label={`${isOpen ? "Collapse" : "Expand"} ${label}`} onClick={() => toggle(key)}>{isOpen ? <ChevronDown size={15} /> : <ChevronRight size={15} />}</button>;
  }

  const visibleRows = useMemo(() => {
    const rows = [];
    for (const business of tree) {
      const brKey = nodeKey("br", business.brId);
      rows.push({ kind: "br", key: brKey, business, expandable: true });
      if (!expanded.includes(brKey)) continue;
      if (!business.requirements.length) {
        rows.push({ kind: "gap", key: `${brKey}:missing-fr`, brId: business.brId, message: "No functional requirement mapped" });
        continue;
      }
      for (const requirement of business.requirements) {
        const frKey = nodeKey("fr", business.brId, requirement.frId);
        rows.push({ kind: "fr", key: frKey, business, requirement, expandable: true });
        if (!expanded.includes(frKey)) continue;
        if (!requirement.acceptanceCriteria.length) {
          rows.push({ kind: "gap", key: `${frKey}:missing-ac`, brId: business.brId, frId: requirement.frId, message: "No acceptance criteria mapped" });
          continue;
        }
        for (const criterion of requirement.acceptanceCriteria) {
          const acKey = nodeKey("ac", business.brId, requirement.frId, criterion.acId);
          rows.push({ kind: "ac", key: acKey, business, requirement, criterion, expandable: true });
          if (!expanded.includes(acKey)) continue;
          if (!criterion.testCases.length) {
            rows.push({ kind: "gap", key: `${acKey}:missing-tc`, brId: business.brId, frId: requirement.frId, acId: criterion.acId, message: "No test case mapped" });
            continue;
          }
          for (const testCase of criterion.testCases) rows.push({ kind: "tc", key: `${acKey}:${testCase.test_case_id}`, business, requirement, criterion, testCase });
        }
      }
    }
    return rows;
  }, [tree, expanded]);

  function renderRow(row) {
    if (row.kind === "br") return <tr key={row.key} className="traceability-row traceability-br"><td><div className="traceability-cell-heading">{expander(row.key, row.business.brId)}<strong>{row.business.brId}</strong></div></td><td>{row.business.brText || emptyBadge("No business description")}</td><td /><td /><td /><td /><td /></tr>;
    if (row.kind === "fr") return <tr key={row.key} className="traceability-row traceability-fr"><td /><td /><td><div className="traceability-cell-heading indent-one">{expander(row.key, row.requirement.frId)}<strong>{row.requirement.frId}</strong></div></td><td>{row.requirement.frText || emptyBadge("No functional description")}</td><td /><td /><td /></tr>;
    if (row.kind === "ac") return <tr key={row.key} className="traceability-row traceability-ac"><td /><td /><td /><td /><td><div className="traceability-cell-heading indent-two">{expander(row.key, row.criterion.acId)}<strong>{row.criterion.acId}</strong><span>{row.criterion.acText || "No acceptance criteria text"}</span></div></td><td /><td /></tr>;
    if (row.kind === "tc") return <tr key={row.key} className="traceability-row traceability-tc"><td /><td /><td /><td /><td /><td><strong className="traceability-id indent-three">{row.testCase.test_case_id}</strong></td><td>{row.testCase.title || row.testCase.scenario || emptyBadge("No test case description")}</td></tr>;
    return <tr key={row.key} className="traceability-row traceability-missing"><td>{row.brId || ""}</td><td /><td>{row.frId || ""}</td><td /><td>{row.acId || ""}</td><td colSpan={2}>{emptyBadge(row.message)}</td></tr>;
  }

  return <div className="requirements-page">
    <div className="page-heading"><div><div className="eyebrow">TRACEABILITY MATRIX</div><h1>Traceability</h1><p>Expand each business requirement to inspect linked functional requirements, acceptance criteria, and generated test cases.</p></div></div>
    <div className="requirements-toolbar"><div><label>Project</label><select value={project?.project_id || ""} onChange={(event) => selectProject(event.target.value)}><option value="">Select project</option>{projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.project_name}</option>)}</select></div><div><label>Source package</label><select value={packageId} onChange={(event) => selectPackage(event.target.value)} disabled={!packages.length}><option value="">Select package</option>{packages.map((item) => <option key={item.package_id} value={item.package_id}>{item.package_name || item.scope || "Source package"} - v{item.package_version || "?"}</option>)}</select></div></div>
    {error && <div className="error-message">{error}</div>}
    {loading ? <LoadingSpinner variant="page" label="Loading traceability" detail="Linking source evidence to requirements and generated artifacts." />
      : !analysis ? <section className="requirements-empty"><div className="empty-state-icon"><FileSearch size={25} /></div><h2>No analysis available yet</h2><p>Run Functional Requirements for a source package to view traceability links.</p></section>
        : tree.length ? <section className="requirements-panel traceability-panel"><div className="panel-title-row"><div><h2>Requirement traceability tree</h2><p className="panel-helper">Expand or collapse each branch to follow coverage from BRD through test cases.</p></div><span>{tree.length} BRD item{tree.length === 1 ? "" : "s"}</span></div><div className="traceability-actions"><button type="button" className="secondary-button compact-button" onClick={expandAll}>Expand all</button><button type="button" className="secondary-button compact-button" onClick={collapseAll}>Collapse all</button></div><div className="traceability-table-wrap"><table className="traceability-table"><thead><tr><th>BRD ID</th><th>Business Description</th><th>FR ID</th><th>Functional Description</th><th>Acceptance Criteria</th><th>Test Case ID</th><th>Test Case Description</th></tr></thead><tbody>{visibleRows.map(renderRow)}</tbody></table></div></section>
          : <section className="requirements-empty"><div className="empty-state-icon"><FileSearch size={25} /></div><h2>No traceability mappings found</h2><p>This analysis does not contain business requirement mappings to trace.</p></section>}
  </div>;
}
