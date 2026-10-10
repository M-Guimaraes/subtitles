// Small DOM-owning helpers shared by every page: toasts, a confirm dialog
// and the auth-token prompt. Each manages its own host element lazily, so
// no page/app wiring is required beyond calling the exported functions.

import { escapeHtml } from "./format.js";

let toastHost = null;
function ensureToastHost() {
  if (!toastHost) {
    toastHost = document.createElement("div");
    toastHost.id = "toast-host";
    document.body.appendChild(toastHost);
  }
  return toastHost;
}

export function showToast(message, kind = "info") {
  const host = ensureToastHost();
  const node = document.createElement("div");
  node.className = `toast ${kind}`;
  node.textContent = message;
  host.appendChild(node);
  requestAnimationFrame(() => node.classList.add("show"));
  setTimeout(() => {
    node.classList.remove("show");
    setTimeout(() => node.remove(), 200);
  }, 4000);
}

let modalHost = null;
function ensureModalHost() {
  if (!modalHost) {
    modalHost = document.createElement("div");
    modalHost.id = "modal-host";
    document.body.appendChild(modalHost);
  }
  return modalHost;
}

function openHost(innerHtml) {
  const host = ensureModalHost();
  host.innerHTML = innerHtml;
  host.classList.remove("hidden");
  return host;
}

function closeHost() {
  if (!modalHost) return;
  modalHost.classList.add("hidden");
  modalHost.innerHTML = "";
}

export function confirmDialog({ title, body, confirmLabel = "Confirmar", danger = false }) {
  return new Promise((resolve) => {
    const host = openHost(`
      <div class="modal-backdrop">
        <div class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
          <h2 id="modal-title">${escapeHtml(title)}</h2>
          <p class="muted">${escapeHtml(body)}</p>
          <div class="modal-actions">
            <button type="button" class="btn secondary" data-action="cancel">Cancelar</button>
            <button type="button" class="btn ${danger ? "danger" : "primary"}" data-action="confirm">${escapeHtml(confirmLabel)}</button>
          </div>
        </div>
      </div>`);
    const finish = (result) => {
      closeHost();
      resolve(result);
    };
    host.querySelector('[data-action="cancel"]').addEventListener("click", () => finish(false));
    host.querySelector('[data-action="confirm"]').addEventListener("click", () => finish(true));
    host.querySelector(".modal-backdrop").addEventListener("click", (event) => {
      if (event.target.classList.contains("modal-backdrop")) finish(false);
    });
  });
}

export function promptToken() {
  return new Promise((resolve) => {
    const host = openHost(`
      <div class="modal-backdrop">
        <div class="modal" role="dialog" aria-modal="true" aria-labelledby="token-title">
          <h2 id="token-title">Autenticação necessária</h2>
          <p class="muted">Informe o token do dashboard (config.yaml → dashboard.token).</p>
          <form id="token-form">
            <input type="password" id="token-input" autocomplete="off" aria-label="Token do dashboard" />
            <div class="modal-actions">
              <button type="button" class="btn secondary" data-action="cancel">Cancelar</button>
              <button type="submit" class="btn primary">Entrar</button>
            </div>
          </form>
        </div>
      </div>`);
    const input = host.querySelector("#token-input");
    input.focus();
    const finish = (value) => {
      closeHost();
      resolve(value);
    };
    host.querySelector("#token-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const value = input.value.trim();
      if (value) finish(value);
    });
    host.querySelector('[data-action="cancel"]').addEventListener("click", () => finish(null));
  });
}
