import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { breathePhase, lerpColor } from "../panels";
import { clickSelection, moveSelection } from "../queueOrder";
import { parseValues, RescanReview, type Combo } from "../rescan";
import { desktopMatch, readDesktopCells, recognize, scanFiles } from "../uploads/scan";
import type { Cell } from "../types";

describe("queue moves", () => {
  const order = ["a", "b", "c", "d", "e"];
  const sel = (keys: string) => (k: string) => keys.includes(k);

  it("moves a selection as a block", () => {
    expect(moveSelection(order, sel("cd"), "up")).toEqual(["a", "c", "d", "b", "e"]);
    expect(moveSelection(order, sel("bc"), "down")).toEqual(["a", "d", "b", "c", "e"]);
    expect(moveSelection(order, sel("bd"), "top")).toEqual(["b", "d", "a", "c", "e"]);
    expect(moveSelection(order, sel("ad"), "bottom")).toEqual(["b", "c", "e", "a", "d"]);
  });

  it("leaves a block already at the edge in place", () => {
    expect(moveSelection(order, sel("ab"), "up")).toEqual(order);
  });

  it("does standard click / Ctrl / Shift selection", () => {
    let state = clickSelection(order, new Set(), null, "b", { shift: false, ctrl: false });
    state = clickSelection(order, state.selected, state.anchor, "d", { shift: true, ctrl: false });
    expect([...state.selected].sort()).toEqual(["b", "c", "d"]);
    state = clickSelection(order, state.selected, state.anchor, "c", { shift: false, ctrl: true });
    expect([...state.selected].sort()).toEqual(["b", "d"]);
    const single = clickSelection(order, new Set(["e"]), "e", "e", { shift: false, ctrl: false });
    expect(single.selected.size).toBe(0); // clicking the only selected row clears it
  });
});

describe("breathing rows", () => {
  it("eases 0 -> 1 -> 0 over one period", () => {
    expect(breathePhase(0)).toBeCloseTo(0);
    expect(breathePhase(1600)).toBeCloseTo(1);
    expect(lerpColor("#000000", "#ffffff", 0.5)).toBe("#808080");
  });
});

describe("folder scanning", () => {
  it("recognizes channel files and upper-cases the prefix", () => {
    expect(recognize("aa12_cck.TIF")).toEqual({ prefix: "AA12", channel: "CCK" });
    expect(recognize("A1_CCL.tif")).toBeNull();
  });

  it("skips misnamed tifs, ignores other files, and reads review files", async () => {
    const f = (name: string, body = "x") => ({ file: new File([body], name), path: `Folder/${name}` });
    const manifest = JSON.stringify({ images: { "A1_SNAP.tif": { hash: "sha256:abc", status: "done" } } });
    const scan = await scanFiles([
      f("A1_SNAP.tif"), f("a1_cck.tif"), f("A1_CCL.tif"), f("notes.txt"), f("cellcounts.json", manifest),
      f("A1_SNAP.tif.cells.json", '{"cells": []}'), { file: new File(["x"], "A1_SNAP.tif"), path: "Folder/sub/A1_SNAP.tif" },
    ]);
    expect(scan.recognized.map((r) => r.name)).toEqual(["a1_cck.tif", "A1_SNAP.tif"]);
    expect(scan.skipped).toEqual(["Folder/A1_CCL.tif"]);
    expect(scan.duplicates).toEqual(["Folder/sub/A1_SNAP.tif"]);
    const snap = scan.recognized.find((r) => r.name === "A1_SNAP.tif")!;
    expect(snap.desktop?.entry.hash).toBe("sha256:abc");
    expect(snap.desktop?.sidecar?.name).toBe("A1_SNAP.tif.cells.json");
    expect(scan.recognized.find((r) => r.name === "a1_cck.tif")!.desktop).toBeNull(); // not in cellcounts.json
  });

  it("matches each image to the nearest cellcounts.json above it", async () => {
    // A parent folder holding two reviewed folders, one with an image in a subfolder.
    const manifest = (names: string[]) => JSON.stringify({ images: Object.fromEntries(
      names.map((n) => [n, { hash: `sha256:${n}`, status: "done", params: { diameter: 0 } }])) });
    const f = (path: string, body = "x") => ({ file: new File([body], path.split("/").pop()!), path });
    const scan = await scanFiles([
      f("Cohorts/A/cellcounts.json", manifest(["A1_SNAP.tif"])),
      f("Cohorts/A/sub/A1_SNAP.tif"),
      f("Cohorts/A/A1_SNAP.tif.cells.json", '{"cells": [{"id": 1}]}'),
      f("Cohorts/B/cellcounts.json", manifest(["B1_SNAP.tif"])),
      f("Cohorts/B/B1_SNAP.tif"),
      f("Cohorts/B/B1_SNAP.tif.cells.json", '{"cells": [{"id": 7}, {"id": 8}]}'),
    ]);
    expect(scan.desktopFolders).toBe(2);
    const [a1, b1] = scan.recognized;
    expect(a1.desktop?.sidecar?.name).toBe("A1_SNAP.tif.cells.json");
    expect((await readDesktopCells(b1.desktop!))?.cells).toHaveLength(2);
  });

  it("uses review data only for finished files with the same content", () => {
    const ref = { entry: { hash: "sha256:abc", status: "done" }, sidecar: new File(["{}"], "x"), embedded: null };
    expect(desktopMatch(ref, "abc")).toBe(ref);
    expect(desktopMatch(ref, "def")).toBeNull(); // the file changed since it was reviewed
    expect(desktopMatch({ ...ref, entry: { hash: "sha256:abc", status: "processing" } }, "abc")).toBeNull();
    expect(desktopMatch({ ...ref, sidecar: null }, "abc")).toBeNull(); // nothing to import
    expect(desktopMatch(ref, undefined)).toBeNull();
  });
});

