import { api } from "../api-client.js";
import { startPolling } from "../utils/poll.js";
import { confirmDialog, showToast } from "../utils/ui.js";
import {
  escapeHtml,
  errorLabel,
  eventLabel,
  formatBool,
  formatClock,
  formatProbability,
  jobKindLabel,
  jobStatus,
  languageName,
  stageLabel,
} from "../utils/format.js";

const ACTION_LABELS = {
  retry: "Tentar novamente",
  cancel: "Cancelar processamento",
  reprocess: "Reprocessar",
};

const ACTION_CONFIRM = {
  cancel: { title: "Cancelar processamento", body: "O job será marcado como cancelado. Essa ação não pode ser desfeita.", danger: true },
  reprocess: {
    title: "Reprocessar",
    body: "O processamento será reenfileirado. Checkpoints e caches válidos poderão ser reutilizados.",
    danger: false,
  },
};

function stageIcon(status) {
  if (status === "done") return '<svg viewBox="0 0 24 24"><path d="M5 13l4 4L19 7"/></svg>';
  if (status === "current")
    return '<svg class="spin" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 9 9"/></svg>';
  return '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/></svg>';
}

function pipelineTimeline(stages) {
  return `
    <ol class="pipeline-timeline">
      ${stages
        .map(
          (row) => `
        <li class="pipeline-step status-${row.status}">
          <span class="pipeline-icon">${stageIcon(row.status)}</span>
          <span class="pipeline-label">${escapeHtml(stageLabel(row.stage))}</span>
          <span class="pipeline-status muted small">${escapeHtml(
            row.status === "done" ? "Concluído" : row.status === "current" ? "Em andamento" : "Pendente",
          )}</span>
        </li>`,
        )
        .join("")}
    </ol>`;
}

function resultCard(payload) {
  const job = payload.job;
  if (job.job_kind === "dubbing") {
    if (!job.output_path) {
      return `<p class="muted">Ainda não disponível. O áudio dublado será gerado ao final do processamento.</p>`;
    }
    return `<p>${escapeHtml(job.output_path)}</p>`;
  }
  if (!job.output_path) {
    return `<p class="muted">Ainda não disponível. O arquivo de legenda será gerado após a conclusão do processamento.</p>`;
  }
  const metrics = payload.metrics || {};
  return `
    <ul class="kv-list">
      <li><span>Arquivo</span><span>${escapeHtml(job.output_path)}</span></li>
      <li><span>Cues</span><span>${metrics.output_cues ?? "—"}</span></li>
      <li><span>Flags de qualidade</span><span>${escapeHtml((metrics.quality_flags || []).join(", ") || "nenhuma")}</span></li>
    </ul>`;
}

function technicalDetails(payload) {
  const job = payload.job;
  return `
    <ul class="kv-list">
      <li><span>ID do job</span><span>${escapeHtml(job.id)}</span></li>
      <li><span>Biblioteca</span><span>${escapeHtml(job.root_id)}</span></li>
      <li><span>Caminho relativo</span><span>${escapeHtml(job.relative_path)}</span></li>
      <li><span>Tentativas</span><span>${job.attempt_count}</span></li>
      <li><span>Código de erro</span><span>${escapeHtml(job.error_code || "nenhum")}</span></li>
      <li><span>Detalhe do erro</span><span>${escapeHtml(job.error_detail || "—")}</span></li>
    </ul>`;
}

function modelsList(models) {
  if (!models.length) return `<p class="muted">Nenhum modelo registrado ainda.</p>`;
  return `<ul class="kv-list">${models
    .map((model) => `<li><span>${escapeHtml(model.kind)}</span><span>${escapeHtml(model.name)}</span></li>`)
    .join("")}</ul>`;
}

function eventsList(events) {
  if (!events.length) return `<p class="muted">Nenhum evento registrado.</p>`;
  return `<ul class="kv-list">${events
    .slice(0, 8)
    .map(
      (event) =>
        `<li><span>${escapeHtml(formatClock(event.created_at))}</span><span>${escapeHtml(eventLabel(event.code))}</span></li>`,
    )
    .join("")}</ul>`;
}

function details(key, title, bodyHtml) {
  return `
    <details class="panel" data-key="${key}">
      <summary>${escapeHtml(title)}</summary>
      <div class="details-body">${bodyHtml}</div>
    </details>`;
}

