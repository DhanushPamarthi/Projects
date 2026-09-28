import { useEffect, useState } from "react";
import { ArrowRight, CheckCircle, Compass, ShieldCheck } from "lucide-react";
import { Alert, Badge, Button, Card, Form } from "react-bootstrap";
import { completeWorkflowStage, executeWorkflow, finalizeWorkflow, getCoverage, getEvidence, runTestDesignAgent } from "../api/client.js";

const copy = {
  readiness_review: ["Readiness review", "Confirm the BRD and metadata are ready for downstream test design."],
  coverage_review: ["Coverage review", "Review generated cases before requesting human approval."],
  human_approval: ["Human approval", "Approve the reviewed test design before selecting a scenario."],
  scenario_selection: ["Scenario selection", "Choose the scenario that will drive mapping and data generation."],
  execution: ["Execution", "Run the approved synthetic data through the selected test surface."],
  evidence_cleanup: ["Evidence and cleanup", "Capture validation evidence and close the generation run."],
};

export function WorkflowGateStep({ requirement, stage, testCases = [], scenarios = [], selectedScenario, onSelectScenario, onComplete, onAgentComplete, initialTargets = {} }) {
  const [surface] = useState(initialTargets.webpage?.length ? "webpage" : initialTargets.api?.length ? "api" : "webpage");
  const [targets] = useState(() => Object.fromEntries(["webpage", "api", "cdp"].map((key) => [key, initialTargets[key]?.[0]?.url || initialTargets[key]?.[0] || ""])));
  const [reviewNote, setReviewNote] = useState("");
  const [coverage, setCoverage] = useState(null);
  const [evidence, setEvidence] = useState([]);
  const [executionResult, setExecutionResult] = useState(null);
  const [loading, setLoading] = useState(false); const [error, setError] = useState(null); const [done, setDone] = useState(false);
  const [title, description] = copy[stage]; const isScenario = stage === "scenario_selection";
  useEffect(() => { if (stage === "coverage_review" && requirement?.key) getCoverage(requirement.key).then(setCoverage).catch(() => {}); }, [stage, requirement?.key]);
  useEffect(() => { if (stage === "evidence_cleanup" && requirement?.key) getEvidence(requirement.key).then((data) => setEvidence(data.items || [])).catch(() => {}); }, [stage, requirement?.key]);
  async function complete() {
    if (!requirement?.key || (isScenario && !selectedScenario)) return;
    setLoading(true); setError(null);
    try {
      const selectedTargets = { [surface]: targets[surface] ? [{ url: targets[surface] }] : [] };
      const result = stage === "execution"
        ? await executeWorkflow(requirement.key, { scenario_key: selectedScenario, mode: "probe", targets: selectedTargets })
        : stage === "evidence_cleanup" ? await finalizeWorkflow(requirement.key)
        : await completeWorkflowStage(requirement.key, stage, stage === "human_approval" ? { reviewer_note: reviewNote } : {});
      if (stage === "execution") setExecutionResult(result?.stages?.execution || result);
      setDone(true); onComplete?.(result);
    } catch (err) { setError(err.message); } finally { setLoading(false); }
  }
  async function runAgent() {
    if (!requirement?.key) return;
    setLoading(true); setError(null);
    try { const result = await runTestDesignAgent(requirement.key); onAgentComplete?.(result); }
    catch (err) { setError(err.message); } finally { setLoading(false); }
  }
  return <Card><Card.Header className="d-flex justify-content-between align-items-center"><span>{title}</span><Badge bg={done ? "success" : "warning"}>{done ? "Complete" : "Review required"}</Badge></Card.Header><Card.Body>
    <p className="text-muted">{description}</p>
    {stage === "readiness_review" && <div className="small mb-3"><CheckCircle size={16} className="text-success me-2" />BRD parsed and CDP metadata available</div>}
    {stage === "coverage_review" && <div className="alert alert-info">{coverage ? `Requirement coverage: ${coverage.requirements.percentage}% · Scenario coverage: ${coverage.scenarios.percentage}% · Business-rule coverage: ${coverage.business_rules.percentage}%` : `${testCases.length} test case(s) generated from the approved requirement.`}</div>}
    {stage === "human_approval" && <div className="small mb-3"><ShieldCheck size={16} className="text-success me-2" />Coverage is ready for a human sign-off.</div>}
    {stage === "human_approval" && <Form.Control as="textarea" rows={3} className="mb-3" placeholder="Approval note (optional)" value={reviewNote} onChange={(e) => setReviewNote(e.target.value)} />}
    {isScenario && <div className="d-flex flex-column gap-2 mb-3">{scenarios.map((scenario) => <Form.Check key={scenario.key} type="radio" name="scenario" label={`${scenario.name} (${scenario.key})`} checked={selectedScenario === scenario.key} onChange={() => onSelectScenario?.(scenario.key)} />)}</div>}
    {stage === "execution" && <div className="small mb-3"><Compass size={16} className="text-primary me-2" />Webpage and API targets are probed; CDP targets remain manual until a browser runner is configured.</div>}
    {stage === "execution" && executionResult?.results && <div className="alert alert-info">{executionResult.results.length} target(s) executed · {executionResult.failed || 0} failed.</div>}
    {stage === "evidence_cleanup" && <div className="alert alert-info">{evidence.length} evidence item(s) captured from validation and execution.</div>}
    {error && <Alert variant="danger">{error}</Alert>}
    {stage === "readiness_review" && <Button variant="outline-primary" className="me-2" onClick={runAgent} disabled={loading}>Run AI preparation</Button>}
    <Button onClick={complete} disabled={loading || done || (isScenario && !selectedScenario)}>{loading ? "Saving..." : done ? "Completed" : `Complete ${title}`} <ArrowRight size={16} className="ms-2" /></Button>
  </Card.Body></Card>;
}
