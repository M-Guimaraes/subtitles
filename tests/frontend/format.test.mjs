// Pure helpers: no DOM, no fetch. Run with `node --test tests/frontend/`.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  escapeHtml,
  formatProbability,
  jobStatus,
  languagePair,
} from "../../src/nas_subtitles/static/js/utils/format.js";

test("escapeHtml neutralises markup and both quote styles", () => {
  assert.equal(
    escapeHtml(`<img src=x onerror="a('b')">&`),
    "&lt;img src=x onerror=&quot;a(&#39;b&#39;)&quot;&gt;&amp;",
  );
  assert.equal(escapeHtml(null), "");
  assert.equal(escapeHtml(undefined), "");
  assert.equal(escapeHtml(42), "42");
});

test("jobStatus falls back to the raw state instead of throwing", () => {
  assert.equal(jobStatus("failed").tone, "danger");
  assert.deepEqual(jobStatus("brand_new_state"), { label: "brand_new_state", tone: "neutral" });
  assert.equal(jobStatus(undefined).label, "—");
});

test("languagePair shows unknown sides as a question mark", () => {
  assert.equal(languagePair({ source_language: "en", target_language: "pt-BR" }).includes("→"), true);
  assert.equal(languagePair({ target_language: "pt-BR" }).startsWith("?"), true);
  assert.equal(languagePair({}), "—");
});

test("formatProbability handles missing values", () => {
  assert.equal(typeof formatProbability(0.93), "string");
  assert.equal(formatProbability(null), formatProbability(undefined));
});
