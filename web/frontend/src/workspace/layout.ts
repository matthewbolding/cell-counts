// layout.ts — builds the review screen's static DOM: toolbar, left column
// (Samples over Queue, with a draggable divider), tabs over the canvas, the
// right-hand info panel, and the log pane. Returns references to the pieces the
// controller updates; behavior is wired up in workspace.ts.

import { CHANNELS, type Channel } from "./constants";
import { el, isMac, shortcut } from "./dom";
import type { DrawStyle, SelectionStyle } from "./types";
import type { Mode } from "./interactions";

export const INSTRUCTIONS = [
  "Review: click toggles cell/not-cell; drag-select marks many at once.",
  "Draw: click to place vertices; double-click or Enter closes (needs ≥3 points); Esc cancels. Freehand: drag an outline.",
  "Delete: click a cell to remove it.",
  "Rescan: drag a region to try other Cellpose parameters on just that crop.",
  `${shortcut("Z")} undoes, ${shortcut("Z", true)} redoes — any number of times, including whole drag-select `
    + "batches as one step.",
  `Hold the right mouse button to hide outlines/masks. Scroll to pan; ${isMac ? "⌘" : "Ctrl"}+scroll or pinch `
    + "to zoom; drag with the middle button to pan.",
];

function radioGroup<T extends string>(name: string, options: [string, T][]): { root: HTMLElement; inputs: Map<T, HTMLInputElement> } {
  const inputs = new Map<T, HTMLInputElement>();
  const root = el("span", { class: "ws-radios", attrs: { role: "radiogroup" } });
  for (const [label, value] of options) {
    const input = el("input", { attrs: { type: "radio", name, value } });
    inputs.set(value, input);
    root.append(el("label", {}, input, label));
  }
  return { root, inputs };
}

export interface Layout {
  root: HTMLElement;
  toolbar: HTMLElement;
  main: HTMLElement;
  left: HTMLElement;
  modes: Map<Mode, HTMLInputElement>;
  selection: Map<SelectionStyle, HTMLInputElement>;
  draw: Map<DrawStyle, HTMLInputElement>;
  renderModeButton: HTMLButtonElement;
  swatches: Map<Channel, { button: HTMLButtonElement; input: HTMLInputElement }>;
  zoomOut: HTMLButtonElement;
  zoomFit: HTMLButtonElement;
  zoomIn: HTMLButtonElement;
  tabs: Map<Channel, HTMLButtonElement>;
  compositeTab: HTMLButtonElement;
  canvasHost: HTMLElement;
  title: HTMLElement;
  counts: HTMLElement;
  notice: HTMLElement;
  noticeAction: HTMLButtonElement;
  instructions: HTMLElement;
  rescanControls: HTMLElement;
  rescanPrev: HTMLButtonElement;
  rescanNext: HTMLButtonElement;
  rescanMerge: HTMLButtonElement;
  rescanDiscard: HTMLButtonElement;
  folderInput: HTMLInputElement;
  dropHint: HTMLElement;
}

