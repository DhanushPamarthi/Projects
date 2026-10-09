import { completeActivity, failActivity, startActivity } from "../activity/activityStore.js";

const defaultBaseUrl = import.meta.env.DEV ? "/api" : "http://127.0.0.1:8001";
const responseCache = new Map();
const inFlightGets = new Map();
const DEFAULT_GET_CACHE_MS = 15_000;
let cacheGeneration = 0;

export function invalidateApiCache() {
  cacheGeneration += 1;
  responseCache.clear();
}

function requestActivity(path, options) {
  if ((options.method || "GET").toUpperCase() === "GET" || options.activity === false) return null;
  const definitions = [
    [/\/projects\/[^/]+\/sources$/, "Uploading source document", "sources"],
    [/\/projects\/[^/]+\/manifests$/, "Creating Resource Package", "sources"],
    [/\/packages\/[^/]+\/knowledge-index$/, "Building Knowledge Base", "knowledge-base"],
    [/\/knowledge-indexes\/[^/]+\/retrieve$/, "Retrieving Knowledge Base evidence", "knowledge-base"],
    [/\/business-requirements\/generate$/, "Generating Business Use Cases", "business-requirements"],
    [/\/(requirements\/analyze|functional-requirements\/generate)$/, "Generating Functional Requirements", "requirements"],
    [/\/requirement-analyses\/[^/]+\/test-cases$/, "Generating test cases", "test-cases"],
    [/\/test-case-generations\/[^/]+\/regenerate$/, "Regenerating test cases", "test-cases"],
    [/\/test-case-generations\/[^/]+\/test-data$/, "Generating test data", "test-data"],
    [/\/execution-runs\/[^/]+\/execute$/, "Executing test run", "test-results"],
  ];
  const match = definitions.find(([pattern]) => pattern.test(path));
  const supplied = options.activity && typeof options.activity === "object" ? options.activity : {};
  if (!match && !supplied.label) return null;
  return { label: supplied.label || match[1], section: supplied.section || match[2], detail: supplied.detail || "The operation continues if you open another section.", kind: supplied.kind || "api" };
}

export function getBaseUrl() {
  const storedUrl = localStorage.getItem("testiq_base_url");
  if (storedUrl === "http://127.0.0.1:8000" || storedUrl === "http://localhost:8000") {
    localStorage.setItem("testiq_base_url", defaultBaseUrl);
    return defaultBaseUrl;
  }
  if (import.meta.env.DEV && (!storedUrl || storedUrl === "/api" || storedUrl === "http://127.0.0.1:8001" || storedUrl === "http://localhost:8001")) {
    return "/api";
  }
  return storedUrl || defaultBaseUrl;
}

export function setBaseUrl(url) {
  localStorage.setItem("testiq_base_url", url);
}

async function request(path, options = {}) {
  const url = `${getBaseUrl()}${path}`;
  const token = localStorage.getItem("testiq_auth_token");
  const headers = { ...(options.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  const method = (options.method || "GET").toUpperCase();
  const cacheEnabled = method === "GET" && options.cache !== "no-store";
  const cacheKey = `${token || "anonymous"}:${url}`;
  if (cacheEnabled) {
    const cached = responseCache.get(cacheKey);
    if (cached && cached.expiresAt > Date.now()) return cached.data;
    if (cached) responseCache.delete(cacheKey);
    const pending = inFlightGets.get(cacheKey);
    if (pending) return pending;
  }
  const activity = requestActivity(path, options);
  const activityId = activity ? startActivity(activity) : null;
  const { activity: _activity, cacheTtl: _cacheTtl, ...fetchOptions } = options;
  const generation = cacheGeneration;
  const operation = (async () => {
    try {
      const response = await fetch(url, { ...fetchOptions, headers });
      const text = await response.text();
      let data = null;
      try {
        data = text ? JSON.parse(text) : null;
      } catch {
        data = { detail: text || `${response.status} ${response.statusText}` };
      }
      if (!response.ok) {
        const detail = data?.detail;
        const message = typeof detail === "string"
          ? detail
          : detail?.validation_messages?.join("; ") || data?.validation_messages?.join("; ") || detail?.message || `${response.status} ${response.statusText}`;
        const error = new Error(message);
        error.status = response.status;
        throw error;
      }
      if (method !== "GET") invalidateApiCache();
      if (cacheEnabled && generation === cacheGeneration) {
        responseCache.set(cacheKey, { data, expiresAt: Date.now() + Number(options.cacheTtl || DEFAULT_GET_CACHE_MS) });
      }
      if (activityId) completeActivity(activityId, "Completed successfully.");
      return data;
    } catch (error) {
      if (activityId) failActivity(activityId, error);
      throw error;
    }
  })();
  if (cacheEnabled) inFlightGets.set(cacheKey, operation);
  try {
    return await operation;
  } finally {
    if (cacheEnabled && inFlightGets.get(cacheKey) === operation) inFlightGets.delete(cacheKey);
  }
}

export async function loginUser(email, password) {
  const data = await request("/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, password }) });
  localStorage.setItem("testiq_auth_token", data.token);
  return data.user;
}

export async function registerUser(email, password, name = "") {
  const data = await request("/auth/register", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, password, name }) });
  localStorage.setItem("testiq_auth_token", data.token);
  return data.user;
}

