import { useEffect, useState } from "react";
import { ClipboardList } from "lucide-react";
import { listGeneratedTestCases, listRequirementAnalyses } from "../api/client.js";
import { useActivities } from "../activity/activityStore.js";
import { TestDataStage } from "./TestDataStage.jsx";
import { LoadingSpinner } from "./LoadingSpinner.jsx";
import { WorkspacePageHero } from "./WorkspacePageHero.jsx";

function getContract(generation) {
  return generation?.contract || generation || {};
}

export function TestDataGenerationPage({ analysis, onGoToTestCases, onContinueToResults }) {
  const activities = useActivities();
  const testDataActivity = activities.find((item) => item.status === "running" && item.label === "Generating test data");
  const [generation, setGeneration] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!analysis?.analysis_id && !(analysis?.generation_pipeline && analysis?.package_id)) {
      setGeneration(null);
      return;
    }
    let active = true;
    setLoading(true);
    setError("");
    const loadSavedGeneration = async () => {
      let analysisId = analysis?.analysis_id;
      if (!analysisId) {
        const analyses = await listRequirementAnalyses(analysis.package_id);
        analysisId = (analyses || []).find((item) => item.analysis_kind === "functional")?.analysis_id;
      }
      if (!analysisId) return null;
      const items = await listGeneratedTestCases(analysisId);
      return items?.[0] || null;
    };
    loadSavedGeneration()
      .then((item) => { if (active) setGeneration(item); })
      .catch((err) => { if (active) setError(err.message || "Could not load generated test cases."); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [analysis?.analysis_id, analysis?.generation_pipeline, analysis?.package_id]);

  const contract = getContract(generation);
  const approvedCases = (contract.test_cases || []).filter((item) => (item.review_status || item.approval?.status || "pending") === "approved").map((item) => ({ ...item, reviewStatus: item.review_status || item.approval?.status || "pending" }));
  const generationId = generation?.generation_id || contract.generation_id;

  return <div className="requirements-page test-generation-page professional-workspace-page">
    <WorkspacePageHero productLabel="TestIQ · Test Data" eyebrow="Data Readiness Center" title="Test Data Operations" context={analysis?.project_name} cues={["Approved Coverage", "Schema-Aligned Data", "Execution Readiness"]} />
    {testDataActivity && <section className="test-data-generation-progress" role="status" aria-live="polite"><div className="test-data-generation-progress-track" role="progressbar" aria-label="Test data generation in progress"><span /></div><div><strong>Generating test data</strong><p>Creating data for the selected cases. You can move to another page and return while this continues.</p></div></section>}
    {error && <div className="error-message">{error}</div>}
    {loading ? testDataActivity ? <div className="test-data-generation-loading">Refreshing approved cases and generated records…</div> : <LoadingSpinner variant="section" label="Loading approved test cases" detail="Preparing test-data generation context." /> : !analysis ? <section className="generation-empty"><ClipboardList size={22} /><h3>No test-case analysis selected</h3><p>Generate and approve test cases first.</p><button type="button" className="primary-button" onClick={onGoToTestCases}>Open test cases</button></section> : !generationId ? <section className="generation-empty"><ClipboardList size={22} /><h3>Generate test cases first</h3><p>Approved test cases are required.</p><button type="button" className="primary-button" onClick={onGoToTestCases}>Open test cases</button></section> : approvedCases.length === 0 ? <section className="generation-empty"><ClipboardList size={22} /><h3>No approved test cases</h3><p>Approve at least one test case.</p><button type="button" className="primary-button" onClick={onGoToTestCases}>Review test cases</button></section> : <TestDataStage projectId={analysis.project_id || contract.project_id} generationId={generationId} approvedCases={approvedCases} reviewer="Product Owner" generationInProgress={Boolean(testDataActivity)} onGoToTestCases={onGoToTestCases} onContinueToResults={onContinueToResults} />}
  </div>;
}
