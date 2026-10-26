// queueOrder.ts — the Queue panel's ▲ / ▼ / ▲▲ / ▼▼ moves, ported from the
// desktop client's processing_queue.py. A multi-selection moves as a block: the
// selected items keep their order relative to each other and shift together.
// Pure functions over keys, so they're easy to test and apply equally to the
// local upload line and to the server's queue.

export type Move = "up" | "down" | "top" | "bottom";

export function moveSelection<T>(items: T[], isSelected: (item: T) => boolean, how: Move): T[] {
  const out = items.slice();
  const sel = out.map(isSelected);
  switch (how) {
    case "up":
      // Each selected item swaps past the unselected item directly above it;
      // one whose neighbor above is also selected stays put this pass, which
      // is what keeps a block moving as a unit.
      for (let i = 1; i < out.length; i++) {
        if (sel[i] && !sel[i - 1]) {
          [out[i - 1], out[i]] = [out[i], out[i - 1]];
          [sel[i - 1], sel[i]] = [sel[i], sel[i - 1]];
        }
      }
      return out;
    case "down":
      for (let i = out.length - 2; i >= 0; i--) {
        if (sel[i] && !sel[i + 1]) {
          [out[i], out[i + 1]] = [out[i + 1], out[i]];
          [sel[i], sel[i + 1]] = [sel[i + 1], sel[i]];
        }
      }
      return out;
    case "top":
      return [...out.filter((_, i) => sel[i]), ...out.filter((_, i) => !sel[i])];
    case "bottom":
      return [...out.filter((_, i) => !sel[i]), ...out.filter((_, i) => sel[i])];
  }
}

/** Standard list selection: plain click selects one (or clears it if it was the
 * only selected row), Ctrl/⌘-click toggles, Shift-click selects the range from
 * the last plain/Ctrl click. */
export function clickSelection(
  order: string[], selected: Set<string>, anchor: string | null, key: string,
  modifiers: { shift: boolean; ctrl: boolean },
): { selected: Set<string>; anchor: string | null } {
  if (modifiers.shift && anchor !== null && order.includes(anchor) && order.includes(key)) {
    const [lo, hi] = [order.indexOf(anchor), order.indexOf(key)].sort((a, b) => a - b);
    return { selected: new Set(order.slice(lo, hi + 1)), anchor };
  }
  if (modifiers.ctrl) {
    const next = new Set(selected);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    return { selected: next, anchor: key };
  }
  if (selected.size === 1 && selected.has(key)) return { selected: new Set(), anchor: null };
  return { selected: new Set([key]), anchor: key };
}