export function buildLayout(): Layout {
  const modes = radioGroup<Mode>("ws-mode", [["Review", "review"], ["Draw", "draw"], ["Delete", "delete"], ["Rescan", "rescan"]]);
  const selection = radioGroup<SelectionStyle>("ws-select", [["Lasso", "lasso"], ["Rectangle", "rectangle"]]);
  const draw = radioGroup<DrawStyle>("ws-draw", [["Freehand", "freehand"], ["Polygon", "polygon"]]);
  const renderModeButton = el("button", { text: "Mode: Outline", attrs: { type: "button" } });

  const swatchWrap = el("span", { class: "ws-swatches" });
  const swatches = new Map<Channel, { button: HTMLButtonElement; input: HTMLInputElement }>();
  for (const ch of CHANNELS) {
    const input = el("input", { class: "ws-color-input", attrs: { type: "color", "aria-label": `${ch} color`, tabindex: "-1" } });
    const button = el("button", { class: "ws-swatch", text: ch, title: `${ch} color`, attrs: { type: "button" } });
    swatches.set(ch, { button, input });
    swatchWrap.append(el("span", { class: "ws-swatch-wrap" }, button, input));
  }

  const zoomOut = el("button", { text: "−", title: "Zoom out", attrs: { type: "button", "aria-label": "Zoom out" } });
  const zoomFit = el("button", { text: "Fit", title: "Fit to window", attrs: { type: "button" } });
  const zoomIn = el("button", { text: "+", title: "Zoom in", attrs: { type: "button", "aria-label": "Zoom in" } });

  const sep = () => el("span", { class: "ws-sep", attrs: { "aria-hidden": "true" } });
  const toolbar = el("div", { class: "ws-toolbar" },
    modes.root, sep(),
    el("span", { class: "ws-label", text: "Select:" }), selection.root, sep(),
    el("span", { class: "ws-label", text: "Draw:" }), draw.root, sep(),
    renderModeButton, sep(),
    el("span", { class: "ws-label", text: "Colors:" }), swatchWrap,
    el("span", { class: "spacer" }),
    el("span", { class: "ws-zoom" }, zoomOut, zoomFit, zoomIn));

  const tabs = new Map<Channel, HTMLButtonElement>();
  const tabBar = el("div", { class: "ws-tabs", attrs: { role: "tablist" } });
  for (const ch of CHANNELS) {
    const tab = el("button", { text: ch, attrs: { type: "button", role: "tab" } });
    tabs.set(ch, tab);
    tabBar.append(tab);
  }
  const compositeTab = el("button", { text: "Composite", attrs: { type: "button", role: "tab" } });
  tabBar.append(el("span", { class: "ws-sep" }), compositeTab);

  const canvasHost = el("div", { class: "ws-canvas-host" });
  const dropHint = el("div", { class: "ws-drop-hint", text: "Drop a folder of images to add it to this project" });
  const center = el("section", { class: "ws-center" }, tabBar, el("div", { class: "ws-canvas-wrap" }, canvasHost, dropHint));

  const title = el("h2", { class: "ws-title" });
  const counts = el("p", { class: "ws-counts" });
  const notice = el("p", { class: "ws-notice" });
  const noticeAction = el("button", { attrs: { type: "button", hidden: "" } });
  const instructions = el("div", { class: "ws-instructions" }, ...INSTRUCTIONS.map((t) => el("p", { text: t })));
  const rescanPrev = el("button", { text: "< Prev", attrs: { type: "button" } });
  const rescanNext = el("button", { text: "Next >", attrs: { type: "button" } });
  const rescanMerge = el("button", { text: "Merge Accepted", class: "default", attrs: { type: "button" } });
  const rescanDiscard = el("button", { text: "Discard Sweep", attrs: { type: "button" } });
  const rescanControls = el("div", { class: "ws-rescan", attrs: { hidden: "" } },
    el("div", { class: "ws-rescan-nav" }, rescanPrev, rescanNext), rescanMerge, rescanDiscard,
    el("p", { class: "muted", text: "Click a proposed cell to accept/un-accept it. Esc also discards." }));
  const right = el("aside", { class: "ws-right" }, title, counts, notice, noticeAction, instructions, rescanControls);

  const left = el("aside", { class: "ws-left" });
  const main = el("div", { class: "ws-main" }, left, center, right);

  const folderInput = el("input", { attrs: { type: "file", multiple: "", hidden: "" } });
  folderInput.setAttribute("webkitdirectory", "");

  const root = el("div", { class: "ws" }, toolbar, main, folderInput);
  return {
    root, toolbar, main, left, modes: modes.inputs, selection: selection.inputs, draw: draw.inputs, renderModeButton,
    swatches, zoomOut, zoomFit, zoomIn, tabs, compositeTab, canvasHost, title, counts, notice, noticeAction,
    instructions, rescanControls, rescanPrev, rescanNext, rescanMerge, rescanDiscard, folderInput, dropHint,
  };
}

/** Samples over Queue with a draggable divider (the desktop's PanedWindow,
 * weights 2:1). */
export function splitPanes(container: HTMLElement, top: HTMLElement, bottom: HTMLElement): void {
  const divider = el("div", { class: "ws-divider", attrs: { role: "separator", "aria-orientation": "horizontal" } });
  container.append(top, divider, bottom);
  let topShare = 2 / 3;
  const apply = () => {
    container.style.gridTemplateRows = `${topShare}fr 5px ${1 - topShare}fr`;
  };
  apply();
  divider.addEventListener("pointerdown", (e) => {
    divider.setPointerCapture(e.pointerId);
    const rect = container.getBoundingClientRect();
    const move = (ev: PointerEvent) => {
      topShare = Math.min(0.85, Math.max(0.15, (ev.clientY - rect.top) / rect.height));
      apply();
    };
    const up = () => {
      divider.removeEventListener("pointermove", move);
      divider.removeEventListener("pointerup", up);
    };
    divider.addEventListener("pointermove", move);
    divider.addEventListener("pointerup", up);
  });
}
