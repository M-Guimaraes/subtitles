// Pure formatting/label helpers. No DOM, no fetch — safe to import from
// anywhere. Stage/event labels translate backend-owned codes into PT-BR
// text; they never invent an *order*, which still always comes from the
// API (`stages`/`progress` in GET /api/jobs/<id>`).

export const JOB_STATUS = {
  queued: { label: "Aguardando", tone: "neutral" },
  running: { label: "Processando", tone: "accent" },
  retry_wait: { label: "Aguardando nova tentativa", tone: "warning" },
  needs_review: { label: "Revisão necessária", tone: "warning" },
  ready_to_publish: { label: "Aguardando publicação", tone: "accent" },
  completed: { label: "Concluído", tone: "success" },
  skipped: { label: "Ignorado", tone: "neutral" },
  failed: { label: "Falhou", tone: "danger" },
  cancelled: { label: "Cancelado", tone: "neutral" },
};

export function jobStatus(state) {
  return JOB_STATUS[state] || { label: state || "—", tone: "neutral" };
}

export const STAGE_LABELS = {
  probe: "Análise do arquivo",
  detect_language: "Identificação do idioma",
  extract: "Extração do áudio",
  separate: "Separação do áudio",
  transcribe: "Transcrição",
  merge: "Preparação dos segmentos",
  translate: "Tradução",
  adapt: "Adaptação do texto",
  synthesize: "Síntese de voz",
  sync: "Sincronização",
  mix: "Mixagem",
  render: "Geração da legenda",
  validate: "Validação",
  validate_audio: "Validação do áudio",
  publish: "Publicação",
};

export function stageLabel(stage) {
  return STAGE_LABELS[stage] || stage || "—";
}

// Only `language_decision` is a real, persisted event code today (see
// docs/dashboard-v2-audit.md §4) — an unmapped code falls back to itself
// rather than a fabricated label (plan §15.3).
export const EVENT_LABELS = {
  language_decision: "Idioma identificado",
};

export function eventLabel(code) {
  return EVENT_LABELS[code] || code;
}

// Friendly text for the 26 ErrorCode values (domain.py). An unmapped code
// still shows — as the code itself, never a blank or a guess.
export const ERROR_LABELS = {
  config_invalid: "Configuração inválida",
  invalid_media: "Arquivo de mídia inválido",
  unsupported_language: "Idioma não suportado",
  output_conflict: "Já existe um arquivo de saída com esse nome",
  unsupported_atomic_publish: "Publicação atômica não suportada neste destino",
  media_root_missing: "Biblioteca de mídia não está mais configurada",
  media_path_outside_roots: "Arquivo fora das bibliotecas configuradas",
  media_unstable: "Arquivo ainda sendo escrito/copiado",
  media_changed: "O arquivo mudou durante o processamento",
  permission_denied: "Permissão negada",
  insufficient_space: "Espaço em disco insuficiente",
  model_missing: "Modelo não instalado",
  translation_pair_missing: "Par de tradução não instalado",
  empty_translation: "Tradução vazia",
  no_cues_for_speech: "Fala reconhecida não gerou legendas",
  language_undetermined: "Não foi possível determinar o idioma com confiança",
  checkpoint_invalid: "Checkpoint inválido ou ausente",
  quality_gate_failed: "Falha nos critérios de qualidade",
  io_error: "Erro de leitura/escrita",
  subprocess_failed: "Falha ao executar um processo externo (ffmpeg/ffprobe)",
  subprocess_timeout: "Tempo limite excedido em uma etapa",
  interrupted: "Processamento interrompido",
  lock_busy: "Worker ocupado em outro job",
  job_not_found: "Job não encontrado",
  invalid_state_transition: "Transição de estado inválida",
  not_implemented: "Funcionalidade ainda não implementada",
};

export function errorLabel(code) {
  return ERROR_LABELS[code] || code || "Erro desconhecido";
}

export const JOB_KIND_LABELS = { subtitles: "Legenda", dubbing: "Dublagem" };

export function jobKindLabel(kind) {
  return JOB_KIND_LABELS[kind] || kind || "—";
}

const LANGUAGE_NAMES = {
  en: "Inglês",
  pt: "Português",
  "pt-BR": "Português",
  es: "Espanhol",
  ja: "Japonês",
  fr: "Francês",
  de: "Alemão",
  it: "Italiano",
  ko: "Coreano",
  zh: "Chinês",
  ru: "Russo",
};

export function languageName(code) {
  if (!code) return null;
  return LANGUAGE_NAMES[code] || code.toUpperCase();
}

export function languagePair(job) {
  const source = languageName(job.source_language || job.detected_language);
  const target = languageName(job.target_language);
  if (!source && !target) return "—";
  return `${source || "?"} → ${target || "?"}`;
}

export function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

export function formatRelativeTime(iso) {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const diffSec = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (diffSec < 60) return `há ${diffSec}s`;
  const diffMin = Math.round(diffSec / 60);
  if (diffMin < 60) return `há ${diffMin} min`;
  const diffHour = Math.round(diffMin / 60);
  if (diffHour < 24) return `há ${diffHour}h`;
  const diffDay = Math.round(diffHour / 24);
  return `há ${diffDay}d`;
}

export function formatClock(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
}

export function formatProbability(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  if (Number.isNaN(number)) return null;
  return `${Math.round(number * 100)}%`;
}

export function formatBool(value) {
  if (value === true) return "Sim";
  if (value === false) return "Não";
  return "—";
}
