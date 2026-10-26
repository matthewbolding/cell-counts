// workspace.ts — the review screen's controller: the review area plus what's
// around it (menus, uploads and processing status, Process › Stop/Start).
//
// State lives here; the pieces it drives are deliberately dumb: the Viewer
// draws whatever Scene it's given, Interactions turns gestures into calls back
// into this class, the panels render rows, CellSaver/EditLease talk to the
// server. The rules: pan/zoom is
// kept across the tabs of one sample and reset to fit on a new one, Composite is
// view-only, a rescan review takes over clicks until merged or discarded, and
// every edit is one undoable step that saves itself a moment later.

import { errorMessage, get, post, put } from "./api";
import { CellStore, entriesFor, type CellEntry } from "./cellStore";
import {
  CHANNELS,
  COEXPRESS_HIGHLIGHT_COLOR,
  DEFAULT_CHANNEL_COLORS,
  MIN_RESCAN_SIZE,
  RESCAN_ACCEPTED_COLOR,
  RESCAN_PROPOSED_COLOR,
  STATUS_POLL_HIDDEN_MS,
  STATUS_POLL_VISIBLE_MS,
  UI_STATE_SAVE_MS,
  ZOOM_STEP,
  type Channel,
} from "./constants";
import { confirmAction, info, isDialogOpen, listBox, showDialog } from "./dialogs";
import { isTypingTarget, plural, shortcut } from "./dom";
import { CellSaver, EditLease, type LeaseState } from "./edits";
import { askExport, runExport } from "./exportDialog";
import { normalizeRect, round2, shoelaceAreaCentroid } from "./geometry";
import { Interactions, type Context, type InteractionHost, type Mode, type Region } from "./interactions";
import { buildLayout, splitPanes, type Layout } from "./layout";
import { MenuBar, signOut } from "./menus";
import { drawCells } from "./overlay";
import { QueuePanel, SamplesPanel, type QueueItem, type Readiness } from "./panels";
import { moveSelection, type Move } from "./queueOrder";
import { QueueView } from "../queue/view";
import { askRescanParams, RescanReview, SweepRunner } from "./rescan";
import { LogPane, StatusBar } from "./statusbar";
import type { TiledImage } from "./tiles";
import { UploadPanel } from "./uploadPanel";
import type {
  Bootstrap,
  Cell,
  CellsResponse,
  CompositeResponse,
  DrawStyle,
  ImageInfo,
  Point,
  QueueRow,
  Rect,
  RenderMode,
  SampleInfo,
  SelectionStyle,
  StatusResponse,
  UiState,
} from "./types";
import { AddCells, ModifyCells, RemoveCells, UndoHistory, type Action } from "./undo";
import { filesFromDrop, filesFromInput, scanFiles, type FoundFile } from "./uploads/scan";
import { UploadManager } from "./uploads/uploader";
import type { ViewSnapshot } from "./viewport";
import { Viewer, type Scene } from "./viewer";

export interface PageConfig {
  projectId: string;
  projectsUrl: string;
  settingsUrl: string;
  accountSettingsUrl: string;
  logoutUrl: string;
  manageUrl: string;
  csrfToken: string;
  /** Django messages waiting for this page (e.g. "Project renamed."). */
  messages: string;
}

const CURSORS: Record<Mode, string> = { review: "pointer", draw: "crosshair", delete: "not-allowed", rescan: "crosshair" };

/** What the main area shows (View menu): the review screen, the GPU queue, or the log. */
type View = "review" | "queue" | "log";

interface CompositeView {
  sampleId: string;
  cck: CellStore;
  chr: CellStore;
  highlight: CellEntry[];
  result: CompositeResponse;
  versions: string;
}

export class Workspace implements InteractionHost {
  private readonly config: PageConfig;
  private readonly boot: Bootstrap;
  private readonly layout: Layout;
  private readonly viewer = new Viewer();
  private readonly interactions: Interactions;
  private readonly menus: MenuBar;
  private readonly statusbar = new StatusBar();
  private readonly log = new LogPane();
  private readonly samplesPanel = new SamplesPanel();
  private readonly queuePanel = new QueuePanel();
  private readonly undo = new UndoHistory();
  private readonly saver: CellSaver;
  private readonly lease: EditLease;
  private readonly uploads: UploadManager;
  private readonly uploadPanel = new UploadPanel();
  private readonly queueView: QueueView;
  private readonly queuePane: HTMLElement;

  private samples: SampleInfo[] = [];
  private ui: UiState;
  mode: Mode = "review";
  selectionStyle: SelectionStyle;
  drawStyle: DrawStyle;

  private prefix: string | null = null;
  private channel: Channel | null = null;
  private composite: CompositeView | null = null;
  private viewingComposite = false;
  private store: CellStore | null = null;
  private storeCache = new Map<string, CellStore>();
  private leaseState: LeaseState | null = null;
  private loadSeq = 0;

  private rescanReview: RescanReview | null = null;
  private rescanRunner: SweepRunner | null = null;
  private preRescanView: ViewSnapshot | null = null;

  private serverQueue: QueueRow[] = [];
  private segmentingPaused = false;
  private pollFailures = 0;
  private pollTimer = 0;
  private view: View = "review";
  private uiSaveTimer = 0;
  private needsFit = true;

