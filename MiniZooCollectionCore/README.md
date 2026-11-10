# Work with saved packages

Inspect a `.wxapkg`, reconstruct its files or extract metadata from an existing
acquisition. These operations run locally without a phone. Collectors already
use this shared module automatically.

## Quick start with Docker

Build the image using the [main guide](../README.md#1-install), then
prepare the folders used by the tool container:

```sh
mkdir -p private/config private/input collection_results
export LOCAL_UID="$(id -u)" LOCAL_GID="$(id -g)"
```

### 1. Inspect your package

Put a package in `collection_results/package.wxapkg`, or replace the path below
with an actual saved package under `/data`:

```sh
docker compose run --rm collector inspect /data/package.wxapkg
```

This prints a JSON report with size, hashes, parsing status and static metadata.
It reads the bytes without executing the package's code.

### 2. Reconstruct its files

```sh
docker compose run --rm collector decompile \
  --package /data/package.wxapkg --output /data/reconstruction-001
```

Open **`collection_results/reconstruction-001/`** for the output and check the
reported result. Choose a new or empty output directory. For a subpackage,
add `--main-dir` pointing to the reconstructed main package. Original bytes are
preserved.

## Extract metadata from a saved acquisition

Use a collector's `processing.json` after successful reconstruction. Keep its
original package files, reports and reconstruction folder at their saved paths.
Replace `APPID` and the acquisition path with the actual values:

```sh
docker compose run --rm collector metadata extract \
  /data/acquisition/processing.json --appid APPID --output /data/metadata/report-001.json
```

The command writes a separate report and prints its SHA-256. Choose a new report
path. Missing metadata is explicitly marked. For verification arguments:

```sh
docker compose run --rm collector metadata verify --help
```

Both collectors can fill a missing category as **Mini game** from an explicit
native WeChat `isMinigame: 1` search result for the runtime-observed AppID.
This is a broad platform classification. Existing service categories and
conflicts take precedence. `evidence.json` records the fallback and a checksum
of its saved source; verification replays it without contacting a phone.

During collection, `package_metadata.json` and `package_metadata.csv` hold package
metadata; `processing.json` records processing outcomes. Check these statuses
before using an acquisition. Keep the complete folder for recovery and later
verification; see [path handling](../docs/DOCKER.md#inspect-or-reconstruct-saved-data-independently).

## Native usage

Follow the [native installation](../docs/NATIVE.md), including Node.js and the
pinned unpacker for reconstruction.

```sh
python3 minizoo.py inspect collection_results/package.wxapkg
python3 minizoo.py decompile \
  --package collection_results/package.wxapkg --output collection_results/reconstruction-001
```

Use `python3 minizoo.py metadata extract --help` for native extraction options.
No separate installation of this core is required. Dependency licenses are
listed in [provenance](../docs/PROVENANCE.md).
