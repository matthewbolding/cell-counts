// statusbar.ts — the bottom bar: a message on the left narrating what's going on,
// the connection light on the right (statusbar.py: Idle / Connecting… /
// Connected / Processing… / Error), and a log pane for View > Show Log.

import { STATE_LABELS, type LightState } from "./constants";
import { el } from "./dom";

export class StatusBar {
  readonly root: HTMLElement;
  private readonly message: HTMLElement;
  private readonly light: HTMLElement;
  private state: LightState = "idle";

  constructor() {
    this.message = el("span", { class: "message", text: "Ready" });
    this.light = el("span", { class: "light", dataset: { state: "idle" }, text: STATE_LABELS.idle });
    this.root = el("footer", { class: "statusbar" }, this.message, this.light);
  }

  setMessage(text: string): void {
    this.message.textContent = text;
    this.message.title = text;
  }

  setState(state: LightState): void {
    if (state === this.state) return;
    this.state = state;
    this.light.dataset.state = state;
    this.light.textContent = STATE_LABELS[state];
  }

  get lightState(): LightState {
    return this.state;
  }
}

export class LogPane {
  readonly root: HTMLElement;
  private readonly lines: HTMLElement;

  constructor() {
    this.lines = el("div", { class: "ws-log-lines", attrs: { role: "log", "aria-live": "polite" } });
    this.root = el("section", { class: "ws-log", attrs: { hidden: "" } }, this.lines);
  }

  append(text: string): void {
    const stamp = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    const atBottom = this.root.scrollTop + this.root.clientHeight >= this.root.scrollHeight - 4;
    this.lines.append(el("div", {}, el("span", { class: "muted", text: `${stamp}  ` }), text));
    if (atBottom) this.root.scrollTop = this.root.scrollHeight;
  }
}
