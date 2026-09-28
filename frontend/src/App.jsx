import { useState } from "react";
import { Activity, Bell, ChevronDown, CircleHelp, ClipboardCheck, FileStack, FlaskConical, LayoutDashboard, ListChecks, LogOut, PackageOpen, Settings, ShieldCheck } from "lucide-react";
import { LoginPage } from "./components/LoginPage.jsx";
import { RegisterPage } from "./components/RegisterPage.jsx";
import { UploadStep } from "./components/UploadStep.jsx";
import { PackagesPage } from "./components/PackagesPage.jsx";
import { RequirementsPage } from "./components/RequirementsPage.jsx";
import { TestCaseGenerationPage } from "./components/TestCaseGenerationPage.jsx";
import { logoutUser } from "./api/client.js";
import { ToastContainer } from "react-toastify";
import { toast } from "react-toastify";

function displayNameFromEmail(email) { return String(email || "").split("@")[0].replace(/[._-]+/g, " ").trim().split(/\s+/).filter(Boolean).map((part) => part[0].toUpperCase() + part.slice(1).toLowerCase()).join(" "); }

export default function App() {
  const [user, setUser] = useState(() => { const saved = localStorage.getItem("testiq_current_user"); const authenticated = localStorage.getItem("testiq_auth_token") || localStorage.getItem("testiq_local_session") === "true"; return saved && authenticated ? JSON.parse(saved) : null; });
  const [authView, setAuthView] = useState("login"); const [activeNav, setActiveNav] = useState("sources"); const [generationAnalysis, setGenerationAnalysis] = useState(null);
  function handleLogin(userData) { const enrichedUser = { ...userData, name: userData?.name || displayNameFromEmail(userData?.email) }; setUser(enrichedUser); localStorage.setItem("testiq_current_user", JSON.stringify(enrichedUser)); if (!localStorage.getItem("testiq_auth_token")) localStorage.setItem("testiq_local_session", "true"); }
  async function handleLogout() { try { await logoutUser(); } catch { /* local cleanup still applies */ } localStorage.removeItem("testiq_current_user"); localStorage.removeItem("testiq_auth_token"); localStorage.removeItem("testiq_local_session"); toast.success("You have been logged out successfully."); setUser(null); }
  const notifications = <ToastContainer position="top-right" autoClose={3500} newestOnTop closeOnClick pauseOnHover theme="light" />;
  if (!user) return <>{notifications}{authView === "login" ? <LoginPage onLogin={handleLogin} onRegister={() => setAuthView("register")} /> : <RegisterPage onBack={() => setAuthView("login")} />}</>;
  const name = user?.name || displayNameFromEmail(user?.email) || "Workspace user"; const initials = name.split(" ").map((part) => part[0]).slice(0, 2).join("").toUpperCase();
  return <>{notifications}<div className="workspace-shell">
    <aside className="workspace-sidebar">
      <div className="brand-lockup"><div className="brand-mark"><FlaskConical size={19} /></div><div><div className="brand-name">Test<span>IQ</span></div><div className="brand-caption">Source workspace</div></div></div>
      <div className="sidebar-section-label">Workspace</div>
      <nav className="sidebar-nav"><button className={`sidebar-nav-item ${activeNav === "overview" ? "selected" : ""}`} onClick={() => setActiveNav("overview")}><LayoutDashboard size={17} /> Overview</button><button className={`sidebar-nav-item ${activeNav === "sources" ? "selected" : ""}`} onClick={() => setActiveNav("sources")}><FileStack size={17} /> Sources <span className="nav-badge">1</span></button><button className={`sidebar-nav-item ${activeNav === "packages" ? "selected" : ""}`} onClick={() => setActiveNav("packages")}><PackageOpen size={17} /> Packages</button><button className={`sidebar-nav-item ${activeNav === "requirements" ? "selected" : ""}`} onClick={() => setActiveNav("requirements")}><ListChecks size={17} /> Requirements</button><button className={`sidebar-nav-item ${activeNav === "test-cases" ? "selected" : ""}`} onClick={() => setActiveNav("test-cases")}><ClipboardCheck size={17} /> Test cases</button><button className={`sidebar-nav-item ${activeNav === "activity" ? "selected" : ""}`} onClick={() => setActiveNav("activity")}><Activity size={17} /> Activity</button></nav>
      <div className="sidebar-divider" /><div className="sidebar-section-label">Workspace tools</div>
      <nav className="sidebar-nav"><button className="sidebar-nav-item"><ShieldCheck size={17} /> Traceability</button><button className="sidebar-nav-item"><Settings size={17} /> Settings</button></nav>
      <div className="sidebar-spacer" /><div className="sidebar-help"><CircleHelp size={17} /><div><strong>Need a hand?</strong><span>Read the source guide</span></div></div>
      <button className="sidebar-user" onClick={handleLogout}><span className="avatar small">{initials}</span><span className="sidebar-user-copy"><strong>{name}</strong><span>{user?.email}</span></span><LogOut size={16} /></button>
    </aside>
    <main className="workspace-main"><header className="workspace-topbar"><div className="breadcrumb"><span>Workspace</span><span className="breadcrumb-slash">/</span><strong>{activeNav === "sources" ? "Sources" : activeNav === "requirements" ? "Requirements Analysis" : activeNav === "test-cases" ? "Test Case Generation" : activeNav[0].toUpperCase() + activeNav.slice(1)}</strong></div><div className="topbar-actions"><button className="icon-button" aria-label="Notifications"><Bell size={18} /><span className="notification-dot" /></button><div className="topbar-user"><span className="avatar">{initials}</span><span>{name}</span><ChevronDown size={15} /></div></div></header><div className="workspace-content">{activeNav === "sources" ? <UploadStep /> : activeNav === "packages" ? <PackagesPage /> : activeNav === "requirements" ? <RequirementsPage onContinueToGeneration={(next) => { setGenerationAnalysis(next); setActiveNav("test-cases"); }} /> : activeNav === "test-cases" ? <TestCaseGenerationPage analysis={generationAnalysis} onBack={() => setActiveNav("requirements")} /> : <div className="empty-state-panel"><div className="empty-state-icon"><Activity size={25} /></div><h2>{activeNav === "overview" ? "Your workspace at a glance" : "Workspace activity"}</h2><p>This view is ready for the next stage of your source-to-test-data workflow. Manage source packages from the sidebar.</p><button className="primary-button" onClick={() => setActiveNav("sources")}>Open sources</button></div>}</div></main>
  </div></>;
}
