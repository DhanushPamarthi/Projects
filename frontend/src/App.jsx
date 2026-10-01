import { useEffect, useMemo, useState } from "react";
import { Activity, Bell, ChevronDown, ChevronLeft, ChevronRight, CircleHelp, ClipboardCheck, FileCheck2, FileStack, FlaskConical, LayoutDashboard, ListChecks, LogOut, Menu, PackageOpen, Settings, ShieldCheck, X } from "lucide-react";
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
const NAV_ITEMS = [
  { id: "overview", label: "Overview", icon: LayoutDashboard },
  { id: "sources", label: "Sources", icon: FileStack, badge: "1", badgeLabel: "1 source issue" },
  { id: "packages", label: "Packages", icon: PackageOpen },
  { id: "business-requirements", label: "Business Requirements", icon: ListChecks },
  { id: "requirements", label: "Functional Requirements", icon: ListChecks },
  { id: "test-cases", label: "Test cases", icon: ClipboardCheck },
  { id: "test-results", label: "Test Results", icon: FileCheck2 },
  { id: "activity", label: "Activity", icon: Activity },
  { id: "traceability", label: "Traceability", icon: ShieldCheck, section: "Workspace tools" },
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
  const renderItem = ({ id, label, icon: Icon, badge, badgeLabel, disabled }) => (
    <button key={id} type="button" className={`sidebar-nav-item ${activeNav === id ? "selected" : ""}`} aria-label={label} aria-current={activeNav === id ? "page" : undefined} aria-disabled={disabled || undefined} title={expanded ? undefined : label} onClick={() => !disabled && onNavigate(id)}>
      <Icon size={18} aria-hidden="true" /><span className="sidebar-nav-label">{label}</span>{badge && <span className="nav-badge" aria-label={badgeLabel}>{badge}</span>}
    </button>
  );
  return <>
    <div className="brand-lockup"><div className="brand-mark"><FlaskConical size={19} /></div><div className="brand-copy"><div className="brand-name">Test<span>IQ</span></div><div className="brand-caption">Source workspace</div></div></div>
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

export default function App() {
  const [user, setUser] = useState(() => { const saved = localStorage.getItem("testiq_current_user"); const authenticated = localStorage.getItem("testiq_auth_token") || localStorage.getItem("testiq_local_session") === "true"; return saved && authenticated ? JSON.parse(saved) : null; });
  const [authView, setAuthView] = useState("login"); const [activeNav, setActiveNav] = useState(() => { const saved = localStorage.getItem(ACTIVE_NAV_KEY); return VALID_NAV_ITEMS.has(saved) ? saved : "sources"; }); const [generationAnalysis, setGenerationAnalysisState] = useState(storedGenerationAnalysis);
  const [sidebarExpanded, setSidebarExpanded] = useState(() => localStorage.getItem("testiq_sidebar_expanded") === "true");
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const availableNavItems = NAV_ITEMS.filter((item) => canAccessNavItem(user, item));
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
  function setGenerationAnalysis(next) { setGenerationAnalysisState(next); if (next) localStorage.setItem(GENERATION_ANALYSIS_KEY, JSON.stringify(next)); else localStorage.removeItem(GENERATION_ANALYSIS_KEY); }
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
    <aside id="mobile-navigation" className={`workspace-sidebar mobile-sidebar ${mobileMenuOpen ? "is-open" : ""}`} aria-label="Mobile navigation" aria-hidden={!mobileMenuOpen} inert={!mobileMenuOpen}>
      <button type="button" className="mobile-sidebar-close" aria-label="Close navigation menu" onClick={() => setMobileMenuOpen(false)}><X size={20} /></button>
      <Navigation items={visibleNavItems} activeNav={currentNav} onNavigate={navigate} onLogout={handleLogout} user={{ ...user, name }} initials={initials} expanded mobile />
    </aside>
    <main className="workspace-main"><header className="workspace-topbar"><div className="topbar-leading"><button type="button" className="mobile-menu-button" aria-label="Open navigation menu" aria-controls="mobile-navigation" aria-expanded={mobileMenuOpen} onClick={() => setMobileMenuOpen(true)}><Menu size={20} /></button><div className="breadcrumb"><span>Workspace</span><span className="breadcrumb-slash">/</span><strong>{activeLabel}</strong></div></div><div className="topbar-actions"><button className="icon-button" aria-label="Notifications"><Bell size={18} /><span className="notification-dot" /></button><div className="topbar-user"><span className="avatar">{initials}</span><span>{name}</span><ChevronDown size={15} /></div></div></header><div className="workspace-content">{!authorizedNav ? <div className="empty-state-panel"><h2>No workspace sections available</h2><p>Your account does not have access to any workspace sections.</p></div> : currentNav === "sources" ? <UploadStep onOpenPackages={() => navigate("packages")} /> : currentNav === "packages" ? <PackagesPage onOpenBusinessRequirements={() => navigate("business-requirements")} /> : currentNav === "business-requirements" ? <BusinessRequirementsPage onContinueToFunctional={() => navigate("requirements")} /> : currentNav === "traceability" ? <TraceabilityPage /> : currentNav === "requirements" ? <RequirementsPage onContinueToGeneration={openTestCaseGeneration} onOpenBusinessRequirements={() => navigate("business-requirements")} /> : currentNav === "test-cases" ? <TestCaseGenerationPage analysis={currentGenerationAnalysis} onBack={() => setActiveNav("requirements")} /> : currentNav === "test-results" ? <TestResultsPage /> : <div className="empty-state-panel"><div className="empty-state-icon"><Activity size={25} /></div><h2>{currentNav === "overview" ? "Your workspace at a glance" : "Workspace activity"}</h2><p>This view is ready for the next stage of your source-to-test-data workflow. Manage source packages from the sidebar.</p><button className="primary-button" onClick={() => navigate("sources")}>Open sources</button></div>}</div></main>
  </div></>;
}
