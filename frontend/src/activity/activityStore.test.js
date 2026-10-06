import assert from "node:assert/strict";
import test from "node:test";

test("running work remains available after another subscriber mounts", async () => {
  const values = new Map();
  globalThis.localStorage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
  globalThis.window = { addEventListener() {} };
  const store = await import(`./activityStore.js?test=${Date.now()}`);
  const id = store.startActivity({ label: "Generating Business Use Cases", section: "business-requirements" });
  assert.equal(store.getActivities()[0].status, "running");
  assert.equal(store.getActivities()[0].section, "business-requirements");
  assert.match(localStorage.getItem("testiq_workspace_activities_v1"), /Generating Business Use Cases/);
  store.completeActivity(id, "Completed successfully.");
  assert.equal(store.getActivities()[0].status, "completed");
});
