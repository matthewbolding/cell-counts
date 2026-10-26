// view.ts — the GPU Queue view: what the GPU is doing, who's waiting, and your own
// queued jobs with their order and estimated start. Renders
// processing.queue.overview() and re-polls /api/queue/ while it's on screen.
//
// Used in two places: the standalone /queue/ page (src/queue/main.ts), and inside
// the review screen as View › Show GPU Queue, so checking the queue never means
// leaving a page that may be in the middle of uploading a folder.
//
// All rendering builds DOM nodes with textContent — names and filenames come from
// other users.

export interface JobRow {
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

export interface PersonRow {
  user: string;
  mine: boolean;
  queued: number;
  paused: boolean;
  next_eta_seconds: number | null;
}

export interface Overview {
  gpu_paused: boolean;
  segmenting_paused: boolean;
  running: JobRow | null;
  users: PersonRow[];
  jobs: JobRow[];
  total_queued: number;
}

export interface QueueViewOptions {
  isStaff: boolean;
  csrfToken: string;
  /** Messages for the page's status bar. */
  onStatus?: (message: string, error?: boolean) => void;
}

const POLL_MS = 3000;

function el<K extends keyof HTMLElementTagNameMap>(tag: K, text?: string, className?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
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

export class QueueView {
  readonly root: HTMLElement;
  private readonly opts: QueueViewOptions;
  private readonly gpuState: HTMLElement;
  private readonly myPause: HTMLButtonElement;
  private readonly gpuPause: HTMLButtonElement | null;
  private readonly people: HTMLTableSectionElement;
  private readonly jobs: HTMLTableSectionElement;
  private readonly cancelButton: HTMLButtonElement;
  private readonly selectAll: HTMLInputElement;
  private readonly selected = new Set<string>();
  private current: Overview | null = null;
  private timer = 0;
  private visible = false;

  constructor(opts: QueueViewOptions) {
    this.opts = opts;

    this.myPause = el("button", "Stop My Segmenting");
    this.myPause.type = "button";
    this.myPause.addEventListener("click", () => this.toggleMyPause());
    this.gpuPause = null;
    if (opts.isStaff) {
      this.gpuPause = el("button", "Pause GPU");
      this.gpuPause.type = "button";
      this.gpuPause.addEventListener("click", () => this.toggleGpuPause());
    }
    this.gpuState = el("p", "Loading…", "light");
    this.gpuState.dataset.state = "idle";
    const gpuHeader = el("header");
    gpuHeader.append(el("h2", "GPU"), this.myPause);
    if (this.gpuPause) gpuHeader.append(this.gpuPause);
    const gpuBody = el("div", undefined, "body");
    gpuBody.append(this.gpuState, el("p", "Everyone shares one GPU. Rescans run first, since someone is waiting on "
      + "them; after that, people take turns, one image each. Use ▲ / ▼ in a project's Queue panel to choose which "
      + "of your own images go first.", "muted"));
    const gpuPanel = el("section", undefined, "panel");
    gpuPanel.append(gpuHeader, gpuBody);

    this.people = el("tbody");
    const peopleTable = el("table", undefined, "list");
    const peopleHead = el("thead");
    peopleHead.append(this.headRow([["Person", ""], ["Images queued", "num"], ["Next one starts", ""]]));
    peopleTable.append(peopleHead, this.people);
    const peopleHeader = el("header");
    peopleHeader.append(el("h2", "Waiting"));
    const peoplePanel = el("section", undefined, "panel");
    peoplePanel.append(peopleHeader, peopleTable);

    this.cancelButton = el("button", "Cancel Selected");
    this.cancelButton.type = "button";
    this.cancelButton.disabled = true;
    this.cancelButton.addEventListener("click", () => this.cancelSelected());
    this.selectAll = el("input");
    this.selectAll.type = "checkbox";
    this.selectAll.setAttribute("aria-label", "Select all");
    this.selectAll.addEventListener("change", () => {
      for (const job of this.current?.jobs ?? []) {
        if (this.selectAll.checked) this.selected.add(job.job_id);
        else this.selected.delete(job.job_id);
      }
      if (this.current) this.renderJobs(this.current);
    });
    this.jobs = el("tbody");
    const jobsTable = el("table", undefined, "list");
    const jobsHead = el("thead");
    const headRow = el("tr");
    const checkTh = el("th", undefined, "check-col");
    checkTh.append(this.selectAll);
    headRow.append(checkTh, el("th", "Order", "num"), el("th", "Image"), el("th", "Project", "hide-narrow"));
    if (opts.isStaff) headRow.append(el("th", "Person"));
    headRow.append(el("th", "Starts"));
    jobsHead.append(headRow);
    jobsTable.append(jobsHead, this.jobs);
    const jobsHeader = el("header");
    jobsHeader.append(el("h2", opts.isStaff ? "All queued jobs" : "Your queued jobs"), this.cancelButton);
    const jobsPanel = el("section", undefined, "panel");
    jobsPanel.append(jobsHeader, jobsTable);

    const top = el("div", undefined, "panel-grid");
    top.append(gpuPanel, peoplePanel);
    this.root = el("div", undefined, "queue-view");
    this.root.append(top, jobsPanel);

    document.addEventListener("visibilitychange", () => {
      if (this.visible) this.schedule(document.hidden ? POLL_MS * 5 : 0);
    });
  }

  private headRow(cols: [string, string][]): HTMLTableRowElement {
    const row = el("tr");
    for (const [text, cls] of cols) row.append(el("th", text, cls || undefined));
    return row;
  }

  /** Start showing (and polling); `initial` avoids a blank first frame. */
  show(initial?: Overview): void {
    this.visible = true;
    if (initial) this.render(initial);
    this.schedule(0);
  }

  hide(): void {
    this.visible = false;
    clearTimeout(this.timer);
  }

  private schedule(ms: number): void {
    clearTimeout(this.timer);
    this.timer = window.setTimeout(() => void this.refresh(), ms);
  }

  async refresh(): Promise<void> {
    if (!this.visible) return;
    try {
      const response = await fetch("/api/queue/", { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (response.status === 401 || response.status === 403) {
        this.status("Your session ended — reload the page to sign in again.", true);
        return;
      }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      this.render(await response.json());
    } catch {
      this.status("Couldn't reach the server; retrying…", true);
    } finally {
      if (this.visible) this.schedule(document.hidden ? POLL_MS * 5 : POLL_MS);
    }
  }

  private status(message: string, error = false): void {
    this.opts.onStatus?.(message, error);
  }

  private render(o: Overview): void {
    this.current = o;
    this.renderGpu(o);
    this.renderPeople(o);
    this.renderJobs(o);
  }

  private renderGpu(o: Overview): void {
    let text: string;
    let state: string;
    if (o.running) {
      const what = o.running.kind === "rescan" ? "a rescan" : (o.running.filename ?? "an image");
      text = `Segmenting ${what} for ${o.running.mine ? "you" : o.running.owner} (${formatElapsed(o.running.started_at)})`;
      state = "processing";
      if (o.gpu_paused) text += " — paused after this job";
    } else if (o.gpu_paused) {
      text = "Paused by an administrator. Nothing new will start until it's resumed.";
      state = "error";
    } else {
      text = o.total_queued ? "Starting the next job…" : "Idle — nothing is waiting.";
      state = "connected";
    }
    this.gpuState.textContent = text;
    this.gpuState.dataset.state = state;
    this.myPause.textContent = o.segmenting_paused ? "Start My Segmenting" : "Stop My Segmenting";
    if (this.gpuPause) this.gpuPause.textContent = o.gpu_paused ? "Resume GPU" : "Pause GPU";
  }

  private renderPeople(o: Overview): void {
    this.people.replaceChildren();
    if (!o.users.length) {
      this.people.append(this.emptyRow("No one is waiting for the GPU.", 3));
      return;
    }
    for (const person of o.users) {
      const row = el("tr");
      row.append(
        el("td", person.mine ? `${person.user} (you)` : person.user),
        el("td", String(person.queued), "num"),
        el("td", formatWait(person.next_eta_seconds, person.paused)),
      );
      this.people.append(row);
    }
  }

  private emptyRow(text: string, span: number): HTMLTableRowElement {
    const row = el("tr", undefined, "empty");
    const cell = el("td", text);
    cell.colSpan = span;
    row.append(cell);
    return row;
  }

  private renderJobs(o: Overview): void {
    const visibleIds = new Set(o.jobs.map((j) => j.job_id));
    for (const id of [...this.selected]) if (!visibleIds.has(id)) this.selected.delete(id);
    this.jobs.replaceChildren();
    if (!o.jobs.length) this.jobs.append(this.emptyRow("Nothing queued.", this.opts.isStaff ? 6 : 5));
    for (const job of o.jobs) {
      const row = el("tr");
      const checkCell = el("td", undefined, "check-col");
      const box = el("input");
      box.type = "checkbox";
      box.checked = this.selected.has(job.job_id);
      box.setAttribute("aria-label", `Select ${job.filename ?? "job"}`);
      box.addEventListener("change", () => {
        if (box.checked) this.selected.add(job.job_id);
        else this.selected.delete(job.job_id);
        this.syncButtons();
      });
      checkCell.append(box);
      const name = job.kind === "rescan" ? `Rescan of ${job.filename ?? "an image"}` : (job.filename ?? "—");
      row.append(
        checkCell,
        el("td", job.ahead === null ? "—" : String(job.ahead + 1), "num"),
        el("td", name),
        el("td", job.project ?? "", "hide-narrow"),
      );
      if (this.opts.isStaff) row.append(el("td", job.mine ? "You" : job.owner));
      row.append(el("td", formatWait(job.eta_seconds, job.ahead === null)));
      this.jobs.append(row);
    }
    this.syncButtons();
  }

  private syncButtons(): void {
    this.cancelButton.disabled = this.selected.size === 0;
    const jobs = this.current?.jobs ?? [];
    this.selectAll.checked = jobs.length > 0 && jobs.every((j) => this.selected.has(j.job_id));
  }

  private async post(url: string, body: unknown): Promise<void> {
    const response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": this.opts.csrfToken },
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

  private async act(action: () => Promise<void>, done: string): Promise<void> {
    try {
      await action();
      this.status(done);
    } catch (error) {
      this.status((error as Error).message, true);
    }
    await this.refresh();
  }

  private toggleMyPause(): void {
    const paused = !this.current?.segmenting_paused;
    void this.act(() => this.post("/api/queue/pause", { paused }),
      paused ? "Your images won't start until you resume." : "Your images will be segmented again.");
  }

  private toggleGpuPause(): void {
    const paused = !this.current?.gpu_paused;
    void this.act(() => this.post("/api/admin/queue/pause", { paused }), paused ? "GPU paused." : "GPU resumed.");
  }

  private cancelSelected(): void {
    const ids = [...this.selected];
    void this.act(async () => {
      await this.post("/api/queue/cancel", { job_ids: ids });
      this.selected.clear();
    }, `Cancelled ${ids.length} job${ids.length === 1 ? "" : "s"}. Use Retry in the project to queue them again.`);
  }
}