  constructor(host: HTMLElement, config: PageConfig, boot: Bootstrap) {
    this.config = config;
    this.boot = boot;
    this.samples = boot.samples;
    this.ui = { ...boot.ui_state, channel_colors: { ...DEFAULT_CHANNEL_COLORS, ...(boot.ui_state.channel_colors ?? {}) } };
    this.selectionStyle = this.ui.selection_style ?? "lasso";
    this.drawStyle = this.ui.draw_style ?? "freehand";

    this.layout = buildLayout();
    this.interactions = new Interactions(this);
    this.viewer.gestures = this.interactions;
    this.viewer.onViewChanged = () => this.rememberZoom();
    this.viewer.onFirstLayout = () => {
      const image = this.currentImage();
      if (image) this.applyViewport(false, image);
      this.viewer.requestRender();
    };
    this.layout.canvasHost.append(this.viewer.root);
    splitPanes(this.layout.left, this.samplesPanel.root, this.queuePanel.root);
    this.uploadPanel.onToggle = () => (this.uploads.paused ? this.uploads.resume() : this.uploads.pause());
    this.uploadPanel.onClose = () => this.uploads.dismissSummary();
    this.layout.root.insertBefore(this.log.root, this.layout.folderInput);
    this.queueView = new QueueView({
      isStaff: boot.me.is_staff,
      csrfToken: config.csrfToken,
      onStatus: (message) => this.view === "queue" && this.statusbar.setMessage(message),
    });
    this.queuePane = document.createElement("section");
    this.queuePane.className = "ws-queue-view content";
    this.queuePane.hidden = true;
    this.queuePane.append(this.queueView.root);
    this.layout.root.insertBefore(this.queuePane, this.layout.folderInput);

    this.saver = new CellSaver({
      onConflict: (id) => void this.reloadAfterConflict(id),
      onLocked: (id, holder) => void this.lostLock(id, holder),
      onError: (_id, message, willRetry) => {
        this.statusbar.setMessage(message);
        if (!willRetry) this.statusbar.setState("error");
      },
      onSaved: () => {
        if (this.statusbar.lightState === "error" && this.pollFailures === 0) this.statusbar.setState("connected");
      },
    });
    this.lease = new EditLease((state) => this.leaseChanged(state));
    this.uploads = new UploadManager(boot.id, {
      changed: () => {
        this.refreshQueueAndDots();
        this.renderUploadProgress();
      },
      problem: (title, message) => void info(title, message),
      log: (message) => this.log.append(message),
      uploaded: () => void this.refreshSamples(),
      confirmReplace: (names) => confirmAction("Changed Files",
        [names.length === 1
          ? "This file differs from the copy already in the project. Replacing it discards its review edits."
          : `${names.length} files in this folder differ from the copies already in the project. Replacing them `
            + "discards their review edits.", listBox(names), names.length === 1 ? "Replace it?" : "Replace them?"],
        "Replace", true),
      quotaExceeded: (message) => void info("Storage Quota", [message,
        "Free up space by deleting a project, or ask your administrator for a larger quota."]),
    });

    this.menus = this.buildMenus();
    host.replaceChildren(this.menus.root, this.layout.root, this.uploadPanel.root, this.statusbar.root);
    document.title = `${boot.name} — Cell Counts`;

    this.wireToolbar();
    this.wireKeyboard();
    this.wireUploadsInput();
    this.wirePageLifecycle();
    this.samplesPanel.onSelect = (prefix, channel) => this.onSampleClick(prefix, channel);
    this.queuePanel.onMove = (keys, how) => void this.moveQueueSelection(keys, how);

    this.log.append(`Project: ${boot.name}${boot.role === "owner" ? "" : ` (${boot.owner}'s, ${boot.role} access)`}`);
    this.renderSamples();
    this.syncToolbar();
    this.selectInitialImage();
    if (config.messages) this.statusbar.setMessage(config.messages);
    void this.poll();
  }

  // ------------------------------------------------------------------ //
  // Lookup helpers
  // ------------------------------------------------------------------ //
  private sample(prefix: string | null): SampleInfo | undefined {
    return this.samples.find((s) => s.prefix === prefix);
  }

  private currentImage(): ImageInfo | null {
    if (this.viewingComposite) return this.sample(this.prefix)?.channels.SNAP ?? null;
    return this.channel ? this.sample(this.prefix)?.channels[this.channel] ?? null : null;
  }

  private colorOf(ch: Channel): string {
    return this.ui.channel_colors?.[ch] ?? DEFAULT_CHANNEL_COLORS[ch];
  }

  private get canEdit(): boolean {
    return this.boot.can_edit;
  }

  private readiness = (prefix: string, ch: Channel): Readiness => {
    if (this.uploads.isPending(prefix, ch)) return "processing";
    const image = this.sample(prefix)?.channels[ch];
    if (!image) return "not_ready";
    if (image.status === "done") return "ready";
    if (image.status === "error") return "not_ready";
    return "processing";
  };

  private compositeAvailable(prefix: string | null): boolean {
    const s = this.sample(prefix);
    return !!s && CHANNELS.every((ch) => s.channels[ch]?.status === "done");
  }

  // ------------------------------------------------------------------ //
  // Menus and toolbar
  // ------------------------------------------------------------------ //
  private buildMenus(): MenuBar {
    const leave = (url: string) => () => window.location.assign(url);
    const errorImages = () => this.samples.flatMap((s) => CHANNELS.map((ch) => s.channels[ch]))
      .filter((i): i is ImageInfo => !!i && i.status === "error");
    return new MenuBar("Cell Counts", this.boot.name, [
      {
        label: "File",
        items: [
          { label: "Open Project…", action: leave(this.config.projectsUrl) },
          { label: "Add Images…", shortcut: shortcut("O"), action: () => this.addImages(),
            enabled: () => this.canEdit },
          { label: "Export Data…", action: () => void this.exportData(), enabled: () => this.samples.length > 0,
            separatorBefore: true },
          { label: "Project Settings…", action: leave(this.config.settingsUrl), separatorBefore: true },
        ],
      },
      {
        label: "View",
        items: [
          { label: "Show Review", action: () => this.setView("review"), enabled: () => this.view !== "review" },
          { label: "Show GPU Queue", action: () => this.setView("queue"), enabled: () => this.view !== "queue" },
          { label: "Show Log", action: () => this.setView("log"), enabled: () => this.view !== "log" },
          { label: "Fit to Window", action: () => this.fit(), enabled: () => this.view === "review",
            separatorBefore: true },
        ],
      },
      {
        label: "Process",
        items: [
          { label: () => (this.uploads.paused ? "Start Uploads" : "Stop Uploads"),
            action: () => (this.uploads.paused ? this.uploads.resume() : this.uploads.pause()),
            enabled: () => this.uploads.active || this.uploads.paused },
          { label: () => (this.segmentingPaused ? "Start Segmenting" : "Stop Segmenting"),
            action: () => void this.toggleSegmenting(), enabled: () => this.canEdit },
          { label: () => `Retry Failed Images${errorImages().length ? ` (${errorImages().length})` : ""}`,
            action: () => void this.retry(errorImages().map((i) => i.id)),
            enabled: () => this.canEdit && errorImages().length > 0, separatorBefore: true },
        ],
      },
      {
        label: this.boot.me.label,
        right: true,
        items: [
          { label: "Settings…", action: leave(this.config.accountSettingsUrl) },
          ...(this.config.manageUrl ? [{ label: "Manage Users…", action: leave(this.config.manageUrl) }] : []),
          { label: "Sign Out", action: () => signOut(this.config.logoutUrl, this.config.csrfToken),
            separatorBefore: true },
        ],
      },
    ]);
  }

  private wireToolbar(): void {
    const l = this.layout;
    for (const [mode, input] of l.modes) input.addEventListener("change", () => input.checked && this.setMode(mode));
    for (const [style, input] of l.selection) {
      input.addEventListener("change", () => {
        if (!input.checked) return;
        this.selectionStyle = style;
        this.saveUi({ selection_style: style });
      });
    }
    for (const [style, input] of l.draw) {
      input.addEventListener("change", () => {
        if (!input.checked) return;
        this.drawStyle = style;
        this.interactions.reset();
        this.saveUi({ draw_style: style });
      });
    }
    l.renderModeButton.addEventListener("click", () => {
      this.saveUi({ render_mode: this.ui.render_mode === "mask" ? "outline" : "mask" });
      this.syncToolbar();
      this.viewer.requestRender();
    });
    for (const [ch, { button, input }] of l.swatches) {
      button.addEventListener("click", () => {
        input.value = this.colorOf(ch);
        input.click();
      });
      input.addEventListener("input", () => {
        this.saveUi({ channel_colors: { ...this.ui.channel_colors, [ch]: input.value } });
        this.syncToolbar();
        this.viewer.requestRender();
      });
    }
    l.zoomOut.addEventListener("click", () => this.viewer.zoomBy(1 / ZOOM_STEP));
    l.zoomIn.addEventListener("click", () => this.viewer.zoomBy(ZOOM_STEP));
    l.zoomFit.addEventListener("click", () => this.fit());
    for (const [ch, tab] of l.tabs) tab.addEventListener("click", () => this.prefix && this.selectImage(this.prefix, ch));
    l.compositeTab.addEventListener("click", () => this.prefix && void this.selectComposite(this.prefix));
    l.rescanPrev.addEventListener("click", () => this.stepRescan(-1));
    l.rescanNext.addEventListener("click", () => this.stepRescan(1));
    l.rescanMerge.addEventListener("click", () => void this.mergeRescan());
    l.rescanDiscard.addEventListener("click", () => this.discardRescan());
  }

  private wireKeyboard(): void {
    document.addEventListener("keydown", (e) => {
      if (isDialogOpen() || isTypingTarget(e.target)) return;
      const mod = e.ctrlKey || e.metaKey;
      const key = e.key.toLowerCase();
      if (mod && key === "o") {
        e.preventDefault();
        if (this.canEdit) this.addImages();
        return;
      }
      if (this.view !== "review") {
        if (e.key === "Escape") this.setView("review");
        return;
      }
      if (mod && key === "z") {
        e.preventDefault();
        if (e.shiftKey) this.redoEdit();
        else this.undoEdit();
      } else if (mod && key === "y") {
        e.preventDefault();
        this.redoEdit();
      } else if (e.key === "Escape") {
        if (this.rescanReview) this.discardRescan();
        else this.interactions.reset();
      } else if (e.key === "Enter" && this.mode === "draw") {
        e.preventDefault();
        this.interactions.commitPolygon();
      }
    });
  }

  private wireUploadsInput(): void {
    const input = this.layout.folderInput;
    input.addEventListener("change", () => {
      if (input.files?.length) void this.addFound(filesFromInput(input.files));
      input.value = "";
    });
    if (!this.canEdit) return;
    let depth = 0;
    const hint = this.layout.dropHint;
    document.addEventListener("dragenter", (e) => {
      if (!e.dataTransfer?.types.includes("Files")) return;
      depth++;
      hint.classList.add("visible");
    });
    document.addEventListener("dragleave", () => {
      depth = Math.max(0, depth - 1);
      if (!depth) hint.classList.remove("visible");
    });
    document.addEventListener("dragover", (e) => {
      if (e.dataTransfer?.types.includes("Files")) e.preventDefault();
    });
    document.addEventListener("drop", (e) => {
      if (!e.dataTransfer?.types.includes("Files")) return;
      e.preventDefault();
      depth = 0;
      hint.classList.remove("visible");
      void this.addFound(filesFromDrop(e.dataTransfer));
    });
  }

  private wirePageLifecycle(): void {
    window.addEventListener("beforeunload", (e) => {
      if (this.uploads.active || this.saver.anyDirty) e.preventDefault();
    });
    window.addEventListener("pagehide", () => {
      this.saver.flushAllOnUnload();
      this.lease.release(true);
    });
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        if (this.store) void this.saver.flush(this.store.imageId);
      } else {
        this.schedulePoll(0);
      }
    });
  }

  private syncToolbar(): void {
    const l = this.layout;
    const readOnly = this.context() !== "edit";
    for (const [mode, input] of l.modes) {
      input.checked = mode === this.mode;
      input.disabled = readOnly;
    }
    for (const [style, input] of l.selection) input.checked = style === this.selectionStyle;
    for (const [style, input] of l.draw) input.checked = style === this.drawStyle;
    l.renderModeButton.textContent = `Mode: ${this.ui.render_mode === "mask" ? "Mask" : "Outline"}`;
    for (const [ch, { button }] of l.swatches) {
      button.style.background = this.colorOf(ch);
      button.style.color = contrastText(this.colorOf(ch));
    }
    const sample = this.sample(this.prefix);
    for (const [ch, tab] of l.tabs) {
      tab.disabled = !sample?.channels[ch];
      tab.setAttribute("aria-pressed", String(!this.viewingComposite && this.channel === ch));
    }
    l.compositeTab.disabled = !this.compositeAvailable(this.prefix);
    l.compositeTab.setAttribute("aria-pressed", String(this.viewingComposite));
    this.viewer.setCursor(readOnly ? "default" : CURSORS[this.mode]);
    this.menus.refresh();
  }

  private setMode(mode: Mode): void {
    this.mode = mode;
    this.interactions.reset();
    this.syncToolbar();
  }

  // ------------------------------------------------------------------ //
  // Samples, dots, queue
  // ------------------------------------------------------------------ //
  private renderSamples(): void {
    this.samplesPanel.setSamples(this.samples.map((s) => s.prefix));
    this.refreshQueueAndDots();
  }

  private refreshQueueAndDots(): void {
    this.samplesPanel.update(this.readiness, this.prefix);
    const items: QueueItem[] = [];
    const label = (prefix: string | null, ch: string | null, owner?: string) =>
      `${prefix ?? "?"} · ${ch ?? "?"}${owner ? ` (${owner})` : ""}`;
    const rows = [...this.serverQueue].sort((a, b) => queueRank(a) - queueRank(b));
    for (const row of rows) {
      items.push({
        key: row.job_id,
        label: label(row.prefix, row.channel, row.mine ? undefined : row.owner),
        state: row.status === "running" && row.kind === "segment" ? "segmenting" : "server-queued",
        title: row.kind === "prepare" ? "Preparing on the server" : row.status === "running"
          ? "Segmenting now" : row.eta_seconds !== null ? `Waiting for the GPU — about ${formatEta(row.eta_seconds)}`
          : "Waiting for the GPU",
      });
    }
    const local = [...this.uploads.items].sort((a, b) => Number(b.phase === "uploading") - Number(a.phase === "uploading"));
    for (const item of local) {
      const pct = `${Math.floor(item.fraction * 100)}%`;
      const uploading = item.phase === "uploading";
      items.push({
        key: item.key,
        label: label(item.prefix, item.channel),
        state: uploading ? "uploading" : "waiting",
        title: uploading ? "Uploading now" : item.phase === "hashing" ? "Checking the file" : "Waiting to upload",
        detail: uploading ? pct : item.phase === "hashing" ? `checking ${pct}` : item.sent ? `${pct} sent` : "",
        progress: uploading || item.sent ? item.fraction : undefined,
      });
    }
    this.queuePanel.setItems(items);
    this.updateLight();
    this.menus.refresh();
  }

  private async moveQueueSelection(keys: Set<string>, how: Move): Promise<void> {
    const localKeys = new Set([...keys].filter((k) => k.startsWith("upload:")));
    if (localKeys.size) this.uploads.reorder(localKeys, how);
    const movable = this.serverQueue.filter((r) => r.mine && r.kind === "segment" && r.status === "queued")
      .sort((a, b) => queueRank(a) - queueRank(b));
    if (!movable.some((r) => keys.has(r.job_id))) return;
    const order = moveSelection(movable, (r) => keys.has(r.job_id), how).map((r) => r.job_id);
    try {
      await post("/api/queue/reorder", { job_ids: order });
      this.schedulePoll(0);
    } catch (err) {
      this.statusbar.setMessage(`Couldn't reorder the server queue: ${errorMessage(err)}`);
    }
  }

  private updateLight(): void {
    if (this.pollFailures > 1) this.statusbar.setState("error");
    else this.statusbar.setState(this.uploads.active || this.serverQueue.length ? "processing" : "connected");
  }

  // ------------------------------------------------------------------ //
  // Polling (job status, as a cheap poll)
  // ------------------------------------------------------------------ //
  private schedulePoll(ms = document.hidden ? STATUS_POLL_HIDDEN_MS : STATUS_POLL_VISIBLE_MS): void {
    clearTimeout(this.pollTimer);
    this.pollTimer = window.setTimeout(() => void this.poll(), ms);
  }

  private async poll(): Promise<void> {
    if (this.statusbar.lightState === "idle") this.statusbar.setState("connecting");
    try {
      const status = await get<StatusResponse>(`/api/projects/${this.boot.id}/status`);
      if (this.pollFailures > 1) this.statusbar.setMessage("Reconnected.");
      this.pollFailures = 0;
      if (this.statusbar.lightState === "error" || this.statusbar.lightState === "connecting") {
        this.statusbar.setState("connected");
      }
      await this.applyStatus(status);
    } catch (err) {
      this.pollFailures++;
      if (this.pollFailures > 1) {
        this.statusbar.setMessage(`Can't reach the server (${errorMessage(err)}); retrying…`);
        this.statusbar.setState("error");
      }
    } finally {
      this.schedulePoll();
    }
  }

  private async applyStatus(status: StatusResponse): Promise<void> {
    this.serverQueue = status.queue;
    this.segmentingPaused = status.segmenting_paused;
    const known = new Map<string, ImageInfo>();
    for (const s of this.samples) for (const ch of CHANNELS) if (s.channels[ch]) known.set(s.channels[ch]!.id, s.channels[ch]!);
    if (status.images.some((i) => !known.has(i.id)) || status.images.length !== known.size) {
      await this.refreshSamples();
      return;
    }
    const before = this.currentImage();
    const beforeStatus = before?.status;
    let compositeChanged = false;
    for (const entry of status.images) {
      const image = known.get(entry.id)!;
      if (this.viewingComposite && image.cells_version !== entry.cells_version) compositeChanged = true;
      Object.assign(image, { status: entry.status, error: entry.error, cells_version: entry.cells_version,
        width: entry.width, height: entry.height, tiles: entry.tiles });
    }
    this.refreshQueueAndDots();
    this.syncToolbar();
    await this.reconcileCurrent(beforeStatus, compositeChanged);
  }

  /** The image on screen changed on the server: it finished, failed, or someone
   * else saved edits to it. */
  private async reconcileCurrent(beforeStatus: string | undefined, compositeChanged: boolean): Promise<void> {
    if (this.viewingComposite) {
      if (!this.compositeAvailable(this.prefix)) {
        if (this.prefix) this.selectImage(this.prefix, "SNAP", true);
      } else if (compositeChanged && !this.rescanReview && this.prefix) {
        await this.selectComposite(this.prefix, true);
      }
      return;
    }
    const image = this.currentImage();
    if (!image || !this.prefix || !this.channel) return;
    const store = this.store;
    if (image.status === "done" && (!store || store.imageId !== image.id)) {
      if (beforeStatus !== "done") this.log.append(`${image.filename} finished processing.`);
      this.selectImage(this.prefix, this.channel, true);
    } else if (store && image.cells_version > store.version && !this.saver.isDirty(image.id) && !this.rescanReview) {
      await this.reloadCells(image, "Updated with the latest saved changes.");
    } else if (beforeStatus !== image.status) {
      this.updateScene();
      this.updateInfoPanel();
    }
  }

  private async refreshSamples(): Promise<void> {
    try {
      const { samples } = await get<{ samples: SampleInfo[] }>(`/api/projects/${this.boot.id}/samples`);
      this.samples = samples;
      this.renderSamples();
      this.syncToolbar();
      if (!this.prefix) this.selectInitialImage();
      else if (!this.viewingComposite && this.channel) {
        const image = this.currentImage();
        if (image && image.status === "done" && this.store?.imageId !== image.id) {
          this.selectImage(this.prefix, this.channel, true);
        } else {
          this.updateScene();
          this.updateInfoPanel();
        }
      }
    } catch (err) {
      this.statusbar.setMessage(`Couldn't refresh the sample list: ${errorMessage(err)}`);
    }
  }

  // ------------------------------------------------------------------ //
  // Navigation
  // ------------------------------------------------------------------ //
  private selectInitialImage(): void {
    const last = this.ui.last_image_id;
    for (const s of this.samples) {
      for (const ch of CHANNELS) {
        if (last && s.channels[ch]?.id === last) {
          this.selectImage(s.prefix, ch);
          return;
        }
      }
    }
    const first = this.samples[0];
    const ch = first && CHANNELS.find((c) => first.channels[c]);
    if (first && ch) {
      this.selectImage(first.prefix, ch);
    } else {
      this.updateScene();
      this.updateInfoPanel();
    }
  }

  private onSampleClick(prefix: string, channel: Channel | null): void {
    const sample = this.sample(prefix);
    if (!sample) return;
    if (!channel || !sample.channels[channel]) {
      channel = [this.channel, ...CHANNELS].find((c): c is Channel => !!c && !!sample.channels[c]) ?? null;
    }
    if (channel) this.selectImage(prefix, channel);
  }

  /** Leave whatever's on screen: abandon a rescan review, save edits, give back
   * the edit lease. */
  private leaveCurrent(): void {
    if (this.rescanReview) this.exitRescanReview();
    this.interactions.reset();
    const store = this.store;
    if (store) {
      void this.saver.flush(store.imageId).then(() => {
        if (this.store?.imageId !== store.imageId) this.lease.release();
      });
    } else {
      this.lease.release();
    }
    this.leaseState = null;
  }

  selectImage(prefix: string, channel: Channel, force = false): void {
    const sample = this.sample(prefix);
    const image = sample?.channels[channel];
    if (!sample || !image) return;
    if (!force && !this.viewingComposite && prefix === this.prefix && channel === this.channel) return;

    const sameSample = prefix === this.prefix;
    const sameImage = !this.viewingComposite && this.store?.imageId === image.id;
    if (!sameImage) this.leaveCurrent();
    if (!sameSample) this.storeCache = new Map([...this.storeCache].filter(([id]) => this.saver.isDirty(id)));
    this.prefix = prefix;
    this.channel = channel;
    this.viewingComposite = false;
    this.composite = null;
    if (!sameImage) {
      this.mode = "review";
      this.store = null;
    }
    this.applyViewport(sameSample, image);
    this.saveUi({ last_image_id: image.id });
    this.samplesPanel.update(this.readiness, prefix);
    this.samplesPanel.scrollTo(prefix);
    this.syncToolbar();
    this.updateScene();
    this.updateInfoPanel();
    this.statusbar.setMessage("");
    if (image.status === "done") void this.loadCells(image, sameImage);
  }

  /** Keep pan/zoom within a sample (its channels are
   * the same field of view), fit × the remembered zoom on a new one. */
  private applyViewport(sameSample: boolean, image: ImageInfo): void {
    if (!image.width || !image.height) return;
    if (!this.viewer.sized) {
      this.needsFit = true; // done in onFirstLayout, once the canvas has a real size
      return;
    }
    if (!sameSample || this.needsFit) {
      this.needsFit = false;
      this.viewer.view.fit(image.width, image.height, this.ui.zoom_multiplier ?? 1);
    }
  }

  private async loadCells(image: ImageInfo, keepEdits: boolean): Promise<void> {
    const seq = ++this.loadSeq;
    const cached = this.storeCache.get(image.id);
    let store: CellStore;
    if (cached && (keepEdits || cached.version >= image.cells_version || this.saver.isDirty(image.id))) {
      store = cached;
    } else {
      try {
        const data = await get<CellsResponse>(`/api/images/${image.id}/cells`);
        store = new CellStore(image.id, data.version, data.cells);
      } catch (err) {
        if (seq === this.loadSeq) this.statusbar.setMessage(`Couldn't load ${image.filename}: ${errorMessage(err)}`);
        return;
      }
    }
    if (seq !== this.loadSeq || this.currentImage()?.id !== image.id || this.viewingComposite) return;
    this.storeCache.set(image.id, store);
    this.store = store;
    if (this.canEdit) {
      this.leaseState = await this.lease.acquire(image.id);
      if (seq !== this.loadSeq) return;
    } else {
      this.leaseState = { imageId: image.id, heldByMe: false, holder: null, readOnly: true };
    }
    this.syncToolbar();
    this.updateScene();
    this.updateInfoPanel();
    // Only worth a status line when it changes what you can do here.
    if (this.leaseState && !this.leaseState.heldByMe && this.leaseState.holder) {
      this.statusbar.setMessage(`${this.leaseState.holder} is editing this image — view only.`);
    }
  }

  private async reloadCells(image: ImageInfo, message: string): Promise<void> {
    this.saver.discard(image.id);
    this.undo.forget(image.id);
    this.storeCache.delete(image.id);
    this.store = null;
    await this.loadCells(image, false);
    this.statusbar.setMessage(`${image.filename}: ${message}`);
  }

  private async reloadAfterConflict(imageId: string): Promise<void> {
    const image = this.currentImage();
    this.log.append("An edit couldn't be saved because the image changed on the server; reloaded the latest version.");
    if (image?.id === imageId) await this.reloadCells(image, "Changed by someone else — reloaded. Your last edit wasn't saved.");
    else this.storeCache.delete(imageId);
  }

  private async lostLock(imageId: string, holder: string): Promise<void> {
    const image = this.currentImage();
    this.log.append(`${holder} is editing ${image?.filename ?? "an image"}; your unsaved edit there was discarded.`);
    if (image?.id === imageId) {
      this.leaseState = { imageId, heldByMe: false, holder, readOnly: false };
      await this.reloadCells(image, `${holder} is editing this image — view only.`);
    }
  }

  private leaseChanged(state: LeaseState): void {
    const before = this.leaseState;
    if (!before || before.imageId !== state.imageId) return;
    this.leaseState = state;
    if (!before.heldByMe && state.heldByMe) this.statusbar.setMessage("You can edit this image now.");
    if (before.heldByMe !== state.heldByMe) {
      this.syncToolbar();
      this.updateInfoPanel();
    }
  }

  async selectComposite(prefix: string, force = false): Promise<void> {
    if (!this.compositeAvailable(prefix)) return;
    if (this.viewingComposite && prefix === this.prefix && !force) return;
    const sample = this.sample(prefix)!;
    const sameSample = prefix === this.prefix;
    if (!this.viewingComposite) this.leaveCurrent();
    this.prefix = prefix;
    this.viewingComposite = true;
    this.store = null;
    this.mode = "review";
    const snap = sample.channels.SNAP!;
    this.applyViewport(sameSample, snap);
    this.saveUi({ last_image_id: snap.id });
    this.syncToolbar();
    this.samplesPanel.update(this.readiness, prefix);
    this.statusbar.setMessage("");

    const seq = ++this.loadSeq;
    try {
      const [result, cck, chr] = await Promise.all([
        get<CompositeResponse>(`/api/samples/${sample.id}/composite`),
        get<CellsResponse>(`/api/images/${sample.channels.CCK!.id}/cells`),
        get<CellsResponse>(`/api/images/${sample.channels.CHR!.id}/cells`),
      ]);
      if (seq !== this.loadSeq || !this.viewingComposite || this.prefix !== prefix) return;
      const kept = (cells: Cell[]) => cells.filter((c) => c.status === "kept");
      const cckIds = new Set(result.cck_ids ?? []);
      const chrIds = new Set(result.chr_ids ?? []);
      const cckKept = kept(cck.cells);
      const chrKept = kept(chr.cells);
      this.composite = {
        sampleId: sample.id,
        cck: new CellStore(cck.image_id, cck.version, cckKept),
        chr: new CellStore(chr.image_id, chr.version, chrKept),
        highlight: entriesFor([...cckKept.filter((c) => cckIds.has(c.id)), ...chrKept.filter((c) => chrIds.has(c.id))]),
        result,
        versions: `${cck.version}/${chr.version}`,
      };
      this.updateScene();
      this.updateInfoPanel();
    } catch (err) {
      if (seq === this.loadSeq) this.statusbar.setMessage(`Couldn't load the composite: ${errorMessage(err)}`);
    }
  }

  // ------------------------------------------------------------------ //
  // Viewport
  // ------------------------------------------------------------------ //
  private fit(): void {
    const image = this.currentImage();
    if (!image?.width || !image.height) return;
    this.viewer.view.fit(image.width, image.height, 1);
    this.saveUi({ zoom_multiplier: 1 });
    this.viewer.requestRender();
  }

  private rememberZoom(): void {
    const image = this.currentImage();
    if (!image?.width || !image.height || this.rescanReview) return;
    const multiplier = this.viewer.view.scale / this.viewer.view.fitScale(image.width, image.height);
    this.saveUi({ zoom_multiplier: Math.round(multiplier * 1000) / 1000 });
  }

  private saveUi(patch: UiState): void {
    this.ui = { ...this.ui, ...patch };
    clearTimeout(this.uiSaveTimer);
    this.uiSaveTimer = window.setTimeout(() => {
      void put(`/api/projects/${this.boot.id}/ui-state`, this.ui).catch(() => {});
    }, UI_STATE_SAVE_MS);
  }

  // ------------------------------------------------------------------ //
  // What the canvas shows, and the right-hand panel
  // ------------------------------------------------------------------ //
  private updateScene(): void {
    const image = this.currentImage();
    const tiled: TiledImage | null = image?.tiles && image.width && image.height
      ? { id: image.id, width: image.width, height: image.height, tiles: image.tiles } : null;
    const scene: Scene = { image: tiled, placeholder: placeholderFor(image, this.samples.length) };
    scene.drawImageSpace = (ctx, view) => {
      const rect = expand(view.visibleRect(), 12 / view.scale);
      const mode: RenderMode = this.ui.render_mode ?? "outline";
      if (this.rescanReview) {
        const { pending, accepted } = this.rescanReview.split();
        drawCells(ctx, pending, view.scale, { mode: "outline", color: RESCAN_PROPOSED_COLOR, allKept: true });
        drawCells(ctx, accepted, view.scale, { mode: "mask", color: RESCAN_ACCEPTED_COLOR, allKept: true });
      } else if (this.viewingComposite && this.composite) {
        drawCells(ctx, this.composite.cck.query(rect), view.scale, { mode: "mask", color: this.colorOf("CCK") });
        drawCells(ctx, this.composite.chr.query(rect), view.scale, { mode: "mask", color: this.colorOf("CHR") });
        drawCells(ctx, this.composite.highlight, view.scale, { mode: "outline", color: COEXPRESS_HIGHLIGHT_COLOR });
      } else if (this.store && this.channel) {
        drawCells(ctx, this.store.query(rect), view.scale, { mode, color: this.colorOf(this.channel) });
      }
    };
    this.viewer.setScene(scene);
  }

  private updateInfoPanel(): void {
    const l = this.layout;
    const image = this.currentImage();
    l.rescanControls.hidden = !this.rescanReview;
    l.instructions.hidden = !!this.rescanReview;
    l.noticeAction.hidden = true;
    l.notice.textContent = "";

    if (this.rescanReview) {
      const r = this.rescanReview;
      const c = r.current;
      l.title.textContent = `Rescan ${r.index + 1}/${r.combos.length}  (diameter=${c.diameter}, cellprob=${c.cellprob_threshold})`;
      l.counts.textContent = `${(c.cells ?? []).length} proposed here\n${r.accepted.size} accepted (all combos)`;
      return;
    }
    if (this.viewingComposite) {
      l.title.textContent = `${this.prefix} — Composite`;
      const res = this.composite?.result;
      l.counts.textContent = res
        ? `${plural(res.n_pairs ?? 0, "coexpressing pair")}\n${res.snap_kept ?? 0} SNAP kept (population)\n`
          + `${res.rate_pct === null || res.rate_pct === undefined ? "—" : res.rate_pct.toFixed(1)}% coexpression rate`
        : "Loading…";
      l.notice.textContent = "View only. Edit on the SNAP, CCK or CHR tab.";
      return;
    }
    l.title.textContent = image?.filename ?? (this.samples.length ? "" : "No images yet");
    if (!image) {
      l.counts.textContent = this.canEdit ? "Use File › Add Images… or drop a folder onto this window." : "";
      return;
    }
    if (this.store) {
      const n = this.store.counts();
      l.counts.textContent = `${n.kept} kept\n${n.filtered} filtered\n${n.total} total\n${n.edited} edited`;
    } else {
      l.counts.textContent = statusLine(image);
    }
    if (image.status === "error") {
      l.notice.textContent = image.error ?? "Processing failed.";
      if (this.canEdit) {
        l.noticeAction.textContent = "Retry";
        l.noticeAction.hidden = false;
        l.noticeAction.onclick = () => void this.retry([image.id]);
      }
    } else if (!this.canEdit) {
      l.notice.textContent = "View only — you have viewer access to this project.";
    } else if (this.leaseState && !this.leaseState.heldByMe && this.leaseState.holder) {
      l.notice.textContent = `View only — ${this.leaseState.holder} is editing this image.`;
    }
  }

  /** View › Show Log / Show GPU Queue / Show Review: the log and the GPU
   * queue replace the whole review area, toolbar included — in place, so an
   * upload in progress carries on. */
  private setView(view: View): void {
    if (view === this.view) return;
    this.view = view;
    this.layout.main.hidden = view !== "review";
    this.layout.toolbar.hidden = view !== "review";
    this.log.root.hidden = view !== "log";
    this.queuePane.hidden = view !== "queue";
    if (view === "queue") this.queueView.show();
    else this.queueView.hide();
    if (view === "review") this.viewer.requestRender();
    this.menus.refresh();
  }

  private renderUploadProgress(): void {
    const p = this.uploads.progress();
    this.uploadPanel.update(p);
    const working = p.stage === "working";
    const fraction = p.bytesTotal ? p.bytesDone / p.bytesTotal : 0;
    // In the tab title too, for when the page is in a background tab.
    const base = `${this.boot.name} — Cell Counts`;
    document.title = working ? `(${p.paused ? "stopped" : `${Math.floor(fraction * 100)}%`}) ${base}` : base;
  }

  // ------------------------------------------------------------------ //
  // InteractionHost: what gestures do
  // ------------------------------------------------------------------ //
  context(): Context {
    if (this.viewingComposite) return "composite";
    if (this.rescanReview) return "rescan-review";
    const image = this.currentImage();
    if (!this.canEdit || !this.store || !image || image.status !== "done") return "readonly";
    if (this.leaseState && !this.leaseState.heldByMe) return "readonly";
    return "edit";
  }

  requestRender(): void {
    this.viewer.requestRender();
  }

  private perform(action: Action): void {
    if (!this.store) return;
    this.saver.queue(this.store, this.undo.perform(this.store, action));
    this.afterEdit();
  }

  private afterEdit(): void {
    this.updateInfoPanel();
    this.viewer.requestRender();
  }

  private undoEdit(): void {
    if (!this.store || this.context() !== "edit") return;
    const ops = this.undo.undo(this.store);
    if (ops) {
      this.saver.queue(this.store, ops);
      this.afterEdit();
    }
  }

  private redoEdit(): void {
    if (!this.store || this.context() !== "edit") return;
    const ops = this.undo.redo(this.store);
    if (ops) {
      this.saver.queue(this.store, ops);
      this.afterEdit();
    }
  }

  private cellsIn(region: Region): Cell[] {
    if (!this.store) return [];
    return region.kind === "rect" ? this.store.inRect(region.rect) : this.store.inLasso(region.points);
  }

  reviewClick(x: number, y: number): void {
    const cell = this.store?.hitTest(x, y, this.viewer.view.scale);
    if (cell) this.perform(ModifyCells.setStatus([cell], cell.status === "kept" ? "filtered" : "kept"));
  }

  async reviewSelect(region: Region): Promise<void> {
    const cells = this.cellsIn(region);
    if (!cells.length) return;
    const choice = await showDialog<"kept" | "filtered" | null>({
      title: "Mark detections",
      body: cells.length === 1
        ? "1 detection selected. Mark it as a cell, or as not a cell?"
        : `${cells.length} detections selected. Mark them all as cells, or as not cells?`,
      buttons: [
        { label: "Cancel", value: null },
        { label: "Not Cells", value: "filtered" },
        { label: "Cells", value: "kept", default: true },
      ],
      cancelValue: null,
    });
    if (choice && this.store) this.perform(ModifyCells.setStatus(cells.filter((c) => this.store!.has(c.id)), choice));
  }

  deleteClick(x: number, y: number): void {
    const cell = this.store?.hitTest(x, y, this.viewer.view.scale);
    if (cell) this.perform(new RemoveCells([cell]));
  }

  deleteSelect(region: Region): void {
    const cells = this.cellsIn(region);
    if (!cells.length) return;
    this.perform(new RemoveCells(cells));
    this.statusbar.setMessage(`Deleted ${plural(cells.length, "cell")}.`);
  }

  commitDraw(points: Point[]): void {
    if (points.length < 3) {
      if (points.length) void info("Draw cell", "Need at least 3 points to close a cell outline.");
      return;
    }
    if (!this.store) return;
    const ring = points.map(([x, y]) => [round2(x), round2(y)]);
    const { area, centroid } = shoelaceAreaCentroid(ring);
    const cell: Cell = { id: this.store.maxId() + 1, polygons: [ring], centroid, area, status: "kept",
      color: null, source: "human", edited: true };
    this.perform(new AddCells([cell]));
  }

  async rescanRegion(rect: Rect): Promise<void> {
    const image = this.currentImage();
    if (!image?.width || !image.height) return;
    if (this.rescanRunner) {
      void info("Rescan", "A rescan sweep is already running — wait for it to finish before starting another.");
      return;
    }
    const clamped = normalizeRect(Math.max(0, rect[0]), Math.max(0, rect[1]),
      Math.min(image.width, rect[2]), Math.min(image.height, rect[3]));
    if (clamped[2] - clamped[0] < MIN_RESCAN_SIZE || clamped[3] - clamped[1] < MIN_RESCAN_SIZE) {
      void info("Rescan", "Drag a larger region to rescan.");
      return;
    }
    const params = await askRescanParams(clamped[2] - clamped[0], clamped[3] - clamped[1]);
    if (!params) return;
    // Snap the mode back to Review while the sweep runs.
    this.setMode("review");
    const runner = new SweepRunner();
    this.rescanRunner = runner;
    const n = params.diameters.length * params.cellprobs.length;
    this.statusbar.setMessage(`Rescan: waiting for the GPU (${plural(n, "combination")})...`);
    try {
      const detail = await runner.run(image.id, clamped, params, (done, total) => {
        this.statusbar.setMessage(done ? `Rescan: combination ${done}/${total}...` : `Rescan: waiting for the GPU (${plural(total, "combination")})...`);
      });
      const ok = detail.combos.filter((c) => c.status === "done" && c.cells);
      if (!ok.length) {
        void info("Rescan failed", "Every combination failed.");
        this.statusbar.setMessage("Rescan failed.");
        return;
      }
      if (this.currentImage()?.id !== image.id || this.viewingComposite) {
        void info("Rescan", `Rescan of ${image.filename} finished, but you've since navigated elsewhere. `
          + "Reopen that channel and rescan again to review the results.");
        return;
      }
      this.enterRescanReview(new RescanReview(image.id, clamped, ok), detail.combos.length - ok.length);
    } catch (err) {
      if ((err as Error).message !== "cancelled") {
        void info("Rescan failed", errorMessage(err));
        this.statusbar.setMessage("Rescan failed.");
      }
    } finally {
      this.rescanRunner = null;
    }
  }

  private enterRescanReview(review: RescanReview, failed: number): void {
    this.rescanReview = review;
    this.preRescanView = this.viewer.view.snapshot();
    this.viewer.view.fitToRect(review.rect);
    this.syncToolbar();
    this.updateScene();
    this.updateInfoPanel();
    const anyCells = review.combos.some((c) => (c.cells ?? []).length);
    this.statusbar.setMessage(anyCells
      ? `Rescan: reviewing ${plural(review.combos.length, "combination")}${failed ? ` (${failed} failed)` : ""}.`
      : `Rescan: no cells found in any of ${plural(review.combos.length, "combination")}.`);
  }

  rescanReviewClick(x: number, y: number): void {
    if (this.rescanReview?.toggleAt(x, y, this.viewer.view.scale)) {
      this.updateInfoPanel();
      this.viewer.requestRender();
    }
  }

  private stepRescan(delta: number): void {
    if (!this.rescanReview) return;
    this.rescanReview.step(delta);
    this.updateInfoPanel();
    this.viewer.requestRender();
  }

  private async mergeRescan(): Promise<void> {
    const review = this.rescanReview;
    if (!review || !this.store) return;
    if (!review.accepted.size) {
      void info("Rescan", "Nothing accepted yet — click proposed cells to accept them before merging.");
      return;
    }
    let nextId = this.store.maxId() + 1;
    const merged: Cell[] = [...review.accepted.values()].map(({ cell, jobId }) => ({
      ...cell, id: nextId++, status: "kept", source: `rescan:${jobId}`, edited: true,
    }));
    this.exitRescanReview();
    this.perform(new AddCells(merged));
    this.statusbar.setMessage(`Rescan: merged ${plural(merged.length, "cell")}.`);
  }

  private discardRescan(): void {
    if (!this.rescanReview) return;
    this.exitRescanReview();
    this.statusbar.setMessage("Rescan discarded.");
  }

  private exitRescanReview(): void {
    this.rescanReview = null;
    if (this.preRescanView) this.viewer.view.restore(this.preRescanView);
    this.preRescanView = null;
    this.syncToolbar();
    this.updateScene();
    this.updateInfoPanel();
  }

  // ------------------------------------------------------------------ //
  // File and Process menu actions
  // ------------------------------------------------------------------ //
  private addImages(): void {
    this.layout.folderInput.click();
  }

  /** Show "Reading folder…" straight away: walking a big dropped folder, and
   * reading its review files, can take a while before anything uploads. */
  private async addFound(found: FoundFile[] | Promise<FoundFile[]>): Promise<void> {
    this.uploads.beginScan();
    const files = await found;
    await this.uploads.add(await scanFiles(files));
  }

  private async exportData(): Promise<void> {
    if (!this.samples.length) {
      void info("Export Data", "No samples in this project yet.");
      return;
    }
    const choice = await askExport(this.samples.map((s) => s.prefix), this.readiness);
    if (!choice) return;
    this.statusbar.setMessage(`Preparing the export of ${plural(choice.prefixes.length, "sample")}...`);
    try {
      const filename = await runExport(this.boot.id, choice);
      this.statusbar.setMessage(`Exported ${plural(choice.prefixes.length, "sample")} to ${filename}`);
    } catch (err) {
      void info("Export failed", errorMessage(err));
      this.statusbar.setMessage("Export failed.");
    }
  }

  private async toggleSegmenting(): Promise<void> {
    const paused = !this.segmentingPaused;
    try {
      await post("/api/queue/pause", { paused });
      this.segmentingPaused = paused;
      this.statusbar.setMessage(paused
        ? "Segmenting stopped: your images won't start on the GPU until you start it again."
        : "Segmenting started again.");
      this.menus.refresh();
    } catch (err) {
      this.log.append(`Couldn't ${paused ? "pause" : "resume"} segmentation: ${errorMessage(err)}`);
    }
  }

  private async retry(imageIds: string[]): Promise<void> {
    if (!imageIds.length) return;
    try {
      const { retried } = await post<{ retried: string[] }>("/api/images/retry", { image_ids: imageIds });
      this.statusbar.setMessage(`Queued ${plural(retried.length, "image")} to try again.`);
      this.schedulePoll(0);
    } catch (err) {
      this.statusbar.setMessage(`Couldn't retry: ${errorMessage(err)}`);
    }
  }
}

