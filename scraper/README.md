# Initial WeChat Mini Program collector

This collector starts from a CSV of app names and preserves original WeChat
packages (`.wxapkg`), metadata, hashes and provenance. It uses **ADB** to navigate
the phone. The [version collector](../version_scraper/README.md) uses Appium.
Both use the bundled [MiniZoo collection core](../MiniZooCollectionCore/README.md)
for package acquisition and processing.

Search opens a candidate; runtime evidence identifies the Mini Program that is
actually in the foreground and links its packages through checksums. If a search
for A opens B, the collector saves B and its observed metadata while leaving A
unresolved. The requested catalogue record remains separate in `queried_*`.
Metadata extracted from package bytes is recorded in `package_metadata.csv`.

## Installation

Use Python **3.12** for the combined repository. The collector also requires:

- Android Platform Tools (`adb`) in `PATH`.
- Frida CLI with its Java bridge and a compatible Frida server on the phone.
  Set `WECHAT_FRIDA` or `--frida` when the executable is not in `PATH`.
- A rooted Android device with USB debugging authorized and WeChat already
  installed and logged in manually.
- ADBKeyboard for Unicode input.
- Node.js 26 and the pinned wxappUnpacker dependencies for reconstruction.
- macOS or Linux for the POSIX automation scripts, which use `fcntl`.

Run the following commands from `scraper/`. Find your device serial with
`adb devices -l` and replace `DEVICE_A`:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
adb -s DEVICE_A get-state
adb -s DEVICE_A shell su -c id
ANDROID_SERIAL=DEVICE_A bash setup_adbkeyboard.sh
```

The keyboard script downloads the APK from
[ADBKeyBoard](https://github.com/senzhk/ADBKeyBoard), verifies its SHA-256, installs
and enables it on the selected device, then sends a short Unicode test. The APK
is not bundled. Its pinned checksum is
`e698adea5633135a067b038f9a0cf41baa4de09888713a81593fb2b9682cdc59`;
a different remote file stops installation.

For reconstruction, initialize and install the decompiler from the repository
root:

```bash
git submodule update --init version_scraper/vendor/wxappUnpacker
npm ci --prefix version_scraper/vendor/wxappUnpacker
```

For a source archive without `.git`, clone the dependencies at the
[pinned revisions](../docs/PROVENANCE.md) instead of submodule initialization.

The core is discovered at `../MiniZooCollectionCore/`. `MINIZOO_CORE_PATH` can
select a separate installation. See the [Docker guide](../docs/DOCKER.md) for the
container alternative and external device setup.

## Collect from a CSV

Supply your own UTF-8 CSV with a `name` column, or select another column using
`--name-column`. Useful fields include:

```text
id,doc_id,appid,name,category,rating,url,description,icon_url,username,developer,app_version,source_query
```

`doc_id` identifies a public metadata record; it does not replace a WeChat AppID.
No real catalogue is distributed in this repository. From `scraper/`:

```bash
python3 wechat_csv_scraper.py \
  --serial DEVICE_A --csv input.csv --limit 3 \
  --output ../collection_results/initial/trial_001
```

Use a new output directory for each run: an existing `manifest.csv` is rejected
before ADB operations begin. `--start` and `--limit` select input rows; they are
not an automatic resume mechanism.

| Option | Behavior |
| --- | --- |
| `--open-method search` | Default: use text search. |
| `--open-method auto` | Try an available Mini Program code/QR, then text search. |
| `--open-method qr` | Explicitly select the available code/QR. |
| `--manual-before-type --manual-open-result` | Allow manual UI actions in an interactive terminal. |
| `--prepare-only` | Prepare metadata/QR assets without ADB; public HTTP sources may still be queried. |
| `--allow-cached-fallback` | Compatibility option; cached bytes must still match the runtime-bound package. |

Every opening method uses the same identity checks. The retired
`--packages-per-app` option is accepted but ignored: new acquisitions retrieve
all available components corroborated by the runtime.
`--clear-cache-before-open` is rejected; device cache and staging files are
preserved. Run `python3 wechat_csv_scraper.py --help` for the full interface.

## Outputs and identity

```text
../collection_results/initial/<run>/
  manifest.csv
  package_history.csv
  package_metadata.csv
  package_metadata_evidence/
  qr_codes/
  attempts/<attempt_id>/
    before/messages.json
    after/messages.json
    opened_app_evidence.json
    evidence.json
    packages/
    package_metadata.json
    package_metadata.csv
    processing.json
