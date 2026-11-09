# MiniZoo shared collection core

Shared package inspection, reconstruction and metadata tools used by both
MiniZoo collectors. The collectors load this module automatically. Saved files
can also be processed independently, without connecting a phone.

Run all commands below from the repository root and replace the example paths
with your own files.

## Setup

For native execution, follow the [Python setup](../README.md#install-only-what-you-use).
Reconstruction also needs Node.js 26+ and the pinned wxappUnpacker described there.
No separate installation of this core is required.

For Docker, build the shared image and prepare the local folders as described in
the [Docker guide](../docs/DOCKER.md):

```sh
docker compose build collection
```

## Inspect a package

```sh
python3 minizoo.py inspect collection_results/package.wxapkg
```

Prints a JSON report with package hashes, size, parsing status and available
static metadata. This step only needs the original `.wxapkg` file.

## Reconstruct a package

```sh
python3 minizoo.py decompile \
  --package collection_results/package.wxapkg \
  --output collection_results/reconstruction-001
```

Choose a new or empty output directory. The command saves reconstructed files
and prints its result. For a subpackage, add `--main-dir` with the reconstructed
main package's directory. Original package files are preserved.

## Extract metadata from a saved acquisition

Use the `processing.json` produced by a collector after successful reconstruction.
Keep its original package files, reports and reconstruction directory at their
saved paths. Replace `APPID` with the acquisition's actual AppID:

```sh
python3 minizoo.py metadata extract \
  collection_results/acquisition/processing.json \
  --appid APPID \
  --output collection_results/metadata/report-001.json
```

The command creates a separate JSON report and prints its SHA-256. Choose a new
report path for each extraction. Missing metadata remains explicitly marked.
For the arguments needed to verify a saved report:

```sh
python3 minizoo.py metadata verify --help
```

## Run with Docker

Files under `collection_results/` are available at `/data/` inside the container:

```sh
docker compose run --rm collector inspect /data/package.wxapkg
docker compose run --rm collector decompile \
  --package /data/package.wxapkg --output /data/reconstruction-001
docker compose run --rm collector metadata extract \
  /data/acquisition/processing.json --appid APPID --output /data/metadata/report-001.json
```

Use acquisition outputs created with the same path layout. See the
[Docker guide](../docs/DOCKER.md#inspect-or-reconstruct-saved-data-independently)
for mount configuration.

## Read the results

During collection, `package_metadata.json` and `package_metadata.csv` contain
package metadata; `processing.json` records the outcome of each processing stage
and the paths to its outputs. Check these statuses before using the results.
Keep the complete acquisition folder to allow later verification or recovery.

Command options are available through `inspect --help`, `decompile --help` and
`metadata --help`. Dependency licenses are listed in [provenance](../docs/PROVENANCE.md).
