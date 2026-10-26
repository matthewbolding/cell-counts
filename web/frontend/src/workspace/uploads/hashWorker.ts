// hashWorker.ts — SHA-256 of a whole file, off the main thread. hash-wasm hashes
// incrementally, so a 300 MB TIFF is read in 8 MB slices and never held in
// memory at once (the browser's built-in crypto.subtle.digest needs the whole
// buffer). Messages: {id, file} in; {id, progress} while working; {id, hash} or
// {id, error} when done.

import { createSHA256 } from "hash-wasm";

const SLICE = 8 * 1024 * 1024;

// The project compiles against the DOM lib, where `self` is a Window; in a
// dedicated worker postMessage takes just the message.
const scope = self as unknown as {
  postMessage(message: unknown): void;
  onmessage: ((event: MessageEvent<{ id: number; file: File }>) => void) | null;
};

scope.onmessage = async (event) => {
  const { id, file } = event.data;
  try {
    const hasher = await createSHA256();
    hasher.init();
    for (let offset = 0; offset < file.size; offset += SLICE) {
      const buffer = await file.slice(offset, offset + SLICE).arrayBuffer();
      hasher.update(new Uint8Array(buffer));
      scope.postMessage({ id, progress: Math.min(1, (offset + SLICE) / file.size) });
    }
    scope.postMessage({ id, hash: hasher.digest("hex") });
  } catch (err) {
    scope.postMessage({ id, error: err instanceof Error ? err.message : String(err) });
  }
};