```

The `opened_app_v1` manifest separates `queried_*` from the observed app's AppID,
name, version and available public metadata. Public fields come from the runtime
or loaded `WxaAttributes` records indexed by the observed AppID. Unknown fields
remain empty. An `ok` result requires a validated runtime/package relationship
and matching checksum. Before/after observations, hashes, paths, device and time
are retained in the evidence JSON. Ambiguous identity, an app switch, or a
checksum mismatch prevents attribution; partial files remain preserved.

Static extraction and decompilation finish before the next search. They have
separate outcomes: reconstruction failure does not remove valid original
packages. Coverage is complete only when the original configuration, runtime
inventory and acquired components agree. Missing components or an unavailable
inventory remain explicit partial coverage.

The [metadata format](README_package_metadata.md) distinguishes query context,
runtime identity and static observations. The static parser does not execute
package code. To inspect a container:

```bash
python3 wxapkg_inspect.py /path/to/package.wxapkg
```

## Automation across devices

Scripts in `automation/` divide the queue among configured devices, with separate
worker directories and CSVs. Assign each phone to only one active collector.

1. Place your catalogue at
   `../collection_results/initial/all_mini_apps/all_mini_apps_total.csv`, with at least
   `appid` and `name`.
2. Create private configuration, edit its paths and device serials, then launch:

```bash
cp automation/config.example.env automation/config.local.env
# Edit config.local.env with your devices and local paths.
set -a
. automation/config.local.env
set +a
python3 build_final_miniapp_csvs.py
python3 automation/wechat_dual_scrape_supervisor.py
```

`WECHAT_DEVICES` accepts one or more distinct comma-separated serials.
`WECHAT_ROOT`, when set, must point to **scraper/**. By default, data lives at `../collection_results/initial/` and automation state
at `../collection_results/automation/`. `WECHAT_DATA_DIR` and `WECHAT_STATE_DIR`
can override these paths. Concurrent processes must share the state directory
that contains their locks. The collector
reads CLI options and environment variables, not the old project's `config.json`.

The supervisor deduplicates queued AppIDs, prioritizes previously failed attempts
and distributes work. It periodically merges packages and rebuilds local catalogues, including at shutdown. Its **legacy monitor stops workers after ten
consecutive global failures**. This is a broader failure rule than the version
collector's specific ten-empty-search rule; checks are periodic across workers.

For an orderly stop, create `STOP_REQUESTED` inside the run directory referenced
by `../collection_results/initial/active_runs/CURRENT_ALL_MINI_APPS_RUN.txt`. Worker PIDs are stored in
`device_<serial>/pid`. Disable your retry scheduler as well when keeping a
collection paused.

The canonical collection is written to:

```text
../collection_results/initial/all_mini_apps/
  <appid>/<sha256>.wxapkg
  all_mini_apps_total.csv
  mini_app_scraping_status.csv
```

These local CSVs are operational inputs to the supervisor and retain names,
queue state, package summaries and provenance. They stay on disk alongside the
packages. No cloud credentials or external storage service are used.

Merge replays the evidence and verifies bytes before creating canonical
associations. Deduplication uses AppID/SHA-256 and retains distinct supported
owners of identical content. A `.runtime-provenance.json` sidecar preserves the
evidence references. The catalogue builder uses observed metadata and keeps a
target unresolved when its search opened another AppID. Existing historical
associations are preserved without retroactive certification; old query-only
manifests cannot create new canonical associations. Runs, manifests, sidecars
and packages remain available.

## Local maintenance, retries and utilities

The standalone merger verifies acquisition evidence, consolidates packages and
rebuilds the local catalogues. Run it without concurrent supervisor maintenance:

```sh
python3 automation/wechat_active_merge.py
```

`wechat_daily_schedule_kickoff.py` starts a retry daemon with a four-hour interval
and at most six attempts. The repository does not install a scheduler; cron,
launchd and their time zones belong to the deployment configuration. Optional
Hermes integration is disabled by default.

| Utility | Purpose |
| --- | --- |
| `build_final_miniapp_csvs.py` | Build the catalogue and collection status from available sources and canonical packages. |
| `merge_collection_results.py` | Historical manual consolidation; stop collectors first and inspect the hash report. |
| `enrich_weweplay_rating_url.py` | Read public WeWePlay pages and add available ratings/URLs. |
| `watch_stop_global_failure_streak.py` | Legacy failure monitor with explicit worker PIDs. |
| `static_wxapkg_reader.py`, `wxapkg_metadata_csv.py` | Static package reading and evidence export. |

## Mini App Discovery

Mini App Discovery is the first of the paper's three modules. It searches for
AppIDs and metadata; this initial collector acquires their packages. Discovery
is unnecessary when a catalogue is already available.

The Docker discovery runner maintains its keyword queue and returned app records
in a persistent SQLite database. It uses the Frida search adapter from the
separately initialized `scraper/MiniCrawler` submodule, mounted read-only rather
than copied into the image. Follow the [Docker guide](../docs/DOCKER.md) for
initialization, bounded runs, checkpoint status and catalogue handoff.

Initialize the external search adapter from the **repository root**:

```bash
git submodule update --init scraper/MiniCrawler
```

The reference is pinned to commit
`5cd8dff0ea25f8bd52f4898954b8bd8d76ef6958` of
[luca959/MiniCrawler](https://github.com/luca959/MiniCrawler). The catalogue
builder can also read a database selected with `WECHAT_DISCOVERY_DB`. Initializing the
submodule does not populate either database, configure a phone or start queries.
The upstream project contains other Frida/Xposed scripts that are not required
by the Docker search runner. Its pinned revision has no license file; consult
[provenance and dependency terms](../docs/PROVENANCE.md) before redistribution.

Compose exposes discovery, initial collection and version acquisition as
separate services. Assign different phones and state paths to simultaneous
roles; Compose does not allocate phones or schedule the three roles itself.

## Tests and provenance

```bash
python3 -B -m unittest discover -s . -p 'test_*.py' -v
```

Tests use synthetic fixtures and mocks; they do not run acquisitions, operate
phones, or contact external storage. If a system temporary directory is a symlink and
parser tests reject it, set `TMPDIR` to a physical directory. Offline tests do
not replace a live check against the WeChat version in use.

This release includes the initial collector derived from
[WeChatMiniAppScraper](https://github.com/luca959/WeChatMiniAppScraper), based on
commit `be2f02653a98a4ccd42270f3a175bf873365fa3b`, with the metadata and preservation
changes described here. See [PROVENANCE.md](../docs/PROVENANCE.md) for scope and
licenses. Private configuration, collected data, APKs and credentials are not
part of the release.
