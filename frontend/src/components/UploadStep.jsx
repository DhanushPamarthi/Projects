import { useEffect, useRef, useState } from "react";
import {
  Check,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  CloudUpload,
  FileText,
  FolderPlus,
  PackageCheck,
  Plus,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  X,
} from "lucide-react";
import { toast as notify } from "react-toastify";
import { createManifest, createProject, getProject, getRevision, listProjects, listSources, uploadSource } from "../api/client.js";
import { completeActivity, failActivity, startActivity, updateActivity } from "../activity/activityStore.js";
import { LoadingSpinner } from "./LoadingSpinner.jsx";
import { AppSelect } from "./AppSelect.jsx";

const supportingTypes = [
  ["campaign_rules", "Campaign rules"],
  ["api_spec", "API specification"],
  ["database_schema", "Database schema"],
  ["message_template", "Message template"],
  ["ui_description", "UI description"],
];

function SourceRow({ source, compact = false }) {
  const status = source.extraction?.status || source.extraction_status || source.status;
  const tone = status === "failed" || status === "rejected" || status === "extraction_failed" ? "danger" : status === "pending" || status === "processing" ? "pending" : "success";
  const revision = source.active_revision || source.latest_revision || {};
  const filename = revision.filename || source.logical_name || source.filename || "Source document";
  const size = revision.size_bytes ? `${(revision.size_bytes / 1024).toFixed(1)} KB` : "Uploaded file";
  const validationMessages = source.validation_messages || revision.validation_messages || [];
  const parsedMessages = Array.isArray(validationMessages) ? validationMessages : (() => { try { return JSON.parse(validationMessages || "[]"); } catch { return []; } })();
  const missingWarnings = parsedMessages.filter((message) => message.startsWith("Missing expected section:")).map((message) => message.replace("Missing expected section:", "").trim());
  return <div className={`source-row ${compact ? "compact" : ""}`}>
    <span className={`source-file-icon ${source.source_type === "brd" ? "brd" : "supporting"}`}><FileText size={17} /></span>
    <div className="source-main">
      <strong title={filename}>{filename}</strong>
      <span>{source.source_type === "brd" ? "Business requirements document" : (source.source_type || "Supporting source").replaceAll("_", " ")} - {size}</span>
      {missingWarnings.length > 0 && <small className="source-warning">Missing sections: {missingWarnings.join(", ")}</small>}
    </div>
    <span className={`source-status ${tone}`}><span />{tone === "success" ? "Ready" : tone === "pending" ? "Processing" : "Needs review"}</span>
    <span className="revision-label">{source.active_revision ? "Active revision" : "Review revision"}</span>
    <strong className="revision-value">v{source.active_revision?.revision || source.latest_revision?.revision || 1}</strong>
  </div>;
}

function missingSectionsFor(source) {
  const messages = source?.validation_messages || source?.active_revision?.validation_messages || [];
  const parsed = Array.isArray(messages) ? messages : (() => { try { return JSON.parse(messages || "[]"); } catch { return []; } })();
  return parsed.filter((message) => message.startsWith("Missing expected section: ")).map((message) => message.replace("Missing expected section: ", ""));
}

