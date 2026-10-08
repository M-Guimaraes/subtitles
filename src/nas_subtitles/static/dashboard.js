const QUEUE_STATES = [
  "queued",
  "running",
  "retry_wait",
  "needs_review",
  "ready_to_publish",
];
const HISTORY_STATES = ["completed", "skipped", "failed", "cancelled"];

const state = {
  view: "queue",
  selected: null,
};

const els = {
  listPage: document.getElementById("list-page"),
  detailPage: document.getElementById("detail-page"),
  settingsPage: document.getElementById("settings-page"),
  title: document.getElementById("page-title"),
  subtitle: document.getElementById("page-subtitle"),
  rows: document.getElementById("job-rows"),
  filter: document.getElementById("state-filter"),
  banner: document.getElementById("banner"),
  worker: document.getElementById("worker-pill"),
  detailTitle: document.getElementById("detail-title"),
  detailPath: document.getElementById("detail-path"),
  detailGrid: document.getElementById("detail-grid"),
  detailActions: document.getElementById("detail-actions"),
  settingsGrid: document.getElementById("settings-grid"),
};

document.querySelectorAll("nav button").forEach((button) => {
  button.addEventListener("click", () => showView(button.dataset.view));
});
document.getElementById("refresh").addEventListener("click", () => loadList());
document.getElementById("rescan").addEventListener("click", rescan);
document.getElementById("back").addEventListener("click", () => {
  state.selected = null;
  showView(state.view === "settings" ? "queue" : state.view);
});
els.filter.addEventListener("change", () => loadList());

function showView(view) {
  state.view = view;
  document.querySelectorAll("nav button").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
  });
  els.listPage.classList.toggle("hidden", view === "settings" || state.selected);
  els.detailPage.classList.toggle("hidden", !state.selected || view === "settings");
  els.settingsPage.classList.toggle("hidden", view !== "settings");
  if (view === "settings") {
    state.selected = null;
    loadSettings();
    return;
  }
  if (state.selected) {
    loadDetail(state.selected);
    return;
  }
  els.title.textContent = view === "history" ? "History" : "Queue";
  els.subtitle.textContent =
    view === "history"
      ? "Completed, skipped, failed and cancelled jobs"
      : "Active and waiting jobs";
  fillFilter(view === "history" ? HISTORY_STATES : QUEUE_STATES);
  loadList();
}

function fillFilter(states) {
  const current = els.filter.value;
  els.filter.innerHTML =
    `<option value="">All</option>` +
    states.map((item) => `<option value="${item}">${item}</option>`).join("");
  els.filter.value = states.includes(current) ? current : "";
}

function authHeaders() {
  const token = sessionStorage.getItem("nas-subs-dashboard-token");
  if (!token) return {};
  return { Authorization: `Bearer ${token}` };
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      Accept: "application/json",
      ...authHeaders(),
      ...(options.headers || {}),
    },
  });
  if (response.status === 401) {
    const entered = window.prompt("Dashboard token");
    if (entered) {
      sessionStorage.setItem("nas-subs-dashboard-token", entered);
      return api(path, options);
    }
  }
  const payload = await response.json();
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.message || `request failed (${response.status})`);
  }
  return payload;
}

async function loadOverview() {
  try {
    const overview = await api("/api/health");
    const healthy = overview.worker && overview.worker.healthy;
    els.worker.textContent = healthy ? "worker up" : "worker down";
    els.worker.className = `pill ${healthy ? "ok" : "bad"}`;
  } catch (error) {
    els.worker.textContent = "api error";
    els.worker.className = "pill bad";
  }
}

async function loadList() {
  await loadOverview();
  const view = state.view === "history" ? "history" : "queue";
  const selectedState = els.filter.value;
  const query = new URLSearchParams({ view, limit: "200" });
  if (selectedState) query.set("state", selectedState);
  try {
    const payload = await api(`/api/jobs?${query}`);
    renderRows(payload.jobs || []);
    hideBanner();
  } catch (error) {
    els.rows.innerHTML = `<tr><td colspan="7" class="muted">${escapeHtml(
      error.message,
    )}</td></tr>`;
  }
}

function renderRows(jobs) {
  if (!jobs.length) {
    els.rows.innerHTML =
      '<tr><td colspan="7" class="muted">No jobs in this view.</td></tr>';
    return;
  }
  els.rows.innerHTML = jobs
    .map((job) => {
      const source = formatSource(job);
      return `<tr data-id="${job.id}">
        <td>
          <div>${escapeHtml(job.title)}</div>
          <div class="muted">${escapeHtml(job.root_id)} / ${escapeHtml(job.relative_path)}</div>
        </td>
        <td><span class="state ${job.state}">${job.state}</span></td>
        <td>${escapeHtml(job.current_stage || "—")}</td>
        <td>${escapeHtml(source)}</td>
        <td>${escapeHtml(job.target_language || "—")}</td>
        <td>${escapeHtml(formatTime(job.updated_at))}</td>
        <td>${escapeHtml(job.error_code || "")}</td>
      </tr>`;
    })
    .join("");
  els.rows.querySelectorAll("tr[data-id]").forEach((row) => {
    row.addEventListener("click", () => {
      state.selected = row.dataset.id;
      showView(state.view);
    });
  });
}

