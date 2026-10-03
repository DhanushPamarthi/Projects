import { useEffect, useState } from "react";
import { CheckCheck, LoaderCircle, PlayCircle, ShieldCheck, X } from "lucide-react";
import { createExecutionRun, generateTestData, listExecutionRuns, listTestData, reviewTestData, saveTestDataToBronze, updateExecutionRunLifecycle } from "../api/client.js";

function displayCaseName(testCase) {
  let name = String(testCase?.title || testCase?.test_case_id || "Test case")
    .replace(/customer data platform\s*\(\s*cdp\s*\)/gi, "")
    .replace(/customer data platform/gi, "")
    .replace(/\bCDP\b/gi, "")
    .replace(/\s+in\s+the\s*([.!?]|$)/gi, "$1")
    .replace(/^verify the objective is to test\s+/i, "Verify ")
    .replace(/^verify the campaign should identify eligible members who are due for a prescription refill and send a refill reminder communication\.?$/i, "Verify refill eligibility and reminder delivery")
    .replace(/^verify the campaign should identify members who meet the refill reminder criteria\.?$/i, "Verify refill reminder eligibility")
    .replace(/^verify the objective is to test\s+/i, "Verify ")
    .replace(/^verify the campaign should\s+/i, "Verify campaign ")
    .replace(/^verify for an eligible member,?\s*/i, "Verify eligible-member ")
    .replace(/\s+/g, " ")
    .trim();
  return name;
}
function displayRecordStatus(record) {
  const status = String(record?.status || "").toLowerCase();
  if (status === "saved") {
    return record?.bronze_raw_file_id ? `Saved to Bronze - ${record.bronze_raw_file_id}` : "Saved to Bronze";
  }
  if (status === "draft") return "Draft";
  return status ? status.charAt(0).toUpperCase() + status.slice(1) : "Generated";
}
function baselineIsTerminal(run) {
  return ["cancelled", "failed", "completed"].includes(String(run?.lifecycle_state || run?.status || "").toLowerCase());
}

