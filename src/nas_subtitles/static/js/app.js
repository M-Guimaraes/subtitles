import { api, onUnauthorized, setToken } from "./api-client.js";
import { promptToken } from "./utils/ui.js";
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
  const { segments, params } = parseHash();
  const [first, second] = segments;
  let activeRoute = "overview";
  if (first === "jobs" && second) {
    activeRoute = "jobs";
    stopCurrentPage = await renderJobDetail(root, decodeURIComponent(second));
  } else if (first === "jobs") {
    activeRoute = "jobs";
    stopCurrentPage = await renderJobs(root, params);
  } else if (first === "settings") {
    activeRoute = "settings";
    stopCurrentPage = await renderSettings(root);
  } else {
    activeRoute = "overview";
    stopCurrentPage = await renderOverview(root);
  }
  navLinks.forEach((link) => {
    link.classList.toggle("active", link.dataset.route === activeRoute);
  });
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
setInterval(refreshWorkerPill, 15000);
