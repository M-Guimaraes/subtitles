import { api } from "../api-client.js";
import { escapeHtml, languageName } from "../utils/format.js";

function card(title, rows) {
  return `
    <section class="panel settings-card">
      <h2>${escapeHtml(title)}</h2>
      <ul class="kv-list">
        ${rows.map(([key, value]) => `<li><span>${escapeHtml(key)}</span><span>${value}</span></li>`).join("")}
      </ul>
    </section>`;
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

  root.innerHTML = `
    <div class="page-header">
      <div>
        <h1>Configurações</h1>
        <p class="muted">Visualização das configurações atuais do sistema.</p>
      </div>
    </div>
    <div class="banner info">
      Configurações somente leitura. Para alterar esses valores, edite <code>config.yaml</code> e reinicie os serviços necessários.
    </div>
    <div class="settings-grid">
      ${card("Geral", [
        ["Worker", `<span class="badge tone-${settings.worker.healthy ? "success" : "danger"}">${settings.worker.healthy ? "Online" : "Offline"}</span>`],
        ["Último heartbeat", escapeHtml(settings.worker.heartbeat_age_seconds != null ? `há ${Math.round(settings.worker.heartbeat_age_seconds)}s` : "—")],
        ["Intervalo de varredura", `${settings.scan_interval_seconds}s`],
        ["Política para arquivos existentes", escapeHtml(settings.existing_subtitle_policy)],
        ["Modo de publicação", escapeHtml(settings.publish_mode)],
      ])}
      ${card(
        "Bibliotecas de mídia",
        (settings.media_roots || []).map((root_) => [root_.root_id, escapeHtml(root_.path)]),
      )}
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
  return () => {};
}
