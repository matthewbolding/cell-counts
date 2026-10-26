// uploadPanel.ts — the upload strip across the bottom of the window, above the
// status bar: one bar for the whole folder, how many files and gigabytes are
// done, speed and time left, and what's being checked or uploaded right now —
// the information a file-copy dialog gives, for uploads that can run for hours.
// Full width so long file names and the numbers fit; hidden when nothing is
// uploading; after a batch it shows the outcome until closed.

import { el } from "./dom";
import { formatBytes, formatDuration, type UploadProgress } from "./uploads/uploader";

export class UploadPanel {
  readonly root: HTMLElement;
  private readonly heading: HTMLElement;
  private readonly percent: HTMLElement;
  private readonly bar: HTMLProgressElement;
  private readonly detail: HTMLElement;
  private readonly toggle: HTMLButtonElement;
  private readonly close: HTMLButtonElement;
  onToggle: () => void = () => {};
  onClose: () => void = () => {};

  constructor() {
    this.heading = el("strong", { class: "ws-upload-heading" });
    this.percent = el("span", { class: "ws-upload-percent" });
    this.bar = el("progress", { attrs: { max: "1" } });
    this.detail = el("div", { class: "ws-upload-detail" });
    this.toggle = el("button", { attrs: { type: "button" }, on: { click: () => this.onToggle() } });
    this.close = el("button", { text: "Close", attrs: { type: "button" }, on: { click: () => this.onClose() } });
    this.root = el("section", { class: "ws-upload", attrs: { hidden: "", "aria-live": "polite", "aria-label": "Uploads" } },
      el("div", { class: "ws-upload-main" }, this.heading, this.percent, this.bar, this.toggle, this.close),
      this.detail);
  }

  update(p: UploadProgress): void {
    this.root.hidden = p.stage === "idle";
    if (this.root.hidden) return;

    const done = p.stage === "done";
    this.close.hidden = !done;
    this.toggle.hidden = done || p.stage !== "working";
    this.toggle.textContent = p.paused ? "Start Uploads" : "Stop Uploads";
    this.root.classList.toggle("paused", p.paused && !done);

    if (p.stage === "scanning" || p.stage === "checking") {
      this.heading.textContent = p.stage === "scanning" ? "Reading the folder…" : "Checking with the server…";
      this.percent.textContent = "";
      this.bar.removeAttribute("value"); // indeterminate
      this.detail.textContent = "";
      return;
    }
    if (done) {
      this.heading.textContent = "Upload finished";
      this.percent.textContent = "";
      this.bar.value = 1;
      this.detail.textContent = p.summary ?? "";
      return;
    }

    const fraction = p.bytesTotal ? p.bytesDone / p.bytesTotal : 0;
    this.heading.textContent = p.paused ? "Uploads stopped" : "Uploading";
    this.percent.textContent = `${Math.floor(fraction * 100)}%`;
    this.bar.value = fraction;

    const parts = [`${p.filesDone} of ${p.filesTotal} files`,
      `${formatBytes(p.bytesDone)} of ${formatBytes(p.bytesTotal)}`];
    if (p.failed) parts.push(`${p.failed} failed`);
    if (p.paused) parts.push("Process › Start Uploads, or the button above, to continue");
    else if (p.bytesPerSecond === 0) parts.push("nothing sent for a while — waiting for the connection");
    else if (p.bytesPerSecond) {
      parts.push(`${formatBytes(p.bytesPerSecond)}/s`);
      if (p.etaSeconds !== null) parts.push(`${formatDuration(p.etaSeconds)} left`);
    } else if (p.uploading) parts.push("measuring speed…");
    if (p.uploading) {
      const more = p.uploading.others ? ` and ${p.uploading.others} more` : "";
      parts.push(`uploading ${p.uploading.name} (${Math.floor(p.uploading.fraction * 100)}%)${more}`);
    }
    if (p.hashing) {
      parts.push(`checking ${p.hashing.name} (${Math.floor(p.hashing.fraction * 100)}%, ${p.hashed} of ${p.filesTotal})`);
    }
    this.detail.textContent = parts.join("  ·  ");
  }
}
