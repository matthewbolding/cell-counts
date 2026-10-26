// types.ts — the JSON shapes the server sends (DESIGN.md, "JSON API").

import type { Channel } from "./constants";

export type ImageStatus = "preparing" | "queued" | "segmenting" | "done" | "error";

export interface TilesInfo {
  rev: string;
  levels: number;
  tile_size: number;
}

export interface ImageInfo {
  id: string;
  filename: string;
  channel: Channel;
  status: ImageStatus;
  error: string | null;
  width: number | null;
  height: number | null;
  cells_version: number;
  tiles: TilesInfo | null;
}

export interface SampleInfo {
  id: string;
  prefix: string;
  channels: Record<Channel, ImageInfo | null>;
}

export type CellStatus = "kept" | "filtered";

export interface Cell {
  id: number;
  polygons: number[][][]; // rings of [x, y]
  centroid: [number, number];
  area: number;
  status: CellStatus;
  filter_reason?: string | null;
  color?: string | null;
  source: string; // model | human | rescan:<job_id>
  edited: boolean;
}

export type RenderMode = "outline" | "mask";
export type SelectionStyle = "lasso" | "rectangle";
export type DrawStyle = "freehand" | "polygon";

export interface UiState {
  last_image_id?: string | null;
  render_mode?: RenderMode;
  channel_colors?: Partial<Record<Channel, string>>;
  zoom_multiplier?: number;
  selection_style?: SelectionStyle;
  draw_style?: DrawStyle;
}

export type Role = "owner" | "editor" | "viewer";

export interface Bootstrap {
  id: string;
  name: string;
  role: Role;
  can_edit: boolean;
  owner: string;
  samples: SampleInfo[];
  ui_state: UiState;
  upload_chunk_size: number;
  me: { id: number; label: string; is_staff: boolean };
}

export interface QueueRow {
  job_id: string;
  kind: "prepare" | "segment" | "rescan" | "export" | "purge";
  status: "queued" | "running" | "done" | "error" | "cancelled";
  mine: boolean;
  owner: string;
  image_id: string | null;
  filename: string | null;
  prefix: string | null;
  channel: Channel | null;
  project_id: string | null;
  position: number;
  ahead: number | null;
  eta_seconds: number | null;
  started_at: string | null;
}

export interface ImageStatusEntry {
  id: string;
  sample_id: string;
  status: ImageStatus;
  error: string | null;
  cells_version: number;
  width: number | null;
  height: number | null;
  tiles: TilesInfo | null;
}

export interface StatusResponse {
  images: ImageStatusEntry[];
  queue: QueueRow[];
  segmenting_paused: boolean;
  gpu_paused: boolean;
}

export interface CellsResponse {
  image_id: string;
  version: number;
  cells: Cell[];
}

export interface CompositeResponse {
  available: boolean;
  pairs?: [number, number, number][];
  cck_ids?: number[];
  chr_ids?: number[];
  n_pairs?: number;
  snap_kept?: number;
  rate_pct?: number | null;
}

export type Rect = [number, number, number, number]; // x0, y0, x1, y1 (image px)
export type Point = [number, number];
