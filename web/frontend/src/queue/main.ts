// GPU Queue page (View > GPU Queue): renders processing.queue.overview() and keeps
// it live by re-polling /api/queue/ every few seconds. All rendering builds DOM
// nodes with textContent — names and filenames come from other users.

interface JobRow {
  job_id: string;
  kind: string;
  status: string;
  mine: boolean;
  owner: string;
  filename: string | null;
  project: string | null;
  ahead: number | null;
  eta_seconds: number | null;
  started_at: string | null;
}

interface PersonRow {
  user: string;
  mine: boolean;
  queued: number;
  paused: boolean;
  next_eta_seconds: number | null;
}

interface Overview {
  gpu_paused: boolean;
  segmenting_paused: boolean;
  running: JobRow | null;
  users: PersonRow[];
  jobs: JobRow[];
  total_queued: number;
}

const POLL_MS = 3000;

const page = document.getElementById("queue-page");
const isStaff = page?.dataset.isStaff === "true";
const csrf = document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content ?? "";
const selected = new Set<string>();
let current: Overview | null = null;

function el<K extends keyof HTMLElementTagNameMap>(tag: K, text?: string, className?: string) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function byId<T extends HTMLElement = HTMLElement>(id: string): T | null {
  return document.getElementById(id) as T | null;
}

