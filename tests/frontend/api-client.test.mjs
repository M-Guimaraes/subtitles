// The API client with fetch and sessionStorage stubbed.
import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";

const store = new Map();
globalThis.sessionStorage = {
  getItem: (key) => (store.has(key) ? store.get(key) : null),
  setItem: (key, value) => store.set(key, String(value)),
};

const { api, onUnauthorized, setToken, ApiError } = await import(
  "../../src/nas_subtitles/static/js/api-client.js"
);

function jsonResponse(status, payload) {
  return { status, ok: status >= 200 && status < 300, json: async () => payload };
}

// Answers 401 until the expected bearer token is sent.
function fakeServer(expectedToken) {
  const calls = [];
  globalThis.fetch = async (path, options) => {
    calls.push({ path, options });
    if (options.headers.Authorization !== `Bearer ${expectedToken}`) {
      return jsonResponse(401, { ok: false, error_code: "permission_denied", message: "token" });
    }
    return jsonResponse(200, { ok: true, path });
  };
  return calls;
}

beforeEach(() => store.clear());

test("concurrent 401s share a single token prompt", async () => {
  fakeServer("secret");
  let prompts = 0;
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  onUnauthorized(async () => {
    prompts += 1;
    await gate;
    setToken("secret");
    return true;
  });
  const both = Promise.all([api.health(), api.overview()]);
  // Let both requests reach the 401 before the user "types" the token.
  await new Promise((resolve) => setTimeout(resolve, 10));
  release();
  const [health, overview] = await both;
  assert.equal(prompts, 1);
  assert.equal(health.path, "/api/health");
  assert.equal(overview.path, "/api/overview");
});

test("a later 401 prompts again after the first login settled", async () => {
  fakeServer("second");
  let prompts = 0;
  onUnauthorized(async () => {
    prompts += 1;
    setToken(prompts === 1 ? "wrong" : "second");
    return true;
  });
  await assert.rejects(api.health(), ApiError);
  await api.health();
  assert.equal(prompts, 2);
});

test("cancelling the prompt surfaces the 401 as an ApiError", async () => {
  fakeServer("secret");
  onUnauthorized(async () => false);
  await assert.rejects(api.jobs({ view: "all" }), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 401);
    assert.equal(error.code, "permission_denied");
    return true;
  });
});

test("JSON bodies are sent with the JSON content type the server requires", async () => {
  const calls = fakeServer("secret");
  setToken("secret");
  await api.deleteJobs(["a", "b"]);
  const [call] = calls;
  assert.equal(call.options.method, "POST");
  assert.equal(call.options.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(call.options.body), { ids: ["a", "b"] });
});

test("job ids are URL-encoded into the path", async () => {
  const calls = fakeServer("secret");
  setToken("secret");
  await api.retry("a/b?c");
  assert.equal(calls[0].path, "/api/jobs/a%2Fb%3Fc/retry");
});
