import { api } from "../api-client.js";
import { escapeHtml, languageName } from "../utils/format.js";
import { confirmDialog, showToast } from "../utils/ui.js";

function card(title, rows) {
  return `
    <section class="panel settings-card">
      <h2>${escapeHtml(title)}</h2>
      <ul class="kv-list">
        ${rows.map(([key, value]) => `<li><span>${escapeHtml(key)}</span><span>${value}</span></li>`).join("")}
      </ul>
    </section>`;
}

function mediaRootsCard(state) {
  const { settings, editing, paths, restartRequired } = state;
  const restart = restartRequired
    ? `<div class="banner warning">Salvo. Reinicie o worker para que ele passe a usar as bibliotecas novas; até lá ele só enxerga as antigas.</div>`
    : "";
  if (!editing) {
    const rows = (settings.media_roots || []).map((root_) => [root_.root_id, escapeHtml(root_.path)]);
    const actions = settings.writable
      ? `<div class="card-actions">
           <button class="btn secondary" type="button" data-action="edit-roots">Editar</button>
           ${
             settings.media_roots_overridden && settings.can_reset_media_roots
               ? '<button class="btn secondary" type="button" data-action="reset-roots">Restaurar do config.yaml</button>'
               : ""
           }
         </div>`
      : `<p class="muted">Edição indisponível: o dashboard está exposto sem token. Defina <code>dashboard.token</code> ou use bind em loopback.</p>`;
    const origin = settings.media_roots_overridden
      ? '<p class="muted">Valor salvo pelo dashboard (sobrepõe o <code>config.yaml</code>).</p>'
      : "";
    return `<section class="panel settings-card" id="media-roots-card">
        <h2>Bibliotecas de mídia</h2>
        <ul class="kv-list">${rows.map(([key, value]) => `<li><span>${escapeHtml(key)}</span><span>${value}</span></li>`).join("")}</ul>
        ${origin}${restart}${actions}
      </section>`;
  }
  const inputs = paths
    .map(
      (value, index) => `<div class="root-row">
        <input type="text" value="${escapeHtml(value)}" data-root-index="${index}" aria-label="Caminho da biblioteca ${index + 1}" placeholder="/caminho/absoluto" />
        <button class="btn secondary" type="button" data-action="remove-root" data-root-index="${index}" aria-label="Remover biblioteca">✕</button>
      </div>`,
    )
    .join("");
  return `<section class="panel settings-card" id="media-roots-card">
      <h2>Bibliotecas de mídia</h2>
      <p class="muted">Caminhos como o serviço os enxerga (dentro do contêiner, se for Docker).</p>
      ${inputs}
      <p class="form-error" id="roots-error" role="alert"></p>
      <div class="card-actions">
        <button class="btn secondary" type="button" data-action="add-root">+ Adicionar biblioteca</button>
        <button class="btn primary" type="button" data-action="save-roots">Salvar</button>
        <button class="btn secondary" type="button" data-action="cancel-roots">Cancelar</button>
      </div>
    </section>`;
}

function bindMediaRoots(root, state) {
  const host = () => root.querySelector("#media-roots-card");
  const redraw = () => {
    host().outerHTML = mediaRootsCard(state);
    bind();
  };
  const readInputs = () =>
    [...host().querySelectorAll("input[data-root-index]")].map((input) => input.value.trim());
  const showError = (message) => {
    const node = host().querySelector("#roots-error");
    if (node) node.textContent = message;
  };

  async function save() {
    const paths = readInputs().filter(Boolean);
    if (!paths.length) {
      showError("Informe ao menos uma biblioteca.");
      return;
    }
    const ok = await confirmDialog({
      title: "Salvar bibliotecas?",
      body: "O worker precisa ser reiniciado para usar as novas bibliotecas. Jobs em andamento nas bibliotecas removidas bloqueiam a alteração.",
      confirmLabel: "Salvar",
    });
    if (!ok) return;
    try {
      state.settings = await api.updateMediaRoots({ paths });
      state.editing = false;
      state.restartRequired = true;
      showToast("Bibliotecas salvas.", "success");
      redraw();
    } catch (error) {
      showError(error.message || "Falha ao salvar.");
    }
  }

  async function reset() {
    const ok = await confirmDialog({
      title: "Restaurar bibliotecas?",
      body: "Volta a usar as bibliotecas do config.yaml. O worker precisa ser reiniciado.",
      confirmLabel: "Restaurar",
    });
    if (!ok) return;
    try {
      state.settings = await api.updateMediaRoots({ reset: true });
      state.restartRequired = true;
      showToast("Bibliotecas restauradas.", "success");
      redraw();
    } catch (error) {
      showToast(error.message || "Falha ao restaurar.", "error");
    }
  }

  function bind() {
    host().addEventListener("click", (event) => {
      const button = event.target.closest("button[data-action]");
      if (!button) return;
      const action = button.dataset.action;
      if (action === "edit-roots") {
        state.editing = true;
        state.paths = (state.settings.media_roots || []).map((item) => item.path);
        redraw();
      } else if (action === "cancel-roots") {
        state.editing = false;
        redraw();
      } else if (action === "add-root") {
        state.paths = [...readInputs(), ""];
        redraw();
      } else if (action === "remove-root") {
        const values = readInputs();
        values.splice(Number(button.dataset.rootIndex), 1);
        state.paths = values;
        redraw();
      } else if (action === "save-roots") {
        save();
      } else if (action === "reset-roots") {
        reset();
      }
    });
  }
  bind();
}

