// constants.ts — the review screen's tuning numbers and palette, all in one
// place. Change them here, not inline.

export type Channel = "SNAP" | "CCK" | "CHR";
export const CHANNELS: readonly Channel[] = ["SNAP", "CCK", "CHR"];

// statusbar.STATE_COLORS / STATE_LABELS
export type LightState = "idle" | "connecting" | "connected" | "processing" | "error";
export const STATE_LABELS: Record<LightState, string> = {
  idle: "Idle",
  connecting: "Connecting…",
  connected: "Connected",
  processing: "Processing…",
  error: "Error",
};

// Viewport / gesture tuning
export const MIN_SCALE = 0.01;
export const MAX_SCALE = 10;
export const ZOOM_STEP = 1.25;
export const DRAG_THRESHOLD = 4; // screen px before a press+move counts as a drag
export const MIN_DRAG_POINT_PX = 3; // screen px between consecutive lasso/freehand points
export const DOUBLE_CLICK_MS = 400;
export const DOUBLE_CLICK_SCREEN_PX = 5;
export const MIN_RESCAN_SIZE = 10; // image px
export const DEFAULT_RESCAN_DIAMETERS = "0, 15, 30";
export const DEFAULT_RESCAN_CELLPROBS = "-2, 0, 2";
export const MAX_RESCAN_COMBOS = 25;

// Queue row colors
export const ROW_BG = "#f0f0f0";
export const ROW_BG_SELECTED = "#cce0ff";
export const QUEUE_UPLOADING_ACTIVE_BG = "#fff2cc";
export const QUEUE_SEGMENTING_ACTIVE_BG = "#b7e6b0";
export const QUEUE_SERVER_QUEUED_BG = "#e2e6f5";
export const QUEUE_BREATHE_PERIOD_MS = 3200;

// Cell drawing
export const FILTERED_COLOR = "rgba(140, 140, 140, 0.784)"; // (140,140,140,200)
export const IN_PROGRESS_COLOR = "#ffdd00";
export const MASK_FILL_ALPHA = 110 / 255;
export const OUTLINE_WIDTH_BASE = 1.5;
export const OUTLINE_WIDTH_MIN = 1;
export const OUTLINE_WIDTH_MAX = 4;
export const LOD_MIN_SCREEN_PX = 8;

// Fixed attention colors
export const COEXPRESS_HIGHLIGHT_COLOR = "#ffffff";
export const RESCAN_PROPOSED_COLOR = "#ffdd00";
export const RESCAN_ACCEPTED_COLOR = "#00e676";
export const CANVAS_BG = "#1a1a1a";

// state.DEFAULT_CHANNEL_COLORS
export const DEFAULT_CHANNEL_COLORS: Record<Channel, string> = {
  SNAP: "#ff0000",
  CCK: "#00ff00",
  CHR: "#00ffff",
};

// Polling / saving cadence
export const STATUS_POLL_VISIBLE_MS = 2000;
export const STATUS_POLL_HIDDEN_MS = 10000;
export const SAVE_IDLE_MS = 1000; // edits are sent this long after the last one
export const UI_STATE_SAVE_MS = 800;
export const LOCK_RENEW_MS = 20000;
