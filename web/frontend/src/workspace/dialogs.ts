// dialogs.ts — the review screen's modal dialogs, built on <dialog> and styled
// like the desktop app's Toplevel dialogs: a title bar, 16 px of padding, and
// right-aligned buttons with the default one rightmost. Esc cancels, Enter
// presses the default button. Never window.alert/confirm/prompt: those freeze
// the page (and the uploads running in it) and look nothing like the app.

import { el } from "./dom";

export interface DialogButton<T> {
  label: string;
  value: T;
  default?: boolean;
  danger?: boolean;
}

export interface DialogOptions<T> {
  title: string;
  body: Node | string | (Node | string)[];
  buttons: DialogButton<T>[];
  /** Returned when the dialog is dismissed with Esc. */
  cancelValue: T;
  /** Called on every button press; return false to keep the dialog open
   * (e.g. a form whose input doesn't validate yet). */
  beforeClose?: (value: T) => boolean;
  wide?: boolean;
}

export function isDialogOpen(): boolean {
  return document.querySelector("dialog.ws-dialog[open]") !== null;
}

export function showDialog<T>(options: DialogOptions<T>): Promise<T> {
  return new Promise((resolve) => {
    const dialog = el("dialog", { class: `ws-dialog${options.wide ? " wide" : ""}` });
    const body = el("div", { class: "ws-dialog-body" });
    for (const part of Array.isArray(options.body) ? options.body : [options.body]) {
      body.append(typeof part === "string" ? el("p", { text: part }) : part);
    }
    const buttons = el("div", { class: "buttons" });
    let defaultButton: HTMLButtonElement | null = null;

    const finish = (value: T) => {
      if (options.beforeClose && !options.beforeClose(value)) return;
      dialog.close();
      dialog.remove();
      resolve(value);
    };

    // Rightmost is the default, matching the desktop dialogs (Cancel, then OK).
    for (const spec of options.buttons) {
      const button = el("button", {
        text: spec.label,
        class: [spec.default ? "default" : "", spec.danger ? "danger" : ""].join(" ").trim(),
        attrs: { type: "button" },
        on: { click: () => finish(spec.value) },
      });
      if (spec.default) defaultButton = button;
      buttons.append(button);
    }

    dialog.append(el("div", { class: "titlebar", text: options.title }), body, buttons);
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      finish(options.cancelValue);
    });
    dialog.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" || !defaultButton) return;
      const target = event.target as HTMLElement;
      if (target.tagName === "BUTTON" || target.tagName === "TEXTAREA") return;
      event.preventDefault();
      defaultButton.click();
    });
    document.body.append(dialog);
    dialog.showModal();
    const firstInput = body.querySelector<HTMLElement>("input, select, textarea");
    (firstInput ?? defaultButton ?? buttons.querySelector("button"))?.focus();
  });
}

export function info(title: string, message: string | Node | (string | Node)[]): Promise<void> {
  return showDialog<void>({
    title,
    body: message,
    buttons: [{ label: "OK", value: undefined, default: true }],
    cancelValue: undefined,
  });
}

export function confirmAction(title: string, message: string | Node | (string | Node)[], okLabel: string,
                              danger = false): Promise<boolean> {
  return showDialog<boolean>({
    title,
    body: message,
    buttons: [
      { label: "Cancel", value: false },
      { label: okLabel, value: true, default: true, danger },
    ],
    cancelValue: false,
  });
}

/** A short scrolling list inside a dialog (file names, sample names). */
export function listBox(items: string[], max = 200): HTMLElement {
  const box = el("ul", { class: "ws-dialog-list" });
  for (const item of items.slice(0, max)) box.append(el("li", { text: item }));
  if (items.length > max) box.append(el("li", { class: "muted", text: `…and ${items.length - max} more` }));
  return box;
}
