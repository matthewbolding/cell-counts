import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

const here = (path: string) => fileURLToPath(new URL(path, import.meta.url));

// Builds straight into Django's static/dist with a manifest that the
// {% vite_entry %} template tag (core/templatetags/vite.py) reads.
export default defineConfig({
  base: "/static/dist/",
  build: {
    outDir: here("../static/dist"),
    emptyOutDir: true,
    manifest: true,
    sourcemap: true,
    rollupOptions: {
      input: {
        workspace: here("src/workspace/main.ts"),
        queue: here("src/queue/main.ts"),
      },
    },
  },
  worker: { format: "es" },
  test: { environment: "node" },
});