export async function logoutUser() {
  try { await request("/auth/logout", { method: "POST" }); } finally { localStorage.removeItem("testiq_auth_token"); }
}

export async function checkHealth() {
  return request("/health");
}

export async function createProject(name) {
  return request("/api/v1/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
}

export async function getProject(projectId) {
  return request(`/api/v1/projects/${encodeURIComponent(projectId)}`);
}

export async function listProjects(options = {}) {
  return request("/api/v1/projects", { cacheTtl: 30_000, ...options });
}

export async function uploadSource(projectId, file, sourceType = "brd", brdSourceId = "", requiredSections = "") {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("source_type", sourceType);
  if (brdSourceId) formData.append("brd_source_id", brdSourceId);
  if (requiredSections) formData.append("required_sections", requiredSections);
  return request(`/api/v1/projects/${encodeURIComponent(projectId)}/sources`, {
    method: "POST",
    body: formData,
    activity: { label: sourceType === "brd" ? "Uploading BRD" : "Uploading supporting source", section: "sources", detail: file.name },
  });
}

export async function listSources(projectId) {
  return request(`/api/v1/projects/${encodeURIComponent(projectId)}/sources`);
}

export async function getRevision(revisionId) {
  return request(`/api/v1/revisions/${encodeURIComponent(revisionId)}`);
}

export async function listSourceRevisions(sourceId) {
  return request(`/api/v1/sources/${encodeURIComponent(sourceId)}/revisions`, { cache: "no-store" });
}

export async function selectActiveRevision(sourceId, revisionId) {
  return request(`/api/v1/sources/${encodeURIComponent(sourceId)}/active-revision`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ revision_id: revisionId }),
  });
}

export async function createManifest(sourceIds, packageName, scope, projectId) {
  return request(`/api/v1/projects/${encodeURIComponent(projectId)}/manifests`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source_ids: sourceIds, package_name: packageName, scope }),
  });
}

export async function listManifests(projectId, options = {}) {
  return request(`/api/v1/projects/${encodeURIComponent(projectId)}/manifests`, { cacheTtl: 30_000, ...options });
}

export async function getProjectDashboard(projectId, options = {}) {
  return request(`/api/v1/projects/${encodeURIComponent(projectId)}/dashboard`, { cache: "no-store", ...options });
}

export async function getProjectWorkspaceSummary(options = {}) {
  return request("/api/v1/projects/workspace-summary", { cache: "no-store", ...options });
}

export async function retryPackageAutomation(packageId) {
  return request(`/api/v1/packages/${encodeURIComponent(packageId)}/automation/retry`, { method: "POST" });
}

export async function stopPackageAutomation(packageId) {
  return request(`/api/v1/packages/${encodeURIComponent(packageId)}/automation/stop`, { method: "POST" });
}

export async function stopProjectPackageCreation(projectId) {
  return request(`/api/v1/projects/${encodeURIComponent(projectId)}/processing/stop`, { method: "POST" });
}

export async function resumeProjectPackageCreation(projectId) {
  return request(`/api/v1/projects/${encodeURIComponent(projectId)}/processing/resume`, { method: "POST" });
}

