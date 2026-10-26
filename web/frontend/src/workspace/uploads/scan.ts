// scan.ts — turn a picked or dropped folder into the files worth uploading:
// every {PREFIX}_{CCK,CHR,SNAP}.tif anywhere under the folder is recognized
// (PREFIX upper-cased), any other .tif is reported as skipped rather than
// silently ignored, and everything else is ignored.
//
// Review work already in the folder comes along too ("desktop" review files,
// below). A reviewed folder holds one cellcounts.json, plus a <name>.cells.json
// per image, covering every image under that folder. So each image is matched to
// the nearest cellcounts.json at or above its own folder — which also handles
// picking a parent folder that holds several reviewed folders. Whether the
// review data still applies (same file content, processing finished) is decided
// at upload time by desktopMatch(), once the file's sha256 is known.

import type { Channel } from "../constants";

export const FILENAME_RE = /^([A-Za-z]+\d+)_(CCK|CHR|SNAP)\.tif$/i;
const MANIFEST_NAME = "cellcounts.json";
const SIDECAR_SUFFIX = ".cells.json";

export interface DesktopEntry {
  hash?: string; // "sha256:<hex>"
  status?: string;
  params?: Record<string, unknown> | null;
}

/** One image's entry in a cellcounts.json, and where its cells are. */
export interface DesktopRef {
  entry: DesktopEntry;
  /** <name>.cells.json next to the cellcounts.json (schema v2). */
  sidecar: File | null;
  /** Schema v1 cellcounts.json files embedded every image's cells. */
  embedded: unknown[] | null;
}

export interface Recognized {
  file: File;
  name: string;
  prefix: string;
  channel: Channel;
  desktop: DesktopRef | null;
}

export interface ScanResult {
  recognized: Recognized[];
  skipped: string[];
  duplicates: string[];
  /** How many cellcounts.json files were found. */
  desktopFolders: number;
}

export function recognize(name: string): { prefix: string; channel: Channel } | null {
  const m = FILENAME_RE.exec(name);
  return m ? { prefix: m[1].toUpperCase(), channel: m[2].toUpperCase() as Channel } : null;
}

/** A file plus where it sits inside the chosen folder ("A3/A3_CCK.tif"). */
export interface FoundFile {
  file: File;
  path: string;
}

const dirOf = (path: string) => (path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "");

export async function scanFiles(found: FoundFile[]): Promise<ScanResult> {
  const desktop = await readDesktopFolders(found);
  const recognized: Recognized[] = [];
  const skipped: string[] = [];
  const duplicates: string[] = [];
  const seen = new Set<string>();

  const sorted = [...found].sort((a, b) => a.path.localeCompare(b.path, undefined, { numeric: true }));
  for (const { file, path } of sorted) {
    const name = file.name;
    if (name === MANIFEST_NAME || name.endsWith(".tmp") || name.endsWith(".bak")) continue;
    if (!/\.tif$/i.test(name)) continue;
    const r = recognize(name);
    if (!r) {
      skipped.push(path);
      continue;
    }
    const slot = `${r.prefix}_${r.channel}`;
    if (seen.has(slot)) {
      duplicates.push(path);
      continue;
    }
    seen.add(slot);
    recognized.push({ file, name, ...r, desktop: desktopRefFor(desktop, path, name) });
  }
  return { recognized, skipped, duplicates, desktopFolders: desktop.size };
}

interface DesktopFolder {
  images: Record<string, DesktopEntry>;
  embedded: Record<string, unknown[]>;
  sidecars: Map<string, File>;
}

/** Every cellcounts.json in the selection, by the folder it's in. */
async function readDesktopFolders(found: FoundFile[]): Promise<Map<string, DesktopFolder>> {
  const folders = new Map<string, DesktopFolder>();
  for (const f of found) {
    if (f.file.name !== MANIFEST_NAME) continue;
    try {
      const data = JSON.parse(await f.file.text());
      folders.set(dirOf(f.path), { images: data.images ?? {}, embedded: data.cells ?? {}, sidecars: new Map() });
    } catch {
      // An unreadable cellcounts.json just means no review data for that folder.
    }
  }
  for (const f of found) {
    const folder = f.file.name.endsWith(SIDECAR_SUFFIX) ? folders.get(dirOf(f.path)) : undefined;
    folder?.sidecars.set(f.file.name.slice(0, -SIDECAR_SUFFIX.length), f.file);
  }
  return folders;
}

function desktopRefFor(folders: Map<string, DesktopFolder>, path: string, name: string): DesktopRef | null {
  for (let dir = dirOf(path); ; dir = dirOf(dir)) {
    const folder = folders.get(dir);
    const entry = folder?.images[name];
    if (folder && entry) return { entry, sidecar: folder.sidecars.get(name) ?? null, embedded: folder.embedded[name] ?? null };
    if (!dir) return null;
  }
}

/** The review data applies only if it was finished for this exact file: same
 * content (cellcounts.json records "sha256:<hex>") and status done. */
export function desktopMatch(ref: DesktopRef | null, sha256: string | null | undefined): DesktopRef | null {
  if (!ref || !sha256) return null;
  if (ref.entry.status !== "done" || ref.entry.hash !== `sha256:${sha256}`) return null;
  return ref.sidecar || ref.embedded ? ref : null;
}

export async function readDesktopCells(ref: DesktopRef): Promise<{ cells: unknown[]; params: unknown } | null> {
  const cells = ref.sidecar ? JSON.parse(await ref.sidecar.text()).cells : ref.embedded;
  return Array.isArray(cells) ? { cells, params: ref.entry.params ?? null } : null;
}

// --------------------------------------------------------------------------- //
// Getting the files: <input webkitdirectory> or a dropped folder
// --------------------------------------------------------------------------- //
export function filesFromInput(list: FileList): FoundFile[] {
  return Array.from(list).map((file) => ({ file, path: file.webkitRelativePath || file.name }));
}

export async function filesFromDrop(transfer: DataTransfer): Promise<FoundFile[]> {
  const entries = Array.from(transfer.items)
    .map((item) => item.webkitGetAsEntry?.())
    .filter((e): e is FileSystemEntry => !!e);
  // No directory entries (an older browser, or loose files rather than a
  // folder): take the dropped files as they are.
  if (!entries.length) return filesFromInput(transfer.files);
  const out: FoundFile[] = [];
  for (const entry of entries) await walk(entry, "", out);
  return out;
}

async function walk(entry: FileSystemEntry, parent: string, out: FoundFile[]): Promise<void> {
  const path = parent ? `${parent}/${entry.name}` : entry.name;
  if (entry.isFile) {
    const file = await new Promise<File>((resolve, reject) => (entry as FileSystemFileEntry).file(resolve, reject));
    out.push({ file, path });
    return;
  }
  const reader = (entry as FileSystemDirectoryEntry).createReader();
  // readEntries returns at most ~100 entries per call; keep asking until empty.
  for (;;) {
    const batch = await new Promise<FileSystemEntry[]>((resolve, reject) => reader.readEntries(resolve, reject));
    if (!batch.length) break;
    for (const child of batch) await walk(child, path, out);
  }
}
