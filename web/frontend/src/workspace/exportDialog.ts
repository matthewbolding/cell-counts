// exportDialog.ts — File › Export Data…: pick samples
// (each with its readiness dots, so it's clear what has data), Select All /
// None, and which sheets; then the server builds the workbook in the background
// and the browser downloads it when it's ready.

import { get, post } from "./api";
import { CHANNELS, type Channel } from "./constants";
import { showDialog } from "./dialogs";
import { el } from "./dom";
import type { Readiness } from "./panels";

export interface ExportChoice {
  prefixes: string[];
  summary: boolean;
  cells: boolean;
}

export async function askExport(prefixes: string[],
                                readiness: (prefix: string, ch: Channel) => Readiness): Promise<ExportChoice | null> {
  const boxes = new Map<string, HTMLInputElement>();
  const list = el("div", { class: "ws-check-list" });
  for (const prefix of prefixes) {
    const box = el("input", { attrs: { type: "checkbox" } });
    box.checked = true;
    boxes.set(prefix, box);
    const dots = el("span", { class: "dots" });
    for (const ch of CHANNELS) {
      const r = readiness(prefix, ch);
      dots.append(el("span", { class: `dot${r === "ready" ? " ready" : r === "processing" ? " processing" : ""}`, title: ch }));
    }
    list.append(el("label", { class: "ws-check-row" }, box, el("span", { class: "ws-row-label", text: prefix }), dots));
  }
  const setAll = (value: boolean) => boxes.forEach((b) => (b.checked = value));
  const summary = el("input", { attrs: { type: "checkbox" } });
  const cells = el("input", { attrs: { type: "checkbox" } });
  summary.checked = cells.checked = true;
  const problem = el("p", { class: "error-text" });

  const body = el("div", { class: "ws-form" },
    el("h3", { text: "Samples to export:" }),
    list,
    el("div", { class: "buttons left" },
      el("button", { text: "Select All", attrs: { type: "button" }, on: { click: () => setAll(true) } }),
      el("button", { text: "Select None", attrs: { type: "button" }, on: { click: () => setAll(false) } })),
    el("hr"),
    el("h3", { text: "Include:" }),
    el("label", { class: "check" }, summary, "Summary sheet (one row per sample)"),
    el("label", { class: "check" }, cells, "Cells sheet (one row per kept cell)"),
    problem);

  let choice: ExportChoice | null = null;
  const go = await showDialog<boolean>({
    title: "Export Data",
    body,
    buttons: [{ label: "Cancel", value: false }, { label: "Export…", value: true, default: true }],
    cancelValue: false,
    beforeClose: (ok) => {
      if (!ok) return true;
      const picked = prefixes.filter((p) => boxes.get(p)!.checked);
      if (!picked.length) {
        problem.textContent = "Select at least one sample to export.";
        return false;
      }
      if (!summary.checked && !cells.checked) {
        problem.textContent = "Select at least Summary or Cells to include.";
        return false;
      }
      choice = { prefixes: picked, summary: summary.checked, cells: cells.checked };
      return true;
    },
  });
  return go ? choice : null;
}

interface ExportDetail {
  status: string;
  filename: string;
  download_url: string | null;
  error: string | null;
}

/** Create the export and wait for it; resolves with the file name once the
 * download has been started. */
export async function runExport(projectId: string, choice: ExportChoice): Promise<string> {
  const { export_id } = await post<{ export_id: string }>(`/api/projects/${projectId}/exports`, {
    prefixes: choice.prefixes, summary: choice.summary, cells: choice.cells });
  for (;;) {
    await new Promise((r) => setTimeout(r, 1000));
    const detail = await get<ExportDetail>(`/api/exports/${export_id}`);
    if (detail.error) throw new Error(detail.error);
    if (detail.download_url) {
      const a = el("a", { attrs: { href: detail.download_url, download: detail.filename, hidden: "" } });
      document.body.append(a);
      a.click();
      a.remove();
      return detail.filename;
    }
  }
}
