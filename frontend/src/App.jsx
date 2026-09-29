import { useEffect, useMemo, useState } from "react";
import { Activity, Bell, ChevronDown, CircleHelp, ClipboardCheck, FileCheck2, FileStack, FlaskConical, LayoutDashboard, ListChecks, LogOut, PackageOpen, Settings, ShieldCheck } from "lucide-react";
import { LoginPage } from "./components/LoginPage.jsx";
import { RegisterPage } from "./components/RegisterPage.jsx";
import { UploadStep } from "./components/UploadStep.jsx";
import { PackagesPage } from "./components/PackagesPage.jsx";
import { RequirementsPage } from "./components/RequirementsPage.jsx";
import { BusinessRequirementsPage } from "./components/BusinessRequirementsPage.jsx";
import { TraceabilityPage } from "./components/TraceabilityPage.jsx";
import { TestCaseGenerationPage } from "./components/TestCaseGenerationPage.jsx";
import { TestResultsPage } from "./components/TestResultsPage.jsx";
import { logoutUser } from "./api/client.js";
import { ToastContainer } from "react-toastify";
import { toast } from "react-toastify";

function displayNameFromEmail(email) { return String(email || "").split("@")[0].replace(/[._-]+/g, " ").trim().split(/\s+/).filter(Boolean).map((part) => part[0].toUpperCase() + part.slice(1).toLowerCase()).join(" "); }

const ACTIVE_NAV_KEY = "testiq_active_nav";
const GENERATION_ANALYSIS_KEY = "testiq_generation_analysis";
const VALID_NAV_ITEMS = new Set(["overview", "sources", "packages", "business-requirements", "requirements", "test-cases", "test-results", "activity", "traceability"]);

function readStoredJson(key) {
  try {
    const saved = localStorage.getItem(key);
    return saved ? JSON.parse(saved) : null;
  } catch {
    localStorage.removeItem(key);
    return null;
  }
}

function storedGenerationAnalysis() {
  const analysis = readStoredJson(GENERATION_ANALYSIS_KEY);
  const selectedPackage = readStoredJson("testiq_current_package");
  if (analysis?.package_id && selectedPackage?.package_id && analysis.package_id !== selectedPackage.package_id) return null;
  return analysis;
}

