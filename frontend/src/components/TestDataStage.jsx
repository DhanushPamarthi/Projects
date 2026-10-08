import { useEffect, useState } from "react";
import { CheckCheck, LoaderCircle, X } from "lucide-react";
import { generateTestData, listTestData, reviewTestData, saveTestDataToBronze } from "../api/client.js";

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
  if (status === "ready_for_execution") return "Ready for Bronze";
  if (status === "saved" && record?.bronze_raw_file_id) return "Sent to Bronze";
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
  const [selectedDataCaseIds, setSelectedDataCaseIds] = useState([]);
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
      const currentRegeneratableCaseIds = new Set(records.filter((item) => ["draft", "ready_for_execution", "saved"].includes(item.status)).map((item) => item.test_case_id));
      const caseIds = replaceExistingDrafts
        ? [...new Set([...currentRegeneratableCaseIds, ...selectedCases])]
        : selectedCases;
      const result = await generateTestData(generationId, { reviewer, test_case_ids: caseIds, replace_existing_drafts: replaceExistingDrafts });
      const nextRecords = result.records.map((item) => ({ ...item, payload_text: JSON.stringify(item.payload, null, 2) }));
      const regeneratedCaseIds = new Set(nextRecords.map((item) => item.test_case_id));
      const preservedRecords = records.filter((item) => !regeneratedCaseIds.has(item.test_case_id));
      const mergedRecords = [...preservedRecords, ...nextRecords];
      setRecords(mergedRecords);
      setSelectedDataCaseIds([]);
      const generated = `${replaceExistingDrafts ? "Regenerated" : "Generated"} data for ${new Set(result.records.map((item) => item.test_case_id)).size} case(s) using ${result.records.length} template record(s)`;
      setMessage(`${generated} for approved cases using case-specific templates built from the database schema and data patterns with ${result.generation_model || "the configured LLM"}. Review the JSON, select cases, then save them for CDP.`);
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }
  async function save() {
    setBusy(true); setError(""); setMessage("");
    try {
      const edited = records.filter((item) => selectedExecutionCaseIds.includes(item.test_case_id) && item.status === "draft").map((item) => ({ record_id: item.record_id, payload: JSON.parse(item.payload_text || "{}") }));
      if (edited.length) await reviewTestData(generationId, edited);
      const result = await saveTestDataToBronze(generationId, selectedExecutionCaseIds, reviewer || "Product Owner");
      const savedById = new Map(result.records.map((item) => [item.record_id, { ...item, payload_text: JSON.stringify(item.payload, null, 2) }]));
      setRecords((current) => current.map((item) => savedById.get(item.record_id) || item));
      setSelectedDataCaseIds([]);
      setMessage(result.message || `${result.sent_count || result.saved_count} test data record(s) sent to the Bronze layer successfully.`);
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }
  function toggle(list, setList, id) { setList((items) => items.includes(id) ? items.filter((item) => item !== id) : [...items, id]); }
  const visibleCases = approvedCases.filter((item) => `${item.test_case_id} ${item.title}`.toLowerCase().includes(caseFilter.toLowerCase()));
  const selectedCaseCount = selectedCases.length;
  const wasSentToBronze = (record) => Boolean(record.bronze_raw_file_id || (record.bronze_references || []).length);
  const canRegenerateDrafts = records.length > 0 && records.every((item) => ["draft", "ready_for_execution", "saved"].includes(item.status) && !wasSentToBronze(item));
  const hasSavedRecords = records.some((item) => ["saved", "ready_for_execution"].includes(item.status));
  const selectableCaseIds = [...new Set(records.map((record) => record.test_case_id))].filter((caseId) => records.filter((record) => record.test_case_id === caseId).every((record) => ["draft", "saved"].includes(record.status) && !wasSentToBronze(record)));
  const selectedExecutionCaseIds = selectedDataCaseIds.filter((caseId) => selectableCaseIds.includes(caseId));
  const selectedExecutionRecordCount = records.filter((record) => selectedExecutionCaseIds.includes(record.test_case_id)).length;
  const allReadyForExecution = records.length > 0 && records.every((item) => item.status === "ready_for_execution");
  const allSavedToBronze = records.length > 0 && records.every((item) => item.status === "saved");
  const groupedRecords = [...records.reduce((groups, record) => {
    if (!groups.has(record.test_case_id)) groups.set(record.test_case_id, []);
    groups.get(record.test_case_id).push(record);
    return groups;
  }, new Map()).entries()];
  const recordStatusCounts = records.reduce((counts, record) => {
    const label = displayRecordStatus(record);
    counts[label] = (counts[label] || 0) + 1;
    return counts;
  }, {});
  const overallStatus = Object.entries(recordStatusCounts).map(([label, count]) => `${label}${Object.keys(recordStatusCounts).length > 1 ? ` (${count})` : ""}`).join(" · ");

  return <section className="requirements-panel test-data-stage-panel">
    <div className="panel-title-row"><div><h2>Approved cases and generated records</h2></div><span>{groupedRecords.length} case(s) · {records.length} template record(s)</span></div>
    {error && <div className="error-message">{error}</div>}{message && <div className="toast-message test-data-toast" role="status" aria-live="polite">{message}</div>}
    <div className="test-data-cases-picker"><div className="test-data-case-heading"><div><h3>Approved test cases</h3><p className="panel-helper">Select one or more cases. Each selected case uses the stored Membership template, Prescription template, or both as the scenario requires. When both are used, their records share the same member_id.</p></div><span>{selectedCases.length} of {approvedCases.length} selected</span></div><div className="test-data-case-actions"><input aria-label="Filter test cases" value={caseFilter} onChange={(event) => setCaseFilter(event.target.value)} placeholder="Filter by ID or title" /><div className="test-data-selection-actions"><button type="button" className="test-data-selection-action" onClick={() => setSelectedCases((items) => [...new Set([...items, ...visibleCases.map((item) => item.test_case_id)])])}><CheckCheck size={15} />{caseFilter.trim() ? "Select filtered" : "Select all"}</button><button type="button" className="test-data-selection-action clear-selection-action" onClick={() => setSelectedCases([])}><X size={15} />Clear selection</button></div></div><div className="test-data-case-list">{visibleCases.map((item) => <label className="test-data-case-item" key={item.test_case_id}><input type="checkbox" checked={selectedCases.includes(item.test_case_id)} onChange={() => toggle(selectedCases, setSelectedCases, item.test_case_id)} /><span><strong>{item.test_case_id} · {displayCaseName(item)}</strong></span></label>)}</div>
      <button type="button" className="primary-button" disabled={busy || (!selectedCases.length && !canRegenerateDrafts) || (records.length > 0 && !canRegenerateDrafts)} onClick={() => generate(canRegenerateDrafts)}>{busy ? <><LoaderCircle className="spin" size={16} /> {canRegenerateDrafts ? "Regenerating..." : "Generating..."}</> : canRegenerateDrafts ? <>Regenerate {new Set(records.map((item) => item.test_case_id)).size} case(s)</> : allReadyForExecution ? "Ready for execution" : allSavedToBronze ? "Data saved" : hasSavedRecords ? "Ready for execution" : <>Generate data for {selectedCaseCount} case{selectedCaseCount === 1 ? "" : "s"}</>}</button></div>
    {records.length > 0 && <>
      <div className="test-data-record-list">
        <div className="test-data-overall-status"><span>Status</span><strong className={overallStatus === "Ready for Bronze" ? "test-data-ready-status" : ""}>{overallStatus}</strong></div>
        <div className="test-data-group-column-head"><span>Test case</span><span>Templates used</span></div>
        {groupedRecords.map(([testCaseId, caseRecords]) => {
          const testCase = approvedCases.find((item) => item.test_case_id === testCaseId);
          const labels = [...new Set(caseRecords.flatMap((record) => {
            if (String(record.entity_name || "").toLowerCase() !== "test_dataset") return [recordTemplateLabel(record)];
            const payload = parsedRecordPayload(record);
            return [payload.membership && "Membership", payload.prescription && "Prescription"].filter(Boolean);
          }))];
          const allRowsAvailable = caseRecords.every((item) => ["draft", "saved"].includes(item.status) && !wasSentToBronze(item));
          const caseSelected = allRowsAvailable && selectedExecutionCaseIds.includes(testCaseId);
          return <div className="test-data-case-selection-group" key={testCaseId}>
            <label className="test-data-case-selection"><input type="checkbox" aria-label={`Select all data for ${testCaseId}`} checked={caseSelected} disabled={!allRowsAvailable} onChange={() => toggle(selectedDataCaseIds, setSelectedDataCaseIds, testCaseId)} /></label>
            <details className="test-data-record test-data-case-group">
            <summary className="test-data-group-summary"><span>{testCaseId} - {displayCaseName(testCase) || "Generated test data"}</span><span>{labels.join(" + ")}</span></summary>
            <div className="test-data-group-content">
              {caseRecords.map((record) => <div className="test-data-record-payload" key={record.record_id}>
                <label className="test-data-json-label">{record.status === "draft" ? `Review ${recordTemplateLabel(record)} JSON` : `${recordTemplateLabel(record)} JSON`}<textarea className="test-data-json-editor" value={record.payload_text} disabled={record.status !== "draft"} onChange={(event) => setRecords((items) => items.map((item) => item.record_id === record.record_id ? { ...item, payload_text: event.target.value } : item))} spellCheck="false" /></label>
              </div>)}
            </div>
            </details>
          </div>;
        })}
      </div>{selectableCaseIds.length > 0 && <div className="test-data-record-selection-bar"><span>{selectedExecutionCaseIds.length} test case(s) selected · {selectedExecutionRecordCount} template record(s)</span><div className="test-data-selection-actions"><button type="button" className="test-data-selection-action" onClick={() => setSelectedDataCaseIds(selectableCaseIds)}><CheckCheck size={15} />Select all available cases</button><button type="button" className="test-data-selection-action clear-selection-action" onClick={() => setSelectedDataCaseIds([])}><X size={15} />Clear</button></div></div>}{selectedExecutionCaseIds.length > 0 && <button type="button" className="primary-button" disabled={busy} onClick={save}>
        {busy ? <><LoaderCircle className="spin" size={16} /> Sending to Bronze...</> : <>Send to Bronze</>}
      </button>}
    </>}
  </section>;
}