describe("rescan", () => {
  it("parses comma-separated numbers", () => {
    expect(parseValues("0, 15,30")).toEqual([0, 15, 30]);
    expect(parseValues(" , ")).toBeNull();
    expect(parseValues("1, x")).toBeNull();
  });

  const cell = (id: number, cx: number): Cell => ({
    id, polygons: [[[cx - 3, 0], [cx + 3, 0], [cx + 3, 6], [cx - 3, 6]]], centroid: [cx, 3], area: 36,
    status: "filtered", source: "model", edited: false,
  });
  const combo = (jobId: string, cells: Cell[]): Combo => ({
    job_id: jobId, diameter: 0, cellprob_threshold: 0, status: "done", cells, error: null });

  it("lands on the first productive combination and accepts across combinations", () => {
    const review = new RescanReview("img", [0, 0, 100, 10], [combo("j1", []), combo("j2", [cell(1, 10)]),
      combo("j3", [cell(1, 50)])]);
    expect(review.index).toBe(1);
    expect(review.toggleAt(10, 3, 1)).toBe(true);
    review.step(1);
    review.toggleAt(50, 3, 1);
    expect([...review.accepted.values()].map((a) => a.jobId)).toEqual(["j2", "j3"]);
    expect(review.split().accepted).toHaveLength(1);
    review.toggleAt(50, 3, 1); // un-accept
    expect(review.accepted.size).toBe(1);
  });
});

describe("CellSaver", () => {
  const calls: { url: string; body: { base_version: number; ops: unknown[] } }[] = [];
  let respond: (url: string, body: { base_version: number }) => Response;

  beforeEach(() => {
    vi.useFakeTimers();
    calls.length = 0;
    vi.stubGlobal("document", { querySelector: () => ({ content: "csrf" }) });
    vi.stubGlobal("window", globalThis);
    vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
      const body = JSON.parse(String(init.body));
      calls.push({ url, body });
      return respond(url, body);
    });
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("debounces, sends in order with the right base version, and reports conflicts", async () => {
    const { CellSaver } = await import("../edits");
    const { CellStore } = await import("../cellStore");
    respond = (_url, body) => new Response(JSON.stringify({ version: body.base_version + 1 }), { status: 200 });
    const events = { onConflict: vi.fn(), onLocked: vi.fn(), onError: vi.fn(), onSaved: vi.fn() };
    const saver = new CellSaver(events);
    const store = new CellStore("img", 4, []);

    saver.queue(store, [{ op: "remove", ids: [1] }]);
    saver.queue(store, [{ op: "remove", ids: [2] }]);
    expect(calls).toHaveLength(0);
    expect(saver.isDirty("img")).toBe(true);
    await vi.advanceTimersByTimeAsync(1100);
    expect(calls).toHaveLength(1);
    expect(calls[0].body).toEqual({ base_version: 4, ops: [{ op: "remove", ids: [1] }, { op: "remove", ids: [2] }] });
    expect(store.version).toBe(5);
    expect(saver.isDirty("img")).toBe(false);

    respond = () => new Response(JSON.stringify({ error: "version_conflict", detail: "changed", version: 9 }), { status: 409 });
    saver.queue(store, [{ op: "remove", ids: [3] }]);
    await saver.flush("img");
    expect(events.onConflict).toHaveBeenCalledWith("img");
    expect(saver.isDirty("img")).toBe(false);
  });
});