function ExecutionBaselinePanel({ generationId, reviewer, eligibleCases }) {
  const [selected, setSelected] = useState([]);
  const [runs, setRuns] = useState([]);
  const [busy, setBusy] = useState(false);
  const [actionId, setActionId] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const eligibleKey = eligibleCases.map((item) => item.test_case_id).join("|");

  useEffect(() => { setSelected(eligibleCases.map((item) => item.test_case_id)); }, [eligibleKey]);
  useEffect(() => {
    if (!generationId) return;
    listExecutionRuns(generationId).then(setRuns).catch(() => setRuns([]));
  }, [generationId]);

  async function freeze() {
    setBusy(true); setError(""); setMessage("");
    try {
      const run = await createExecutionRun(generationId, { reviewer, selected_test_case_ids: selected });
      setRuns((items) => [run, ...items.filter((item) => item.run_id !== run.run_id)]);
      setMessage(`Baseline ${run.run_id} frozen with ${selected.length} case(s) and their saved test data.`);
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }

  async function updateLifecycle(run, lifecycleState) {
    const id = `${run.run_id}:${lifecycleState}`;
    setActionId(id); setError(""); setMessage("");
    try {
      const next = await updateExecutionRunLifecycle(run.run_id, { lifecycle_state: lifecycleState, reviewer });
      setRuns((items) => items.map((item) => item.run_id === next.run_id ? next : item));
      setMessage(`Baseline ${next.run_id} marked ${next.lifecycle_state || next.status}.`);
    } catch (err) { setError(err.message); }
    finally { setActionId(""); }
  }

  return <section className="requirements-panel execution-baseline-panel"><div className="panel-title-row"><div><h2>Freeze execution baseline</h2><p className="panel-helper">This step is available only for approved cases whose test data has been saved to Bronze. Freezing records a selection; it does not execute the tests.</p></div><span>{selected.length} selected</span></div>
    {error && <div className="error-message">{error}</div>}{message && <div className="toast-message">{message}</div>}
    {eligibleCases.length ? <><div className="selection-actions"><label className="select-all"><input type="checkbox" checked={selected.length === eligibleCases.length} onChange={(event) => setSelected(event.target.checked ? eligibleCases.map((item) => item.test_case_id) : [])} /> Select all data-ready cases</label><button type="button" className="primary-button" disabled={busy || !selected.length} onClick={freeze}>{busy ? <><LoaderCircle className="spin" size={16} /> Freezing...</> : <><PlayCircle size={16} /> Freeze for execution</>}</button></div><div className="execution-case-list">{eligibleCases.map((item) => <label className="execution-case-item" key={item.test_case_id}><input type="checkbox" checked={selected.includes(item.test_case_id)} onChange={() => setSelected((items) => items.includes(item.test_case_id) ? items.filter((id) => id !== item.test_case_id) : [...items, item.test_case_id])} /><span><strong>{item.test_case_id}</strong>{item.title}</span></label>)}</div></> : <div className="generation-empty"><ShieldCheck size={22} /><h3>Test data must be saved first</h3><p>Generate and insert the approved cases' test data into Bronze. Then return here to freeze the cases for execution.</p></div>}
    {runs.length > 0 && <div className="execution-run-list"><h3>Frozen baselines</h3>{runs.map((run) => { const state = run.lifecycle_state || run.status; const terminal = baselineIsTerminal(run); return <article className={String(state || "").toLowerCase()} key={run.run_id}><div className="execution-run-head"><div><strong>{run.run_id}</strong><span>{run.selected_test_case_ids?.length || 0} case(s) - {state}</span></div><span className={`execution-run-status ${state}`}>{state}</span></div><small>Baseline hash: {run.baseline_hash}</small>{run.error && <small className="execution-run-error">{run.error}</small>}{!terminal && <div className="execution-run-actions"><button type="button" className="secondary-button compact-button" disabled={actionId === `${run.run_id}:cancelled`} onClick={() => updateLifecycle(run, "cancelled")}>{actionId === `${run.run_id}:cancelled` ? "Cancelling..." : "Cancel baseline"}</button><button type="button" className="secondary-button compact-button danger-compact-button" disabled={actionId === `${run.run_id}:failed`} onClick={() => updateLifecycle(run, "failed")}>{actionId === `${run.run_id}:failed` ? "Marking..." : "Mark baseline failed"}</button></div>}</article>; })}</div>}
  </section>;
}

export function TestDataStage({ projectId, generationId, approvedCases, reviewer }) {
  const [selectedCases, setSelectedCases] = useState([]);
  const [records, setRecords] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [generationContext, setGenerationContext] = useState(null);
  const [caseFilter, setCaseFilter] = useState("");
  useEffect(() => {
    if (!message) return undefined;
    const timer = window.setTimeout(() => setMessage(""), 4500);
    return () => window.clearTimeout(timer);
  }, [message]);
  useEffect(() => {
    setSelectedCases(approvedCases.map((item) => item.test_case_id));
  }, [approvedCases.map((item) => `${item.test_case_id}:${item.reviewStatus}`).join("|")]);
  useEffect(() => {
    if (!generationId) return;
    listTestData(generationId).then((items) => setRecords(items.map((item) => ({ ...item, payload_text: JSON.stringify(item.payload, null, 2) })))).catch(() => setRecords([]));
  }, [generationId]);

  async function generate(replaceExistingDrafts = false) {
    setBusy(true); setError(""); setMessage("");
    try {
      // Replacing drafts clears the existing draft set on the server, so include
      // both previously generated cases and any newly selected approved cases.
      const caseIds = replaceExistingDrafts
        ? [...new Set([...records.map((item) => item.test_case_id), ...selectedCases])]
        : selectedCases;
      const result = await generateTestData(generationId, { reviewer, test_case_ids: caseIds, replace_existing_drafts: replaceExistingDrafts });
      setRecords(result.records.map((item) => ({ ...item, payload_text: JSON.stringify(item.payload, null, 2) })));
      const generated = `${replaceExistingDrafts ? "Regenerated" : "Generated"} ${result.records.length} record(s)`;
      const knowledgeContext = result.rag_status || { used: Boolean(result.rag_metadata?.index_id), index_id: result.rag_metadata?.index_id };
      setGenerationContext(knowledgeContext);
      const knowledgeMessage = knowledgeContext.used
        ? `Knowledge Base used${knowledgeContext.index_id ? ` (${knowledgeContext.index_id})` : ""}.`
        : "Knowledge Base was not used for this run.";
      setMessage(result.generation_mode === "template_fallback"
        ? `${generated} from Knowledge Base templates with the local data fallback because the LLM was unavailable. ${knowledgeMessage} Review the JSON before inserting into Bronze.`
        : `${generated} from Knowledge Base templates with ${result.generation_model || "the configured LLM"}. ${knowledgeMessage} Review the JSON before inserting into Bronze.`);
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }
  async function save() {
    setBusy(true); setError(""); setMessage("");
    try {
      const edited = records.map((item) => ({ record_id: item.record_id, payload: JSON.parse(item.payload_text || "{}") }));
      await reviewTestData(generationId, edited);
      const result = await saveTestDataToBronze(generationId);
      setRecords(result.records.map((item) => ({ ...item, payload_text: JSON.stringify(item.payload, null, 2) })));
      setMessage(`Saved ${result.saved_count} record(s) to Bronze.`);
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }
  function toggle(list, setList, id) { setList((items) => items.includes(id) ? items.filter((item) => item !== id) : [...items, id]); }
  const visibleCases = approvedCases.filter((item) => `${item.test_case_id} ${item.title}`.toLowerCase().includes(caseFilter.toLowerCase()));
  const selectedCaseCount = selectedCases.length;
  const canRegenerateDrafts = records.length > 0 && records.every((item) => item.status === "draft");
  const hasSavedRecords = records.some((item) => item.status === "saved");
  const dataReadyCaseIds = new Set(records.filter((record) => record.status === "saved").map((record) => record.test_case_id));
  const dataReadyCases = approvedCases.filter((item) => dataReadyCaseIds.has(item.test_case_id));

  return <section className="requirements-panel test-data-stage-panel">
    <div className="panel-title-row"><div><h2>Approved cases and generated records</h2></div><span>{records.length} record(s)</span></div>
    {error && <div className="error-message">{error}</div>}{message && <div className="toast-message test-data-toast" role="status" aria-live="polite">{message}</div>}
    {generationContext && <div className={`toast-message test-data-toast ${generationContext.used ? "" : "error-message"}`} role="status" aria-live="polite">{generationContext.used ? `Knowledge Base used${generationContext.index_id ? ` · ${generationContext.index_id}` : ""}` : "Knowledge Base was not used for this run."}</div>}
    <div className="test-data-cases-picker"><div className="test-data-case-heading"><div><h3>Approved test cases</h3><p className="panel-helper">Template data is derived from the selected package Knowledge Base for each case.</p></div><span>{selectedCases.length} of {approvedCases.length} selected</span></div><div className="test-data-case-actions"><input aria-label="Filter test cases" value={caseFilter} onChange={(event) => setCaseFilter(event.target.value)} placeholder="Filter by ID or title" /><div className="test-data-selection-actions"><button type="button" className="test-data-selection-action" onClick={() => setSelectedCases((items) => [...new Set([...items, ...visibleCases.map((item) => item.test_case_id)])])}><CheckCheck size={15} />{caseFilter.trim() ? "Select filtered" : "Select all"}</button><button type="button" className="test-data-selection-action clear-selection-action" onClick={() => setSelectedCases([])}><X size={15} />Clear selection</button></div></div><div className="test-data-case-list">{visibleCases.map((item) => <label className="test-data-case-item" key={item.test_case_id}><input type="checkbox" checked={selectedCases.includes(item.test_case_id)} onChange={() => toggle(selectedCases, setSelectedCases, item.test_case_id)} /><span><strong>{item.test_case_id} · {displayCaseName(item)}</strong></span></label>)}</div>
      <button type="button" className="primary-button" disabled={busy || (!selectedCases.length && !canRegenerateDrafts) || (records.length > 0 && !canRegenerateDrafts)} onClick={() => generate(canRegenerateDrafts)}>{busy ? <><LoaderCircle className="spin" size={16} /> {canRegenerateDrafts ? "Regenerating..." : "Generating..."}</> : canRegenerateDrafts ? <>Regenerate {new Set([...records.map((item) => item.test_case_id), ...selectedCases]).size} case(s)</> : hasSavedRecords ? "Data saved to Bronze" : <>Generate data for {selectedCaseCount} case{selectedCaseCount === 1 ? "" : "s"}</>}</button></div>
      {hasSavedRecords && <p className="panel-helper">Saved Bronze records are immutable. Create a new test-case generation to produce another dataset.</p>}
    {records.length > 0 && <>
      <div className="test-data-record-list">
        {records.map((record) => {
          const testCase = approvedCases.find((item) => item.test_case_id === record.test_case_id);
          return (
            <details className="test-data-record" key={record.record_id}>
              <summary>
                <strong>{record.test_case_id} - {displayCaseName(testCase) || "Generated test data"}</strong>
                <span>{displayRecordStatus(record)}</span>
              </summary>
              <label className="test-data-json-label">
                Review generated JSON
                <textarea
                  className="test-data-json-editor"
                  value={record.payload_text}
                  disabled={record.status === "saved"}
                  onChange={(event) => setRecords((items) => items.map((item) => item.record_id === record.record_id ? { ...item, payload_text: event.target.value } : item))}
                  spellCheck="false"
                />
              </label>
            </details>
          );
        })}
      </div>
      <button type="button" className="primary-button" disabled={busy || records.every((item) => item.status === "saved")} onClick={save}>
        {busy ? <><LoaderCircle className="spin" size={16} /> Saving...</> : <>Review and insert into Bronze</>}
      </button>
    </>}
    <ExecutionBaselinePanel generationId={generationId} reviewer={reviewer || "Product Owner"} eligibleCases={dataReadyCases} />
  </section>;
}
