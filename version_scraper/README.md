# Mini App Version Crawler

Revisit known AppIDs, retain packages and runtime evidence, and compare versions
against a working CSV. Appium navigates, ADB reads the cache and Frida verifies
identity. This module has its own configuration and checkpoints; it does not
launch discovery or initial collection. Run commands from the repository root.

## Commands

```sh
python3 minizoo.py version --help
```

For a container installation, use the independent `version` service in the
[Docker guide](../docs/DOCKER.md).

## Installation

Use the common Python environment and reconstruction dependency from the
[main guide](../README.md#install-only-what-you-use). Live collection additionally
needs Android Platform Tools, a JDK, Node.js 26+, an authorized rooted phone with
WeChat logged in, and a matching Frida server. Install the pinned Appium server
and Android driver:

```sh
npm ci --prefix version_scraper --no-audit --no-fund
```

For native macOS, install Xcode Command Line Tools and compile Apple Vision OCR:

```sh
swiftc version_scraper/tools/ocr.swift -o version_scraper/tools/ocr
```

For native Linux, install Tesseract with English and Simplified Chinese language
data and `python3 -m pip install -r docker/requirements.txt`. The configuration
generator chooses Tesseract on Linux and Vision on macOS; `--ocr` overrides it.
[Docker](../docs/DOCKER.md) includes Tesseract, Python, Appium, the JDK and
Android SDK. On macOS, only the USB ADB server stays on the host; Linux can
run that server in Docker too. OCR selects controls; runtime evidence
still determines whether a package can be accepted.

## Configure your input once

Supply a UTF-8 CSV with `appid`, `name` and `previous_app_version`. The schema
example at `version_scraper/examples/apps.csv` is synthetic and is not a live
collection queue. Choose the phone serial from `adb devices -l`:

```sh
python3 minizoo.py configure-version \
  --serial DEVICE_C --csv private/input/apps.csv
python3 minizoo.py version --config private/config/version.json init
python3 minizoo.py version --config private/config/version.json status
```

Configuration generation counts rows, preserves the source and refuses to
overwrite an existing config. `init` creates a working CSV and local SQLite
state; neither `init` nor `status` contacts a phone. Results default to
`collection_results/version/`. Use `--data-dir` and `--output` at configuration
time for an independent trial. Relative paths in a manually edited config
resolve from that config's folder, regardless of the launch folder.

The [example config](config.example.json) documents pacing and adapter commands.
Unknown configuration fields are rejected before collection starts.
Each device needs a unique `name`, `udid` and Appium `system_port`; `account`
groups devices sharing account-level pauses. Keep real settings under `private/`.
`load_wait` is the delay in seconds after opening a mini app (default: 8). Raise
it for slow first downloads. An app still downloading may have no verifiable
package binding; retain the outcome and retry after loading completes.

## Start one bounded acquisition

Start Appium in a separate terminal:

```sh
./version_scraper/start_appium.sh
```

With the phone awake, unlocked and showing WeChat:

```sh
python3 minizoo.py version --config private/config/version.json doctor
python3 minizoo.py version --config private/config/version.json run --max-items 1
python3 minizoo.py version --config private/config/version.json status
```

`doctor` contacts the phone and Appium. `run` starts acquisition and resumes saved
local processing first. The item limit includes failed attempts; omit it only
when you intend to process the full queue. Use one active collector per phone
and per output directory. Press Ctrl-C once and allow the checkpoint to finish.

| Command | Purpose |
| --- | --- |
| `status` | Inspect local state. |
| `report` | Write local JSON/CSV reports. |
| `resume` | Deliberately clear an eligible persisted pause or retry processing. |
| `retry --appids APPID` | Requeue selected outcomes, retaining older attempts. |
| `probe` | Contact an idle phone and retain diagnostics. |

Ten consecutive distinct empty searches pause collection; rate-limit responses
stop it immediately. Counters and the minimum wait survive restart. `run` does
not silently clear a pause. Interrupted searches stay pending without consuming
the retry budget. Stop acquisition before `retry`, `resume` or `report`.

Connection or worker failures return a nonzero exit code and retain a device
report. A completed command can still contain per-app failures: inspect
`status` or `report` before treating an acquisition as complete.

## Local outputs and evidence

```text
collection_results/version/
  apps.csv                          # Working baseline; original input preserved
  output/
    initial_manifest.json
    state.sqlite3
    checkpoints/
    reports/
    collections/<appid>/<attempt>/
      manifest.json
      packages/
      evidence.json
      package_metadata.json
      processing.json
      logs/
```

Public metadata and capture time are saved before decompilation. Missing fields
remain explicit and do not invalidate original bytes. Verified reconstructed
root manifests can provide a separately recorded, provenance-checked metadata
fallback; they cannot invent release versions or acquisition dates. See the
[metadata guide](../MiniZooCollectionCore/README.md#extract-metadata-from-a-saved-acquisition).

A baseline advances only for a newer comparable runtime version with verified
identity, complete package coverage and verified artifacts. Failed decompilation,
partial coverage and ambiguous identity retain their files without becoming
verified complete releases. Package hashes, saved manifests and reconstruction
receipts are checked again when resuming. Preserve the complete output tree at
its original paths; do not rewrite evidence to move checkpoints.
