import { useEffect, useState } from "react";
import { ClipboardList } from "lucide-react";
import { listGeneratedTestCases } from "../api/client.js";
import { TestDataStage } from "./TestDataStage.jsx";
import { LoadingSpinner } from "./LoadingSpinner.jsx";
import { WorkspacePageHero } from "./WorkspacePageHero.jsx";

function getContract(generation) {
  return generation?.contract || generation || {};
}

export function TestDataGenerationPage({ analysis, onGoToTestCases }) {
  const [generation, setGeneration] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!analysis?.analysis_id) {
      setGeneration(null);
      return;
    }
    let active = true;
    setLoading(true);
    setError("");
    listGeneratedTestCases(analysis.analysis_id)
      .then((items) => { if (active) setGeneration(items[0] || null); })
      .catch((err) => { if (active) setError(err.message || "Could not load generated test cases."); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [analysis?.analysis_id]);

  const contract = getContract(generation);
  const approvedCases = (contract.test_cases || []).filter((item) => (item.review_status || item.approval?.status || "pending") === "approved").map((item) => ({ ...item, reviewStatus: item.review_status || item.approval?.status || "pending" }));
  const generationId = generation?.generation_id || contract.generation_id;

  return <div className="requirements-page test-generation-page professional-workspace-page">
    <WorkspacePageHero productLabel="TestIQ · Test Data" eyebrow="TEST DATA" title="Test Data Intelligence" context={analysis?.project_name} cues={["Approved scope", "Controlled generation", "Bronze readiness"]} />
    {error && <div className="error-message">{error}</div>}
    {loading ? <LoadingSpinner variant="section" label="Loading approved test cases" detail="Preparing test-data generation context." /> : !analysis ? <section className="generation-empty"><ClipboardList size={22} /><h3>No test-case analysis selected</h3><p>Generate and approve test cases first.</p><button type="button" className="primary-button" onClick={onGoToTestCases}>Open test cases</button></section> : !generationId ? <section className="generation-empty"><ClipboardList size={22} /><h3>Generate test cases first</h3><p>Approved test cases are required.</p><button type="button" className="primary-button" onClick={onGoToTestCases}>Open test cases</button></section> : approvedCases.length === 0 ? <section className="generation-empty"><ClipboardList size={22} /><h3>No approved test cases</h3><p>Approve at least one test case.</p><button type="button" className="primary-button" onClick={onGoToTestCases}>Review test cases</button></section> : <TestDataStage projectId={analysis.project_id || contract.project_id} generationId={generationId} approvedCases={approvedCases} reviewer="Product Owner" onGoToTestCases={onGoToTestCases} />}
  </div>;
}
