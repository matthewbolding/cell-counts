# cell-counts

Cell/nuclei segmentation and coexpression review for fluorescence microscopy TIFFs, in the browser at
[cellcounts.matthewbolding.com](https://cellcounts.matthewbolding.com). Researchers sign in, add a folder of
images to a project, and review what the GPU found; nothing is installed on their computers. Projects are
private to their owner unless shared with a collaborator, and everyone takes turns on one GPU server that is
never exposed to the internet.

The app lives in **[web/](web/)**: [web/README.md](web/README.md) for development,
[web/DESIGN.md](web/DESIGN.md) for how it works, and [web/DEPLOY.md](web/DEPLOY.md) for running it.

## How it fits together

Each sample is three channel images following `{PREFIX}_{CCK,CHR,SNAP}.tif`, where `PREFIX` is one or more
letters (animal) + number (sample), e.g. `A1`, `K9`, `AA1`. SNAP is the full cell population for that sample;
CCK and CHR are gene-expression markers. All three channels are segmented the same way (Cellpose-SAM) and,
once reviewed, the interesting result is the *overlap* between CCK and CHR detections: where they coincide is
where the genes coexpress.

A project holds one folder's worth of samples. Adding the folder again only uploads files that are new or
whose contents changed; everything else is recognized by its SHA-256. Review edits are saved as you make
them, and the export is an Excel workbook with per-sample counts, coexpression rates and every cell.

## Authors

Built by Matthew Bolding with [Claude](https://claude.ai) (Anthropic).

Segmentation runs on [Cellpose-SAM](https://github.com/MouseLand/cellpose); see that repo for citation details
if you use this tool's output in published work.
