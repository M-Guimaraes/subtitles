import { api } from "../api-client.js";
import { startPolling } from "../utils/poll.js";
import { showToast } from "../utils/ui.js";
import {
  escapeHtml,
  errorLabel,
  eventLabel,
  formatClock,
  formatRelativeTime,
  jobKindLabel,
  languagePair,
  stageLabel,
} from "../utils/format.js";

const STAT_CARDS = [
  { key: "running", label: "Processando", icon: "spinner", filter: { state: "running" } },
  { key: "waiting", label: "Aguardando", icon: "clock", filter: { state: "queued,retry_wait" } },
  { key: "completed", label: "Concluídos", icon: "check", filter: { state: "completed" } },
  {
    key: "ready_to_publish",
    label: "Aguardando publicação",
    icon: "upload",
    filter: { state: "ready_to_publish" },
  },
  { key: "attention", label: "Problemas", icon: "warning", filter: { state: "failed,needs_review" } },
];

function statCard(stats, spec) {
  const value = stats[spec.key === "ready_to_publish" ? "ready_to_publish" : spec.key] ?? 0;
  const query = new URLSearchParams(spec.filter).toString();
  return `
    <a class="stat-card tone-${spec.key}" href="#/jobs?${query}">
      <span class="stat-icon">${icon(spec.icon)}</span>
      <span class="stat-body">
        <span class="stat-value">${value}</span>
        <span class="stat-label">${spec.label}</span>
      </span>
    </a>`;
}

function icon(name) {
  const icons = {
    spinner: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 9 9"/></svg>',
    clock: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>',
    check: '<svg viewBox="0 0 24 24"><path d="M5 13l4 4L19 7"/></svg>',
    upload: '<svg viewBox="0 0 24 24"><path d="M12 19V5M5 12l7-7 7 7"/></svg>',
    warning: '<svg viewBox="0 0 24 24"><path d="M12 3 2 20h20L12 3Z"/><path d="M12 10v4M12 17h.01"/></svg>',
  };
  return icons[name] || "";
}

function emptyRow(message) {
  return `<p class="empty-state">${escapeHtml(message)}</p>`;
}

function activeJobCard(job, detail) {
  const stages = (detail && detail.stages) || [];
  const progress = (detail && detail.progress) || { stage_index: 0, stage_total: stages.length };
  const current = stages.find((row) => row.status === "current");
  const percent = progress.stage_total
    ? Math.round((progress.stage_index / progress.stage_total) * 100)
    : 0;
  return `
    <a class="active-job" href="#/jobs/${encodeURIComponent(job.id)}">
      <div class="active-job-head">
        <span class="badge kind-${escapeHtml(job.job_kind)}">${escapeHtml(jobKindLabel(job.job_kind))}</span>
        <span class="muted small">${escapeHtml(languagePair(job))}</span>
      </div>
      <div class="active-job-title">${escapeHtml(job.title)}</div>
      <div class="active-job-stage">${escapeHtml(current ? stageLabel(current.stage) : "—")}</div>
      <div class="progress-bar"><div class="progress-fill" style="width:${percent}%"></div></div>
      <div class="muted small">Etapa ${progress.stage_index} de ${progress.stage_total}</div>
    </a>`;
}

function attentionJobCard(job) {
  const reason = job.error_detail || errorLabel(job.error_code);
  return `
    <a class="attention-job" href="#/jobs/${encodeURIComponent(job.id)}">
      <div class="attention-job-head">
        <span class="attention-title">${escapeHtml(job.title)}</span>
        <span class="badge tone-${job.state === "failed" ? "danger" : "warning"}">${escapeHtml(
          job.state === "failed" ? "Falhou" : "Revisão necessária",
        )}</span>
      </div>
      <div class="muted small">${escapeHtml(reason)}</div>
      <div class="muted small">${escapeHtml(formatRelativeTime(job.updated_at))}</div>
    </a>`;
}

function activityRow(item) {
  return `
    <a class="activity-row" href="${item.job_id ? `#/jobs/${encodeURIComponent(item.job_id)}` : "#/jobs"}">
      <span class="activity-time">${escapeHtml(formatClock(item.created_at))}</span>
      <span class="activity-dot level-${escapeHtml(item.level)}"></span>
      <span class="activity-body">
        <span class="activity-title">${escapeHtml(item.title || "—")}</span>
        <span class="muted small">${escapeHtml(eventLabel(item.code))}</span>
      </span>
    </a>`;
}

async function fetchWithProgress(job) {
  try {
    const detail = await api.job(job.id);
    return detail.job ? detail : null;
  } catch {
    return null;
  }
}

async function load(root) {
  const overview = await api.overview();
  const activeDetails = await Promise.all(overview.active_jobs.map((job) => fetchWithProgress(job)));
  root.innerHTML = `
    <div class="page-header">
      <div>
        <h1>Visão Geral</h1>
        <p class="muted">Acompanhe o processamento dos seus arquivos de mídia.</p>
      </div>
      <div class="page-actions">
        <button type="button" class="btn primary" id="rescan-btn">${icon("clock")} Varredura da biblioteca</button>
      </div>
    </div>
    <div class="stat-grid">
      ${STAT_CARDS.map((spec) => statCard(overview.stats, spec)).join("")}
    </div>
    <div class="columns">
      <div class="column-main">
        <section class="panel">
          <div class="panel-head">
            <h2>Processamento atual</h2>
            <a class="link" href="#/jobs?state=running">Ver todos →</a>
          </div>
          ${
            overview.active_jobs.length
              ? overview.active_jobs
                  .map((job, index) => activeJobCard(job, activeDetails[index]))
                  .join("")
              : emptyRow("Nenhum job ativo no momento.")
          }
        </section>
        <section class="panel">
          <div class="panel-head">
            <h2>Precisam de atenção</h2>
            <a class="link" href="#/jobs?state=failed,needs_review">Ver todos →</a>
          </div>
          ${
            overview.attention_jobs.length
              ? overview.attention_jobs.map(attentionJobCard).join("")
              : emptyRow("Nenhum problema encontrado.")
          }
        </section>
      </div>
      <div class="column-side">
        <section class="panel">
          <div class="panel-head">
            <h2>Atividade recente</h2>
          </div>
          ${
            overview.recent_activity.length
              ? overview.recent_activity.map(activityRow).join("")
              : emptyRow("Nenhuma atividade recente.")
          }
        </section>
      </div>
    </div>`;

  root.querySelector("#rescan-btn").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await api.scan();
      const scan = result.scan || {};
      showToast(`Varredura concluída: ${scan.examined || 0} examinados, ${scan.enqueued || 0} enfileirados.`, "success");
      await load(root);
    } catch (error) {
      showToast(error.message || "Falha ao varrer a biblioteca.", "error");
    } finally {
      button.disabled = false;
    }
  });
}

export async function renderOverview(root) {
  root.innerHTML = '<p class="loading">Carregando…</p>';
  try {
    await load(root);
  } catch (error) {
    root.innerHTML = `<p class="empty-state error">${escapeHtml(error.message || "Falha ao carregar.")}</p>`;
  }
  return startPolling(() => load(root).catch(() => {}), 5000);
}