async function load(root, jobId) {
  const openSections = new Set(
    [...root.querySelectorAll("details[open]")].map((node) => node.dataset.key),
  );
  const payload = await api.job(jobId);
  const job = payload.job;
  const status = jobStatus(job.state);
  const language = payload.language || {};
  const progress = payload.progress || { stage_index: 0, stage_total: 0 };
  const percent = progress.stage_total ? Math.round((progress.stage_index / progress.stage_total) * 100) : 0;
  const current = (payload.stages || []).find((row) => row.status === "current");

  root.innerHTML = `
    <a class="back-link" href="#/jobs">← Voltar para Jobs</a>
    <div class="page-header">
      <div>
        <div class="detail-title-row">
          <h1>${escapeHtml(job.title)}</h1>
          <span class="badge tone-${status.tone}">${escapeHtml(status.label)}</span>
          <span class="badge kind-${escapeHtml(job.job_kind)}">${escapeHtml(jobKindLabel(job.job_kind))}</span>
        </div>
        <p class="muted">${escapeHtml(job.root_id)} / ${escapeHtml(job.relative_path)}</p>
      </div>
      <div class="page-actions" id="detail-actions"></div>
    </div>
    <div class="columns">
      <div class="column-main">
        <section class="panel">
          <h2>Progresso do processamento</h2>
          <p>${escapeHtml(current ? stageLabel(current.stage) : "—")}</p>
          <div class="progress-bar"><div class="progress-fill" style="width:${percent}%"></div></div>
          <p class="muted small">Etapa ${progress.stage_index} de ${progress.stage_total} · progresso aproximado por etapa</p>
        </section>
        <section class="panel">
          <h2>Pipeline de processamento</h2>
          ${pipelineTimeline(payload.stages || [])}
        </section>
        ${details("result", "Resultado", resultCard(payload))}
        ${details("models", "Modelos utilizados", modelsList(payload.models || []))}
        ${details("technical", "Informações técnicas", technicalDetails(payload))}
        ${details("events", `Histórico de eventos (últimos ${(payload.events || []).length})`, eventsList(payload.events || []))}
      </div>
      <div class="column-side">
        <section class="panel">
          <h2>Arquivo e idiomas</h2>
          <ul class="kv-list">
            <li><span>Nome do arquivo</span><span>${escapeHtml(job.title)}</span></li>
            <li><span>Caminho relativo</span><span>${escapeHtml(job.relative_path)}</span></li>
            <li><span>Idioma detectado</span><span>${escapeHtml(
              language.detected_language
                ? `${languageName(language.detected_language)} (${formatProbability(language.detection_probability) || "—"})`
                : "—",
            )}</span></li>
            <li><span>Idioma de destino</span><span>${escapeHtml(languageName(job.target_language) || "—")}</span></li>
            <li><span>Faixa de áudio</span><span>${job.selected_audio_stream_index ?? "—"}</span></li>
            <li><span>Tradução executada</span><span>${escapeHtml(formatBool(language.translation_executed))}</span></li>
          </ul>
        </section>
      </div>
    </div>`;

  root.querySelectorAll("details[data-key]").forEach((node) => {
    if (openSections.has(node.dataset.key)) node.open = true;
  });

  const actionsHost = root.querySelector("#detail-actions");
  actionsHost.innerHTML = (job.actions || [])
    .map(
      (action) =>
        `<button type="button" class="btn ${action === "cancel" ? "danger" : "secondary"}" data-action="${action}">${escapeHtml(ACTION_LABELS[action] || action)}</button>`,
    )
    .join("");
  actionsHost.querySelectorAll("button[data-action]").forEach((button) => {
    button.addEventListener("click", () => runAction(root, jobId, button));
  });
}

async function runAction(root, jobId, button) {
  const action = button.dataset.action;
  const confirmSpec = ACTION_CONFIRM[action];
  if (confirmSpec) {
    const confirmed = await confirmDialog({ ...confirmSpec, confirmLabel: ACTION_LABELS[action] });
    if (!confirmed) return;
  }
  button.disabled = true;
  try {
    if (action === "retry") await api.retry(jobId);
    else if (action === "cancel") await api.cancel(jobId);
    else if (action === "reprocess") await api.reprocess(jobId);
    showToast(`${ACTION_LABELS[action] || action} solicitado.`, "success");
    await load(root, jobId);
  } catch (error) {
    if (error.status === 409) {
      showToast("O estado do job mudou enquanto você agia. Atualizando…", "warning");
      await load(root, jobId);
    } else {
      showToast(error.message || errorLabel(error.code), "error");
    }
  } finally {
    button.disabled = false;
  }
}

export async function renderJobDetail(root, jobId) {
  root.innerHTML = '<p class="loading">Carregando…</p>';
  try {
    await load(root, jobId);
  } catch (error) {
    root.innerHTML = `
      <a class="back-link" href="#/jobs">← Voltar para Jobs</a>
      <p class="empty-state error">${escapeHtml(error.message || "Falha ao carregar o job.")}</p>`;
    return () => {};
  }
  return startPolling(() => load(root, jobId).catch(() => {}), 5000);
}
