// hasher.ts — the main-thread side of hashWorker.ts: one long-lived worker,
// one file at a time.

let worker: Worker | null = null;
let nextId = 1;

export function hashFile(file: File, onProgress?: (fraction: number) => void): Promise<string> {
  worker ??= new Worker(new URL("./hashWorker.ts", import.meta.url), { type: "module" });
  const w = worker;
  const id = nextId++;
  return new Promise((resolve, reject) => {
    const onMessage = (event: MessageEvent<{ id: number; progress?: number; hash?: string; error?: string }>) => {
      const data = event.data;
      if (data.id !== id) return;
      if (data.progress !== undefined) {
        onProgress?.(data.progress);
        return;
      }
      w.removeEventListener("message", onMessage);
      if (data.hash) resolve(data.hash);
      else reject(new Error(data.error ?? "hashing failed"));
    };
    w.addEventListener("message", onMessage);
    w.postMessage({ id, file });
  });
}
