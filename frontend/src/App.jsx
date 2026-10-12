import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Activity, Bell, BookOpen, ChevronDown, ChevronLeft, ChevronRight, ClipboardCheck, Database, FileCheck2, FileSearch, FileStack, FlaskConical, Gauge, LayoutDashboard, LoaderCircle, LogOut, Menu, PackageOpen, ShieldCheck, UploadCloud, X } from "lucide-react";
import { LoginPage } from "./components/LoginPage.jsx";
import { RegisterPage } from "./components/RegisterPage.jsx";
import { ProjectsPage } from "./components/ProjectsPage.jsx";
import { SourceDocumentsPage } from "./components/SourceDocumentsPage.jsx";
import { PackagesPage } from "./components/PackagesPage.jsx";
import { RequirementsPage } from "./components/RequirementsPage.jsx";
import { BusinessRequirementsPage } from "./components/BusinessRequirementsPage.jsx";
import { TraceabilityPage } from "./components/TraceabilityPage.jsx";
import { TestCaseGenerationPage } from "./components/TestCaseGenerationPage.jsx";
import { TestDataGenerationPage } from "./components/TestDataGenerationPage.jsx";
import { TestResultsPage } from "./components/TestResultsPage.jsx";
import { KnowledgeBasePage } from "./components/KnowledgeBasePage.jsx";
import { TokenUsagePage } from "./components/TokenUsagePage.jsx";
import { DashboardPage } from "./components/DashboardPage.jsx";
import { getCurrentUser, logoutUser } from "./api/client.js";
import { ToastContainer } from "react-toastify";
import { toast } from "react-toastify";
import { reconcileStaleActivities, recoverInterruptedTabActivities, useActivities } from "./activity/activityStore.js";

function displayNameFromEmail(email) { return String(email || "").split("@")[0].replace(/[._-]+/g, " ").trim().split(/\s+/).filter(Boolean).map((part) => part[0].toUpperCase() + part.slice(1).toLowerCase()).join(" "); }

const ACTIVE_NAV_KEY = "testiq_active_nav";
const GENERATION_ANALYSIS_KEY = "testiq_generation_analysis";
const WORKSPACE_STATE_VERSION_KEY = "testiq_workspace_state_version";
const WORKSPACE_STATE_VERSION = "automatic-kb-clean-slate-v1";
// Remove credentials from the old local-only POC login implementation.
localStorage.removeItem("testiq_users");
localStorage.removeItem("testiq_local_session");
if (localStorage.getItem(WORKSPACE_STATE_VERSION_KEY) !== WORKSPACE_STATE_VERSION) {
  Object.keys(localStorage).filter((key) => key === "testiq_current_project" || key === "testiq_current_package" || key === GENERATION_ANALYSIS_KEY || key === "testiq_workspace_activities_v1" || key.startsWith("testiq_business_rule_selection:") || key.startsWith("testiq_functional_review_draft:")).forEach((key) => localStorage.removeItem(key));
  localStorage.setItem(WORKSPACE_STATE_VERSION_KEY, WORKSPACE_STATE_VERSION);
}
const VALID_NAV_ITEMS = new Set(["projects", "sources", "packages", "business-requirements", "requirements", "test-cases", "test-data", "test-results", "traceability", "knowledge-base", "dashboard", "token-usage"]);
const NAV_ITEMS = [
  { id: "dashboard", label: "Dashboard", icon: LayoutDashboard },
  { id: "projects", label: "Manage Projects & Upload BRD", icon: UploadCloud },
  { id: "test-cases", label: "Test Cases", icon: ClipboardCheck },
  { id: "test-data", label: "Test Data", icon: Database },
  { id: "test-results", label: "Test Results", icon: FileCheck2 },
  { id: "traceability", label: "Traceability", icon: ShieldCheck },
  { id: "sources", label: "Source Documents", icon: FileStack, parent: "traceability" },
  { id: "packages", label: "Packages", icon: PackageOpen, parent: "traceability" },
  { id: "business-requirements", label: "Business Use Cases", icon: ShieldCheck, parent: "traceability" },
  { id: "knowledge-base", label: "Knowledge Base", icon: BookOpen, parent: "traceability" },
  { id: "requirements", label: "Functional Requirements", icon: FileSearch, parent: "traceability" },
  { id: "token-usage", label: "LLM Token Usage", icon: Gauge, parent: "traceability" },
];