async function loadDetail(jobId) {
  els.listPage.classList.add("hidden");
  els.detailPage.classList.remove("hidden");
  try {
    const payload = await api(`/api/jobs/${jobId}`);
    const job = payload.job;
    els.detailTitle.textContent = job.title;
    els.detailPath.textContent = `${job.root_id} / ${job.relative_path}`;
    els.detailActions.innerHTML = (payload.actions || [])
      .map((action) => `<button type="button" data-action="${action}">${labelAction(action)}</button>`)
      .join("");
    els.detailActions.querySelectorAll("button").forEach((button) => {
      button.addEventListener("click", () => runAction(jobId, button.dataset.action));
    });
    const language = payload.language || {};
    const metrics = payload.metrics || {};
    els.detailGrid.innerHTML = [
      card("Status", [
        `State: ${job.state}`,
        `Stage: ${job.current_stage || "—"}`,
        `Attempts: ${job.attempt_count}`,
        `Updated: ${formatTime(job.updated_at)}`,
        job.error_code ? `Error: ${job.error_code}` : "Error: none",
        job.error_detail || "",
      ]),
      card("Language", [
        `Source: ${language.source_language || job.source_language || "—"}`,
        `Detected: ${language.detected_language || "—"} (${formatProb(language.detection_probability)})`,
        `Confident: ${formatBool(language.source_language_confident)}`,
        `Decision: ${language.source_language_source || "—"}`,
        `Target: ${language.target_language || job.target_language}`,
        `Translation: ${formatBool(language.translation_executed)}`,
        language.source_language_reason || "",
      ]),
      card("Audio / output", [
        `Stream index: ${job.selected_audio_stream_index ?? "—"}`,
        `Stream language: ${language.stream_language || "—"}`,
        `Output: ${job.output_path || "—"}`,
        `Cues: ${metrics.output_cues ?? "—"}`,
        `Quality flags: ${(metrics.quality_flags || []).join(", ") || "none"}`,
      ]),
      card(
        "Models",
        (payload.models || []).map((model) => `${model.kind}: ${model.name}`),
      ),
      stagesCard(payload.stages || []),
      card(
        "Recent events",
        (payload.events || []).slice(0, 8).map((event) => `${event.code} (${event.level})`),
      ),
    ].join("");
    hideBanner();
  } catch (error) {
    showBanner(error.message);
  }
}

async function loadSettings() {
  await loadOverview();
  try {
    const settings = await api("/api/settings");
    els.settingsGrid.innerHTML = [
      card("Automatic processing", [
        settings.automatic_processing ? "Worker heartbeat is recent" : "Worker is not healthy",
        settings.worker ? settings.worker.reason : "",
      ]),
      card(
        "Media roots",
        (settings.media_roots || []).map((root) => `${root.root_id}`),
      ),
      card("Languages", [
        `Source: ${settings.languages.source}`,
        `Target: ${settings.languages.target}`,
        `Low confidence: ${settings.languages.low_confidence}`,
      ]),
      card("Audio", [
        `Stream: ${settings.audio.stream}`,
        `Preferred: ${(settings.audio.preferred_languages || []).join(", ")}`,
      ]),
      card("Processing", [
        `Scan interval: ${settings.scan_interval_seconds}s`,
        `Existing subtitle policy: ${settings.existing_subtitle_policy}`,
        `Publish mode: ${settings.publish_mode}`,
        `ASR model: ${settings.asr_model}`,
      ]),
      card("Dashboard", [
        `Bind: ${settings.dashboard.bind}:${settings.dashboard.port}`,
        `Token configured: ${settings.dashboard.token_configured ? "yes" : "no"}`,
        "Do not expose this UI on the public internet without authentication.",
      ]),
    ].join("");
  } catch (error) {
    els.settingsGrid.innerHTML = `<div class="card">${escapeHtml(error.message)}</div>`;
  }
}

async function runAction(jobId, action) {
  try {
    await api(`/api/jobs/${jobId}/${action}`, { method: "POST" });
    showBanner(`${labelAction(action)} requested.`);
    await loadDetail(jobId);
  } catch (error) {
    showBanner(error.message);
  }
}

async function rescan() {
  try {
    const payload = await api("/api/scan", { method: "POST" });
    const scan = payload.scan || {};
    showBanner(`Scan examined ${scan.examined || 0}, enqueued ${scan.enqueued || 0}.`);
    await loadList();
  } catch (error) {
    showBanner(error.message);
  }
}

function card(title, lines) {
  const items = (lines || []).filter(Boolean);
  const body = items.length
    ? `<ul>${items.map((line) => `<li>${escapeHtml(String(line))}</li>`).join("")}</ul>`
    : `<p class="muted">None</p>`;
  return `<article class="card"><h2>${escapeHtml(title)}</h2>${body}</article>`;
}

function stagesCard(stages) {
  const chips = stages
    .map((item) => `<span class="stage ${item.status}">${escapeHtml(item.stage)}</span>`)
    .join("");
  return `<article class="card"><h2>Pipeline</h2><div class="stages">${chips}</div></article>`;
}

function formatSource(job) {
  const language = job.detected_language || job.source_language;
  if (!language) return "—";
  const probability = formatProb(job.detection_probability);
  return probability === "—" ? language : `${language} ${probability}`;
}

function formatProb(value) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  if (Number.isNaN(number)) return "—";
  return `${Math.round(number * 100)}%`;
}

function formatBool(value) {
  if (value === true) return "yes";
  if (value === false) return "no";
  return "—";
}

function formatTime(value) {
  if (!value) return "—";
  return value.replace("T", " ").replace(/\.\d+/, "").replace("+00:00", "Z");
}

function labelAction(action) {
  if (action === "retry") return "Retry";
  if (action === "cancel") return "Cancel";
  if (action === "reprocess") return "Reprocess";
  return action;
}

function showBanner(message) {
  els.banner.textContent = message;
  els.banner.classList.remove("hidden");
}

function hideBanner() {
  els.banner.classList.add("hidden");
  els.banner.textContent = "";
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

showView("queue");
setInterval(() => {
  if (!state.selected && state.view !== "settings") loadList();
}, 15000);