export async function buildKnowledgeIndex(packageId, options = {}) {
  return request(`/api/v1/packages/${encodeURIComponent(packageId)}/knowledge-index`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(options) });
}
export async function listKnowledgeIndexes(packageId) {
  return request(`/api/v1/packages/${encodeURIComponent(packageId)}/knowledge-indexes`);
}
export async function getKnowledgeSummary(packageId, options = {}) {
  return request(`/api/v1/packages/${encodeURIComponent(packageId)}/knowledge-summary`, options);
}
export async function listProjectKnowledgeVersions(projectId) {
  return request(`/api/v1/projects/${encodeURIComponent(projectId)}/knowledge-versions`);
}
export async function getKnowledgeIndex(indexId) {
  return request(`/api/v1/knowledge-indexes/${encodeURIComponent(indexId)}`);
}
export async function getRagReadiness(packageId) {
  return request(`/api/v1/packages/${encodeURIComponent(packageId)}/rag-readiness`);
}
export async function listPackageBusinessRules(packageId) {
  return request(`/api/v1/packages/${encodeURIComponent(packageId)}/business-rules`);
}
export async function reviewPackageBusinessRules(packageId, payload) {
  return request(`/api/v1/packages/${encodeURIComponent(packageId)}/business-rules/review`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
}
export async function retrieveKnowledge(indexId, payload) {
  return request(`/api/v1/knowledge-indexes/${encodeURIComponent(indexId)}/retrieve`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
}
export async function evaluateKnowledgeIndex(indexId, payload = {}) {
  return request(`/api/v1/knowledge-indexes/${encodeURIComponent(indexId)}/evaluate`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
}

export async function generateBusinessRequirements(packageId) {
  return request(`/api/v1/packages/${encodeURIComponent(packageId)}/business-requirements/generate`, {
    method: "POST",
  });
}
export async function analyzeRequirements(packageId) {
  return request(`/api/v1/packages/${encodeURIComponent(packageId)}/requirements/analyze`, {
    method: "POST",
  });
}

export async function generateFunctionalRequirements(packageId) {
  return request(`/api/v1/packages/${encodeURIComponent(packageId)}/functional-requirements/generate`, {
    method: "POST",
  });
}

export async function listRequirementAnalyses(packageId) {
  return request(`/api/v1/packages/${encodeURIComponent(packageId)}/requirements`);
}

export async function getLatestRequirementAnalyses(packageId, options = {}) {
  return request(`/api/v1/packages/${encodeURIComponent(packageId)}/requirements/latest-set`, options);
}

export async function getRequirementAnalysis(analysisId) {
  return request(`/api/v1/requirement-analyses/${encodeURIComponent(analysisId)}`);
}

export async function reviewRequirementAnalysis(analysisId, review) {
  return request(`/api/v1/requirement-analyses/${encodeURIComponent(analysisId)}/review`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(review),
  });
}

export async function generateTestCasesFromAnalysis(analysisId, options = {}) {
  return request(`/api/v1/requirement-analyses/${encodeURIComponent(analysisId)}/test-cases`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(options) });
}

export async function previewTestCaseReuse(analysisId) {
  return request(`/api/v1/requirement-analyses/${encodeURIComponent(analysisId)}/test-case-reuse`, { cache: "no-store" });
}

export async function importExternalTestCases(projectId, payload) {
  return request(`/api/v1/projects/${encodeURIComponent(projectId)}/external-test-cases`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  });
}

export async function listProjectTestCaseLibrary(projectId, packageId) {
  return request(`/api/v1/projects/${encodeURIComponent(projectId)}/test-case-library?package_id=${encodeURIComponent(packageId)}`, { cache: "no-store" });
}

export async function getProjectTestCaseHistorySummary(projectId) {
  return request(`/api/v1/projects/${encodeURIComponent(projectId)}/test-case-history-summary`, { cache: "no-store" });
}

export async function listProjectTestCaseHistory(projectId) {
  return request(`/api/v1/projects/${encodeURIComponent(projectId)}/test-case-history`, { cache: "no-store" });
}

export async function getTestCaseGeneration(generationId) {
  return request(`/api/v1/test-case-generations/${encodeURIComponent(generationId)}`, { cache: "no-store" });
}

export async function listGeneratedTestCases(analysisId) {
  // Review decisions are persisted through a separate POST endpoint. Always
  // reload the latest generation contract instead of reusing a cached,
  // pre-review GET response when navigating to Test Data.
  return request(`/api/v1/requirement-analyses/${encodeURIComponent(analysisId)}/test-cases`, { cache: "no-store" });
}

export async function reviewTestCaseGeneration(generationId, review) {
  return request(`/api/v1/test-case-generations/${encodeURIComponent(generationId)}/review`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(review),
  });
}

export async function getTestCaseCoverageReview(generationId) {
  return request(`/api/v1/test-case-generations/${encodeURIComponent(generationId)}/coverage-review`);
}