export function formatWait(seconds: number | null, paused = false): string {
  if (paused) return "Paused";
  if (seconds === null) return "—";
  if (seconds < 60) return "Under a minute";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `About ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `About ${hours} h ${rest} min` : `About ${hours} h`;
}

function formatElapsed(startedAt: string | null): string {
  if (!startedAt) return "";
  const seconds = Math.max(0, Math.round((Date.now() - Date.parse(startedAt)) / 1000));
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m ? `${m}m ${s}s` : `${s}s`;
}

function setStatus(text: string, state?: "idle" | "connected" | "processing" | "error") {
  const status = byId("queue-status");
  if (status) status.textContent = text;
  if (state) byId("gpu-state")?.setAttribute("data-state", state);
}

function renderGpu(o: Overview) {
  const gpu = byId("gpu-state");
  if (gpu) {
    let text: string;
    let state: string;
    if (o.running) {
      const what = o.running.kind === "rescan" ? "a rescan" : (o.running.filename ?? "an image");
      text = `Segmenting ${what} for ${o.running.mine ? "you" : o.running.owner} (${formatElapsed(o.running.started_at)})`;
      state = "processing";
    } else if (o.gpu_paused) {
      text = "Paused by an administrator. Nothing new will start until it's resumed.";
      state = "error";
    } else {
      text = o.total_queued ? "Starting the next job…" : "Idle — nothing is waiting.";
      state = "connected";
    }
    if (o.gpu_paused && o.running) text += " — paused after this job";
    gpu.textContent = text;
    gpu.dataset.state = state;
  }
  const mine = byId<HTMLButtonElement>("toggle-my-pause");
  if (mine) mine.textContent = o.segmenting_paused ? "Start My Segmenting" : "Stop My Segmenting";
  const all = byId<HTMLButtonElement>("toggle-gpu-pause");
  if (all) all.textContent = o.gpu_paused ? "Resume GPU" : "Pause GPU";
}

function renderPeople(o: Overview) {
  const body = byId("queue-people");
  if (!body) return;
  body.replaceChildren();
  if (!o.users.length) {
    const row = el("tr", undefined, "empty");
    const cell = el("td", "No one is waiting for the GPU.");
    cell.colSpan = 3;
    row.append(cell);
    body.append(row);
    return;
  }
  for (const person of o.users) {
    const row = el("tr");
    row.append(
      el("td", person.mine ? `${person.user} (you)` : person.user),
      el("td", String(person.queued), "num"),
      el("td", formatWait(person.next_eta_seconds, person.paused)),
    );
    body.append(row);
  }
}

function renderJobs(o: Overview) {
  const body = byId("queue-jobs");
  if (!body) return;
  const visibleIds = new Set(o.jobs.map((j) => j.job_id));
  for (const id of [...selected]) if (!visibleIds.has(id)) selected.delete(id);

  body.replaceChildren();
  if (!o.jobs.length) {
    const row = el("tr", undefined, "empty");
    const cell = el("td", "Nothing queued.");
    cell.colSpan = isStaff ? 6 : 5;
    row.append(cell);
    body.append(row);
  }
  for (const job of o.jobs) {
    const row = el("tr");
    const checkCell = el("td", undefined, "check-col");
    const box = el("input");
    box.type = "checkbox";
    box.checked = selected.has(job.job_id);
    box.setAttribute("aria-label", `Select ${job.filename ?? "job"}`);
    box.addEventListener("change", () => {
      if (box.checked) selected.add(job.job_id);
      else selected.delete(job.job_id);
      syncCancelButton();
    });
    checkCell.append(box);
    const name = job.kind === "rescan" ? `Rescan of ${job.filename ?? "an image"}` : (job.filename ?? "—");
    row.append(
      checkCell,
      el("td", job.ahead === null ? "—" : String(job.ahead + 1), "num"),
      el("td", name),
      el("td", job.project ?? "", "hide-narrow"),
    );
    if (isStaff) row.append(el("td", job.mine ? "You" : job.owner));
    row.append(el("td", formatWait(job.eta_seconds, job.ahead === null)));
    body.append(row);
  }
  syncCancelButton();
}

function syncCancelButton() {
  const button = byId<HTMLButtonElement>("cancel-selected");
  if (button) button.disabled = selected.size === 0;
  const all = byId<HTMLInputElement>("select-all");
  if (all && current) {
    all.checked = current.jobs.length > 0 && current.jobs.every((j) => selected.has(j.job_id));
  }
}

function render(o: Overview) {
  current = o;
  renderGpu(o);
  renderPeople(o);
  renderJobs(o);
}

async function post(url: string, body: unknown): Promise<void> {
  const response = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = `Request failed (${response.status}).`;
    try {
      detail = (await response.json()).detail || detail;
    } catch {
      /* not JSON */
    }
    throw new Error(detail);
  }
}

async function refresh(): Promise<void> {
  try {
    const response = await fetch("/api/queue/", { credentials: "same-origin" });
    if (response.status === 401 || response.status === 403) {
      setStatus("Your session ended — reload the page to sign in again.", "error");
      return;
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
    setStatus(`Updated ${new Date().toLocaleTimeString()}`);
  } catch {
    setStatus("Couldn't reach the server; retrying…", "error");
  }
}

async function act(action: () => Promise<void>, done: string) {
  try {
    await action();
    setStatus(done);
  } catch (error) {
    setStatus((error as Error).message, "error");
  }
  await refresh();
}

function wire() {
  byId("toggle-my-pause")?.addEventListener("click", () => {
    const paused = !current?.segmenting_paused;
    void act(() => post("/api/queue/pause", { paused }),
      paused ? "Your images won't start until you resume." : "Your images will be segmented again.");
  });
  byId("toggle-gpu-pause")?.addEventListener("click", () => {
    const paused = !current?.gpu_paused;
    void act(() => post("/api/admin/queue/pause", { paused }), paused ? "GPU paused." : "GPU resumed.");
  });
  byId("cancel-selected")?.addEventListener("click", () => {
    const ids = [...selected];
    void act(async () => {
      await post("/api/queue/cancel", { job_ids: ids });
      selected.clear();
    }, `Cancelled ${ids.length} job${ids.length === 1 ? "" : "s"}. Use Retry in the project to queue them again.`);
  });
  byId("select-all")?.addEventListener("change", (event) => {
    const checked = (event.target as HTMLInputElement).checked;
    for (const job of current?.jobs ?? []) {
      if (checked) selected.add(job.job_id);
      else selected.delete(job.job_id);
    }
    if (current) renderJobs(current);
  });
}

if (page) {
  const initial = byId("queue-initial");
  if (initial?.textContent) render(JSON.parse(initial.textContent) as Overview);
  wire();
  let timer = window.setInterval(refresh, POLL_MS);
  document.addEventListener("visibilitychange", () => {
    window.clearInterval(timer);
    timer = window.setInterval(refresh, document.hidden ? POLL_MS * 5 : POLL_MS);
    if (!document.hidden) void refresh();
  });
}
