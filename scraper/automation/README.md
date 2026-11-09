# Optional initial-collection batches

Use this only after a successful bounded run of the
[initial collector](../README.md). Discovery and version collection remain
separate. This supervisor distributes initial collection across the explicitly
configured phones and merges local results; it does not install a scheduler.
Run commands below from the repository root on macOS or Linux.

## Configure and start explicitly

Copy and edit the ignored local settings, including distinct device serials:

```sh
cp scraper/automation/config.example.env scraper/automation/config.local.env
# Edit config.local.env before continuing.
set -a
. scraper/automation/config.local.env
set +a
python3 scraper/build_final_miniapp_csvs.py
python3 scraper/automation/wechat_dual_scrape_supervisor.py
```

Before starting, provide the intended catalogue as
`collection_results/initial/all_mini_apps/all_mini_apps_total.csv`, with at least
`appid` and `name`, or configure `WECHAT_DISCOVERY_DB` for the discovery database.
`WECHAT_ROOT`, if overridden, points to `scraper/`. Data and locks default to
`collection_results/initial/` and `collection_results/automation/`. The data
directory can be outside `scraper/`, including the mounted `/data/initial` in
Docker. Canonical provenance points to retained runs within that data directory.
Concurrent processes must share the lock directory.

The supervisor deduplicates AppIDs, prioritizes unresolved attempts, partitions
the queue and uses one output per worker. Every ten minutes it verifies and
merges available acquisitions into local canonical storage. No run history is
deleted. Its global failure threshold is separate from the version collector's
ten-empty-search policy.

## Stop and consolidate

Press Ctrl-C and allow worker shutdown and the final merge to finish. Another
process can request an orderly stop by creating `STOP_REQUESTED` in the run
folder named by `collection_results/initial/active_runs/CURRENT_ALL_MINI_APPS_RUN.txt`.

To consolidate saved initial runs independently after stopping the supervisor:

```sh
python3 scraper/automation/wechat_active_merge.py
```

The merger replays evidence and verifies package bytes before creating AppID/hash
associations. The catalogue builder retains unresolved targets and provenance.
Canonical packages and the two operational CSVs stay under
`collection_results/initial/all_mini_apps/`. Source attempts, metadata and
checkpoints are retained. No periodic retry daemon or external scheduler
integration is included in this public release.

## Run in Docker

Prepare the local mounts and phone connection in the [Docker guide](../../docs/DOCKER.md).
Use container paths in settings and choose the intended phones explicitly:

```sh
docker compose run --rm collector python3 scraper/build_final_miniapp_csvs.py
docker compose run --rm -e WECHAT_DEVICES=DEVICE_A,DEVICE_B collector python3 scraper/automation/wechat_dual_scrape_supervisor.py
```

The defaults use `/data/initial` and `/data/automation`. To build from a discovery
database, pass `-e WECHAT_DISCOVERY_DB=/data/discovery/data.db` to the builder.
After stopping the supervisor, consolidate independently with:

```sh
docker compose run --rm collector python3 scraper/automation/wechat_active_merge.py
```