export async function regenerateTestCasesFromGeneration(generationId, payload = {}) {
  return request(`/api/v1/test-case-generations/${encodeURIComponent(generationId)}/regenerate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function createExecutionRun(generationId, payload) {
  return request(`/api/v1/test-case-generations/${encodeURIComponent(generationId)}/execution-runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function listExecutionRuns(generationId) {
  return request(`/api/v1/test-case-generations/${encodeURIComponent(generationId)}/execution-runs`);
}

export async function updateExecutionRunLifecycle(runId, payload) {
  return request(`/api/v1/execution-runs/${encodeURIComponent(runId)}/lifecycle`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function executeExecutionRun(runId, payload = {}) {
  return request(`/api/v1/execution-runs/${encodeURIComponent(runId)}/execute`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function generateTestData(generationId, payload) {
  return request(`/api/v1/test-case-generations/${encodeURIComponent(generationId)}/test-data`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
}

export async function listTestData(generationId) {
  return request(`/api/v1/test-case-generations/${encodeURIComponent(generationId)}/test-data`);
}

export async function reviewTestData(generationId, items) {
  return request(`/api/v1/test-case-generations/${encodeURIComponent(generationId)}/test-data/review`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ items }) });
}

export async function saveTestDataToBronze(generationId, testCaseIds, reviewer = "Product Owner") {
  return request(`/api/v1/test-case-generations/${encodeURIComponent(generationId)}/test-data/save`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ test_case_ids: testCaseIds, reviewer }),
    activity: { label: "Sending test data to Bronze", section: "test-data", detail: "The selected test data is being validated and loaded through the database stored procedure." },
  });
}

export async function getTokenUsage({ projectId, packageId, stage } = {}) {
  const params = new URLSearchParams();
  if (projectId) params.set("project_id", projectId);
  if (packageId) params.set("package_id", packageId);
  if (stage) params.set("stage", stage);
  return request(`/api/v1/token-usage${params.size ? `?${params.toString()}` : ""}`, { cache: "no-store" });
}


export async function getRequirement(key) {
  return request(`/requirements/${encodeURIComponent(key)}`);
}

export async function listRequirements() {
  return request("/requirements");
}

export async function getWorkflow(requirementKey) {
  return request(`/workflow/${encodeURIComponent(requirementKey)}`);
}

export async function getWorkflowAudit(requirementKey) {
  return request(`/workflow/${encodeURIComponent(requirementKey)}/audit`);
}

export async function listRegisteredDatasets() {
  return request("/registry/datasets");
}

export async function getCoverage(requirementKey) {
  return request(`/coverage/${encodeURIComponent(requirementKey)}`);
}

export async function getEvidence(requirementKey) {
  return request(`/evidence/${encodeURIComponent(requirementKey)}`);
}

export async function completeWorkflowStage(requirementKey, stage, details = {}) {
  return request(`/workflow/${encodeURIComponent(requirementKey)}/${stage}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(details),
  });
}

export async function executeWorkflow(requirementKey, details = {}) {
  return request(`/workflow/${encodeURIComponent(requirementKey)}/execution`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(details),
  });
}

export async function finalizeWorkflow(requirementKey) {
  return request(`/workflow/${encodeURIComponent(requirementKey)}/evidence-cleanup`, { method: "POST" });
}

export async function runTestDesignAgent(requirementKey) {
  return request(`/agents/test-design/${encodeURIComponent(requirementKey)}`, { method: "POST" });
}

export async function generateTestCases(requirementKey) {
  return request(`/requirements/${encodeURIComponent(requirementKey)}/test-cases`, { method: "POST" });
}

export async function generateSelectedTestCases(scenarioKeys, recordCount, options) {
  return request('/test-cases/generate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ scenario_keys: scenarioKeys, record_count: recordCount, options }),
  })
}

export async function buildRecipe(scenarioKey) {
  return request(`/scenarios/${encodeURIComponent(scenarioKey)}/recipe`, {
    method: "POST",
  });
}

export async function saveRecipe(scenarioKey, recipe) {
  return request(`/recipes/${encodeURIComponent(scenarioKey)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(recipe),
  });
}

export async function generateData(scenarioKey, recordCount = 1, options = {}) {
  const params = new URLSearchParams({ record_count: String(recordCount) });
  if (options.seed !== undefined && options.seed !== null) params.set("seed", String(options.seed));
  if (options.referentialIntegrity !== undefined) params.set("referential_integrity", String(options.referentialIntegrity));
  if (options.businessRules !== undefined) params.set("business_rules", String(options.businessRules));
  if (options.realisticValues !== undefined) params.set("realistic_values", String(options.realisticValues));
  return request(
    `/recipes/${encodeURIComponent(scenarioKey)}/generate?${params.toString()}`,
    { method: "POST" },
  );
}

export async function validateData(scenarioKey) {
  return request(`/recipes/${encodeURIComponent(scenarioKey)}/validate`);
}

export async function exportData(scenarioKey) {
  return request(`/recipes/${encodeURIComponent(scenarioKey)}/export`);
}

export async function listGenerationRuns(scenarioKey) {
  return request(`/recipes/${encodeURIComponent(scenarioKey)}/runs`);
}
