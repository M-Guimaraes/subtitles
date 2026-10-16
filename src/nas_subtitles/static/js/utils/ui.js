// Small DOM-owning helpers shared by every page: toasts, a confirm dialog
// and the auth-token prompt. Each manages its own host element lazily, so
// no page/app wiring is required beyond calling the exported functions.

import { escapeHtml } from "./format.js";

let toastHost = null;
function ensureToastHost() {
  if (!toastHost) {
    toastHost = document.createElement("div");
    toastHost.id = "toast-host";
    // Screen readers announce each toast without moving focus.
    toastHost.setAttribute("role", "status");
    toastHost.setAttribute("aria-live", "polite");
    document.body.appendChild(toastHost);
  }
  return toastHost;
}

export function showToast(message, kind = "info") {
  const host = ensureToastHost();
  const node = document.createElement("div");
  node.className = `toast ${kind}`;
  if (kind === "error") node.setAttribute("role", "alert");
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

const FOCUSABLE = 'button:not([disabled]), input:not([disabled]), select, textarea, a[href], [tabindex]:not([tabindex="-1"])';

let previousFocus = null;
let keyHandler = null;

// Opens the modal and wires the keyboard: Esc calls `onEscape`, and Tab
// cycles inside the dialog instead of escaping to the page behind it.
function openHost(innerHtml, onEscape) {
  const host = ensureModalHost();
  previousFocus = document.activeElement;
  host.innerHTML = innerHtml;
  host.classList.remove("hidden", "show");
  requestAnimationFrame(() => host.classList.add("show"));
  if (keyHandler) document.removeEventListener("keydown", keyHandler);
  keyHandler = (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      onEscape();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = [...host.querySelectorAll(FOCUSABLE)];
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    } else if (!host.contains(document.activeElement)) {
      event.preventDefault();
      first.focus();
    }
  };
  document.addEventListener("keydown", keyHandler);
  return host;
}

function closeHost() {
  if (!modalHost) return;
  const host = modalHost;
  if (keyHandler) {
    document.removeEventListener("keydown", keyHandler);
    keyHandler = null;
  }
  if (previousFocus && typeof previousFocus.focus === "function") previousFocus.focus();
  previousFocus = null;
  host.classList.remove("show");
  setTimeout(() => {
    host.classList.add("hidden");
    host.innerHTML = "";
  }, 200);
}

export function confirmDialog({ title, body, confirmLabel = "Confirmar", danger = false }) {
  return new Promise((resolve) => {
    let finish = null;
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
      </div>`, () => finish(false));
    finish = (result) => {
      closeHost();
      resolve(result);
    };
    host.querySelector('[data-action="cancel"]').focus();
    host.querySelector('[data-action="cancel"]').addEventListener("click", () => finish(false));
    host.querySelector('[data-action="confirm"]').addEventListener("click", () => finish(true));
    host.querySelector(".modal-backdrop").addEventListener("click", (event) => {
      if (event.target.classList.contains("modal-backdrop")) finish(false);
    });
  });
}

export function promptToken() {
  return new Promise((resolve) => {
    let finish = null;
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
      </div>`, () => finish(null));
    const input = host.querySelector("#token-input");
    input.focus();
    finish = (value) => {
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
