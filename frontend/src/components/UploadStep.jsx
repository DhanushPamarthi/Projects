import { useEffect, useState } from "react";
import {
  Check,
  CheckCircle2,
  ChevronRight,
  CircleAlert,
  CloudUpload,
  FileText,
  FolderPlus,
  LoaderCircle,
  PackageCheck,
  Plus,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  X,
} from "lucide-react";
import { createManifest, createProject, getProject, getRevision, listProjects, listSources, uploadSource } from "../api/client.js";

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
  const warning = source.validation_messages?.find((message) => message.startsWith("Missing expected section:") || message.startsWith("OpenAI extraction failed:") || message.startsWith("Extraction failed:"));
  return <div className={`source-row ${compact ? "compact" : ""}`}>
    <span className={`source-file-icon ${source.source_type === "brd" ? "brd" : "supporting"}`}><FileText size={17} /></span>
    <div className="source-main">
      <strong title={filename}>{filename}</strong>
      <span>{source.source_type === "brd" ? "Business requirements document" : (source.source_type || "Supporting source").replaceAll("_", " ")} - {size}</span>
      {warning && <small className="source-warning">{warning}</small>}
    </div>
    <span className={`source-status ${tone}`}><span />{tone === "success" ? "Ready" : tone === "pending" ? "Processing" : "Needs review"}</span>
    <span className="revision-label">{source.active_revision ? "Active revision" : "Review revision"}</span>
    <strong className="revision-value">v{source.active_revision?.revision || source.latest_revision?.revision || 1}</strong>
  </div>;
}