function canAccessNavItem(user, item) {
  const permissions = user?.permissions ?? user?.navigation ?? user?.nav_items;
  if (!Array.isArray(permissions)) return true;
  return permissions.includes("*") || permissions.includes(item.id) || (item.permission && permissions.includes(item.permission));
}

function Navigation({ items, activeNav, onNavigate, onLogout, user, initials, expanded, mobile = false }) {
  const traceabilityChildren = items.filter((item) => item.parent === "traceability");
  const workspaceToolChildren = items.filter((item) => item.parent === "workspace-tools");
  const primaryItems = items.filter((item) => !item.parent);
  const childIsActive = traceabilityChildren.some((item) => item.id === activeNav);
  const workspaceToolIsActive = workspaceToolChildren.some((item) => item.id === activeNav);
  const [traceabilityOpen, setTraceabilityOpen] = useState(() => localStorage.getItem("testiq_traceability_menu_open") === "true" || childIsActive);
  const [workspaceToolsOpen, setWorkspaceToolsOpen] = useState(() => localStorage.getItem("testiq_workspace_tools_menu_open") === "true" || workspaceToolIsActive);
  useEffect(() => { if (childIsActive) setTraceabilityOpen(true); }, [childIsActive]);
  useEffect(() => { if (workspaceToolIsActive) setWorkspaceToolsOpen(true); }, [workspaceToolIsActive]);
  useEffect(() => { localStorage.setItem("testiq_traceability_menu_open", String(traceabilityOpen)); }, [traceabilityOpen]);
  useEffect(() => { localStorage.setItem("testiq_workspace_tools_menu_open", String(workspaceToolsOpen)); }, [workspaceToolsOpen]);
  const renderItem = ({ id, label, icon: Icon, badge, badgeLabel, disabled, running }, nested = false) => (
    <button key={id} type="button" className={`sidebar-nav-item ${nested ? "sidebar-nav-child" : ""} ${activeNav === id ? "selected" : ""}`} aria-label={label} aria-current={activeNav === id ? "page" : undefined} aria-disabled={disabled || undefined} title={expanded ? undefined : label} onClick={() => !disabled && onNavigate(id)}>
      <Icon size={18} aria-hidden="true" /><span className="sidebar-nav-label">{label}</span>{running && <LoaderCircle className="spin nav-runner" size={16} aria-label={`${label} is running`} />}{badge && <span className="nav-badge" aria-label={badgeLabel}>{badge}</span>}
    </button>
  );
  return <>
    <div className="brand-lockup"><div className="brand-mark"><FlaskConical size={19} /></div><div className="brand-copy"><div className="brand-name">Dava.Test<span>IQ</span></div><div className="brand-caption">Source workspace</div></div></div>
    <div className="sidebar-section-label">Workspace</div><nav className="sidebar-nav" aria-label="Workspace navigation">{primaryItems.map((item) => item.id === "workspace-tools" && workspaceToolChildren.length ? <div className={`sidebar-nav-group ${workspaceToolsOpen ? "is-open" : ""}`} key={item.id}><div className="sidebar-nav-parent">{renderItem(item)}<button type="button" className="sidebar-subnav-toggle" aria-label={`${workspaceToolsOpen ? "Hide" : "Show"} Workspace Tools`} aria-expanded={workspaceToolsOpen} onClick={() => setWorkspaceToolsOpen((open) => !open)}><ChevronDown size={15} /></button></div>{workspaceToolsOpen && <div className="sidebar-subnav" aria-label="Workspace Tools">{workspaceToolChildren.map((child) => renderItem(child, true))}</div>}</div> : item.id === "traceability" && traceabilityChildren.length ? <div className={`sidebar-nav-group ${traceabilityOpen ? "is-open" : ""}`} key={item.id}><div className="sidebar-nav-parent">{renderItem(item)}<button type="button" className="sidebar-subnav-toggle" aria-label={`${traceabilityOpen ? "Hide" : "Show"} Traceability tools`} aria-expanded={traceabilityOpen} onClick={() => setTraceabilityOpen((open) => !open)}><ChevronDown size={15} /></button></div>{traceabilityOpen && <div className="sidebar-subnav" aria-label="Traceability tools">{traceabilityChildren.map((child) => renderItem(child, true))}</div>}</div> : renderItem(item))}</nav>
    <div className="sidebar-spacer" />
    <button type="button" className="sidebar-user" aria-label={`Log out ${user?.name || user?.email || "user"}`} title="Log out" onClick={onLogout}><span className="avatar small">{initials}</span><span className="sidebar-user-copy"><strong>{user?.name}</strong><span>{user?.email}</span></span><LogOut size={16} aria-hidden="true" /></button>
    {mobile && <span className="sr-only">Navigation menu</span>}
  </>;
}

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
  const [user, setUser] = useState(() => { const saved = localStorage.getItem("testiq_current_user"); return saved && localStorage.getItem("testiq_auth_token") ? JSON.parse(saved) : null; });
  const [authReady, setAuthReady] = useState(() => !localStorage.getItem("testiq_auth_token"));
  const [authView, setAuthView] = useState("login"); const [activeNav, setActiveNav] = useState(() => { const saved = localStorage.getItem(ACTIVE_NAV_KEY); return VALID_NAV_ITEMS.has(saved) ? saved : "projects"; }); const [generationAnalysis, setGenerationAnalysisState] = useState(storedGenerationAnalysis);
  const [sidebarExpanded, setSidebarExpanded] = useState(() => localStorage.getItem("testiq_sidebar_expanded") === "true");
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const workspaceContentRef = useRef(null);
  const activities = useActivities();
  useEffect(() => {
    const token = localStorage.getItem("testiq_auth_token");
    if (!token) {
      localStorage.removeItem("testiq_current_user");
      setUser(null);
      setAuthReady(true);
      return undefined;
    }
    let active = true;
    getCurrentUser()
      .then((currentUser) => {
        if (!active) return;
        localStorage.setItem("testiq_current_user", JSON.stringify(currentUser));
        setUser(currentUser);
      })
      .catch(() => {
        if (!active) return;
        localStorage.removeItem("testiq_auth_token");
        localStorage.removeItem("testiq_current_user");
        setUser(null);
      })
      .finally(() => { if (active) setAuthReady(true); });
    return () => { active = false; };
  }, []);
  useEffect(() => {
    recoverInterruptedTabActivities();
    reconcileStaleActivities();
    const timer = window.setInterval(reconcileStaleActivities, 60_000);
    return () => window.clearInterval(timer);
  }, []);
  const runningActivityCount = activities.filter((item) => item.status === "running").length;
  const navigationItems = NAV_ITEMS.map((item) => {
    const running = activities.some((activity) => activity.status === "running" && activity.section === item.id);
    return running ? { ...item, running: true } : item;
  });
  const availableNavItems = navigationItems.filter((item) => canAccessNavItem(user, item));
  const visibleNavItems = availableNavItems.filter((item) => !item.disabled);
  const authorizedNav = visibleNavItems.find((item) => item.id === activeNav) || visibleNavItems[0];
  const currentNav = authorizedNav?.id;
  useEffect(() => {
    if (!mobileMenuOpen) return undefined;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKeyDown = (event) => { if (event.key === "Escape") setMobileMenuOpen(false); };
    document.addEventListener("keydown", onKeyDown);
    return () => { document.body.style.overflow = previousOverflow; document.removeEventListener("keydown", onKeyDown); };
  }, [mobileMenuOpen]);
  const currentGenerationAnalysis = useMemo(() => {
    if (!generationAnalysis) return null;
    const selectedPackage = readStoredJson("testiq_current_package");
    if (generationAnalysis.package_id && selectedPackage?.package_id && generationAnalysis.package_id !== selectedPackage.package_id) return null;
    return generationAnalysis;
  }, [generationAnalysis, activeNav]);
  useEffect(() => { localStorage.setItem(ACTIVE_NAV_KEY, activeNav); }, [activeNav]);
  useEffect(() => { localStorage.setItem("testiq_sidebar_expanded", String(sidebarExpanded)); }, [sidebarExpanded]);
  useEffect(() => { workspaceContentRef.current?.scrollTo({ top: 0 }); }, [activeNav]);
  function setGenerationAnalysis(next) { setGenerationAnalysisState(next); if (next) localStorage.setItem(GENERATION_ANALYSIS_KEY, JSON.stringify(next)); else localStorage.removeItem(GENERATION_ANALYSIS_KEY); }
  const clearGenerationAnalysis = useCallback(() => { setGenerationAnalysisState(null); localStorage.removeItem(GENERATION_ANALYSIS_KEY); }, []);
  function openTestCaseGeneration(next) { setGenerationAnalysis(next); setActiveNav("test-cases"); }
  function handleLogin(userData) { const enrichedUser = { ...userData, name: userData?.name || displayNameFromEmail(userData?.email) }; setUser(enrichedUser); localStorage.setItem("testiq_current_user", JSON.stringify(enrichedUser)); }
  async function handleLogout() { try { await logoutUser(); } catch { /* local cleanup still applies */ } localStorage.removeItem("testiq_current_user"); localStorage.removeItem("testiq_auth_token"); localStorage.removeItem(ACTIVE_NAV_KEY); localStorage.removeItem(GENERATION_ANALYSIS_KEY); setGenerationAnalysisState(null); setActiveNav("projects"); toast.success("You have been logged out successfully."); setUser(null); }
  const notifications = <ToastContainer position="top-right" autoClose={3500} newestOnTop closeOnClick pauseOnHover theme="light" />;
  if (!authReady) return <>{notifications}<div role="status" aria-live="polite" style={{ minHeight: "100vh", display: "grid", placeItems: "center", color: "#26415c" }}>Checking your sign-in… <LoaderCircle className="spin" size={18} /></div></>;
  if (!user) return <>{notifications}{authView === "login" ? <LoginPage onLogin={handleLogin} onRegister={() => setAuthView("register")} /> : <RegisterPage onBack={() => setAuthView("login")} />}</>;
  const name = user?.name || displayNameFromEmail(user?.email) || "Workspace user"; const initials = name.split(" ").map((part) => part[0]).slice(0, 2).join("").toUpperCase();
  const navigate = (destination) => { setActiveNav(destination); setMobileMenuOpen(false); };
  const activeLabel = authorizedNav?.label || "Workspace";
  return <>{notifications}<div className={`workspace-shell ${sidebarExpanded ? "sidebar-is-expanded" : ""} ${mobileMenuOpen ? "mobile-menu-open" : ""}`} data-theme={document.documentElement.dataset.theme || "light"}>
    <aside className={`workspace-sidebar ${sidebarExpanded ? "sidebar-expanded" : "sidebar-collapsed"}`} aria-label="Main sidebar">
      <Navigation items={visibleNavItems} activeNav={currentNav} onNavigate={navigate} onLogout={handleLogout} user={{ ...user, name }} initials={initials} expanded={sidebarExpanded} />
    </aside>
    <button type="button" className="sidebar-edge-toggle" aria-label={sidebarExpanded ? "Collapse sidebar" : "Expand sidebar"} aria-expanded={sidebarExpanded} onClick={() => setSidebarExpanded((expanded) => !expanded)}>{sidebarExpanded ? <ChevronLeft size={17} /> : <ChevronRight size={17} />}</button>
    <div className={`mobile-sidebar-backdrop ${mobileMenuOpen ? "is-visible" : ""}`} aria-hidden="true" onClick={() => setMobileMenuOpen(false)} />
    <aside id="mobile-navigation" className={`workspace-sidebar mobile-sidebar ${mobileMenuOpen ? "is-open" : ""}`} aria-label="Mobile navigation" aria-hidden={!mobileMenuOpen} {...(!mobileMenuOpen ? { inert: "" } : {})}>
      <button type="button" className="mobile-sidebar-close" aria-label="Close navigation menu" onClick={() => setMobileMenuOpen(false)}><X size={20} /></button>
      <Navigation items={visibleNavItems} activeNav={currentNav} onNavigate={navigate} onLogout={handleLogout} user={{ ...user, name }} initials={initials} expanded mobile />
    </aside>
    <main className="workspace-main"><header className="workspace-topbar"><div className="topbar-leading"><button type="button" className="mobile-menu-button" aria-label="Open navigation menu" aria-controls="mobile-navigation" aria-expanded={mobileMenuOpen} onClick={() => setMobileMenuOpen(true)}><Menu size={20} /></button><div className="breadcrumb"><span>Workspace</span><span className="breadcrumb-slash">/</span><strong>{activeLabel}</strong></div></div><div className="topbar-actions"><button className="icon-button" aria-label="Notifications"><Bell size={18} /><span className="notification-dot" /></button><div className="topbar-user"><span className="avatar">{initials}</span><span>{name}</span></div></div></header><div className="workspace-content" ref={workspaceContentRef}>{!authorizedNav ? <div className="empty-state-panel"><h2>No workspace sections available</h2><p>Your account does not have access to any workspace sections.</p></div> : currentNav === "dashboard" ? <DashboardPage onNavigate={navigate} /> : currentNav === "projects" ? <ProjectsPage onOpenTestCases={openTestCaseGeneration} /> : currentNav === "sources" ? <SourceDocumentsPage /> : currentNav === "packages" ? <PackagesPage onOpenBusinessRequirements={() => navigate("business-requirements")} /> : currentNav === "business-requirements" ? <BusinessRequirementsPage onContinueToKnowledgeBase={() => navigate("knowledge-base")} /> : currentNav === "traceability" ? <TraceabilityPage /> : currentNav === "token-usage" ? <TokenUsagePage /> : currentNav === "knowledge-base" ? <KnowledgeBasePage onContinueToFunctional={() => navigate("requirements")} onOpenBusinessRequirements={() => navigate("business-requirements")} /> : currentNav === "requirements" ? <RequirementsPage onContinueToGeneration={openTestCaseGeneration} onOpenBusinessRequirements={() => navigate("business-requirements")} onOpenKnowledgeBase={() => navigate("knowledge-base")} /> : currentNav === "test-cases" ? <TestCaseGenerationPage analysis={currentGenerationAnalysis} onBack={() => navigate("projects")} onReviewSaved={() => { toast.success("Test case review saved."); setActiveNav("test-data"); }} onAnalysisPrepared={setGenerationAnalysis} onAnalysisUnavailable={clearGenerationAnalysis} /> : currentNav === "test-data" ? <TestDataGenerationPage analysis={currentGenerationAnalysis} onGoToTestCases={() => setActiveNav("test-cases")} onContinueToResults={() => setActiveNav("test-results")} /> : currentNav === "test-results" ? <TestResultsPage /> : <div className="empty-state-panel"><div className="empty-state-icon"><Activity size={25} /></div><h2>Workspace</h2><p>Choose a workspace section to continue.</p><button className="primary-button" onClick={() => navigate("projects")}>Open projects</button></div>}</div></main>
  </div></>;
}
