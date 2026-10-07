import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Activity, Bell, BookOpen, CheckCircle2, ChevronDown, ChevronLeft, ChevronRight, CircleAlert, CircleHelp, ClipboardCheck, Database, FileCheck2, FileStack, FlaskConical, Gauge, LayoutDashboard, ListChecks, LoaderCircle, LogOut, Menu, PackageOpen, Settings, ShieldCheck, X } from "lucide-react";
import { LoginPage } from "./components/LoginPage.jsx";
import { RegisterPage } from "./components/RegisterPage.jsx";
import { UploadStep } from "./components/UploadStep.jsx";
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
import { logoutUser } from "./api/client.js";
import { ToastContainer } from "react-toastify";
import { toast } from "react-toastify";
import { clearFinishedActivities, dismissActivity, reconcileStaleActivities, recoverInterruptedTabActivities, useActivities } from "./activity/activityStore.js";

function displayNameFromEmail(email) { return String(email || "").split("@")[0].replace(/[._-]+/g, " ").trim().split(/\s+/).filter(Boolean).map((part) => part[0].toUpperCase() + part.slice(1).toLowerCase()).join(" "); }

const ACTIVE_NAV_KEY = "testiq_active_nav";
const GENERATION_ANALYSIS_KEY = "testiq_generation_analysis";
const WORKSPACE_STATE_VERSION_KEY = "testiq_workspace_state_version";
const WORKSPACE_STATE_VERSION = "phase3-postgres-v1";
if (localStorage.getItem(WORKSPACE_STATE_VERSION_KEY) !== WORKSPACE_STATE_VERSION) {
  ["testiq_current_project", "testiq_current_package", GENERATION_ANALYSIS_KEY].forEach((key) => localStorage.removeItem(key));
  localStorage.setItem(WORKSPACE_STATE_VERSION_KEY, WORKSPACE_STATE_VERSION);
}
const VALID_NAV_ITEMS = new Set(["overview", "dashboard", "sources", "packages", "business-requirements", "requirements", "test-cases", "test-data", "test-results", "activity", "traceability", "knowledge-base", "token-usage"]);
const NAV_ITEMS = [
  { id: "overview", label: "Overview", icon: LayoutDashboard },
  { id: "sources", label: "Upload & Resource Package", icon: FileStack },
  { id: "packages", label: "Packages", icon: PackageOpen },
  { id: "business-requirements", label: "Business Use Cases", icon: ListChecks },
  { id: "knowledge-base", label: "Knowledge Base", icon: BookOpen },
  { id: "requirements", label: "Functional Requirements", icon: ListChecks },
  { id: "test-cases", label: "Test cases", icon: ClipboardCheck },
  { id: "test-data", label: "Test data", icon: Database },
  { id: "test-results", label: "Test Results", icon: FileCheck2 },
  { id: "activity", label: "Activity", icon: Activity },
  { id: "traceability", label: "Traceability", icon: ShieldCheck, section: "Workspace tools" },
  { id: "dashboard", label: "Dashboard", icon: LayoutDashboard, section: "Workspace tools" },
  { id: "token-usage", label: "LLM Token Usage", icon: Gauge, section: "Workspace tools" },
  { id: "settings", label: "Settings", icon: Settings, section: "Workspace tools", disabled: true },
];

function canAccessNavItem(user, item) {
  const permissions = user?.permissions ?? user?.navigation ?? user?.nav_items;
  if (!Array.isArray(permissions)) return true;
  return permissions.includes("*") || permissions.includes(item.id) || (item.permission && permissions.includes(item.permission));
}

