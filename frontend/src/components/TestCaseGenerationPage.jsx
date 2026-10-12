import { useEffect, useMemo, useState } from "react";
import { AlertCircle, ArrowLeft, Check, CheckCircle2, ChevronLeft, ChevronRight, ClipboardList, LoaderCircle, Pencil, Plus, RotateCcw, ShieldCheck, Sparkles, X } from "lucide-react";
import { addTestCaseToGeneration, generateFunctionalRequirements, generateTestCasesFromAnalysis, getRequirementAnalysis, getTestCaseGeneration, importExternalTestCases, listGeneratedTestCases, listProjectFunctionalRequirementHistory, listProjectTestCases, listProjectTestCaseHistory, listProjectTestCaseLibrary, listRequirementAnalyses, listTestData, previewTestCaseReuse, reviewTestCaseGeneration } from "../api/client.js";
import { useActivities } from "../activity/activityStore.js";
import { AppSelect } from "./AppSelect.jsx";

function lines(value) {
  return String(value || "")
    .replace(/\\r\\n/g, "\n")
    .replace(/\\n/g, "\n")
    .split(/\r?\n/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function caseContract(generation) {
  return generation?.contract || generation || {};
}

function advanceProgressUntilComplete(setProgress) {
  return window.setInterval(() => {
    setProgress((current) => {
      if (!current || current.percent >= 94) return current;
      const nextPercent = Math.min(94, current.percent + Math.max(1, Math.ceil((94 - current.percent) / 12)));
      return { ...current, percent: nextPercent };
    });
  }, 700);
}

function shortCategory(value) {
  return String(value || "case").replaceAll("_", " ");
}

function caseDescription(item) {
  const explicit = displayText(item.description);
  if (explicit && !/^requirement behavior is validated from the approved contract\.?$/i.test(explicit)) return explicit;
  const expected = Array.isArray(item.expected_results) ? item.expected_results.find((value) => typeof value === "string" && value.trim()) : "";
  return buildCaseDescription(item, expected);
}

function displayText(value) {
  return String(value || "")
    .replace(/\\r\\n/g, " ")
    .replace(/\\n/g, " ")
    .replace(/[\r\n]+/g, " ")
    .replace(/customer data platform\s*\(\s*cdp\s*\)/gi, "")
    .replace(/customer data platform/gi, "")
    .replace(/\bCDP\b/gi, "")
    .replace(/\b(member|campaign|customer|prescription|account|claim|patient|order|user|transaction)\s+(?:with\s+)?ID\s+['"][^'"]+['"]/gi, (_, entity) => `${entity.toLowerCase()}_id`)
    .replace(/\s+in\s+the\s*([.!?]|$)/gi, "$1")
    .replace(/\s+/g, " ")
    .trim();
}

function buildCaseDescription(item, expectedResult) {
  const rawScenario = displayText(item.scenario || item.title || "the documented behavior");
  const scenario = (/^requirement behavior is validated from the approved contract\.?$/i.test(rawScenario) ? caseBaseTitle(item) : rawScenario).replace(/[.!?]+$/, "");
  const dataFields = [...new Set((item.data_needs || []).map((need) => displayText(typeof need === "string" ? need : need.name)).filter(Boolean))];
  const dataSummary = dataFields.length ? `It uses ${dataFields.join(" and ")} for this condition` : "It uses the documented test data";
  const outcome = displayText(expectedResult || "the expected behavior from the approved requirement").replace(/[.!?]+$/, "");
  return `This case covers the scenario where ${scenario[0].toLowerCase()}${scenario.slice(1)}. ${dataSummary} and checks for the expected outcome: ${outcome}.`;
}

function caseBaseTitle(item) {
  let title = displayText(item.title);
  title = title.replace(/^verify the objective is to test\s+/i, "Verify ")
    .replace(/^verify the campaign should identify eligible members who are due for a prescription refill and send a refill reminder communication\.?$/i, "Verify refill eligibility and reminder delivery")
    .replace(/^verify the campaign should identify members who meet the refill reminder criteria\.?$/i, "Verify refill reminder eligibility")
    .replace(/^verify the campaign should\s+/i, "Verify campaign ")
    .replace(/^verify for an eligible member,?\s*/i, "Verify eligible-member ")
    .replace(/\s+/g, " ")
    .trim();
  if (!/^(?:requirement validation|functional behavior for\b|AC-\d+\b|BR-\d+\b)/i.test(title)) {
    return title || item.test_case_id;
  }
  const expected = Array.isArray(item.expected_results) ? item.expected_results.find((value) => typeof value === "string" && value.trim()) : "";
  const behavior = displayText(expected || item.scenario).replace(/[.!?]+$/, "");
  if (!behavior) return title;
  const formatted = behavior[0].toLowerCase() + behavior.slice(1);
  return `Verify ${formatted}`;
}

function caseDisplayTitle(item, allCases = []) {
  const title = caseBaseTitle(item);
  const duplicates = allCases.filter((candidate) => caseBaseTitle(candidate).toLocaleLowerCase() === title.toLocaleLowerCase());
  if (duplicates.length < 2) return title;
  const sourceId = item.acceptance_criteria_ids?.[0] || item.business_rule_ids?.[0] || item.requirement_id || item.test_case_id;
  const sameSource = duplicates.filter((candidate) => (candidate.acceptance_criteria_ids?.[0] || candidate.business_rule_ids?.[0] || candidate.requirement_id || candidate.test_case_id) === sourceId);
  return `${title} · ${sameSource.length > 1 ? item.test_case_id : sourceId}`;
}

export function TestCaseGenerationPage({ analysis, onBack, onReviewSaved, onAnalysisUnavailable, onAnalysisPrepared }) {
  const activities = useActivities();
  const [verifiedAnalysis, setVerifiedAnalysis] = useState(null);
  const [analysisLoading, setAnalysisLoading] = useState(Boolean(analysis?.analysis_id));
  const [analysisError, setAnalysisError] = useState("");
  const [generation, setGeneration] = useState(null);
  const [selected, setSelected] = useState([]);
  const [statuses, setStatuses] = useState({});
  const [reviewCommentsByCase, setReviewCommentsByCase] = useState({});
  const [editingId, setEditingId] = useState("");
  const [addingAfterId, setAddingAfterId] = useState("");
  const [editingTitle, setEditingTitle] = useState("");
  const [editingSteps, setEditingSteps] = useState("");
  const [editingExpected, setEditingExpected] = useState("");
  const [editingReviewComment, setEditingReviewComment] = useState("");
  const [reviewer, setReviewer] = useState("Product Owner");
  const [comments, setComments] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [generatedDataByCase, setGeneratedDataByCase] = useState({});
  const [workflowProgress, setWorkflowProgress] = useState(null);
  const [casePage, setCasePage] = useState(1);
  const [functionalHistoryPage, setFunctionalHistoryPage] = useState(1);
  const [projectHistoryPage, setProjectHistoryPage] = useState(1);
  const [reusePreview, setReusePreview] = useState(null);
  const [reuseSelections, setReuseSelections] = useState({});
  const [library, setLibrary] = useState([]);
  const [runHistory, setRunHistory] = useState([]);
  const [viewedHistoryRun, setViewedHistoryRun] = useState(null);
  const [libraryEdit, setLibraryEdit] = useState(null);
  const [reviewTab, setReviewTab] = useState("pending");
  const [projectCaseMode, setProjectCaseMode] = useState("cumulative");
  const [selectedPackageVersion, setSelectedPackageVersion] = useState("");
  const [projectCaseData, setProjectCaseData] = useState(null);
  const [versionCaseData, setVersionCaseData] = useState(null);
  const [functionalHistory, setFunctionalHistory] = useState(null);
  const [functionalHistoryLoading, setFunctionalHistoryLoading] = useState(false);
  const [functionalHistoryError, setFunctionalHistoryError] = useState("");
  const activeGenerationActivity = activities.find((item) => item.status === "running" && (item.label === "Generating Functional Requirements" || item.label === "Generating test cases"));
  const generationActivityKey = activities.filter((item) => item.label === "Generating Functional Requirements" || item.label === "Generating test cases").map((item) => `${item.id}:${item.status}:${item.updatedAt}`).join("|");

  const currentAnalysis = verifiedAnalysis && (
    verifiedAnalysis.analysis_id === analysis?.analysis_id ||
    (analysis?.generation_pipeline && verifiedAnalysis.package_id === analysis?.package_id)
  ) ? verifiedAnalysis : null;
  const projectId = currentAnalysis?.project_id || analysis?.project_id || "";
  const isAnalysisLoading = analysisLoading || Boolean(analysis?.analysis_id && !currentAnalysis && !analysisError);
  const requirements = currentAnalysis?.contract?.requirements || [];
  const approvedIds = new Set(currentAnalysis?.contract?.approved_items || []);
  const approvedRequirements = requirements.filter((item) => approvedIds.has(item.requirement_id) || item.review_status === "approved");
  const generationRequirements = approvedRequirements.length ? approvedRequirements : requirements;
  const contract = caseContract(generation);
  /* Coverage and grounding is intentionally hidden for the current release.
  const coverageSummary = contract.coverage_review?.summary || {};
  const coverageGaps = (contract.coverage_review?.matrix || []).flatMap((requirement) => []);
  */
  const generationId = generation?.generation_id || contract.generation_id;
  const testCases = contract.test_cases || [];
  const generationMode = contract.generation_mode;
  const generationModel = contract.generation_model;
  const approvedCount = testCases.filter((item) => (statuses[item.test_case_id] || item.review_status || item.approval?.status || "pending") === "approved").length;
  const hasUnsavedDecisions = testCases.some((item) => (statuses[item.test_case_id] || "pending") !== (item.review_status || item.approval?.status || "pending"));
  const allApproved = testCases.length > 0 && approvedCount === testCases.length && !hasUnsavedDecisions;

  useEffect(() => {
    if (!editingId && !addingAfterId) return undefined;
    const previousOverflow = document.body.style.overflow;
    const closeOnEscape = (event) => {
      if (event.key === "Escape" && !saving) { setEditingId(""); setAddingAfterId(""); }
    };
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", closeOnEscape);
    return () => { document.body.style.overflow = previousOverflow; window.removeEventListener("keydown", closeOnEscape); };
  }, [editingId, addingAfterId, saving]);

  useEffect(() => {
    if (!projectId) { setFunctionalHistory(null); return undefined; }
    let active = true;
    setFunctionalHistoryLoading(true);
    setFunctionalHistoryError("");
    listProjectFunctionalRequirementHistory(projectId)
      .then((data) => { if (active) setFunctionalHistory(data); })
      .catch((err) => { if (active) setFunctionalHistoryError(err.message || "Could not load Functional Requirement history."); })
      .finally(() => { if (active) setFunctionalHistoryLoading(false); });
    return () => { active = false; };
  }, [projectId, generationId, generationActivityKey]);

  useEffect(() => {
    if (!projectId) { setProjectCaseData(null); return undefined; }
    let active = true;
    listProjectTestCases(projectId).then((data) => {
      if (!active) return;
      setProjectCaseData(data);
      const selectedVersionExists = data.versions?.some((version) => String(version.package_version) === selectedPackageVersion);
      if (!selectedVersionExists) setSelectedPackageVersion(data.versions?.length ? String(data.versions[0].package_version) : "");
    }).catch((err) => { if (active) setError(err.message || "Could not load project test-case history."); });
    return () => { active = false; };
  }, [projectId, generationId, generationActivityKey, selectedPackageVersion]);

  useEffect(() => {
    if (!projectId || projectCaseMode !== "version" || !selectedPackageVersion) { setVersionCaseData(null); return undefined; }
    let active = true;
    listProjectTestCases(projectId, selectedPackageVersion).then((data) => { if (active) setVersionCaseData(data); })
      .catch((err) => { if (active) setError(err.message || "Could not load test cases for that version."); });
    return () => { active = false; };
  }, [projectId, projectCaseMode, selectedPackageVersion, generationId, generationActivityKey]);

  useEffect(() => {
    if (!generationId) { setGeneratedDataByCase({}); return undefined; }
    let active = true;
    listTestData(generationId).then((records) => {
      if (!active) return;
      const grouped = {};
      for (const record of records || []) {
        grouped[record.test_case_id] = { ...(grouped[record.test_case_id] || {}), ...(record.payload || {}) };
      }
      setGeneratedDataByCase(grouped);
    }).catch(() => { if (active) setGeneratedDataByCase({}); });
    return () => { active = false; };
  }, [generationId]);

  function applyGeneration(next) {
    const nextContract = caseContract(next);
    const nextCases = nextContract.test_cases || [];
    const nextStatuses = Object.fromEntries(nextCases.map((item) => [item.test_case_id, item.review_status || item.approval?.status || "pending"]));
    const nextComments = Object.fromEntries(nextCases.map((item) => [item.test_case_id, item.approval?.comments || item.review_comments || ""]));
    setGeneration(next);
    setStatuses(nextStatuses);
    setReviewCommentsByCase(nextComments);
  }

  useEffect(() => {
    let active = true;
    setAnalysisError("");
    if (!analysis?.analysis_id) {
      setAnalysisLoading(false);
      if (!analysis?.generation_pipeline) {
        setGeneration(null);
        setVerifiedAnalysis(null);
      }
      return () => { active = false; };
    }
    if (verifiedAnalysis?.analysis_id === analysis.analysis_id) {
      setAnalysisLoading(false);
      return () => { active = false; };
    }
    setAnalysisLoading(true);
    getRequirementAnalysis(analysis.analysis_id)
      .then((items) => {
        if (active) setVerifiedAnalysis(items);
      })
      .catch((err) => {
        if (!active) return;
        setAnalysisError(err.status === 404 ? "not-found" : err.message || "load-failed");
        setGeneration(null);
        if (err.status === 404) onAnalysisUnavailable?.();
      })
      .finally(() => { if (active) setAnalysisLoading(false); });
    return () => { active = false; };
  }, [analysis?.analysis_id, analysis?.generation_pipeline, verifiedAnalysis?.analysis_id, onAnalysisUnavailable]);
  useEffect(() => {
    if (!analysis?.generation_pipeline || !analysis?.package_id || analysis?.analysis_id) return undefined;
    let active = true;
    setAnalysisLoading(true);
    setError("");
    listRequirementAnalyses(analysis.package_id)
      .then(async (items) => {
        if (!active) return;
        const savedAnalysis = (items || []).find((item) => item.analysis_kind === "functional");
        if (!savedAnalysis) return;
        setVerifiedAnalysis(savedAnalysis);
        onAnalysisPrepared?.({ ...savedAnalysis, generation_pipeline: true, project_id: analysis.project_id || savedAnalysis.project_id, project_name: analysis.project_name });
        const savedGenerations = await listGeneratedTestCases(savedAnalysis.analysis_id);
        if (active) applyGeneration(savedGenerations?.[0] || null);
      })
      .catch((err) => {
        if (active) setError(err.message || "Could not load the saved test-generation results.");
      })
      .finally(() => { if (active) setAnalysisLoading(false); });
    return () => { active = false; };
  }, [analysis?.generation_pipeline, analysis?.package_id, analysis?.analysis_id, analysis?.project_id, analysis?.project_name, onAnalysisPrepared]);

  useEffect(() => {
    if (isAnalysisLoading || loading || !currentAnalysis?.analysis_id) return;
    listGeneratedTestCases(currentAnalysis.analysis_id)
      .then((items) => applyGeneration(items[0] || null))
      .catch((err) => setError(err.message || "Could not load generated test cases."));
  }, [currentAnalysis?.analysis_id, isAnalysisLoading, loading, generationActivityKey]);

  useEffect(() => {
    if (!analysis?.generation_pipeline) return;
    if (!activeGenerationActivity) {
      setLoading(false);
      return;
    }
    const generatingCases = activeGenerationActivity.label === "Generating test cases";
    setLoading(true);
    setWorkflowProgress({ percent: generatingCases ? 72 : 24, label: activeGenerationActivity.label });
  }, [analysis?.generation_pipeline, activeGenerationActivity?.id, activeGenerationActivity?.label]);

  async function generate() {
    if (!currentAnalysis?.analysis_id) return;
    let progressTimer;
    setLoading(true);
    setError("");
    setSuccess("");
    if (analysis?.generation_pipeline) {
      setWorkflowProgress({ percent: 58, label: "Generating Test Cases" });
      progressTimer = advanceProgressUntilComplete(setWorkflowProgress);
    }
    try {
      const next = await generateTestCasesFromAnalysis(currentAnalysis.analysis_id, {});
      applyGeneration(next);
      setSelected([]);
      if (analysis?.generation_pipeline) setWorkflowProgress({ percent: 100, label: "Test Cases generated" });
    } catch (err) {
      setError(err.message);
    } finally {
      if (progressTimer) window.clearInterval(progressTimer);
      setLoading(false);
    }
  }

  async function generateProjectTestCases() {
    if (!analysis?.package_id || loading) return;
    let progressTimer;
    setLoading(true);
    setError("");
    setSuccess("");
    setWorkflowProgress({ percent: 5, label: "Preparing Functional Requirements" });
    let requirementsReady = false;
    try {
      setWorkflowProgress({ percent: 18, label: "Generating Functional Requirements" });
      const nextAnalysis = await generateFunctionalRequirements(analysis.package_id);
      requirementsReady = true;
      setWorkflowProgress({ percent: 50, label: "Preparing test cases" });
      setVerifiedAnalysis(nextAnalysis);
      onAnalysisPrepared?.({ ...nextAnalysis, generation_pipeline: true, project_id: analysis.project_id, project_name: analysis.project_name });
      setWorkflowProgress({ percent: 58, label: "Generating test cases" });
      progressTimer = advanceProgressUntilComplete(setWorkflowProgress);
      const nextGeneration = await generateTestCasesFromAnalysis(nextAnalysis.analysis_id, {});
      applyGeneration(nextGeneration);
      setSelected([]);
      setWorkflowProgress({ percent: 100, label: "Test cases generated" });
      setSuccess("Test cases are ready for review. You can also review project-wide test-case history below.");
    } catch (err) {
      setError(requirementsReady
        ? `Functional Requirements are ready, but test-case generation failed: ${err.message || "Please try again."}`
        : err.message || "Could not prepare Functional Requirements.");
    } finally {
      if (progressTimer) window.clearInterval(progressTimer);
      setLoading(false);
    }
  }

  async function refreshProjectCases() {
    if (!projectId) return;
    const [data, versionData] = await Promise.all([
      listProjectTestCases(projectId),
      projectCaseMode === "version" && selectedPackageVersion ? listProjectTestCases(projectId, selectedPackageVersion) : Promise.resolve(null),
    ]);
    setProjectCaseData(data);
    if (versionData) setVersionCaseData(versionData);
  }

  async function uploadExternalCases(event) {
    const file = event.target.files?.[0];
    if (!file || !projectId) return;
    try {
      const parsed = JSON.parse(await file.text());
      const cases = Array.isArray(parsed) ? parsed : parsed.cases;
      if (!Array.isArray(cases)) throw new Error("JSON must contain a cases array.");
      const result = await importExternalTestCases(projectId, { source_name: file.name, cases });
      await refreshProjectCases();
      setSuccess(`${result.imported_count} external test cases imported into this project's history.`);
    } catch (err) { setError(err.message || "Could not import external cases."); }
    event.target.value = "";
  }

  async function saveLibraryEdit() {
    if (!libraryEdit) return;
    try {
      const changes = { title: libraryEdit.title.trim(), steps: lines(libraryEdit.steps), expected_results: lines(libraryEdit.expected_results) };
      if (!changes.title || !changes.steps.length || !changes.expected_results.length) throw new Error("Title, steps, and expected results are required.");
      if (libraryEdit.origin === "external") {
        await importExternalTestCases(currentAnalysis.project_id, { source_name: `${libraryEdit.source_name || "External import"} (edited)`, cases: [{ requirement_text: libraryEdit.requirement_text, ...changes }] });
      } else {
        const run = await getTestCaseGeneration(libraryEdit.generation_id);
        await reviewTestCaseGeneration(libraryEdit.generation_id, { reviewer: reviewer.trim() || "Product Owner", comments: "Edited from project test-case library", items: run.contract.test_cases.map((item) => ({
          test_case_id: item.test_case_id, review_status: item.review_status || item.approval?.status || "pending",
          title: item.test_case_id === libraryEdit.case.test_case_id ? changes.title : item.title,
          steps: item.test_case_id === libraryEdit.case.test_case_id ? changes.steps : item.steps,
          expected_results: item.test_case_id === libraryEdit.case.test_case_id ? changes.expected_results : item.expected_results,
        })) });
      }
      setLibraryEdit(null);
      await refreshReuse();
      if (viewedHistoryRun?.generation_id === libraryEdit.generation_id) {
        setViewedHistoryRun(await getTestCaseGeneration(libraryEdit.generation_id));
      }
      setSuccess("Test case edit saved; earlier versions remain in review history.");
    } catch (err) { setError(err.message); }
  }

  async function moveSelected(status, itemIds = selected, itemComments = {}) {
    if (!generationId || !itemIds.length || saving) return;
    const nextStatuses = { ...statuses, ...Object.fromEntries(itemIds.map((id) => [id, status])) };
    const nextComments = { ...reviewCommentsByCase, ...itemComments };
    setStatuses(nextStatuses);
    setSelected((current) => current.filter((id) => !itemIds.includes(id)));
    setSaving(true);
    setError("");
    try {
      const next = await reviewTestCaseGeneration(generationId, {
        reviewer: reviewer.trim(),
        comments: comments.trim() || null,
        items: testCases.map((item) => ({
          test_case_id: item.test_case_id,
          review_status: nextStatuses[item.test_case_id] || item.review_status || item.approval?.status || "pending",
          comments: nextComments[item.test_case_id] || null,
          title: item.title,
          steps: item.steps,
          expected_results: item.expected_results,
        })),
      });
      applyGeneration(next);
      setSuccess("Review status saved.");
    } catch (err) {
      setStatuses(statuses);
      setError(err.message || "Could not save the review status.");
    } finally {
      setSaving(false);
    }
  }

  function toggleSelected(id) {
    setSelected((items) => items.includes(id) ? items.filter((item) => item !== id) : [...items, id]);
  }

  function handleStatusAction(status, itemIds) {
    if (status !== "rejected") return moveSelected(status, itemIds);
    const comments = {};
    for (const id of itemIds) {
      const reason = window.prompt(`Review comment for ${id} (required)`, reviewCommentsByCase[id] || "");
      if (!reason || !reason.trim()) return;
      comments[id] = reason.trim();
    }
    return moveSelected(status, itemIds, comments);
  }

  function startEdit(item) {
    setAddingAfterId("");
    setError("");
    setEditingId(item.test_case_id);
    setEditingTitle(caseDisplayTitle(item, testCases));
    setEditingSteps((item.steps || []).join("\n"));
    setEditingExpected((item.expected_results || []).join("\n"));
    setEditingReviewComment(reviewCommentsByCase[item.test_case_id] || item.approval?.comments || item.review_comments || "");
  }

  function startAdd(item) {
    setEditingId("");
    setAddingAfterId(item.test_case_id);
    setEditingTitle("");
    setEditingSteps("");
    setEditingExpected("");
    setEditingReviewComment("");
    setError("");
  }

  function closeCaseModal() {
    setEditingId("");
    setAddingAfterId("");
    setError("");
  }

  async function addTestCase() {
    const title = editingTitle.trim();
    const steps = lines(editingSteps);
    const expectedResults = lines(editingExpected);
    if (!title || !steps.length || !expectedResults.length) { setError("Title, steps, and expected results are required."); return; }
    setSaving(true); setError(""); setSuccess("");
    try {
      const next = await addTestCaseToGeneration(generationId, {
        source_test_case_id: addingAfterId,
        reviewer: reviewer.trim() || "Product Owner",
        title,
        steps,
        expected_results: expectedResults,
        comments: editingReviewComment.trim() || null,
      });
      const nextPending = (caseContract(next).test_cases || []).filter((item) => (item.review_status || item.approval?.status || "pending") === "pending");
      applyGeneration(next);
      setReviewTab("pending");
      setCasePage(Math.max(1, Math.ceil(nextPending.length / 6)));
      closeCaseModal();
      await refreshProjectCases();
      setSuccess(`${next.created_test_case_id || "New test case"} added and ready for review.`);
    } catch (err) { setError(err.message); }
    finally { setSaving(false); }
  }

  async function saveEdit(id) {
    const title = editingTitle.trim();
    const steps = lines(editingSteps);
    const expectedResults = lines(editingExpected);
    if (!title || !steps.length || !expectedResults.length) { setError("Title, steps, and expected results are required."); return; }
    setSaving(true); setError("");
    try {
      const editComment = editingReviewComment.trim();
      const next = await reviewTestCaseGeneration(generationId, { reviewer: reviewer.trim() || "Product Owner", comments: "Test case edited", items: testCases.map((item) => ({
        test_case_id: item.test_case_id,
        review_status: statuses[item.test_case_id] || item.review_status || item.approval?.status || "pending",
        comments: item.test_case_id === id ? (editComment || null) : (reviewCommentsByCase[item.test_case_id] || item.approval?.comments || null),
        title: item.test_case_id === id ? title : item.title,
        steps: item.test_case_id === id ? steps : item.steps,
        expected_results: item.test_case_id === id ? expectedResults : item.expected_results,
      })) });
      applyGeneration(next);
      setReviewCommentsByCase((current) => ({ ...current, [id]: editComment }));
      setEditingId("");
      await refreshProjectCases();
      setSuccess("Test case edit saved in review history.");
    } catch (err) { setError(err.message); }
    finally { setSaving(false); }
  }

  async function saveReview() {
    if (!generationId) return;
    setSaving(true);
    setError("");
    setSuccess("");
    try {
      const next = await reviewTestCaseGeneration(generationId, {
        reviewer: reviewer.trim(),
        comments: comments.trim() || null,
        items: testCases.map((item) => ({
          test_case_id: item.test_case_id,
          review_status: statuses[item.test_case_id] || item.review_status || item.approval?.status || "pending",
          comments: reviewCommentsByCase[item.test_case_id] || null,
          title: item.title,
          steps: item.steps,
          expected_results: item.expected_results,
        })),
      });
      applyGeneration(next);
      await refreshProjectCases();
      if (onReviewSaved) onReviewSaved();
      else setSuccess("Test-case review saved. Approved cases are ready for test data generation.");
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  const caseItems = useMemo(() => testCases.map((item) => ({ ...item, reviewStatus: statuses[item.test_case_id] || item.review_status || item.approval?.status || "pending" })), [testCases, statuses]);
  const pendingCaseItems = caseItems.filter((item) => !["approved", "rejected"].includes(item.reviewStatus));
  const approvedCaseItems = caseItems.filter((item) => item.reviewStatus === "approved");
  const rejectedCaseItems = caseItems.filter((item) => item.reviewStatus === "rejected");
  useEffect(() => { setCasePage(1); }, [generationId]);

  function pageHeader() {
    return <section className="test-design-header"><div className="test-design-header-copy"><div className="test-design-header-meta"><span><Sparkles size={12} /> TestIQ Test Design</span><div className="eyebrow">QUALITY ASSURANCE WORKSPACE</div></div><h1>Test Design Intelligence</h1><div className="test-design-cues"><span>AI-assisted generation</span><span>Human approval</span><span>Traceable coverage</span></div></div><div className="test-design-header-actions">{analysis?.project_name && <span className="test-design-project">{analysis.project_name}</span>}<button type="button" className="test-design-back" onClick={onBack}><ArrowLeft size={15} /> Manage Projects &amp; Upload BRD</button></div></section>;
  }

  function sectionPagination(page, total, pageSize, setPage, label) {
    const pageCount = Math.max(1, Math.ceil(total / pageSize));
    if (pageCount <= 1) return null;
    const currentPage = Math.min(page, pageCount);
    const firstPage = Math.max(1, Math.min(currentPage - 2, pageCount - 4));
    const visiblePages = Array.from({ length: Math.min(5, pageCount) }, (_, index) => firstPage + index);
    return <nav className="app-pagination history-card-pagination" aria-label={`${label} pages`}><span>Showing {(currentPage - 1) * pageSize + 1}–{Math.min(currentPage * pageSize, total)} of {total}</span><div><button type="button" aria-label={`Previous ${label} page`} disabled={currentPage === 1} onClick={() => setPage(currentPage - 1)}><ChevronLeft size={15} /></button>{visiblePages.map((pageNumber) => <button type="button" className={currentPage === pageNumber ? "active" : ""} aria-current={currentPage === pageNumber ? "page" : undefined} key={pageNumber} onClick={() => setPage(pageNumber)}>{pageNumber}</button>)}<button type="button" aria-label={`Next ${label} page`} disabled={currentPage === pageCount} onClick={() => setPage(currentPage + 1)}><ChevronRight size={15} /></button></div></nav>;
  }

  function functionalRequirementHistorySection() {
    if (!projectId) return null;
    const history = functionalHistory?.analyses || [];
    const pageSize = 4;
    const requirementCount = history.reduce((total, item) => total + (item.requirements?.length || 0), 0);
    const currentPage = Math.min(functionalHistoryPage, Math.max(1, Math.ceil(requirementCount / pageSize)));
    const pageStart = (currentPage - 1) * pageSize;
    const pageEnd = pageStart + pageSize;
    let requirementOffset = 0;
    const visibleHistory = history.map((item) => {
      const itemStart = requirementOffset;
      const itemRequirements = item.requirements || [];
      requirementOffset += itemRequirements.length;
      const visibleRequirements = itemRequirements.filter((_, index) => {
        const absoluteIndex = itemStart + index;
        return absoluteIndex >= pageStart && absoluteIndex < pageEnd;
      });
      return { ...item, requirements: visibleRequirements, total_requirement_count: itemRequirements.length };
    }).filter((item) => item.requirements.length > 0);
    return <details className="requirements-panel project-case-history-panel functional-requirement-history-panel history-collapse-panel">
      <summary className="history-collapse-summary"><div><h2>Functional Requirement history</h2><p className="panel-helper">Versioned requirements and linked test coverage.</p></div><span className="history-collapse-count">{requirementCount} FRs · {history.length} analyses</span></summary>
      <div className="history-collapse-content">
      {functionalHistoryLoading && !functionalHistory ? <p className="project-case-empty">Loading Functional Requirement history…</p> : null}
      {functionalHistoryError ? <p className="error-message">{functionalHistoryError}</p> : null}
      {!functionalHistoryLoading && !history.length ? <p className="project-case-empty">No previous Functional Requirement analyses are saved for this project.</p> : null}
      {history.length > 0 && <div className="project-case-history-list">{visibleHistory.map((item) => {
        const brdNames = (item.brds || []).map((brd) => `${brd.filename || brd.logical_name || "BRD"}${brd.owner_version ? ` · ${brd.owner_version}` : ` · revision ${brd.revision}`}`);
        return <details className="project-case-history-item" key={item.analysis_id} open={item.analysis_id === currentAnalysis?.analysis_id}>
          <summary><span><strong>Package v{item.package_version} · Analysis v{item.analysis_version}</strong><small>{brdNames.join("; ") || item.package_name || "BRD source details unavailable"} · {item.total_requirement_count} FRs · {item.created_at}</small></span><span className={`test-case-review-badge ${item.status === "generated" ? "approved" : "pending"}`}>{item.status}</span></summary>
          <div className="project-case-history-details functional-requirement-history-list">{(item.requirements || []).map((requirement, index) => {
            const lineage = requirement.history_lineage || {};
            const matches = lineage.matches || [];
            const oldCases = requirement.test_case_history || [];
            return <details className="functional-requirement-history-item" key={`${item.analysis_id}:${requirement.requirement_id || index}`}>
              <summary><span><strong>{requirement.requirement_id || `FR ${index + 1}`} · {requirement.title || "Untitled requirement"}</strong><small>{lineage.classification ? `History: ${lineage.classification}` : "Earlier analysis · no comparison metadata"} · {oldCases.length} linked test case records</small></span></summary>
              <div className="functional-requirement-history-content">
                {requirement.statement && <p>{displayText(requirement.statement)}</p>}
                <section><b>BRD evidence</b><ul>{(requirement.source_references || []).map((reference, refIndex) => <li key={`${reference.fragment_id || "ref"}-${refIndex}`}>{displayText(reference.source_reference || reference.heading || reference.fragment_id || "Source reference")}</li>)}</ul></section>
                <section><b>Previous FR matches</b>{matches.length ? <ul>{matches.map((match, matchIndex) => <li key={`${match.previous_analysis_id}:${match.previous_requirement_id}:${matchIndex}`}>
                  {match.previous_requirement_id} · package v{match.previous_package_version} · {match.relationship} ({Math.round(match.similarity * 100)}%)
                  {match.previous_test_cases?.length ? <ul>{match.previous_test_cases.map((testCase, caseIndex) => <li key={`${testCase.generation_id}:${testCase.test_case_id}:${caseIndex}`}>{testCase.test_case_id}: {testCase.title || "Untitled test case"} · {testCase.review_status}</li>)}</ul> : <span> · no prior test cases</span>}
                </li>)}</ul> : <p>No earlier matching FR was recorded.</p>}</section>
                <section><b>Test cases generated for this FR</b>{oldCases.length ? <ul>{oldCases.map((testCase, caseIndex) => <li key={`${testCase.generation_id}:${testCase.test_case_id}:${caseIndex}`}>{testCase.test_case_id}: {testCase.title || "Untitled test case"} · {testCase.review_status}</li>)}</ul> : <p>No test cases generated for this FR yet.</p>}</section>
              </div>
            </details>;
          })}</div>
        </details>;
      })}</div>}
      {sectionPagination(currentPage, requirementCount, pageSize, setFunctionalHistoryPage, "Functional Requirement history")}
      </div>
    </details>;
  }

  function projectHistorySection() {
    if (!projectId) return null;
    const versions = projectCaseData?.versions || [];
    const cases = projectCaseMode === "cumulative" ? (projectCaseData?.cases || []) : (versionCaseData?.cases || []);
    const pageSize = 6;
    const currentPage = Math.min(projectHistoryPage, Math.max(1, Math.ceil(cases.length / pageSize)));
    const visibleCases = cases.slice((currentPage - 1) * pageSize, currentPage * pageSize);
    return <details className="requirements-panel project-case-history-panel history-collapse-panel">
      <summary className="history-collapse-summary"><div><h2>Project test cases</h2><p className="panel-helper">Review cumulative coverage or a specific BRD version.</p></div><span className="history-collapse-count">{cases.length} unique</span></summary>
      <div className="history-collapse-content">
      <div className="project-case-history-controls"><button type="button" className={`secondary-button compact-button ${projectCaseMode === "cumulative" ? "active" : ""}`} aria-pressed={projectCaseMode === "cumulative"} onClick={() => { setProjectCaseMode("cumulative"); setProjectHistoryPage(1); }}>Cumulative test cases</button><button type="button" className={`secondary-button compact-button ${projectCaseMode === "version" ? "active" : ""}`} aria-pressed={projectCaseMode === "version"} onClick={() => { setProjectCaseMode("version"); setProjectHistoryPage(1); }} disabled={!versions.length}>BRD version</button>{projectCaseMode === "version" && versions.length > 0 && <label className="project-case-version-select">Version<AppSelect aria-label="Select BRD version" value={selectedPackageVersion} onChange={(event) => { setSelectedPackageVersion(event.target.value); setProjectHistoryPage(1); }}>{versions.map((version) => <option key={version.package_version} value={version.package_version}>Version {version.package_version} · {version.test_case_count} cases</option>)}</AppSelect></label>}</div>
      {projectCaseMode === "version" && !versions.length ? <p className="project-case-empty">No BRD versions are available yet.</p> : projectCaseMode === "version" && !versionCaseData ? <p className="project-case-empty">Loading version test cases…</p> : cases.length ? <div className="project-case-history-list">{visibleCases.map((item, index) => {
        const testCase = item.case || {};
        const sourceVersions = item.package_versions?.length ? item.package_versions.map((version) => `v${version}`).join(", ") : item.package_version ? `v${item.package_version}` : "External import";
        return <details className="project-case-history-item" key={`${item.generation_ids?.[0] || item.generation_id || item.source_name || "external"}:${testCase.test_case_id || testCase.title || index}`}><summary><span><strong>{testCase.title || testCase.test_case_id || "Imported test case"}</strong><small>{testCase.requirement_id || displayText(testCase.requirement_text) || "External"} · {item.origin === "external" ? (item.sources?.join(", ") || item.source_name || "External import") : `BRD / Resource Package ${sourceVersions}`}</small></span><span className={`test-case-review-badge ${testCase.review_status || "pending"}`}>{testCase.review_status || "Imported"}</span></summary><div className="project-case-history-details"><section><b>Steps</b><ol>{(testCase.steps || []).map((step, stepIndex) => <li key={stepIndex}>{displayText(step)}</li>)}</ol></section><section><b>Expected results</b><ul>{(testCase.expected_results || []).map((result, resultIndex) => <li key={resultIndex}>{displayText(result)}</li>)}</ul></section></div></details>;
      })}</div> : <p className="project-case-empty">No test cases are saved for this selection yet.</p>}
      {sectionPagination(currentPage, cases.length, pageSize, setProjectHistoryPage, "Project test cases")}
      </div>
    </details>;
  }

  function externalImportSection() {
    if (!projectId) return null;
    return <section className="requirements-panel external-test-case-import-panel"><div className="external-import-copy"><span className="external-import-icon"><Plus size={17} /></span><div><div className="eyebrow">EXTEND COVERAGE</div><h2>Import external test cases</h2><p className="panel-helper">Add approved JSON test designs to this project.</p></div></div><label className="test-case-import-label"><strong>Choose JSON file</strong><span>Required: requirement_text, title, steps, expected_results</span><input type="file" accept="application/json,.json" onChange={uploadExternalCases} /></label></section>;
  }

  function listBlock(title, items, empty = "Not specified") {
    return <div className="case-detail-block"><h4>{title}</h4>{Array.isArray(items) && items.length ? <ul>{items.map((item, index) => <li key={`${title}-${index}`}>{displayText(typeof item === "string" ? item : item.text || item.action || item.name || JSON.stringify(item))}</li>)}</ul> : <p>{empty}</p>}</div>;
  }

  function testCaseCard(item) {
    return <article className={`test-case-review-item rich-test-case ${item.reviewStatus}`} key={item.test_case_id}>
      <input type="checkbox" checked={selected.includes(item.test_case_id)} onChange={() => toggleSelected(item.test_case_id)} />
      <div className="test-case-review-content">
        <div className="test-case-review-heading">
          <div><strong>{item.test_case_id}</strong><span className="case-category">{shortCategory(item.category)}</span></div>
          <div><span className={`test-case-status ${item.execution_status || "not_run"}`}>{String(item.execution_status || "not_run").replaceAll("_", " ")}</span><span className={`test-case-status ${item.reviewStatus}`}>Review: {item.reviewStatus}</span></div>
        </div>
        <h3>{caseDisplayTitle(item, testCases)}</h3>
        <p className="case-description-label">Description</p>
        <p className="case-scenario">{caseDescription(item)}</p>
        <div className="test-case-chip-row">
          <span>{item.requirement_id}</span>
          <span>{shortCategory(item.type)}</span>
          <span>{item.priority || "medium"}</span>
        </div>
        <div className="case-detail-grid">
          {listBlock("Test data", item.data_needs, "No test data fields specified")}
          <div className="case-detail-side">
            {listBlock("Steps", item.steps)}
            {listBlock("Expected results", item.expected_results)}
          </div>
        </div>
        <div className="test-case-item-actions"><button type="button" className="item-icon-button item-edit" title="Edit test case" aria-label={`Edit ${item.test_case_id}`} onClick={() => startEdit(item)}><Pencil size={14} /></button><button type="button" className="item-icon-button item-add" title="Add a related test case" aria-label={`Add test case after ${item.test_case_id}`} onClick={() => startAdd(item)}><Plus size={14} /></button></div>
      </div>
    </article>;
  }

  function testCaseTableRow(item, selectable = false) {
    const dataNeeds = (item.data_needs || []).map((need) => {
      if (typeof need === "string") return need;
      const name = displayText(need.name || "");
      const generatedValues = generatedDataByCase[item.test_case_id] || item.generated_data || item.test_data_values || {};
      const generatedValue = generatedValues[name] || generatedValues[name.toLowerCase()];
      return generatedValue ? `${name}: ${displayText(generatedValue)}` : name;
    }).filter(Boolean).filter((value, index, values) => values.findIndex((candidate) => candidate.split(":")[0].trim().toLowerCase() === value.split(":")[0].trim().toLowerCase()) === index);
    const valueByName = new Map(dataNeeds.map((value) => [value.split(":")[0].trim().toLowerCase(), value]));
    const data = (item.actions || []).flatMap((action) => (action.data_inputs || []).map((field) => valueByName.get(String(field).toLowerCase()) || field)).filter(Boolean);
    if (!data.length) data.push(...dataNeeds);
    const uniqueData = data.filter((value, index, values) => values.findIndex((candidate) => candidate.split(":")[0].trim().toLowerCase() === value.split(":")[0].trim().toLowerCase()) === index);
    const expected = (item.expected_results || []).map((value) => displayText(typeof value === "string" ? value : value.text || JSON.stringify(value))).filter(Boolean);
    const reviewComment = reviewCommentsByCase[item.test_case_id] || item.approval?.comments || "";
    return <tr key={item.test_case_id} className="test-case-grid-row">
      <td className="test-case-grid-select">{selectable && <input type="checkbox" checked={selected.includes(item.test_case_id)} onChange={() => toggleSelected(item.test_case_id)} />}</td>
      <td className="test-case-grid-id"><strong>{item.test_case_id}</strong></td>
      <td><strong>{caseDisplayTitle(item, testCases)}</strong><small>{item.requirement_id}</small></td>
      <td>{(item.steps || []).map((step, index) => <div key={`${item.test_case_id}-step-${index}`} className="test-case-grid-line">Step {index + 1}: {displayText(step).replace(/^(?:step\s*\d+\s*:\s*)+/i, "")}</div>)}</td>
      <td>{uniqueData.length ? uniqueData.map((value, index) => <div key={`${item.test_case_id}-data-${index}`} className="test-case-grid-line">{value}</div>) : <span className="traceability-gap">Not specified</span>}</td>
      <td>{expected.map((value, index) => <div key={`${item.test_case_id}-expected-${index}`} className="test-case-grid-line">{value}</div>)}</td>
      <td className="test-case-review-status"><span className={`test-case-review-badge ${item.reviewStatus}`}>{item.reviewStatus === "approved" ? "Approved" : item.reviewStatus === "rejected" ? "Rejected" : "Pending review"}</span></td>
      {item.reviewStatus === "rejected" && <td className="test-case-review-comment">{reviewComment || "No comment"}</td>}
    </tr>;
  }

  function reviewTable(title, items, selectable = false) {
    if (!items.length) return <div className="review-empty-state"><ClipboardList size={22} /><strong>{title === "Pending review" ? "No pending test cases" : title === "Approved test cases" ? "No approved test cases" : "No rejected test cases"}</strong><span>Cases moved into this status will appear here.</span></div>;
    const casesPerPage = 6;
    const pageCount = Math.max(1, Math.ceil(items.length / casesPerPage));
    const currentPage = Math.min(casePage, pageCount);
    const visibleItems = items.slice((currentPage - 1) * casesPerPage, currentPage * casesPerPage);
    const allSelected = selectable && items.every((item) => selected.includes(item.test_case_id));
    const actions = title === "Pending review"
      ? [{ status: "approved", label: "Approve", icon: <Check size={14} />, className: "approved-action" }, { status: "rejected", label: "Reject", icon: <X size={14} />, className: "rejected-action" }]
      : title === "Approved test cases"
        ? [{ status: "pending", label: "Move to pending", icon: <RotateCcw size={14} />, className: "pending-action" }, { status: "rejected", label: "Reject", icon: <X size={14} />, className: "rejected-action" }]
        : [{ status: "pending", label: "Move to pending", icon: <RotateCcw size={14} />, className: "pending-action" }, { status: "approved", label: "Approve", icon: <Check size={14} />, className: "approved-action" }];
    const selectedInTable = items.map((item) => item.test_case_id).filter((id) => selected.includes(id));
    const selectedCase = selectedInTable.length === 1 ? items.find((item) => item.test_case_id === selectedInTable[0]) : null;
    const showApprovedCaseActions = title === "Approved test cases";
    const showComments = title === "Rejected test cases";
    return <section className={`requirements-panel generated-cases-panel review-status-panel review-status-${title.toLowerCase().replace(/\s+/g, "-")}`}><div className="panel-title-row"><div><h2>{title}</h2><p className="panel-helper">{items.length} test case{items.length === 1 ? "" : "s"}</p></div><div className="review-panel-heading-actions"><span>{items.length}</span></div></div>{selectable && <div className="selection-actions"><label className="select-all"><input type="checkbox" checked={allSelected} onChange={(event) => setSelected(event.target.checked ? items.map((item) => item.test_case_id) : [])} /> Select All</label><div className="review-move-actions">{showApprovedCaseActions && <><button type="button" className="review-icon-action case-toolbar-edit" disabled={!selectedCase || saving} title="Select one approved test case to edit" onClick={() => startEdit(selectedCase)}><Pencil size={14} /> Edit</button><button type="button" className="review-icon-action case-toolbar-add" disabled={saving} title="Add a new test case" onClick={() => startAdd(selectedCase || items[0])}><Plus size={14} /> Add New</button></>}{actions.map((action) => <button key={action.status} type="button" className={`review-icon-action ${action.className}`} disabled={!selectedInTable.length || saving} title={`${action.label} selected test cases`} onClick={() => handleStatusAction(action.status, selectedInTable)}>{action.icon} {action.label}</button>)}</div></div>}<div className="test-case-execution-table-wrap"><table className="test-case-execution-table"><thead><tr><th className="test-case-grid-select-heading"></th><th>TC#</th><th>Test Title</th><th>Test Steps</th><th>Test Data</th><th>Expected</th><th>Status</th>{showComments && <th>Review Comment</th>}</tr></thead><tbody>{visibleItems.map((item) => testCaseTableRow(item, selectable))}</tbody></table></div>{pageCount > 1 && <nav className="test-case-pagination" aria-label={`${title} pages`}><span>Showing {(currentPage - 1) * casesPerPage + 1}–{Math.min(currentPage * casesPerPage, items.length)} of {items.length}</span><div><button type="button" aria-label="Previous page" disabled={currentPage === 1} onClick={() => setCasePage(currentPage - 1)}><ChevronLeft size={16} /></button>{Array.from({ length: pageCount }, (_, index) => index + 1).map((pageNumber) => <button type="button" className={currentPage === pageNumber ? "active" : ""} aria-current={currentPage === pageNumber ? "page" : undefined} key={pageNumber} onClick={() => setCasePage(pageNumber)}>{pageNumber}</button>)}<button type="button" aria-label="Next page" disabled={currentPage === pageCount} onClick={() => setCasePage(currentPage + 1)}><ChevronRight size={16} /></button></div></nav>}</section>;
  }

  if (isAnalysisLoading && !currentAnalysis) {
    return <div className="requirements-page test-generation-page">{pageHeader()}<section className="generation-empty generation-context-loading" role="status"><LoaderCircle className="spin" size={22} /><div><h3>Preparing test case generation</h3><p>Loading the selected project’s requirements and generation history.</p></div></section></div>;
  }
  if (!currentAnalysis && analysis?.generation_pipeline && analysis?.package_id) {
    const progress = workflowProgress || { percent: 0, label: "Ready to start" };
    return <div className="requirements-page test-generation-page">
      {pageHeader()}
      {error && <div className="error-message"><AlertCircle size={17} /> {error}</div>}
      <section className="generation-next-card project-test-generation-card"><div><div className="eyebrow">GENERATION PIPELINE</div><h2>{activeGenerationActivity ? "Generation in progress" : "Generate test cases"}</h2><p>{activeGenerationActivity ? "You can open another section. Generation will continue in the background." : "Prepare requirements and create review-ready test cases."}</p></div><div className="generation-controls project-generation-controls">{(workflowProgress || activeGenerationActivity) && <div className="test-case-inline-progress" aria-label="Test Case generation progress"><div className={`project-progress-track ${progress.percent === 100 ? "completed" : ""} ${(loading || activeGenerationActivity) ? "running" : ""}`} role="progressbar" aria-label="Test Case generation" aria-valuemin="0" aria-valuemax="100" aria-valuenow={progress.percent}><span style={{ width: `${progress.percent}%` }} /></div><div className={`project-progress-meta ${progress.percent === 100 ? "completed" : ""}`}><strong>{progress.percent}%</strong><span>{progress.label}</span></div></div>}{!loading && !activeGenerationActivity && <button type="button" className="primary-button" onClick={generateProjectTestCases}><ShieldCheck size={16} /> Generate Test Cases</button>}</div></section>
      {functionalRequirementHistorySection()}
      {projectHistorySection()}
      {externalImportSection()}
    </div>;
  }
  if (!currentAnalysis) {
    return <div className="requirements-page test-generation-page"><div className="empty-state-panel"><div className="empty-state-icon"><ClipboardList size={25} /></div><h2>{analysisError === "not-found" ? "No saved requirement analysis" : analysisError ? "Could not verify requirement analysis" : "No requirement analysis selected"}</h2><p>{analysisError === "not-found" ? "This saved analysis is not in the database. Return to Manage Projects &amp; BRDs to select or prepare the correct project package." : analysisError ? "The saved analysis could not be loaded. Check the database connection, then return to Manage Projects &amp; BRDs and try again." : "Open Manage Projects &amp; BRDs, prepare the project scope, then continue to test-case generation."}</p><button type="button" className="primary-button" onClick={onBack}>Back to Manage Projects &amp; BRDs</button></div></div>;
  }

  return <div className="requirements-page test-generation-page">
    {pageHeader()}
    {error && <div className="error-message"><AlertCircle size={17} /> {error}</div>}
    {success && <div className="toast-message"><CheckCircle2 size={17} /> {success}</div>}
    <section className="generation-next-card"><div><div className="eyebrow">GENERATION</div><h2>{generation ? "Test cases ready for review" : activeGenerationActivity ? "Generation in progress" : "Generate test cases"}</h2><p>{generation ? `${testCases.length} cases generated from the approved project context.` : activeGenerationActivity ? "Generation continues in the background. You can safely open another page." : "Create positive and negative cases from approved requirements."}</p></div><div className="generation-controls project-generation-controls">{analysis?.generation_pipeline && loading && workflowProgress && <div className="test-case-inline-progress" aria-label="Test Case generation progress"><div className={`project-progress-track ${workflowProgress.percent === 100 ? "completed" : ""} running`} role="progressbar" aria-label="Test Case generation" aria-valuemin="0" aria-valuemax="100" aria-valuenow={workflowProgress.percent}><span style={{ width: `${workflowProgress.percent}%` }} /></div><div className={`project-progress-meta ${workflowProgress.percent === 100 ? "completed" : ""}`}><strong>{workflowProgress.percent}%</strong><span>{workflowProgress.label}</span></div></div>}{generation ? <span className="test-case-generation-ready"><CheckCircle2 size={16} /> Generated · {testCases.length} cases</span> : !(analysis?.generation_pipeline && loading) && !activeGenerationActivity && <button type="button" className="primary-button" disabled={!generationRequirements.length || loading} onClick={generate}><ShieldCheck size={16} /> Generate test cases</button>}</div></section>
    {generation && <section className="review-tabs-shell"><div className="review-tabs" role="tablist" aria-label="Test case review status"><button type="button" className={reviewTab === "pending" ? "active" : ""} role="tab" aria-selected={reviewTab === "pending"} onClick={() => { setReviewTab("pending"); setCasePage(1); }}>Pending Review <span>{pendingCaseItems.length}</span></button><button type="button" className={reviewTab === "approved" ? "active" : ""} role="tab" aria-selected={reviewTab === "approved"} onClick={() => { setReviewTab("approved"); setCasePage(1); }}>Approved <span>{approvedCaseItems.length}</span></button><button type="button" className={reviewTab === "rejected" ? "active" : ""} role="tab" aria-selected={reviewTab === "rejected"} onClick={() => { setReviewTab("rejected"); setCasePage(1); }}>Rejected <span>{rejectedCaseItems.length}</span></button></div><div className="review-tab-content">{reviewTab === "pending" && reviewTable("Pending review", pendingCaseItems, true)}{reviewTab === "approved" && reviewTable("Approved test cases", approvedCaseItems, true)}{reviewTab === "rejected" && reviewTable("Rejected test cases", rejectedCaseItems, true)}</div></section>}
    {(editingId || addingAfterId) && <div className="modal-backdrop case-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !saving) closeCaseModal(); }}><div className={`confirm-modal case-edit-modal ${addingAfterId ? "case-add-modal" : ""}`} role="dialog" aria-modal="true" aria-labelledby="case-edit-heading"><header className="case-modal-header"><span className="case-modal-icon">{addingAfterId ? <Plus size={20} /> : <Pencil size={19} />}</span><div><div className="eyebrow">{addingAfterId ? `NEW TEST CASE · RELATED TO ${addingAfterId}` : `EDIT TEST CASE · ${editingId}`}</div><h2 id="case-edit-heading">{addingAfterId ? "Add test case" : "Update test case details"}</h2><p className="case-edit-context">{addingAfterId ? "Create additional coverage using the selected case’s requirement and traceability context." : "Refine the test design while preserving its requirement and execution history."}</p></div><button type="button" className="modal-close" disabled={saving} aria-label={addingAfterId ? "Close add test case" : "Close edit test case"} onClick={closeCaseModal}><X size={18} /></button></header><div className="case-modal-body">{error && <div className="case-modal-error" role="alert"><AlertCircle size={15} /> {error}</div>}<label className="case-edit-field"><span>Test title</span><input className="test-case-grid-input" value={editingTitle} onChange={(event) => setEditingTitle(event.target.value)} placeholder="Describe the behavior being validated" autoFocus /></label><div className="case-modal-field-grid"><label className="case-edit-field"><span>Test steps</span><small>Enter one action per line.</small><textarea className="test-case-grid-input test-case-grid-textarea" value={editingSteps} onChange={(event) => setEditingSteps(event.target.value)} placeholder={'Open the workflow\nSubmit valid test data\nVerify the processed result'} /></label><label className="case-edit-field"><span>Expected results</span><small>Enter one outcome per line.</small><textarea className="test-case-grid-input test-case-grid-textarea" value={editingExpected} onChange={(event) => setEditingExpected(event.target.value)} placeholder={'Request is accepted\nExpected record is created\nStatus is updated correctly'} /></label></div><label className="case-edit-field case-edit-review-notes"><span>{addingAfterId ? "Creation notes" : "Review notes"}</span><small className="case-edit-help">{addingAfterId ? "Optional context for reviewers." : (statuses[editingId] || testCases.find((item) => item.test_case_id === editingId)?.review_status || testCases.find((item) => item.test_case_id === editingId)?.approval?.status || "pending") === "rejected" ? "Required for rejected test cases." : "Optional reviewer context or revision guidance."}</small><textarea className="test-case-grid-input test-case-grid-textarea" value={editingReviewComment} onChange={(event) => setEditingReviewComment(event.target.value)} placeholder={addingAfterId ? "Why is this coverage needed?" : "Add a review comment"} /></label></div><footer className="confirm-actions"><span className="case-modal-required">Title, steps, and expected results are required.</span><button type="button" className="secondary-button" disabled={saving} onClick={closeCaseModal}>Cancel</button><button type="button" className="primary-button" disabled={saving} onClick={addingAfterId ? addTestCase : () => saveEdit(editingId)}>{saving ? <><LoaderCircle className="spin" size={16} /> {addingAfterId ? "Adding..." : "Saving..."}</> : addingAfterId ? <><Plus size={16} /> Add</> : <><Check size={16} /> Save changes</>}</button></footer></div></div>}
    {generation && <section className="requirements-review-card"><div className="test-review-heading"><div><div className="eyebrow">HUMAN REVIEW</div><h2>{allApproved ? "Review complete" : "Finalize review"}</h2></div></div><div className="review-fields"><label>Reviewer<input value={reviewer} onChange={(event) => setReviewer(event.target.value)} /></label><label>Review comments<textarea value={comments} onChange={(event) => setComments(event.target.value)} placeholder="Optional review notes" /></label><button className="primary-button" onClick={saveReview} disabled={saving || !testCases.length}>{saving ? <><LoaderCircle className="spin" size={16} /> Saving...</> : <><ShieldCheck size={16} /> Save review</>}</button></div></section>}
    {functionalRequirementHistorySection()}
    {projectHistorySection()}
    {/* Coverage and grounding is intentionally hidden for now.
    {generation && <details className="requirements-panel test-case-coverage-summary coverage-collapse-panel" aria-label="Test case coverage and evidence"><summary className="coverage-collapse-summary"><h2>Coverage and grounding</h2><span>{coverageSummary.requirements_covered || 0}/{coverageSummary.requirements_total || 0} requirements covered</span></summary><div className="coverage-collapse-content"><div className="test-case-coverage-metrics"><span>FRs: {coverageSummary.requirements_covered || 0}/{coverageSummary.requirements_total || 0}</span><span>Acceptance criteria: {coverageSummary.acceptance_criteria_covered || 0}/{coverageSummary.acceptance_criteria_total || 0}</span><span>Business rules: {coverageSummary.business_rules_covered || 0}/{coverageSummary.business_rules_total || 0}</span><span>{knowledgeUsed ? "Knowledge Base evidence used" : "Knowledge Base evidence unavailable"}</span></div><p>{testCases.length} cases generated for this version. The cumulative history combines exact or equivalent cases across versions.</p>{(coverageGaps.length > 0 || (contract.global_gaps || []).length > 0) && <details className="coverage-gap-details"><summary>View coverage gaps ({coverageGaps.length + (contract.global_gaps || []).length})</summary>{coverageGaps.length > 0 && <ul className="coverage-gap-list">{coverageGaps.map((gap, index) => <li key={`${gap.kind}-${gap.id}-${index}`}><strong>{gap.kind}{gap.id ? ` ${gap.id}` : ""}{gap.requirementId ? ` · ${gap.requirementId}` : ""}:</strong> {gap.label || "No description available"}</li>)}</ul>}{(contract.global_gaps || []).length > 0 && <ul className="coverage-gap-list">{contract.global_gaps.map((gap, index) => <li key={`global-${index}`}>{gap}</li>)}</ul>}</details>}</div></details>}
    */}
    {externalImportSection()}
  </div>;
}
