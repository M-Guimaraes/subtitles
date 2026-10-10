// Every HTTP call to the dashboard API goes through here (plan §19/§20.2):
// relative URLs only, auth header attached automatically, one retry after a
// token prompt on 401, and errors normalised into ApiError so pages never
// touch `fetch` or `Response` directly.

const TOKEN_KEY = "nas-subs-dashboard-token";

export class ApiError extends Error {
  constructor(message, status, code) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

let unauthorizedHandler = null;
export function onUnauthorized(handler) {
  unauthorizedHandler = handler;
}

export function setToken(token) {
  sessionStorage.setItem(TOKEN_KEY, token);
}

function authHeaders() {
  const token = sessionStorage.getItem(TOKEN_KEY);
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function buildQuery(params = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    query.set(key, String(value));
  }
  const text = query.toString();
  return text ? `?${text}` : "";
}

async function request(path, options = {}, { retried = false } = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      Accept: "application/json",
      ...authHeaders(),
      ...(options.headers || {}),
    },
  });
  if (response.status === 401 && !retried && unauthorizedHandler) {
    const authorised = await unauthorizedHandler();
    if (authorised) return request(path, options, { retried: true });
  }
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new ApiError("resposta inválida do servidor", response.status, "invalid_response");
  }
  if (!response.ok || payload.ok === false) {
    throw new ApiError(
      payload.message || `falha na requisição (${response.status})`,
      response.status,
      payload.error_code,
    );
  }
  return payload;
}

export const api = {
  health: (options) => request("/api/health", options),
  overview: (options) => request("/api/overview", options),
  jobs: (params, options) => request(`/api/jobs${buildQuery(params)}`, options),
  job: (id, options) => request(`/api/jobs/${encodeURIComponent(id)}`, options),
  retry: (id) => request(`/api/jobs/${encodeURIComponent(id)}/retry`, { method: "POST" }),
  cancel: (id) => request(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
  reprocess: (id) => request(`/api/jobs/${encodeURIComponent(id)}/reprocess`, { method: "POST" }),
  scan: () => request("/api/scan", { method: "POST" }),
  settings: (options) => request("/api/settings", options),
};