export async function renderSettings(root) {
  root.innerHTML = '<p class="loading">Carregando…</p>';
  let settings;
  try {
    settings = await api.settings();
  } catch (error) {
    root.innerHTML = `<p class="empty-state error">${escapeHtml(error.message || "Falha ao carregar configurações.")}</p>`;
    return () => {};
  }

  const state = { settings, editing: false, paths: [], restartRequired: false };
  root.innerHTML = `
    <div class="page-header">
      <div>
        <h1>Configurações</h1>
        <p class="muted">Visualização das configurações atuais do sistema.</p>
      </div>
    </div>
    <div class="banner info">
      Somente as bibliotecas de mídia podem ser alteradas aqui. Para os demais valores, edite <code>config.yaml</code> e reinicie os serviços necessários.
    </div>
    <div class="settings-grid">
      ${card("Geral", [
        ["Worker", `<span class="badge tone-${settings.worker.healthy ? "success" : "danger"}">${settings.worker.healthy ? "Online" : "Offline"}</span>`],
        ["Último heartbeat", escapeHtml(settings.worker.heartbeat_age_seconds != null ? `há ${Math.round(settings.worker.heartbeat_age_seconds)}s` : "—")],
        ["Intervalo de varredura", `${settings.scan_interval_seconds}s`],
        ["Política para arquivos existentes", escapeHtml(settings.existing_subtitle_policy)],
        ["Modo de publicação", escapeHtml(settings.publish_mode)],
      ])}
      ${mediaRootsCard(state)}
      ${card("Idiomas e áudio", [
        ["Idioma de origem", escapeHtml(settings.languages.source === "auto" ? "Automático (detecção)" : languageName(settings.languages.source))],
        ["Idiomas de destino", escapeHtml((settings.languages.targets || []).map((code) => languageName(code)).join(", "))],
        ["Preferência de faixas de áudio", escapeHtml(settings.audio.preferred_languages.map((code) => languageName(code)).join(", ") || "automático")],
        ["Política de baixa confiança", escapeHtml(settings.languages.low_confidence)],
      ])}
      ${card("Modelos e processamento", [
        ["Modelo ASR", escapeHtml(settings.asr_model)],
        ["Motor de tradução", escapeHtml(settings.translation_engine === "argos" ? "Argos Translate" : settings.translation_engine)],
        ["Tamanho do chunk (ASR)", `${settings.asr_chunk_seconds}s`],
        ["Limite de tentativas", `${settings.retry.max_attempts}`],
        ["Processamento paralelo", "Desabilitado (worker único)"],
      ])}
      ${card("Segurança e dashboard", [
        ["Bind", `${escapeHtml(settings.dashboard.bind)}:${settings.dashboard.port}`],
        ["Autenticação", settings.dashboard.token_configured ? "Habilitada" : "Desabilitada"],
      ])}
      ${card("Webhooks", [
        ["Bind", `${escapeHtml(settings.webhooks.bind)}:${settings.webhooks.port}`],
        ["Autenticação", settings.webhooks.token_configured ? "Habilitada" : "Desabilitada"],
      ])}
    </div>
    ${
      !settings.dashboard.token_configured
        ? `<div class="banner warning">Não exponha o dashboard na internet pública sem autenticação. Use um proxy reverso com HTTPS se necessário.</div>`
        : ""
    }`;
  bindMediaRoots(root, state);
  return () => {};
}
