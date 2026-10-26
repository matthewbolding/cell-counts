// dom.ts — a tiny element builder. The review screen is plain DOM (no framework):
// a handful of panels whose contents change in small, well-defined ways.

type Child = Node | string | number | null | undefined | false;

export interface ElOptions {
  class?: string;
  text?: string;
  title?: string;
  attrs?: Record<string, string>;
  dataset?: Record<string, string>;
  on?: Partial<{ [K in keyof HTMLElementEventMap]: (event: HTMLElementEventMap[K]) => void }>;
}

export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  options: ElOptions = {},
  ...children: Child[]
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (options.class) node.className = options.class;
  if (options.text !== undefined) node.textContent = options.text;
  if (options.title) node.title = options.title;
  for (const [k, v] of Object.entries(options.attrs ?? {})) node.setAttribute(k, v);
  for (const [k, v] of Object.entries(options.dataset ?? {})) node.dataset[k] = v;
  for (const [type, handler] of Object.entries(options.on ?? {})) {
    node.addEventListener(type, handler as EventListener);
  }
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    node.append(typeof child === "number" ? String(child) : child);
  }
  return node;
}

export function clear(node: Element): void {
  while (node.firstChild) node.firstChild.remove();
}

export const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);

/** "Ctrl+Z" on Windows/Linux, "⌘Z" on macOS — for menu shortcuts and hints. */
export function shortcut(key: string, shift = false): string {
  if (isMac) return `${shift ? "⇧" : ""}⌘${key}`;
  return `Ctrl+${shift ? "Shift+" : ""}${key}`;
}

/** True when keyboard focus is somewhere typing should go (so global shortcuts
 * must stay out of the way). */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}