export default function App() {
  const [user, setUser] = useState(() => { const saved = localStorage.getItem("testiq_current_user"); const authenticated = localStorage.getItem("testiq_auth_token") || localStorage.getItem("testiq_local_session") === "true"; return saved && authenticated ? JSON.parse(saved) : null; });
  const [authView, setAuthView] = useState("login"); const [activeNav, setActiveNav] = useState(() => { const saved = localStorage.getItem(ACTIVE_NAV_KEY); return VALID_NAV_ITEMS.has(saved) ? saved : "sources"; }); const [generationAnalysis, setGenerationAnalysisState] = useState(storedGenerationAnalysis);
  const currentGenerationAnalysis = useMemo(() => {
    if (!generationAnalysis) return null;
    const selectedPackage = readStoredJson("testiq_current_package");
    if (generationAnalysis.package_id && selectedPackage?.package_id && generationAnalysis.package_id !== selectedPackage.package_id) return null;
    return generationAnalysis;
  }, [generationAnalysis, activeNav]);
  useEffect(() => { localStorage.setItem(ACTIVE_NAV_KEY, activeNav); }, [activeNav]);
  function setGenerationAnalysis(next) { setGenerationAnalysisState(next); if (next) localStorage.setItem(GENERATION_ANALYSIS_KEY, JSON.stringify(next)); else localStorage.removeItem(GENERATION_ANALYSIS_KEY); }
  function openTestCaseGeneration(next) { setGenerationAnalysis(next); setActiveNav("test-cases"); }
  function handleLogin(userData) { const enrichedUser = { ...userData, name: userData?.name || displayNameFromEmail(userData?.email) }; setUser(enrichedUser); localStorage.setItem("testiq_current_user", JSON.stringify(enrichedUser)); if (!localStorage.getItem("testiq_auth_token")) localStorage.setItem("testiq_local_session", "true"); }
  async function handleLogout() { try { await logoutUser(); } catch { /* local cleanup still applies */ } localStorage.removeItem("testiq_current_user"); localStorage.removeItem("testiq_auth_token"); localStorage.removeItem("testiq_local_session"); localStorage.removeItem(ACTIVE_NAV_KEY); localStorage.removeItem(GENERATION_ANALYSIS_KEY); setGenerationAnalysisState(null); setActiveNav("sources"); toast.success("You have been logged out successfully."); setUser(null); }
  const notifications = <ToastContainer position="top-right" autoClose={3500} newestOnTop closeOnClick pauseOnHover theme="light" />;
  if (!user) return <>{notifications}{authView === "login" ? <LoginPage onLogin={handleLogin} onRegister={() => setAuthView("register")} /> : <RegisterPage onBack={() => setAuthView("login")} />}</>;
  const name = user?.name || displayNameFromEmail(user?.email) || "Workspace user"; const initials = name.split(" ").map((part) => part[0]).slice(0, 2).join("").toUpperCase();
  return <>{notifications}<div className="workspace-shell">
    <aside className="workspace-sidebar">
      <div className="brand-lockup"><div className="brand-mark"><FlaskConical size={19} /></div><div><div className="brand-name">Test<span>IQ</span></div><div className="brand-caption">Source workspace</div></div></div>
      <div className="sidebar-section-label">Workspace</div>
      <nav className="sidebar-nav"><button className={`sidebar-nav-item ${activeNav === "overview" ? "selected" : ""}`} onClick={() => setActiveNav("overview")}><LayoutDashboard size={17} /> Overview</button><button className={`sidebar-nav-item ${activeNav === "sources" ? "selected" : ""}`} onClick={() => setActiveNav("sources")}><FileStack size={17} /> Sources <span className="nav-badge">1</span></button><button className={`sidebar-nav-item ${activeNav === "packages" ? "selected" : ""}`} onClick={() => setActiveNav("packages")}><PackageOpen size={17} /> Packages</button><button className={`sidebar-nav-item ${activeNav === "business-requirements" ? "selected" : ""}`} onClick={() => setActiveNav("business-requirements")}><ListChecks size={17} /> Business Requirements</button><button className={`sidebar-nav-item ${activeNav === "requirements" ? "selected" : ""}`} onClick={() => setActiveNav("requirements")}><ListChecks size={17} /> Functional Requirements</button><button className={`sidebar-nav-item ${activeNav === "test-cases" ? "selected" : ""}`} onClick={() => setActiveNav("test-cases")}><ClipboardCheck size={17} /> Test cases</button><button className={`sidebar-nav-item ${activeNav === "test-results" ? "selected" : ""}`} onClick={() => setActiveNav("test-results")}><FileCheck2 size={17} /> Test Results</button><button className={`sidebar-nav-item ${activeNav === "activity" ? "selected" : ""}`} onClick={() => setActiveNav("activity")}><Activity size={17} /> Activity</button></nav>
      <div className="sidebar-divider" /><div className="sidebar-section-label">Workspace tools</div>
      <nav className="sidebar-nav"><button className={`sidebar-nav-item ${activeNav === "traceability" ? "selected" : ""}`} onClick={() => setActiveNav("traceability")}><ShieldCheck size={17} /> Traceability</button><button className="sidebar-nav-item"><Settings size={17} /> Settings</button></nav>
      <div className="sidebar-spacer" /><div className="sidebar-help"><CircleHelp size={17} /><div><strong>Need a hand?</strong><span>Read the source guide</span></div></div>
      <button className="sidebar-user" onClick={handleLogout}><span className="avatar small">{initials}</span><span className="sidebar-user-copy"><strong>{name}</strong><span>{user?.email}</span></span><LogOut size={16} /></button>
    </aside>
    <main className="workspace-main"><header className="workspace-topbar"><div className="breadcrumb"><span>Workspace</span><span className="breadcrumb-slash">/</span><strong>{activeNav === "sources" ? "Sources" : activeNav === "requirements" ? "Functional Requirements" : activeNav === "business-requirements" ? "Business Requirements" : activeNav === "traceability" ? "Traceability" : activeNav === "test-cases" ? "Test Case Generation" : activeNav === "test-results" ? "Test Results" : activeNav[0].toUpperCase() + activeNav.slice(1)}</strong></div><div className="topbar-actions"><button className="icon-button" aria-label="Notifications"><Bell size={18} /><span className="notification-dot" /></button><div className="topbar-user"><span className="avatar">{initials}</span><span>{name}</span><ChevronDown size={15} /></div></div></header><div className="workspace-content">{activeNav === "sources" ? <UploadStep /> : activeNav === "packages" ? <PackagesPage /> : activeNav === "business-requirements" ? <BusinessRequirementsPage /> : activeNav === "traceability" ? <TraceabilityPage /> : activeNav === "requirements" ? <RequirementsPage onContinueToGeneration={openTestCaseGeneration} /> : activeNav === "test-cases" ? <TestCaseGenerationPage analysis={currentGenerationAnalysis} onBack={() => setActiveNav("requirements")} /> : activeNav === "test-results" ? <TestResultsPage /> : <div className="empty-state-panel"><div className="empty-state-icon"><Activity size={25} /></div><h2>{activeNav === "overview" ? "Your workspace at a glance" : "Workspace activity"}</h2><p>This view is ready for the next stage of your source-to-test-data workflow. Manage source packages from the sidebar.</p><button className="primary-button" onClick={() => setActiveNav("sources")}>Open sources</button></div>}</div></main>
  </div></>;
}
