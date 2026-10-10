import { api } from "../api-client.js";
import { startPolling } from "../utils/poll.js";
import {
  escapeHtml,
  formatRelativeTime,
  jobKindLabel,
  jobStatus,
  languageName,
  languagePair,
} from "../utils/format.js";

const QUICK_FILTERS = [
  { key: "all", label: "Todos", state: null },
  { key: "running", label: "Processando", state: "running" },
  { key: "waiting", label: "Aguardando", state: "queued,retry_wait" },
  { key: "completed", label: "Concluídos", state: "completed" },
  { key: "ready_to_publish", label: "Aguardando publicação", state: "ready_to_publish" },
  { key: "attention", label: "Problemas", state: "failed,needs_review" },
];

const SORT_OPTIONS = [
  { value: "updated_desc", label: "Mais recentes" },
  { value: "updated_asc", label: "Mais antigos" },
  { value: "created_desc", label: "Criados recentemente" },
  { value: "created_asc", label: "Criados há mais tempo" },
  { value: "title_asc", label: "Arquivo (A-Z)" },
  { value: "title_desc", label: "Arquivo (Z-A)" },
];

function quickFilterKeyFor(state) {
  const match = QUICK_FILTERS.find((item) => item.state === state);
  return match ? match.key : "all";
}

function parseState(params) {
  return params.state || null;
}

function progressCell(job) {
  const status = jobStatus(job.state);
  if (job.state === "running" || job.state === "retry_wait") {
    return '<div class="progress-bar indeterminate"><div class="progress-fill"></div></div>';
  }
  if (job.state === "completed" || job.state === "ready_to_publish" || job.state === "skipped") {
    return '<div class="progress-bar"><div class="progress-fill" style="width:100%"></div></div>';
  }
  return `<span class="muted">—</span>`;
}

function row(job) {
  const status = jobStatus(job.state);
  const folder = job.relative_path.includes("/")
    ? job.relative_path.slice(0, job.relative_path.lastIndexOf("/") + 1)
    : "";
  return `
    <tr data-id="${escapeHtml(job.id)}">
      <td>
        <div class="file-title">${escapeHtml(job.title)}</div>
        <div class="muted small">${escapeHtml(folder)}</div>
      </td>
      <td><span class="badge kind-${escapeHtml(job.job_kind)}">${escapeHtml(jobKindLabel(job.job_kind))}</span></td>
      <td>${escapeHtml(languagePair(job))}</td>
      <td><span class="badge tone-${status.tone}">${escapeHtml(status.label)}</span></td>
      <td>${progressCell(job)}</td>
      <td class="muted small">${escapeHtml(formatRelativeTime(job.updated_at))}</td>
    </tr>`;
}

function paginationControls(pagination) {
  const { limit, offset, total } = pagination;
  const page = Math.floor(offset / limit) + 1;
  const pageCount = Math.max(1, Math.ceil(total / limit));
  const start = total === 0 ? 0 : offset + 1;
  const end = Math.min(total, offset + limit);
  return `
    <div class="pagination">
      <span class="muted small">Exibindo ${start}–${end} de ${total}</span>
      <div class="pagination-buttons">
        <button type="button" class="btn secondary" data-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>Anterior</button>
        <span class="muted small">Página ${page} de ${pageCount}</span>
        <button type="button" class="btn secondary" data-page="${page + 1}" ${page >= pageCount ? "disabled" : ""}>Próximo</button>
      </div>
    </div>`;
}

function stateQuery(params) {
  return {
    search: params.search || null,
    kind: params.kind || null,
    target_language: params.target_language || null,
    sort: params.sort || "updated_desc",
    state: parseState(params),
    limit: params.limit || 20,
    offset: params.offset || 0,
  };
}

function pushParams(params) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value) query.set(key, String(value));
  }
  const text = query.toString();
  location.hash = `/jobs${text ? `?${text}` : ""}`;
}

async function loadCounts(root) {
  const [overview, all] = await Promise.all([api.overview(), api.jobs({ limit: 1 })]);
  const counts = {
    all: all.pagination.total,
    running: overview.stats.running,
    waiting: overview.stats.waiting,
    completed: overview.stats.completed,
    ready_to_publish: overview.stats.ready_to_publish,
    attention: overview.stats.attention,
  };
  root.querySelectorAll("[data-count]").forEach((node) => {
    node.textContent = counts[node.dataset.count] ?? 0;
  });
}