function Navigation({ items, activeNav, onNavigate, onLogout, user, initials, expanded, mobile = false }) {
  const primaryItems = items.filter((item) => !item.section);
  const toolItems = items.filter((item) => item.section === "Workspace tools");
  const renderItem = ({ id, label, icon: Icon, badge, badgeLabel, disabled, running }) => (
    <button key={id} type="button" className={`sidebar-nav-item ${activeNav === id ? "selected" : ""}`} aria-label={label} aria-current={activeNav === id ? "page" : undefined} aria-disabled={disabled || undefined} title={expanded ? undefined : label} onClick={() => !disabled && onNavigate(id)}>
      <Icon size={18} aria-hidden="true" /><span className="sidebar-nav-label">{label}</span>{running && <LoaderCircle className="spin nav-runner" size={16} aria-label={`${label} is running`} />}{badge && <span className="nav-badge" aria-label={badgeLabel}>{badge}</span>}
    </button>
  );
  return <>
    <div className="brand-lockup"><div className="brand-mark"><FlaskConical size={19} /></div><div className="brand-copy"><div className="brand-name">Dava.Test<span>IQ</span></div><div className="brand-caption">Source workspace</div></div></div>
    <div className="sidebar-section-label">Workspace</div><nav className="sidebar-nav" aria-label="Workspace navigation">{primaryItems.map(renderItem)}</nav>
    {toolItems.length > 0 && <><div className="sidebar-divider" /><div className="sidebar-section-label">Workspace tools</div><nav className="sidebar-nav" aria-label="Workspace tools">{toolItems.map(renderItem)}</nav></>}
    <div className="sidebar-spacer" /><div className="sidebar-help"><CircleHelp size={18} aria-hidden="true" /><div className="sidebar-help-copy"><strong>Need a hand?</strong><span>Read the source guide</span></div></div>
    <button type="button" className="sidebar-user" aria-label={`Log out ${user?.name || user?.email || "user"}`} onClick={onLogout}><span className="avatar small">{initials}</span><span className="sidebar-user-copy"><strong>{user?.name}</strong><span>{user?.email}</span></span><LogOut size={16} aria-hidden="true" /></button>
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

function activityTime(value) {
  if (!value) return "";
  return new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function ActivityIcon({ status }) {
  if (status === "running") return <LoaderCircle className="spin" size={17} />;
  if (status === "completed") return <CheckCircle2 size={17} />;
  return <CircleAlert size={17} />;
}

function ActivityPage({ activities, onNavigate }) {
  const running = activities.filter((item) => item.status === "running").length;
  return <div className="activity-page"><div className="page-heading"><div><div className="eyebrow">WORKSPACE OPERATIONS</div><h1>Activity</h1><p>Follow uploads, Knowledge Base processing, and generation work across the complete workflow.</p></div>{activities.some((item) => item.status !== "running") && <button type="button" className="secondary-button" onClick={clearFinishedActivities}>Clear finished</button>}</div>
    <section className="activity-summary"><div><strong>{running}</strong><span>Running</span></div><div><strong>{activities.filter((item) => item.status === "completed").length}</strong><span>Completed</span></div><div><strong>{activities.filter((item) => item.status === "failed").length}</strong><span>Needs attention</span></div></section>
    {!activities.length ? <section className="requirements-empty"><div className="empty-state-icon"><Activity size={24} /></div><h2>No recorded activity</h2><p>Operations will appear here when you upload documents or start generation.</p></section> : <section className="activity-list">{activities.map((item) => <article className={`activity-row ${item.status}`} key={item.id}><span className="activity-row-icon"><ActivityIcon status={item.status} /></span><div className="activity-row-main"><div><strong>{item.label}</strong><span className={`activity-status ${item.status}`}>{item.status === "running" ? "Running" : item.status === "completed" ? "Completed" : "Failed"}</span></div><p>{item.detail || "Processing"}</p>{item.status === "running" && <div className={`activity-progress ${item.progress == null ? "indeterminate" : ""}`}><span style={item.progress == null ? undefined : { width: `${item.progress}%` }} /></div>}<small>Started {activityTime(item.startedAt)}{item.updatedAt ? ` · Updated ${activityTime(item.updatedAt)}` : ""}</small></div><div className="activity-row-actions"><button type="button" className="text-button" onClick={() => onNavigate(item.section || "activity")}>Open</button>{item.status !== "running" && <button type="button" className="icon-button" aria-label={`Dismiss ${item.label}`} onClick={() => dismissActivity(item.id)}><X size={15} /></button>}</div></article>)}</section>}
  </div>;
}

export default function App() {
  const [user, setUser] = useState(() => { const saved = localStorage.getItem("testiq_current_user"); const authenticated = localStorage.getItem("testiq_auth_token") || localStorage.getItem("testiq_local_session") === "true"; return saved && authenticated ? JSON.parse(saved) : null; });
  const [authView, setAuthView] = useState("login"); const [activeNav, setActiveNav] = useState(() => { const saved = localStorage.getItem(ACTIVE_NAV_KEY); return VALID_NAV_ITEMS.has(saved) ? saved : "sources"; }); const [generationAnalysis, setGenerationAnalysisState] = useState(storedGenerationAnalysis);
  const [sidebarExpanded, setSidebarExpanded] = useState(() => localStorage.getItem("testiq_sidebar_expanded") === "true");
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const workspaceContentRef = useRef(null);
  const activities = useActivities();
  useEffect(() => {
    recoverInterruptedTabActivities();
    reconcileStaleActivities();
    const timer = window.setInterval(reconcileStaleActivities, 60_000);
    return () => window.clearInterval(timer);
  }, []);
  const runningActivityCount = activities.filter((item) => item.status === "running").length;
  const navigationItems = NAV_ITEMS.map((item) => {
    const running = activities.some((activity) => activity.status === "running" && activity.section === item.id);
    if (item.id === "activity" && runningActivityCount) return { ...item, running, badge: runningActivityCount, badgeLabel: `${runningActivityCount} running activities` };
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
  function handleLogin(userData) { const enrichedUser = { ...userData, name: userData?.name || displayNameFromEmail(userData?.email) }; setUser(enrichedUser); localStorage.setItem("testiq_current_user", JSON.stringify(enrichedUser)); if (!localStorage.getItem("testiq_auth_token")) localStorage.setItem("testiq_local_session", "true"); }
  async function handleLogout() { try { await logoutUser(); } catch { /* local cleanup still applies */ } localStorage.removeItem("testiq_current_user"); localStorage.removeItem("testiq_auth_token"); localStorage.removeItem("testiq_local_session"); localStorage.removeItem(ACTIVE_NAV_KEY); localStorage.removeItem(GENERATION_ANALYSIS_KEY); setGenerationAnalysisState(null); setActiveNav("sources"); toast.success("You have been logged out successfully."); setUser(null); }
  const notifications = <ToastContainer position="top-right" autoClose={3500} newestOnTop closeOnClick pauseOnHover theme="light" />;
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
    <main className="workspace-main"><header className="workspace-topbar"><div className="topbar-leading"><button type="button" className="mobile-menu-button" aria-label="Open navigation menu" aria-controls="mobile-navigation" aria-expanded={mobileMenuOpen} onClick={() => setMobileMenuOpen(true)}><Menu size={20} /></button><div className="breadcrumb"><span>Workspace</span><span className="breadcrumb-slash">/</span><strong>{activeLabel}</strong></div></div><div className="topbar-actions"><button className="icon-button" aria-label="Notifications"><Bell size={18} /><span className="notification-dot" /></button><div className="topbar-user"><span className="avatar">{initials}</span><span>{name}</span><ChevronDown size={15} /></div></div></header><div className="workspace-content" ref={workspaceContentRef}>{!authorizedNav ? <div className="empty-state-panel"><h2>No workspace sections available</h2><p>Your account does not have access to any workspace sections.</p></div> : currentNav === "dashboard" ? <DashboardPage onNavigate={navigate} /> : currentNav === "sources" ? <UploadStep onContinueToBusinessUseCases={() => navigate("business-requirements")} /> : currentNav === "packages" ? <PackagesPage onOpenBusinessRequirements={() => navigate("business-requirements")} /> : currentNav === "business-requirements" ? <BusinessRequirementsPage onContinueToKnowledgeBase={() => navigate("knowledge-base")} /> : currentNav === "traceability" ? <TraceabilityPage /> : currentNav === "token-usage" ? <TokenUsagePage /> : currentNav === "knowledge-base" ? <KnowledgeBasePage onContinueToFunctional={() => navigate("requirements")} onOpenBusinessRequirements={() => navigate("business-requirements")} /> : currentNav === "requirements" ? <RequirementsPage onContinueToGeneration={openTestCaseGeneration} onOpenBusinessRequirements={() => navigate("business-requirements")} onOpenKnowledgeBase={() => navigate("knowledge-base")} /> : currentNav === "test-cases" ? <TestCaseGenerationPage analysis={currentGenerationAnalysis} onBack={() => setActiveNav("requirements")} onContinueToTestData={() => setActiveNav("test-data")} onReviewSaved={() => { toast.success("Test case review saved."); setActiveNav("test-data"); }} onAnalysisUnavailable={clearGenerationAnalysis} /> : currentNav === "test-data" ? <TestDataGenerationPage analysis={currentGenerationAnalysis} onGoToTestCases={() => setActiveNav("test-cases")} /> : currentNav === "test-results" ? <TestResultsPage /> : currentNav === "activity" ? <ActivityPage activities={activities} onNavigate={navigate} /> : <div className="empty-state-panel"><div className="empty-state-icon"><Activity size={25} /></div><h2>Workspace</h2><p>Choose a workspace section to continue.</p><button className="primary-button" onClick={() => navigate("sources")}>Open sources</button></div>}</div></main>
  </div></>;
}
