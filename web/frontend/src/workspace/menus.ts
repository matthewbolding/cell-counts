// menus.ts — the desktop-style menu bar (File / View / Process, account menu on
// the right). Same markup as the server-rendered pages (<details class="menu">,
// styled by app.css), plus what a live app needs: labels that change (Stop
// Uploads <-> Start Uploads), items that enable and disable, and
// keyboard shortcuts shown beside their items.

import { el } from "./dom";

export interface MenuItem {
  label: string | (() => string);
  action: () => void;
  shortcut?: string;
  enabled?: () => boolean;
  separatorBefore?: boolean;
}

export interface Menu {
  label: string;
  items: MenuItem[];
  right?: boolean;
  /** A non-clickable line at the top of the menu (the account menu's email). */
  heading?: string;
}

interface Rendered {
  item: MenuItem;
  button: HTMLButtonElement;
  label: HTMLElement;
}

export class MenuBar {
  readonly root: HTMLElement;
  private readonly rendered: Rendered[] = [];
  private readonly details: HTMLDetailsElement[] = [];

  constructor(title: string, subtitle: string, menus: Menu[]) {
    this.root = el("nav", { class: "menubar", attrs: { "aria-label": "Main menu" } },
      el("span", { class: "app-name", text: title }),
      el("span", { class: "project-name", text: subtitle }));
    let spacerAdded = false;
    for (const menu of menus) {
      if (menu.right && !spacerAdded) {
        this.root.append(el("span", { class: "spacer" }));
        spacerAdded = true;
      }
      this.root.append(this.renderMenu(menu));
    }

    document.addEventListener("click", (event) => {
      for (const d of this.details) if (!d.contains(event.target as Node)) d.open = false;
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") this.closeAll();
    });
  }

  private renderMenu(menu: Menu): HTMLDetailsElement {
    const items = el("div", { class: "menu-items", attrs: { role: "menu" } });
    if (menu.heading) items.append(el("div", { class: "menu-label", text: menu.heading }), el("hr"));
    for (const item of menu.items) {
      if (item.separatorBefore) items.append(el("hr"));
      const label = el("span");
      const button = el("button", { attrs: { type: "button", role: "menuitem" } }, label,
        item.shortcut ? el("span", { class: "shortcut", text: item.shortcut }) : null);
      button.addEventListener("click", () => {
        this.closeAll();
        item.action();
      });
      items.append(button);
      this.rendered.push({ item, button, label });
    }
    const summary = el("summary", { text: menu.label });
    const details = el("details", { class: `menu${menu.right ? " right" : ""}` }, summary, items);
    details.addEventListener("toggle", () => {
      if (!details.open) return;
      this.refresh();
      for (const other of this.details) if (other !== details) other.open = false;
    });
    summary.addEventListener("mouseenter", () => {
      if (this.details.some((d) => d.open && d !== details)) details.open = true;
    });
    this.details.push(details);
    return details;
  }

  closeAll(): void {
    for (const d of this.details) d.open = false;
  }

  /** Re-evaluate every label and enabled state (cheap; done whenever a menu
   * opens and when the app's state changes). */
  refresh(): void {
    for (const { item, button, label } of this.rendered) {
      label.textContent = typeof item.label === "function" ? item.label() : item.label;
      button.disabled = item.enabled ? !item.enabled() : false;
    }
  }
}

/** Sign Out has to be a POST with the CSRF token, like the server-rendered
 * pages' account menu. */
export function signOut(logoutUrl: string, csrfToken: string): void {
  const form = el("form", { attrs: { method: "post", action: logoutUrl, hidden: "" } },
    el("input", { attrs: { type: "hidden", name: "csrfmiddlewaretoken", value: csrfToken } }));
  document.body.append(form);
  form.submit();
}
