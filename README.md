# cell-counts

Cell/nuclei segmentation and coexpression review for fluorescence microscopy TIFFs,
split into a client and a server:

- **`server/`** — a GPU compute endpoint (Cellpose-SAM) reachable over the internet
  at a domain you control. Ingests TIFFs, returns detected cells as JSON.
  See **[SERVER.md](SERVER.md)** for install/deploy.
- **`client/`** — the reviewer's application. You pick a folder of images; it hashes
  every file, skips anything already processed, and sends the rest to the server.
  See **[CLIENT.md](CLIENT.md)** for install/run.

## How it fits together

Each sample is three channel images following `{PREFIX}_{CCK,CHR,SNAP}.tif`, where
`PREFIX` is a letter (animal) + number (sample), e.g. `A1`, `K9`. SNAP is the full
cell population for that sample; CCK and CHR are gene-expression markers. The client
segments all three channels the same way and — once reviewed — the interesting
result is the *overlap* between CCK and CHR detections: where they coincide is where
the genes coexpress.

Opening a folder in the client produces one `cellcounts.json` in that folder — a
manifest of every recognized image (filename + hash + processing status) plus every
detected cell (polygon outline, status, source). Re-opening the same folder only
(re)processes files that are new or whose contents changed since the hash was last
recorded; everything else is read straight from the manifest.

## Authors

Built by Matthew Bolding with [Claude](https://claude.ai) (Anthropic).

Segmentation runs on [Cellpose-SAM](https://github.com/MouseLand/cellpose) — see
that repo for citation details if you use this tool's output in published work.
