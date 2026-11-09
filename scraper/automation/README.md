# Optional initial-collection batches

Use this only after a successful bounded run of the
[initial collector](../README.md). Discovery and version collection remain
separate. This supervisor distributes initial collection across the explicitly
configured phones and merges local results; it does not install a scheduler.
Run commands below from the repository root on macOS or Linux.

## Test without phones

```sh
python3 minizoo.py test collection
```

The suite covers disjoint queues, locks, stop behavior, verified consolidation
and preservation of original runs using mocked workers.

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
`collection_results/initial/` and `collection_results/automation/`. Concurrent
processes must share the lock directory.

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
