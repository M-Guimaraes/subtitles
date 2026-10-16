// startPolling with a minimal `document` stub.
import assert from "node:assert/strict";
import { test } from "node:test";

const listeners = new Map();
globalThis.document = {
  hidden: false,
  addEventListener: (name, fn) => listeners.set(name, fn),
  removeEventListener: (name) => listeners.delete(name),
};

const { startPolling } = await import("../../src/nas_subtitles/static/js/utils/poll.js");

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

test("never overlaps a slow task and stops cleanly", async () => {
  let running = 0;
  let maxRunning = 0;
  let runs = 0;
  const stop = startPolling(async () => {
    running += 1;
    runs += 1;
    maxRunning = Math.max(maxRunning, running);
    await wait(30);
    running -= 1;
  }, 5);
  await wait(80);
  stop();
  const after = runs;
  await wait(40);
  assert.equal(maxRunning, 1);
  assert.ok(after >= 1);
  assert.equal(runs, after);
  assert.equal(listeners.has("visibilitychange"), false);
});

test("pauses while the tab is hidden and refreshes when it is shown", async () => {
  let runs = 0;
  document.hidden = true;
  const stop = startPolling(async () => {
    runs += 1;
  }, 5);
  await wait(30);
  assert.equal(runs, 0);
  document.hidden = false;
  listeners.get("visibilitychange")();
  await wait(1);
  assert.ok(runs >= 1);
  stop();
});