export function UploadStep() {
  const [project, setProject] = useState(() => JSON.parse(localStorage.getItem("testiq_current_project") || "null"));
  const [projects, setProjects] = useState([]);
  const [projectName, setProjectName] = useState("");
  const [sources, setSources] = useState([]);
  const [file, setFile] = useState(null);
  const [dragging, setDragging] = useState(false);
  const [sourceType, setSourceType] = useState("brd");
  const [brdSourceId, setBrdSourceId] = useState("");
  const [owner, setOwner] = useState("");
  const [ownerVersion, setOwnerVersion] = useState("");
  const [requiredSections, setRequiredSections] = useState("");
  const [step, setStep] = useState(1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");
  const [manifest, setManifest] = useState(null);
  const [processing, setProcessing] = useState(false);
  const [confirmNewPackage, setConfirmNewPackage] = useState(false);

  const brds = sources.filter((item) => item.source_type === "brd");
  const supporting = sources.filter((item) => item.source_type !== "brd");
  const currentStep = !brds.length ? 1 : manifest ? 3 : step;

  async function refreshSources(id = project?.project_id) {
    if (id) setSources(await listSources(id));
  }

  useEffect(() => { listProjects().then(setProjects).catch(() => {}); }, []);
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

  async function handleUpload() {
    if (!file) return;
    setBusy(true);
    setError("");
    try {
      if (sourceType !== "brd" && !brdSourceId) throw new Error("Choose the BRD this source belongs to.");
      const activeProject = project || await createProject(projectName.trim() || "Untitled source package");
      setProject(activeProject);
      setProjectName(activeProject.project_name);
      localStorage.setItem("testiq_current_project", JSON.stringify(activeProject));
      const uploaded = await uploadSource(activeProject.project_id, file, sourceType, brdSourceId, owner, ownerVersion, requiredSections);
      const isBrdUpload = sourceType === "brd";
      setToast(uploaded.status === "duplicate" ? "Duplicate retained for traceability" : isBrdUpload ? "BRD uploaded successfully. Processing extraction..." : "Source uploaded successfully");
      setFile(null);
      await refreshSources(activeProject.project_id);
      listProjects().then(setProjects).catch(() => {});
      if (isBrdUpload) {
        setBrdSourceId(uploaded.source_id);
        setSourceType("brd");
      }
      if (uploaded.extraction?.status === "pending") pollExtraction(uploaded, activeProject.project_id, isBrdUpload);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function pollExtraction(uploaded, id, isBrdUpload = false) {
    setProcessing(true);
    let finalStatus = "pending";
    for (let attempt = 0; attempt < 45; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      try {
        const revision = await getRevision(uploaded.revision_id);
        finalStatus = revision.extraction?.status || "pending";
        if (["succeeded", "failed", "skipped"].includes(finalStatus)) break;
      } catch {
        finalStatus = "failed";
        break;
      }
    }
    await refreshSources(id);
    setProcessing(false);
    if (isBrdUpload) setToast(finalStatus === "succeeded" ? "BRD uploaded and processed successfully." : finalStatus === "failed" ? "BRD uploaded, but extraction needs review." : "BRD uploaded successfully.");
  }

  async function createPackage() {
    if (!project || !sources.length) return;
    setBusy(true);
    setError("");
    try {
      const created = await createManifest(sources.map((item) => item.source_id), "MVP", project.project_id);
      setManifest(created);
      localStorage.setItem("testiq_current_package", JSON.stringify(created));
      setToast("Source package snapshot created");
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
    setSources([]);
    setManifest(null);
    setFile(null);
    setStep(1);
    setSourceType("brd");
    localStorage.removeItem("testiq_current_project");
    localStorage.removeItem("testiq_current_package");
  }

  return <div className="source-page">
    <div className="page-heading">
      <div><div className="eyebrow">SOURCE CONTROL CENTER</div><h1>Source packages</h1><p>Bring business and technical context together before creating test data.</p></div>
      <button className="secondary-button" onClick={() => setConfirmNewPackage(true)}><Plus size={17} /> New package</button>
    </div>

    <div className="project-bar">
      <div className="project-context"><span className="project-icon"><FolderPlus size={17} /></span><div><span className="muted-label">Current project</span><strong>{project?.project_name || "No project selected"}</strong></div></div>
      <div className="project-actions">
        {projects.length > 0 && <select value={project?.project_id || ""} onChange={async (event) => {
          const selected = await getProject(event.target.value);
          setProject(selected);
          setProjectName(selected.project_name);
          setManifest(null);
          setSources([]);
          localStorage.removeItem("testiq_current_package");
          await refreshSources(selected.project_id);
        }}><option value="">Choose project</option>{projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.project_name}</option>)}</select>}
        <span className="project-count">{sources.length} source{sources.length === 1 ? "" : "s"}</span>
      </div>
    </div>

    <div className="stepper">{[[1, "Start with your BRD", "Primary requirements"], [2, "Add supporting sources", "Rules, APIs and schemas"], [3, "Review package", "Create a fixed snapshot"]].map(([number, title, copy]) => <div key={number} className={`stepper-item ${currentStep === number ? "current" : ""} ${currentStep > number ? "complete" : ""}`}><span className="stepper-number">{currentStep > number ? <Check size={15} /> : number}</span><div><strong>{title}</strong><span>{copy}</span></div>{number < 3 && <ChevronRight className="stepper-arrow" size={17} />}</div>)}</div>
    {toast && <div className="toast-message"><CheckCircle2 size={17} /> {toast}<button onClick={() => setToast("")}><X size={15} /></button></div>}
    {error && <div className="error-message"><CircleAlert size={17} /> {error}<button onClick={() => setError("")}><X size={15} /></button></div>}

    {!manifest && currentStep < 3 && <div className="content-grid">
      <section className="upload-panel">
        <div className="panel-title"><div><h2>{currentStep === 1 ? (brds.length ? "Upload an updated BRD" : "Upload your BRD") : "Add a supporting source"}</h2><p>{currentStep === 1 ? (brds.length ? "Upload an updated BRD. The previous file will remain available for traceability." : "Your business requirements document anchors this package.") : "Connect the context your team needs for accurate test data."}</p></div><span className="step-pill">Step {currentStep} of 3</span></div>
        {currentStep === 1 && <div className="field-block"><label>Project name</label><input value={projectName} disabled={Boolean(project)} onChange={(event) => setProjectName(event.target.value)} placeholder="e.g. PBM Refill Reminder" /><span className="field-hint">A project is created automatically with your first upload.</span></div>}
        {currentStep === 2 && <><div className="field-block"><label>Source type</label><select value={sourceType} onChange={(event) => setSourceType(event.target.value)}><option value="brd">Another BRD revision</option>{supportingTypes.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></div>{sourceType !== "brd" && <div className="field-block"><label>Associated BRD</label><select value={brdSourceId} onChange={(event) => setBrdSourceId(event.target.value)}><option value="">Select a BRD</option>{brds.map((item) => <option key={item.source_id} value={item.source_id}>{item.logical_name}</option>)}</select></div>}</>}
        <div className="field-row"><div className="field-block"><label>Source owner <em>Optional</em></label><input value={owner} onChange={(event) => setOwner(event.target.value)} placeholder="e.g. Product team" /></div><div className="field-block"><label>Owner version <em>Optional</em></label><input value={ownerVersion} onChange={(event) => setOwnerVersion(event.target.value)} placeholder="e.g. 1.0" /></div></div>
        {currentStep === 1 && <div className="field-block"><label>Expected sections <em>Optional</em></label><input value={requiredSections} onChange={(event) => setRequiredSections(event.target.value)} placeholder="Scope, Requirements, Acceptance Criteria" /><span className="field-hint">Missing sections become review warnings, not upload blockers.</span></div>}
        <div className={`drop-zone-modern ${dragging ? "dragging" : ""}`} onDragOver={(event) => { event.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={(event) => { event.preventDefault(); setDragging(false); chooseFile(event.dataTransfer.files?.[0]); }}>{currentStep === 1 && brds.length > 0 && !file ? <><div className="upload-icon existing"><FileText size={23} /></div><h3 className="existing-file-name" title={brds[0].active_revision?.filename || brds[0].logical_name}>{brds[0].active_revision?.filename || brds[0].logical_name}</h3><p>Current file - v{brds[0].active_revision?.revision || brds[0].revision || 1}</p><label className="choose-file">Upload replacement<input type="file" accept=".docx,.pdf,.txt,.md" onChange={(event) => chooseFile(event.target.files?.[0])} /></label></> : <><div className="upload-icon"><CloudUpload size={23} /></div><h3>Drop a file here</h3><p>or choose from your computer</p><label className="choose-file">Choose file<input type="file" accept=".docx,.pdf,.txt,.md" onChange={(event) => chooseFile(event.target.files?.[0])} /></label><span className="file-hint">DOCX, PDF, TXT or MD - up to 25 MB</span></>}</div>
        {file && <div className="selected-file"><span className="file-type"><FileText size={17} /></span><div><strong>{file.name}</strong><span>{(file.size / 1024).toFixed(1)} KB - Ready to upload</span></div><button onClick={() => setFile(null)}><X size={17} /></button></div>}
        <div className="panel-footer"><span className="secure-note"><ShieldCheck size={15} /> Original files are preserved securely</span><div className="panel-actions"><button className="primary-button" disabled={!file || busy || (sourceType !== "brd" && !brdSourceId)} onClick={handleUpload}>{busy ? <><LoaderCircle className="spin" size={16} /> Uploading...</> : <><CloudUpload size={16} /> {currentStep === 1 ? (brds.length ? "Upload updated BRD" : "Upload BRD") : "Add source"}</>}</button>{currentStep === 1 && brds.length > 0 && <button className="secondary-button" onClick={async () => { await refreshSources(project?.project_id); setStep(2); setSourceType("api_spec"); setBrdSourceId(brds[0].source_id); }}>Continue to sources <ChevronRight size={15} /></button>}</div></div>
      </section>
      <aside className="side-panel"><div className="side-panel-head"><span className="side-icon"><Sparkles size={17} /></span><div><h3>Smart processing</h3><p>What happens next</p></div></div><div className="info-list"><div><b>01</b><span>We preserve the original file and create a versioned revision.</span></div><div><b>02</b><span>AI extraction runs automatically with local fallback.</span></div><div><b>03</b><span>Every section stays traceable to its source and page.</span></div></div><div className="privacy-note"><ShieldCheck size={16} /><span>Your API key stays on the backend. It is never sent from this screen.</span></div></aside>
    </div>}

    {currentStep === 2 && !manifest && <section className="source-list-card"><div className="list-card-header"><div><h2>Package sources</h2><p>{supporting.length ? "Your BRD and supporting context are ready to review." : "Your BRD is ready. Add a supporting source, or review now."}</p></div><span className="health-badge"><span /> {processing ? "Processing" : "Healthy"}</span></div>{sources.map((source) => <SourceRow key={source.source_id} source={source} />)}<div className="list-card-actions"><button className="text-button" onClick={() => setStep(1)}>Back to BRD</button><button className="secondary-button" onClick={() => setStep(3)}><PackageCheck size={16} /> Review package <ChevronRight size={15} /></button></div></section>}
    {currentStep === 3 && !manifest && <section className="review-card"><div className="review-header"><div><div className="eyebrow">FINAL REVIEW</div><h2>Ready to create your package?</h2><p>This snapshot will lock the exact source revisions used for future traceability.</p></div><div className="review-check"><PackageCheck size={22} /></div></div><div className="review-summary"><div><span>Project</span><strong>{project?.project_name}</strong></div><div><span>Sources included</span><strong>{sources.length}</strong></div><div><span>Scope</span><strong>MVP</strong></div></div><div className="review-source-list">{sources.map((source) => <SourceRow key={source.source_id} source={source} compact />)}</div><div className="review-actions"><button className="text-button" onClick={() => setStep(2)}>Back to sources</button><button className="primary-button" onClick={createPackage} disabled={busy}>{busy ? <><LoaderCircle className="spin" size={16} /> Creating...</> : <><PackageCheck size={16} /> Create source package</>}</button></div></section>}
    {manifest && <section className="success-card package-ready-card"><div className="success-mark"><Check size={26} /></div><div className="eyebrow">PACKAGE CREATED</div><h2>Your source package is ready</h2><p>All {manifest.items?.length || sources.length} source revisions are captured in an immutable snapshot.</p><div className="manifest-id"><span>Package version</span><strong>v{manifest.package_version || 1}</strong><span className="manifest-dot" /><span>Scope</span><strong>{manifest.scope || "MVP"}</strong></div><div className="success-actions"><button className="secondary-button" onClick={() => { setManifest(null); setStep(2); }}><RefreshCw size={16} /> Continue managing sources</button></div></section>}
    {confirmNewPackage && <div className="modal-backdrop" role="presentation" onMouseDown={() => setConfirmNewPackage(false)}><div className="confirm-modal" role="dialog" aria-modal="true" aria-labelledby="new-package-title" onMouseDown={(event) => event.stopPropagation()}><div className="confirm-icon"><Plus size={21} /></div><h2 id="new-package-title">Start a new package?</h2><p>Your current project and all created packages will remain saved. You'll begin a fresh source package here.</p><div className="confirm-actions"><button className="text-button" onClick={() => setConfirmNewPackage(false)}>Cancel</button><button className="primary-button" onClick={confirmResetProject}>Start new package</button></div></div></div>}
  </div>;
}
