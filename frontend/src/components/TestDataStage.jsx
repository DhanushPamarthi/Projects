import { useEffect, useState } from "react";
import { CheckCheck, LoaderCircle, X } from "lucide-react";
import { createExecutionRun, generateTestData, listExecutionRuns, listTestData, reviewTestData, saveTestDataToBronze } from "../api/client.js";

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
  if (status === "ready_for_cdp") return "Queued for CDP";
  if (status === "saved") return "Saved";
  if (status === "draft") return "Draft";
  return status ? status.charAt(0).toUpperCase() + status.slice(1) : "Generated";
}
function parsedRecordPayload(record) {
  try { return JSON.parse(record.payload_text || "{}"); } catch { return record.payload || {}; }
}
function recordTemplateLabel(record) {
  if (String(record.entity_name || "").toLowerCase() === "test_dataset") return "Membership + Prescription";
  return String(record.entity_name || record.template_name || "Test data").replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}
export function TestDataStage({ projectId, generationId, approvedCases, reviewer, onGoToTestCases }) {
  const [selectedCases, setSelectedCases] = useState([]);
  const [records, setRecords] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
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
    listTestData(generationId).then((items) => {
      const nextRecords = items.map((item) => ({ ...item, payload_text: JSON.stringify(item.payload, null, 2) }));
      setRecords(nextRecords);
    }).catch(() => setRecords([]));
  }, [generationId]);

  async function generate(replaceExistingDrafts = false) {
    setBusy(true); setError(""); setMessage("");
    try {
      // Replacing drafts clears the existing draft set on the server, so include
      // both previously generated cases and any newly selected approved cases.
      const currentRegeneratableCaseIds = new Set(records.filter((item) => ["draft", "ready_for_cdp", "saved"].includes(item.status)).map((item) => item.test_case_id));
      const caseIds = replaceExistingDrafts
        ? [...new Set([...currentRegeneratableCaseIds, ...selectedCases])]
        : selectedCases;
      const result = await generateTestData(generationId, { reviewer, test_case_ids: caseIds, replace_existing_drafts: replaceExistingDrafts });
      const nextRecords = result.records.map((item) => ({ ...item, payload_text: JSON.stringify(item.payload, null, 2) }));
      const regeneratedCaseIds = new Set(nextRecords.map((item) => item.test_case_id));
      const preservedRecords = records.filter((item) => !regeneratedCaseIds.has(item.test_case_id));
      const mergedRecords = [...preservedRecords, ...nextRecords];
      setRecords(mergedRecords);
      const generated = `${replaceExistingDrafts ? "Regenerated" : "Generated"} data for ${new Set(result.records.map((item) => item.test_case_id)).size} case(s) using ${result.records.length} template record(s)`;
      setMessage(`${generated} from the stored BronzeBox Membership and Prescription templates with ${result.generation_model || "the configured LLM"}. Review the JSON, select cases, then save them in the shared database for CDP.`);
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }
  async function save() {
    setBusy(true); setError(""); setMessage("");
    try {
      const edited = records.map((item) => ({ record_id: item.record_id, payload: JSON.parse(item.payload_text || "{}") }));
      await reviewTestData(generationId, edited);
      const result = await saveTestDataToBronze(generationId, selectedDraftCaseIds);
      const savedById = new Map(result.records.map((item) => [item.record_id, { ...item, payload_text: JSON.stringify(item.payload, null, 2) }]));
      setRecords((current) => current.map((item) => savedById.get(item.record_id) || item));
      const existingRuns = await listExecutionRuns(generationId);
      const selectedKey = [...selectedDraftCaseIds].sort().join("|");
      const existingRun = existingRuns.find((run) => [...(run.selected_test_case_ids || [])].sort().join("|") === selectedKey && !["cancelled", "failed"].includes(String(run.lifecycle_state || run.status).toLowerCase()));
      const run = existingRun || await createExecutionRun(generationId, { reviewer: reviewer || "Product Owner", selected_test_case_ids: selectedDraftCaseIds });
      setMessage(`Saved ${result.saved_count} test data record(s) for ${result.ready_case_count} selected case(s) in the shared database. CDP can now pick them up. Execution run ${run.run_id} was created; check Test Results after CDP finishes processing.`);
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }
  function toggle(list, setList, id) { setList((items) => items.includes(id) ? items.filter((item) => item !== id) : [...items, id]); }
  const visibleCases = approvedCases.filter((item) => `${item.test_case_id} ${item.title}`.toLowerCase().includes(caseFilter.toLowerCase()));
  const selectedCaseCount = selectedCases.length;
  const canRegenerateDrafts = records.length > 0 && records.every((item) => ["draft", "ready_for_cdp", "saved"].includes(item.status));
  const hasSavedRecords = records.some((item) => ["saved", "ready_for_cdp"].includes(item.status));
  const draftCaseIds = new Set(records.filter((record) => record.status === "draft").map((record) => record.test_case_id));
  const selectedDraftCaseIds = selectedCases.filter((caseId) => draftCaseIds.has(caseId));
  const allReadyForCdp = records.length > 0 && records.every((item) => item.status === "ready_for_cdp");
  const allSavedToBronze = records.length > 0 && records.every((item) => item.status === "saved");
  const groupedRecords = [...records.reduce((groups, record) => {
    if (!groups.has(record.test_case_id)) groups.set(record.test_case_id, []);
    groups.get(record.test_case_id).push(record);
    return groups;
  }, new Map()).entries()];

  return <section className="requirements-panel test-data-stage-panel">
    <div className="panel-title-row"><div><h2>Approved cases and generated records</h2></div><span>{groupedRecords.length} case(s) · {records.length} template record(s)</span></div>
    {error && <div className="error-message">{error}</div>}{message && <div className="toast-message test-data-toast" role="status" aria-live="polite">{message}</div>}
    <div className="test-data-cases-picker"><div className="test-data-case-heading"><div><h3>Approved test cases</h3><p className="panel-helper">Select one or more cases. Each selected case uses the stored Membership template, Prescription template, or both as the scenario requires. When both are used, their records share the same member_id.</p></div><span>{selectedCases.length} of {approvedCases.length} selected</span></div><div className="test-data-case-actions"><input aria-label="Filter test cases" value={caseFilter} onChange={(event) => setCaseFilter(event.target.value)} placeholder="Filter by ID or title" /><div className="test-data-selection-actions"><button type="button" className="test-data-selection-action" onClick={() => setSelectedCases((items) => [...new Set([...items, ...visibleCases.map((item) => item.test_case_id)])])}><CheckCheck size={15} />{caseFilter.trim() ? "Select filtered" : "Select all"}</button><button type="button" className="test-data-selection-action clear-selection-action" onClick={() => setSelectedCases([])}><X size={15} />Clear selection</button></div></div><div className="test-data-case-list">{visibleCases.map((item) => <label className="test-data-case-item" key={item.test_case_id}><input type="checkbox" checked={selectedCases.includes(item.test_case_id)} onChange={() => toggle(selectedCases, setSelectedCases, item.test_case_id)} /><span><strong>{item.test_case_id} · {displayCaseName(item)}</strong></span></label>)}</div>
      <button type="button" className="primary-button" disabled={busy || (!selectedCases.length && !canRegenerateDrafts) || (records.length > 0 && !canRegenerateDrafts)} onClick={() => generate(canRegenerateDrafts)}>{busy ? <><LoaderCircle className="spin" size={16} /> {canRegenerateDrafts ? "Regenerating..." : "Generating..."}</> : canRegenerateDrafts ? <>Regenerate {new Set(records.map((item) => item.test_case_id)).size} case(s)</> : allReadyForCdp ? "Data queued for CDP" : allSavedToBronze ? "Data saved" : hasSavedRecords ? "Saved for CDP" : <>Generate data for {selectedCaseCount} case{selectedCaseCount === 1 ? "" : "s"}</>}</button></div>
    {records.length > 0 && <>
      <div className="test-data-record-list">
        <div className="test-data-group-column-head"><span>Test case</span><span>Templates used</span><span>Status</span></div>
        {groupedRecords.map(([testCaseId, caseRecords]) => {
          const testCase = approvedCases.find((item) => item.test_case_id === testCaseId);
          const labels = [...new Set(caseRecords.flatMap((record) => {
            if (String(record.entity_name || "").toLowerCase() !== "test_dataset") return [recordTemplateLabel(record)];
            const payload = parsedRecordPayload(record);
            return [payload.membership && "Membership", payload.prescription && "Prescription"].filter(Boolean);
          }))];
          const status = caseRecords.every((item) => item.status === caseRecords[0].status)
            ? displayRecordStatus(caseRecords[0]) : `${caseRecords.length} records in different states`;
          return <details className="test-data-record test-data-case-group" key={testCaseId}>
            <summary className="test-data-group-summary"><span>{testCaseId} - {displayCaseName(testCase) || "Generated test data"}</span><span>{labels.join(" + ")}</span><span>{status}</span></summary>
            <div className="test-data-group-content">
              {caseRecords.map((record) => <label className="test-data-json-label" key={record.record_id}>{record.status === "draft" ? `Review ${recordTemplateLabel(record)} JSON` : `${recordTemplateLabel(record)} JSON`}<textarea className="test-data-json-editor" value={record.payload_text} disabled={record.status !== "draft"} onChange={(event) => setRecords((items) => items.map((item) => item.record_id === record.record_id ? { ...item, payload_text: event.target.value } : item))} spellCheck="false" /></label>)}
            </div>
          </details>;
        })}
      </div>{selectedDraftCaseIds.length > 0 && <button type="button" className="primary-button" disabled={busy} onClick={save}>
        {busy ? <><LoaderCircle className="spin" size={16} /> Saving for CDP...</> : <>Save {selectedDraftCaseIds.length} selected case(s) for CDP and create execution run</>}
      </button>}
    </>}
  </section>;
}
