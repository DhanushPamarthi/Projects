const defaultBaseUrl = import.meta.env.DEV ? "/api" : "http://127.0.0.1:8000";

export function getBaseUrl() {
  const storedUrl = localStorage.getItem("testiq_base_url");
  if (import.meta.env.DEV && (!storedUrl || storedUrl === "/api" || storedUrl === "http://127.0.0.1:8000" || storedUrl === "http://localhost:8000")) {
    return "/api";
  }
  if (storedUrl === "http://127.0.0.1:8001") {
    localStorage.setItem("testiq_base_url", defaultBaseUrl);
    return defaultBaseUrl;
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
  const response = await fetch(url, { ...options, headers });
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
    throw new Error(message);
  }
  return data;
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

export async function listProjects() {
  return request("/api/v1/projects");
}

export async function uploadSource(projectId, file, sourceType = "brd", brdSourceId = "", owner = "", ownerVersion = "", requiredSections = "") {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("source_type", sourceType);
  if (brdSourceId) formData.append("brd_source_id", brdSourceId);
  if (owner) formData.append("owner", owner);
  if (ownerVersion) formData.append("owner_version", ownerVersion);
  if (requiredSections) formData.append("required_sections", requiredSections);
  return request(`/api/v1/projects/${encodeURIComponent(projectId)}/sources`, {
    method: "POST",
    body: formData,
  });
}

export async function listSources(projectId) {
  return request(`/api/v1/projects/${encodeURIComponent(projectId)}/sources`);
}

export async function getRevision(revisionId) {
  return request(`/api/v1/revisions/${encodeURIComponent(revisionId)}`);
}

export async function createManifest(sourceIds, packageName, scope, projectId) {
  return request(`/api/v1/projects/${encodeURIComponent(projectId)}/manifests`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source_ids: sourceIds, package_name: packageName, scope }),
  });
}

export async function listManifests(projectId) {
  return request(`/api/v1/projects/${encodeURIComponent(projectId)}/manifests`);
}

export async function analyzeRequirements(packageId) {
  return request(`/api/v1/packages/${encodeURIComponent(packageId)}/requirements/analyze`, {
    method: "POST",
  });
}

export async function listRequirementAnalyses(packageId) {
  return request(`/api/v1/packages/${encodeURIComponent(packageId)}/requirements`);
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

export async function generateTestCasesFromAnalysis(analysisId, count = 5) {
  return request(`/api/v1/requirement-analyses/${encodeURIComponent(analysisId)}/test-cases`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ count }) });
}

export async function listGeneratedTestCases(analysisId) {
  return request(`/api/v1/requirement-analyses/${encodeURIComponent(analysisId)}/test-cases`);
}

export async function reviewTestCaseGeneration(generationId, review) {
  return request(`/api/v1/test-case-generations/${encodeURIComponent(generationId)}/review`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(review),
  });
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
