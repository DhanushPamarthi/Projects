import { useSyncExternalStore } from "react";

const STORAGE_KEY = "testiq_workspace_activities_v1";
const TAB_SESSION_KEY = "testiq_workspace_tab_session_v1";
const MAX_ACTIVITIES = 30;
const STALE_ACTIVITY_MS = 20 * 60 * 1000;
const listeners = new Set();

function getTabSessionId() {
  if (typeof sessionStorage === "undefined") return `tab_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
  let id = sessionStorage.getItem(TAB_SESSION_KEY);
  if (!id) {
    id = `tab_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
    sessionStorage.setItem(TAB_SESSION_KEY, id);
  }
  return id;
}

const tabSessionId = getTabSessionId();

function loadActivities() {
  try {
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    if (!Array.isArray(stored)) return [];
    let changed = false;
    const currentTime = Date.now();
    const normalized = stored.slice(0, MAX_ACTIVITIES).map((item) => {
      const lastUpdate = new Date(item.updatedAt || item.startedAt || 0).getTime();
      if (item.status !== "running" || !Number.isFinite(lastUpdate) || currentTime - lastUpdate <= STALE_ACTIVITY_MS) return item;
      changed = true;
      const finishedAt = new Date().toISOString();
      return { ...item, status: "failed", finishedAt, updatedAt: finishedAt,
        error: "The activity stopped reporting progress.", detail: "The activity was interrupted or timed out. Retry if needed." };
    });
    if (changed) localStorage.setItem(STORAGE_KEY, JSON.stringify(normalized));
    return normalized;
  } catch {
    localStorage.removeItem(STORAGE_KEY);
    return [];
  }
}

let activities = loadActivities();

function publish(next) {
  activities = next.slice(0, MAX_ACTIVITIES);
  localStorage.setItem(STORAGE_KEY, JSON.stringify(activities));
  listeners.forEach((listener) => listener());
}

function timestamp() { return new Date().toISOString(); }
function makeId() { return `activity_${Date.now()}_${Math.random().toString(36).slice(2, 9)}`; }

export function startActivity({ id, label, detail = "", section = "activity", progress = null, kind = "operation" }) {
  const activityId = id || makeId();
  const now = timestamp();
  const existing = activities.find((item) => item.id === activityId);
  const next = { id: activityId, label, detail, section, kind, progress, status: "running", tabSessionId, startedAt: existing?.startedAt || now, updatedAt: now, finishedAt: null, error: "" };
  publish([next, ...activities.filter((item) => item.id !== activityId)]);
  return activityId;
}

export function updateActivity(id, changes = {}) {
  const current = activities.find((item) => item.id === id);
  if (!current) return;
  const next = { ...current, ...changes, id, updatedAt: timestamp() };
  publish([next, ...activities.filter((item) => item.id !== id)]);
}

export function completeActivity(id, detail) {
  const current = activities.find((item) => item.id === id);
  updateActivity(id, { status: "completed", progress: 100, detail: detail ?? current?.detail ?? "", finishedAt: timestamp(), error: "" });
}

export function failActivity(id, error) {
  const message = error instanceof Error ? error.message : String(error || "The operation failed.");
  updateActivity(id, { status: "failed", finishedAt: timestamp(), error: message, detail: message });
}

export function dismissActivity(id) { publish(activities.filter((item) => item.id !== id)); }
export function clearFinishedActivities() { publish(activities.filter((item) => item.status === "running")); }
export function reconcileStaleActivities() {
  const currentTime = Date.now();
  let changed = false;
  const next = activities.map((item) => {
    const lastUpdate = new Date(item.updatedAt || item.startedAt || 0).getTime();
    if (item.status !== "running" || !Number.isFinite(lastUpdate) || currentTime - lastUpdate <= STALE_ACTIVITY_MS) return item;
    changed = true;
    const finishedAt = timestamp();
    return { ...item, status: "failed", finishedAt, updatedAt: finishedAt,
      error: "The activity stopped reporting progress.", detail: "The activity was interrupted or timed out. Retry if needed." };
  });
  if (changed) publish(next);
}
export function recoverInterruptedTabActivities() {
  let changed = false;
  const finishedAt = timestamp();
  const next = activities.map((item) => {
    if (item.status !== "running" || (item.tabSessionId && item.tabSessionId !== tabSessionId)) return item;
    changed = true;
    return { ...item, status: "failed", finishedAt, updatedAt: finishedAt,
      error: "The page was reloaded before this activity completed.",
      detail: "The previous request was interrupted. Retry the operation if it is still needed." };
  });
  if (changed) publish(next);
}
export function getActivities() { return activities; }
export function subscribeActivities(listener) { listeners.add(listener); return () => listeners.delete(listener); }
export function useActivities() { return useSyncExternalStore(subscribeActivities, getActivities, getActivities); }

if (typeof window !== "undefined") {
  window.addEventListener("pagehide", recoverInterruptedTabActivities);
  window.addEventListener("storage", (event) => {
    if (event.key !== STORAGE_KEY) return;
    activities = loadActivities();
    listeners.forEach((listener) => listener());
  });
}
