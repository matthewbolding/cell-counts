// undo.ts — per-image undo/redo: one
// action per user gesture (a drag-select batch or a rescan merge is ONE step),
// unbounded depth, history kept per image so switching channels and back keeps
// it. Every action also says what it means for the server: applying or
// reverting it returns the cell ops (DESIGN.md) that make the database match,
// so undo/redo saves exactly like any other edit.

import type { CellStore } from "./cellStore";
import type { Cell, CellStatus } from "./types";

export interface StatusChange {
  id: number;
  status: CellStatus;
  edited: boolean;
}

export type CellOp =
  | { op: "update"; changes: StatusChange[] }
  | { op: "add"; cells: Cell[] }
  | { op: "remove"; ids: number[] };

export interface Action {
  /** Apply (first time, or redo). */
  redo(store: CellStore): CellOp[];
  undo(store: CellStore): CellOp[];
}

const snapshot = (c: Cell): Cell => ({ ...c, centroid: [c.centroid[0], c.centroid[1]] });

/** Cells were added — drawn by hand, or merged from a rescan. */
export class AddCells implements Action {
  private readonly cells: Cell[];

  constructor(cells: Cell[]) {
    this.cells = cells.map(snapshot);
  }

  redo(store: CellStore): CellOp[] {
    for (const c of this.cells) store.add(snapshot(c));
    return [{ op: "add", cells: this.cells.map(snapshot) }];
  }

  undo(store: CellStore): CellOp[] {
    for (const c of this.cells) store.remove(c.id);
    return [{ op: "remove", ids: this.cells.map((c) => c.id) }];
  }
}

/** Cells were deleted (one click, or a whole lasso/rectangle). */
export class RemoveCells implements Action {
  private readonly cells: Cell[];

  constructor(cells: Cell[]) {
    this.cells = cells.map(snapshot);
  }

  redo(store: CellStore): CellOp[] {
    for (const c of this.cells) store.remove(c.id);
    return [{ op: "remove", ids: this.cells.map((c) => c.id) }];
  }

  undo(store: CellStore): CellOp[] {
    for (const c of this.cells) store.add(snapshot(c));
    return [{ op: "add", cells: this.cells.map(snapshot) }];
  }
}

/** One or more cells changed status in place (click toggle or batch mark). */
export class ModifyCells implements Action {
  private readonly before: StatusChange[];
  private readonly after: StatusChange[];

  constructor(before: StatusChange[], after: StatusChange[]) {
    this.before = before;
    this.after = after;
  }

  static setStatus(cells: Cell[], status: CellStatus): ModifyCells {
    return new ModifyCells(
      cells.map((c) => ({ id: c.id, status: c.status, edited: c.edited })),
      cells.map((c) => ({ id: c.id, status, edited: true })),
    );
  }

  private apply(store: CellStore, changes: StatusChange[]): CellOp[] {
    for (const ch of changes) store.setStatus(ch.id, ch.status, ch.edited);
    return [{ op: "update", changes: changes.map((ch) => ({ ...ch })) }];
  }

  redo(store: CellStore): CellOp[] {
    return this.apply(store, this.after);
  }

  undo(store: CellStore): CellOp[] {
    return this.apply(store, this.before);
  }
}

interface Stacks {
  undo: Action[];
  redo: Action[];
}

export class UndoHistory {
  private readonly stacks = new Map<string, Stacks>();

  private for(imageId: string): Stacks {
    let s = this.stacks.get(imageId);
    if (!s) {
      s = { undo: [], redo: [] };
      this.stacks.set(imageId, s);
    }
    return s;
  }

  /** Apply a fresh action; any fresh edit clears that image's redo history. */
  perform(store: CellStore, action: Action): CellOp[] {
    const s = this.for(store.imageId);
    const ops = action.redo(store);
    s.undo.push(action);
    s.redo = [];
    return ops;
  }

  undo(store: CellStore): CellOp[] | null {
    const s = this.for(store.imageId);
    const action = s.undo.pop();
    if (!action) return null;
    s.redo.push(action);
    return action.undo(store);
  }

  redo(store: CellStore): CellOp[] | null {
    const s = this.for(store.imageId);
    const action = s.redo.pop();
    if (!action) return null;
    s.undo.push(action);
    return action.redo(store);
  }

  canUndo(imageId: string): boolean {
    return (this.stacks.get(imageId)?.undo.length ?? 0) > 0;
  }

  canRedo(imageId: string): boolean {
    return (this.stacks.get(imageId)?.redo.length ?? 0) > 0;
  }

  /** The server copy changed underneath us: old steps would no longer apply. */
  forget(imageId: string): void {
    this.stacks.delete(imageId);
  }
}