async function loadTable(root, params) {
  const tbody = root.querySelector("#job-rows");
  const payload = await api.jobs(stateQuery(params));
  tbody.innerHTML = payload.jobs.length
    ? payload.jobs.map(row).join("")
    : `<tr><td colspan="6" class="empty-state">Nenhum job encontrado para esse filtro.</td></tr>`;
  tbody.querySelectorAll("tr[data-id]").forEach((node) => {
    node.addEventListener("click", () => {
      location.hash = `/jobs/${encodeURIComponent(node.dataset.id)}`;
    });
  });
  const paginationHost = root.querySelector("#pagination-host");
  paginationHost.innerHTML = paginationControls(payload.pagination);
  paginationHost.querySelectorAll("[data-page]").forEach((button) => {
    button.addEventListener("click", () => {
      const page = Number(button.dataset.page);
      if (page < 1) return;
      pushParams({ ...params, offset: (page - 1) * (params.limit || 20) });
    });
  });
}

export async function renderJobs(root, params) {
  const activeKey = quickFilterKeyFor(parseState(params));
  root.innerHTML = `
    <div class="page-header">
      <div>
        <h1>Jobs</h1>
        <p class="muted">Gerencie legendas e dublagens para todos os seus arquivos.</p>
      </div>
      <div class="page-actions">
        <button type="button" class="btn primary" id="rescan-btn">Varredura da biblioteca</button>
        <button type="button" class="btn secondary" id="refresh-btn">Atualizar</button>
      </div>
    </div>
    <div class="toolbar">
      <input type="search" id="search-input" placeholder="Buscar por nome do arquivo…" value="${escapeHtml(params.search || "")}" />
      <label class="field">Tipo
        <select id="kind-select">
          <option value="">Todos</option>
          <option value="subtitles">Legenda</option>
          <option value="dubbing">Dublagem</option>
        </select>
      </label>
      <label class="field">Idioma
        <select id="target-select" disabled>
          <option value="">Todos</option>
        </select>
      </label>
      <label class="field">Ordenar
        <select id="sort-select">
          ${SORT_OPTIONS.map(
            (option) =>
              `<option value="${option.value}" ${option.value === (params.sort || "updated_desc") ? "selected" : ""}>${option.label}</option>`,
          ).join("")}
        </select>
      </label>
    </div>
    <div class="tabs">
      ${QUICK_FILTERS.map(
        (filter) => `
        <button type="button" class="tab ${filter.key === activeKey ? "active" : ""}" data-state="${filter.state || ""}">
          ${filter.label} <span class="tab-count" data-count="${filter.key}">0</span>
        </button>`,
      ).join("")}
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Arquivo</th>
            <th>Tipo</th>
            <th>Idiomas</th>
            <th>Status</th>
            <th>Progresso</th>
            <th>Atualizado</th>
          </tr>
        </thead>
        <tbody id="job-rows">
          <tr><td colspan="6" class="loading">Carregando…</td></tr>
        </tbody>
      </table>
    </div>
    <div id="pagination-host"></div>`;

  root.querySelector("#kind-select").value = params.kind || "";
  populateTargetLanguages(root, params);
  root.querySelector("#target-select").addEventListener("change", (event) => {
    pushParams({ ...params, target_language: event.target.value, offset: 0 });
  });
  root.querySelector("#search-input").addEventListener(
    "input",
    debounce((event) => {
      pushParams({ ...params, search: event.target.value, offset: 0 });
    }, 300),
  );
  root.querySelector("#kind-select").addEventListener("change", (event) => {
    pushParams({ ...params, kind: event.target.value, offset: 0 });
  });
  root.querySelector("#sort-select").addEventListener("change", (event) => {
    pushParams({ ...params, sort: event.target.value, offset: 0 });
  });
  root.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => {
      pushParams({ ...params, state: button.dataset.state || null, offset: 0 });
    });
  });
  root.querySelector("#refresh-btn").addEventListener("click", () => {
    loadTable(root, params);
    loadCounts(root);
  });
  root.querySelector("#rescan-btn").addEventListener("click", async (event) => {
    event.currentTarget.disabled = true;
    try {
      await api.scan();
      await loadTable(root, params);
      await loadCounts(root);
    } finally {
      event.currentTarget.disabled = false;
    }
  });

  await Promise.all([loadTable(root, params), loadCounts(root)]);
  return startPolling(() => loadTable(root, params).catch(() => {}), 10000);
}

async function populateTargetLanguages(root, params) {
  try {
    const settings = await api.settings();
    const select = root.querySelector("#target-select");
    const targets = settings.languages.targets || [];
    select.innerHTML =
      `<option value="">Todos</option>` +
      targets
        .map(
          (code) =>
            `<option value="${escapeHtml(code)}" ${code === params.target_language ? "selected" : ""}>${escapeHtml(languageName(code) || code)}</option>`,
        )
        .join("");
    select.disabled = false;
  } catch {
    // Settings failing to load should not block the job table itself.
  }
}

function debounce(fn, delay) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}
