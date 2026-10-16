import { api, onUnauthorized, setToken } from "./api-client.js";
import { promptToken } from "./utils/ui.js";
import { startPolling } from "./utils/poll.js";
import { renderOverview } from "./pages/overview.js";
import { renderJobs } from "./pages/jobs.js";
import { renderJobDetail } from "./pages/job-detail.js";
import { renderSettings } from "./pages/settings.js";

onUnauthorized(async () => {
  const token = await promptToken();
  if (!token) return false;
  setToken(token);
  return true;
});

const root = document.getElementById("view");
const navLinks = document.querySelectorAll("[data-route]");
const workerPill = document.getElementById("worker-pill");

let stopCurrentPage = null;
// Bumped on every navigation. A render that finishes after the user has
// already moved on is stopped at once, so it never keeps polling unseen.
let navigationId = 0;
let settledId = 0;

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [path, queryString] = raw.split("?");
  const segments = path.split("/").filter(Boolean);
  const params = Object.fromEntries(new URLSearchParams(queryString || ""));
  return { segments, params };
}

async function navigate() {
  if (stopCurrentPage) {
    stopCurrentPage();
    stopCurrentPage = null;
  }
  const current = ++navigationId;
  const { segments, params } = parseHash();
  const [first, second] = segments;
  let activeRoute = "overview";
  let render;
  if (first === "jobs" && second) {
    activeRoute = "jobs";
    render = () => renderJobDetail(root, decodeURIComponent(second));
  } else if (first === "jobs") {
    activeRoute = "jobs";
    render = () => renderJobs(root, params);
  } else if (first === "settings") {
    activeRoute = "settings";
    render = () => renderSettings(root);
  } else {
    activeRoute = "overview";
    render = () => renderOverview(root);
  }
  navLinks.forEach((link) => {
    const active = link.dataset.route === activeRoute;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  let stop;
  try {
    stop = await render();
  } catch (error) {
    // A render the user already navigated away from may fail on DOM nodes
    // the newer page replaced; that is expected and harmless.
    if (current === navigationId) throw error;
  }
  if (current !== navigationId) {
    if (typeof stop === "function") stop();
    // This late render may have drawn over a newer page that already
    // finished; draw the current route again.
    if (settledId === navigationId) navigate();
    return;
  }
  settledId = current;
  stopCurrentPage = stop;
}

window.addEventListener("hashchange", navigate);

async function refreshWorkerPill() {
  try {
    const health = await api.health();
    const healthy = Boolean(health.worker && health.worker.healthy);
    workerPill.querySelector(".pill-text").textContent = healthy ? "Worker online" : "Worker offline";
    workerPill.querySelector(".pill-sub").textContent = health.worker && health.worker.heartbeat_age_seconds != null
      ? `Último heartbeat há ${Math.round(health.worker.heartbeat_age_seconds)}s`
      : "";
    workerPill.className = `worker-pill ${healthy ? "ok" : "bad"}`;
  } catch {
    workerPill.querySelector(".pill-text").textContent = "Erro de conexão";
    workerPill.querySelector(".pill-sub").textContent = "";
    workerPill.className = "worker-pill bad";
  }
}

navigate();
refreshWorkerPill();
// Pauses while the tab is hidden and refreshes as soon as it is visible again.
startPolling(refreshWorkerPill, 15000);