// --------------------------------------------------------------------------- //
// Helpers
// --------------------------------------------------------------------------- //
/** Segmenting now, then preparing, then waiting for the GPU. */
function queueRank(row: QueueRow): number {
  if (row.kind === "segment" && row.status === "running") return -1;
  if (row.kind === "prepare") return 0;
  return 1 + (row.ahead ?? 1e6);
}

function formatEta(seconds: number): string {
  if (seconds < 60) return "under a minute";
  const minutes = Math.round(seconds / 60);
  return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

function statusLine(image: ImageInfo): string {
  switch (image.status) {
    case "preparing":
      return `${image.filename} — preparing on the server...`;
    case "queued":
      return `${image.filename} — waiting for the GPU.`;
    case "segmenting":
      return `${image.filename} — segmenting now...`;
    case "error":
      return `${image.filename} — processing failed.`;
    default:
      return image.filename;
  }
}

function placeholderFor(image: ImageInfo | null, nSamples: number): string {
  if (!image) return nSamples ? "" : "No images yet — File › Add Images…, or drop a folder here.";
  if (image.status === "error") return `${image.filename} couldn't be processed.`;
  return `Preparing ${image.filename}…`;
}

function expand(rect: Rect, by: number): Rect {
  return [rect[0] - by, rect[1] - by, rect[2] + by, rect[3] + by];
}

/** Black or white label text on a channel-color swatch, whichever reads. */
function contrastText(hex: string): string {
  const v = parseInt(hex.slice(1), 16);
  const lum = (0.299 * ((v >> 16) & 255) + 0.587 * ((v >> 8) & 255) + 0.114 * (v & 255)) / 255;
  return lum > 0.55 ? "#000000" : "#ffffff";
}