export function UploadStep({ onContinueToBusinessUseCases }) {
  const [project, setProject] = useState(() => JSON.parse(localStorage.getItem("testiq_current_project") || "null"));
  const [projects, setProjects] = useState([]);
  const [projectName, setProjectName] = useState("");
  const [packageName, setPackageName] = useState("");
  const [sources, setSources] = useState([]);
  const [selectedPackageSourceIds, setSelectedPackageSourceIds] = useState([]);
  const [file, setFile] = useState(null);
  const [dragging, setDragging] = useState(false);
  const [sourceType, setSourceType] = useState("brd");
  const [brdSourceId, setBrdSourceId] = useState("");
  const [requiredSections, setRequiredSections] = useState("");
  const [step, setStep] = useState(1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [uploadValidationError, setUploadValidationError] = useState("");
  const [toast, setToast] = useState("");
  const [manifest, setManifest] = useState(null);
  const [confirmNewPackage, setConfirmNewPackage] = useState(false);
  const [missingSections, setMissingSections] = useState([]);
  const [showMissingSections, setShowMissingSections] = useState(false);
  const sourceTypeRef = useRef(null);
  const packageNameRef = useRef(null);

  const brds = sources.filter((item) => item.source_type === "brd" && item.active_revision);
  const packageSources = sources.filter((item) => selectedPackageSourceIds.includes(item.source_id));
  const selectedBrds = packageSources.filter((item) => item.source_type === "brd");
  const packagePending = packageSources.some((item) => ["pending", "processing"].includes(item.extraction?.status || item.extraction_status || item.status));
  const packageRejected = packageSources.some((item) => ["rejected", "extraction_failed"].includes(item.status || item.active_revision?.status));
  const packageReady = selectedBrds.length === 1 && !packagePending && !packageRejected;
  const activeBrdSourceId = brdSourceId || brds[0]?.source_id || "";
  const currentStep = manifest ? 3 : step;

  async function refreshSources(id = project?.project_id) {
    if (id) {
      const rows = await listSources(id);
      setSources(rows);
      setSelectedPackageSourceIds((current) => {
        const valid = current.filter((sourceId) => rows.some((row) => row.source_id === sourceId));
        const brd = rows.find((row) => row.source_type === "brd" && valid.includes(row.source_id)) || rows.find((row) => row.source_type === "brd");
        const supportingIds = rows.filter((row) => row.source_type !== "brd" && (!valid.length || valid.includes(row.source_id))).map((row) => row.source_id);
        return [...new Set([...supportingIds, ...(brd ? [brd.source_id] : [])])];
      });
    }
  }

  useEffect(() => { listProjects().then(setProjects).catch(() => {}); }, []);
  useEffect(() => {
    if (currentStep === 2) sourceTypeRef.current?.focus();
    if (currentStep === 3 && !manifest) packageNameRef.current?.focus();
  }, [currentStep, manifest]);
  useEffect(() => {
    if (!project?.project_id) return;
    getProject(project.project_id)
      .then((current) => {
        setProject(current);
        setProjectName(current.project_name);
        return refreshSources(current.project_id);
      })
      .catch(() => {
        localStorage.removeItem("testiq_current_project");
        localStorage.removeItem("testiq_project_name");
        localStorage.removeItem("testiq_current_package");
        setProject(null);
        setProjectName("");
        setSources([]);
        setManifest(null);
        setStep(1);
      });
  }, [project?.project_id]);

  function chooseFile(next) {
    setError("");
    if (next?.size > 25 * 1024 * 1024) return setError("Files must be 25 MB or smaller.");
    setFile(next || null);
  }

  function continueToSources() {
    proceedToSources();
  }

  function proceedToSources() {
    setShowMissingSections(false);
    setStep(2);
    setSourceType("api_spec");
    setBrdSourceId(brds[0]?.source_id || "");
  }

  function optimisticSourceFromUpload(uploaded, selectedFile, selectedSourceType) {
    const revision = {
      revision_id: uploaded.revision_id,
      revision: uploaded.revision || 1,
      filename: selectedFile.name,
      size_bytes: selectedFile.size,
      validation_messages: uploaded.validation_messages || [],
    };
    return {
      source_id: uploaded.source_id,
      source_type: selectedSourceType,
      logical_name: selectedFile.name,
      active_revision: revision,
      latest_revision: revision,
      status: uploaded.status === "duplicate" ? "uploaded" : "pending",
      validation_messages: uploaded.validation_messages || [],
      extraction: uploaded.extraction || { status: "pending" },
    };
  }

  function mergeSource(nextSource) {
    setSources((items) => {
      const existing = items.filter((item) => item.source_id !== nextSource.source_id);
      return [...existing, nextSource];
    });
  }

  function updateSourceFromRevision(sourceId, revision) {
    setSources((items) => items.map((item) => {
      if (item.source_id !== sourceId) return item;
      const latestRevision = {
        ...(item.latest_revision || {}),
        revision_id: revision.revision_id,
        revision: revision.revision,
        filename: revision.filename,
        size_bytes: revision.size_bytes,
        validation_messages: revision.validation_messages || [],
      };
      return {
        ...item,
        active_revision: latestRevision,
        latest_revision: latestRevision,
        status: revision.status,
        validation_messages: revision.validation_messages || [],
        extraction: revision.extraction || item.extraction,
      };
    }));
  }

  async function handleUpload() {
    if (!file) return;
    if (sourceType === "brd" && !project && !projectName.trim()) {
      setError("Enter a project name before uploading the BRD.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      if (sourceType !== "brd" && !activeBrdSourceId) throw new Error("Upload a BRD before adding supporting sources.");
      const selectedFile = file;
      const selectedSourceType = sourceType;
      const activeProject = project || await createProject(projectName.trim());
      setProject(activeProject);
      setProjectName(activeProject.project_name);
      localStorage.setItem("testiq_current_project", JSON.stringify(activeProject));
      const uploaded = await uploadSource(activeProject.project_id, selectedFile, selectedSourceType, activeBrdSourceId, requiredSections);
      const isBrdUpload = selectedSourceType === "brd";
      mergeSource(optimisticSourceFromUpload(uploaded, selectedFile, selectedSourceType));
      setSelectedPackageSourceIds((current) => {
        const withoutOtherBrds = selectedSourceType === "brd" ? current.filter((sourceId) => !brds.some((item) => item.source_id === sourceId)) : current;
        return [...new Set([...withoutOtherBrds, uploaded.source_id])];
      });
      const successMessage = uploaded.status === "duplicate"
        ? "Duplicate retained for traceability."
        : isBrdUpload
          ? "BRD uploaded successfully. Validation and extraction have started."
          : "Source uploaded successfully.";
      notify.success(successMessage);
      setToast("");
      if (isBrdUpload) {
        setBrdSourceId(uploaded.source_id);
        setSourceType("api_spec");
        setStep(2);
      } else {
        setPackageName("");
      }
      setFile(null);
      refreshSources(activeProject.project_id).catch(() => {
        setError("Upload succeeded, but the source list could not refresh. Reload the page to see the uploaded file.");
      });
      listProjects().then(setProjects).catch(() => {});
      if (uploaded.extraction?.status === "pending") {
        const extractionActivityId = startActivity({
          id: `extraction:${uploaded.revision_id}`,
          label: isBrdUpload ? "Processing BRD" : "Processing supporting source",
          detail: `${selectedFile.name} · preparing extracted sections`,
          section: "sources",
          progress: 5,
          kind: "extraction",
        });
        pollExtraction(uploaded, activeProject.project_id, isBrdUpload, extractionActivityId);
      }
    } catch (err) {
      setUploadValidationError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function pollExtraction(uploaded, id, isBrdUpload = false, activityId = null) {
    let finalStatus = "pending";
    let completedRevision = null;
    for (let attempt = 0; attempt < 40; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 3000));
      try {
        const revision = await getRevision(uploaded.revision_id);
        completedRevision = revision;
        finalStatus = revision.extraction?.status || "pending";
        updateSourceFromRevision(uploaded.source_id, revision);
        if (activityId) updateActivity(activityId, { progress: Math.min(95, 10 + Math.round(((attempt + 1) / 40) * 85)), detail: `${revision.filename || "Source"} · ${finalStatus === "pending" ? "extracting content" : finalStatus}` });
        if (["succeeded", "failed", "skipped"].includes(finalStatus)) break;
      } catch (error) {
        finalStatus = "failed";
        if (activityId) failActivity(activityId, error);
        break;
      }
    }
    await refreshSources(id);
    const missing = completedRevision?.extraction_summary?.missing_sections || [];
    setMissingSections(missing);
    if (isBrdUpload) {
      if (finalStatus === "succeeded") {
        if (activityId) completeActivity(activityId, "BRD extraction completed successfully.");
        setToast("BRD uploaded and processed successfully.");
      } else if (finalStatus === "failed") {
        const failureMessage = completedRevision?.extraction?.error || completedRevision?.validation_messages?.find((message) => message.startsWith("Extraction failed:"))?.replace("Extraction failed: ", "") || "The BRD could not be extracted.";
        setToast("");
        setUploadValidationError(failureMessage);
        if (activityId) failActivity(activityId, failureMessage);
        setStep(1);
      } else {
        if (activityId) updateActivity(activityId, { detail: "Processing continues in the backend. Return to Sources to refresh the final status." });
        setToast("BRD processing is still running. You can continue adding supporting sources while it finishes.");
      }
    } else if (activityId) {
      if (["succeeded", "skipped"].includes(finalStatus)) completeActivity(activityId, "Source extraction completed successfully.");
      else if (finalStatus === "failed") failActivity(activityId, completedRevision?.extraction?.error || "Source extraction failed.");
    }
  }

  async function createPackage({ allowMissingSections = false } = {}) {
    if (!project || !sources.length) return;
    if (!packageName.trim()) {
      setError("Enter a package name before creating the Resource Package.");
      return;
    }
    const selectedBrd = selectedBrds[0];
    if (!selectedBrd) {
      setError("Select exactly one BRD for the Resource Package.");
      return;
    }
    if (selectedBrds.length !== 1) {
      setError("A Resource Package must contain exactly one BRD.");
      return;
    }
    const missing = missingSectionsFor(selectedBrd);
    if (missing.length > 0 && !allowMissingSections) {
      setMissingSections(missing);
      setShowMissingSections(true);
      return;
    }
    setBusy(true);
    setError("");
    try {
      const created = await createManifest(packageSources.map((item) => item.source_id), packageName.trim(), "MVP", project.project_id);
      setManifest(created);
      setPackageName("");
      localStorage.setItem("testiq_current_package", JSON.stringify(created));
      setToast("Resource Package created. It is ready for Business Use Case generation.");
      notify.success("Resource Package created. Continue to Business Use Cases.");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  function confirmResetProject() {
    setConfirmNewPackage(false);
    setProject(null);
    setProjectName("");
    setPackageName("");
    setSources([]);
    setManifest(null);
    setToast("");
    setFile(null);
    setRequiredSections("");
    setMissingSections([]);
    setShowMissingSections(false);
    setStep(1);
    setSourceType("brd");
    localStorage.removeItem("testiq_current_project");
    localStorage.removeItem("testiq_current_package");
  }

  return <div className="source-page">
    <div className="page-heading">
      <div><div className="eyebrow">SOURCE DOCUMENTS</div><h1>Upload & Resource Package</h1><p>Upload the BRD and optional supporting documents, then capture them in a versioned Resource Package.</p></div>
      {(project?.project_id || sources.length > 0 || manifest) && <button className="secondary-button" onClick={() => setConfirmNewPackage(true)}><Plus size={17} /> New package</button>}
    </div>

    <div className="project-bar">
      <div className="project-context"><span className="project-icon"><FolderPlus size={17} /></span><div><span className="muted-label">Current project</span><strong>{project?.project_name || "No project selected"}</strong></div></div>
      <div className="project-actions">
        {projects.length > 0 && <AppSelect value={project?.project_id || ""} onChange={async (event) => {
          const selected = await getProject(event.target.value);
          setProject(selected);
          setProjectName(selected.project_name);
          setPackageName("");
          setManifest(null);
          setSources([]);
          setSelectedPackageSourceIds([]);
          localStorage.removeItem("testiq_current_package");
          await refreshSources(selected.project_id);
        }}><option value="">Choose project</option>{projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.project_name}</option>)}</AppSelect>}
        <span className="project-count">{sources.length} source{sources.length === 1 ? "" : "s"}</span>
      </div>
    </div>

    <div className="stepper">{[[1, "Start with your BRD", "Primary requirements"], [2, "Add supporting sources", "Optional API and schema"], [3, "Review package", "Create a fixed snapshot"]].map(([number, title, copy]) => { const complete = currentStep > number || (Boolean(manifest) && number === 3); return <div key={number} className={`stepper-item ${currentStep === number && !complete ? "current" : ""} ${complete ? "complete" : ""}`}><span className="stepper-number">{complete ? <Check size={15} /> : number}</span><div><strong>{title}</strong><span>{copy}</span></div>{number < 3 && <ChevronRight className="stepper-arrow" size={17} />}</div>; })}</div>
    {toast && <div className="toast-message"><CheckCircle2 size={17} /> {toast}<button onClick={() => setToast("")}><X size={15} /></button></div>}
    {error && <div className="error-message"><CircleAlert size={17} /> {error}<button onClick={() => setError("")}><X size={15} /></button></div>}
    {uploadValidationError && <div className="modal-backdrop" role="presentation"><div className="confirm-modal upload-error-modal" role="dialog" aria-modal="true" aria-labelledby="upload-error-title"><button type="button" className="modal-close" aria-label="Close upload error" onClick={() => setUploadValidationError("")}><X size={18} /></button><div className="confirm-icon error"><CircleAlert size={21} /></div><h2 id="upload-error-title">Upload could not be completed</h2><p>{uploadValidationError}</p><p className="modal-helper">The original file has been preserved. Choose a valid file to continue.</p><div className="confirm-actions"><button className="primary-button" onClick={() => { setUploadValidationError(""); setFile(null); }}>Choose another file</button></div></div></div>}
    {showMissingSections && <div className="modal-backdrop" role="presentation"><div className="confirm-modal missing-sections-modal" role="dialog" aria-modal="true" aria-labelledby="missing-sections-title"><button type="button" className="modal-close" aria-label="Close missing sections dialog" onClick={() => setShowMissingSections(false)}><X size={18} /></button><div className="confirm-icon"><CircleAlert size={21} /></div><h2 id="missing-sections-title">Expected sections are missing</h2><p>The following section{missingSections.length === 1 ? " is" : "s are"} not present in the extracted BRD:</p><ul>{missingSections.map((section) => <li key={section}>{section}</li>)}</ul><p>Do you still want to create the Resource Package?</p><div className="confirm-actions"><button className="text-button" onClick={() => { setShowMissingSections(false); setStep(1); }}>Review BRD</button><button className="primary-button" onClick={() => { setShowMissingSections(false); createPackage({ allowMissingSections: true }); }}>Create Resource Package</button></div></div></div>}

    {!manifest && currentStep < 3 && <div className="content-grid">
      <section className="upload-panel">
        <div className="panel-title"><div><h2>{currentStep === 1 ? (brds.length ? "Upload an updated BRD" : "Upload your BRD") : "Add a supporting source"}</h2><p>{currentStep === 1 ? (brds.length ? "Upload an updated BRD. The previous file will remain available for traceability." : "Your business requirements document anchors this Resource Package.") : "Connect the context your team needs for accurate test data."}</p></div><span className="step-pill">Step {currentStep} of 3</span></div>
        {currentStep === 1 && <div className="field-block"><label>Project name</label><input value={projectName} disabled={Boolean(project)} onChange={(event) => setProjectName(event.target.value)} placeholder="e.g. PBM Refill Reminder" /><span className="field-hint">A project is created automatically with your first upload.</span></div>}
        {currentStep === 2 && <div className="field-block"><label>Source type <em>Optional technical context</em></label><AppSelect ref={sourceTypeRef} value={sourceType} onChange={(event) => setSourceType(event.target.value)}>{supportingTypes.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</AppSelect><span className="field-hint">Add API, database schema, or other supporting sources when they are available. The BRD is the only required document.</span></div>}
        {currentStep === 1 && <div className="field-block"><label>Expected sections <em>Optional</em></label><input value={requiredSections} onChange={(event) => setRequiredSections(event.target.value)} placeholder="Scope, Requirements, Acceptance Criteria" /><span className="field-hint">Missing sections become review warnings, not upload blockers.</span></div>}
        <div className={`drop-zone-modern ${dragging ? "dragging" : ""}`} onDragOver={(event) => { event.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={(event) => { event.preventDefault(); setDragging(false); chooseFile(event.dataTransfer.files?.[0]); }}>{currentStep === 1 && brds.length > 0 && !file ? <><div className="upload-icon existing"><FileText size={23} /></div><h3 className="existing-file-name" title={brds[0].active_revision?.filename || brds[0].logical_name}>{brds[0].active_revision?.filename || brds[0].logical_name}</h3><p>Current file - v{brds[0].active_revision?.revision || brds[0].revision || 1}</p><label className="choose-file">Upload replacement<input type="file" accept=".docx,.pdf,.txt,.md,.json,.yaml,.yml,.sql,.csv" onChange={(event) => chooseFile(event.target.files?.[0])} /></label></> : <><div className="upload-icon"><CloudUpload size={23} /></div><h3>Drop a file here</h3><p>or choose from your computer</p><label className="choose-file">Choose file<input type="file" accept=".docx,.pdf,.txt,.md,.json,.yaml,.yml,.sql,.csv" onChange={(event) => chooseFile(event.target.files?.[0])} /></label><span className="file-hint">DOCX, PDF, TXT, MD, JSON, YAML, SQL or CSV - up to 25 MB</span></>}</div>
        {file && <div className="selected-file"><span className="file-type"><FileText size={17} /></span><div><strong>{file.name}</strong><span>{(file.size / 1024).toFixed(1)} KB - Ready to upload</span></div><button onClick={() => setFile(null)}><X size={17} /></button></div>}
        <div className="panel-footer"><span className="secure-note"><ShieldCheck size={15} /> Original files are preserved securely</span><div className="panel-actions"><button className="primary-button" disabled={!file || busy || (sourceType === "brd" && !project && !projectName.trim()) || (sourceType !== "brd" && !activeBrdSourceId)} onClick={handleUpload}>{busy ? <LoadingSpinner variant="inline" label="Uploading" /> : <><CloudUpload size={16} /> {currentStep === 1 ? (brds.length ? "Upload updated BRD" : "Upload BRD") : "Add source"}</>}</button>{currentStep === 1 && brds.length > 0 && <button className="secondary-button" disabled={busy} onClick={async () => { await refreshSources(project?.project_id); continueToSources(); setSourceType("api_spec"); setBrdSourceId(brds[0].source_id); }}>Continue to sources <ChevronRight size={15} /></button>}</div></div>
      </section>
      <aside className="side-panel"><div className="side-panel-head"><span className="side-icon"><Sparkles size={17} /></span><div><h3>Smart processing</h3><p>What happens next</p></div></div><div className="info-list"><div><b>01</b><span>We preserve the original file and create a versioned revision.</span></div><div><b>02</b><span>Extraction prepares traceable source evidence in the background.</span></div><div><b>03</b><span>Every section stays traceable to its source and page.</span></div></div><div className="privacy-note"><ShieldCheck size={16} /><span>Your API key stays on the backend. It is never sent from this screen.</span></div></aside>
    </div>}

    {currentStep === 2 && !manifest && <section className="source-list-card"><div className="list-card-header"><div><h2>Package sources</h2><p>All supporting sources and the active BRD are included in this package.</p></div><span className="health-badge"><span /> {packagePending ? "Processing" : "Sources ready"}</span></div>{packagePending && <LoadingSpinner variant="section" label="Processing package sources" detail="Extracting content and preparing traceable source evidence." />}<div className="package-source-selection">{sources.filter((source) => selectedPackageSourceIds.includes(source.source_id)).map((source) => <div className="package-source-option" key={source.source_id}><SourceRow source={source} compact /></div>)}</div><div className="list-card-actions"><button className="secondary-button" onClick={() => { setStep(1); setSourceType("brd"); setBrdSourceId(""); }}><ChevronLeft size={15} /> Back to BRD</button><button className="secondary-button" disabled={!packageReady} onClick={() => setStep(3)}><PackageCheck size={16} /> {packagePending ? "Waiting for extraction" : packageRejected ? "Fix selected sources" : selectedBrds.length !== 1 ? "Select one BRD" : "Review package"} <ChevronRight size={15} /></button></div></section>}
    {currentStep === 3 && !manifest && <section className="review-card"><div className="review-header"><div><div className="eyebrow">FINAL REVIEW</div><h2>Ready to create the Resource Package?</h2><p>The package records the exact document versions used by every later step.</p></div><div className="review-check"><PackageCheck size={22} /></div></div><div className="field-block package-name-field"><label>Package name <em>Required</em></label><input ref={packageNameRef} value={packageName} onChange={(event) => setPackageName(event.target.value)} placeholder="e.g. PBM Refill Reminder Resource Package" /></div><div className="review-summary"><div><span>Project</span><strong>{project?.project_name}</strong></div><div><span>Documents included</span><strong>{packageSources.length}</strong></div><div><span>Scope</span><strong>MVP</strong></div></div><div className="review-source-list">{packageSources.map((source) => <SourceRow key={source.source_id} source={source} compact />)}</div><div className="review-actions"><button className="secondary-button" onClick={() => { setToast(""); setStep(2); }}><ChevronLeft size={15} /> Back to documents</button><button className="primary-button" onClick={createPackage} disabled={busy || !packageName.trim()}>{busy ? <LoadingSpinner variant="inline" label="Creating" /> : <><PackageCheck size={16} /> Create Resource Package</>}</button></div></section>}
    {manifest && <section className="success-card package-ready-card"><div className="success-mark"><Check size={26} /></div><div className="eyebrow">PACKAGE CREATED</div><h2>{manifest.package_name || packageName}</h2><p>All {manifest.items?.length || packageSources.length} source revisions are captured in an immutable snapshot.</p><div className="manifest-id"><span>Package version</span><strong>v{manifest.package_version || 1}</strong><span className="manifest-dot" /><span>Scope</span><strong>{manifest.scope || "MVP"}</strong></div><div className="success-actions"><button className="secondary-button" onClick={() => { setManifest(null); setToast(""); setStep(2); }}><RefreshCw size={16} /> Continue managing sources</button><button className="primary-button" onClick={() => onContinueToBusinessUseCases?.()} disabled={!onContinueToBusinessUseCases}><PackageCheck size={16} /> Continue to Business Use Cases</button></div></section>}
    {confirmNewPackage && <div className="modal-backdrop" role="presentation" onMouseDown={() => setConfirmNewPackage(false)}><div className="confirm-modal" role="dialog" aria-modal="true" aria-labelledby="new-package-title" onMouseDown={(event) => event.stopPropagation()}><button type="button" className="modal-close" aria-label="Close new Resource Package dialog" onClick={() => setConfirmNewPackage(false)}><X size={18} /></button><div className="confirm-icon"><Plus size={21} /></div><h2 id="new-package-title">Start a new Resource Package?</h2><p>Your current project and all created Resource Packages will remain saved. You will begin a new Resource Package here.</p><div className="confirm-actions"><button className="secondary-button" onClick={() => setConfirmNewPackage(false)}>Cancel</button><button className="primary-button" onClick={confirmResetProject}>Start Resource Package</button></div></div></div>}
  </div>;
}
