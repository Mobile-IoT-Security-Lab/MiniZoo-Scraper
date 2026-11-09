# Initial Mini App Collector

Open names from a CSV, identify the Mini Program actually opened, and retain its
original packages and evidence locally. Navigation uses ADB and ADBKeyboard.
This module does not require Appium, the discovery module or a versioning queue.
Run the following commands from the repository root.

## Commands

```sh
python3 minizoo.py collection --help
```

For a container installation, use the independent `collection` service in the
[Docker guide](../docs/DOCKER.md).

## Installation

Create the common environment and install Frida as shown in the
[main guide](../README.md#install-only-what-you-use). For live collection, use
Android Platform Tools and an authorized rooted phone with WeChat logged in
and a matching Frida server. Find the serial with `adb devices -l`.

```sh
adb -s DEVICE_A get-state
adb -s DEVICE_A shell su -c id
ANDROID_SERIAL=DEVICE_A bash scraper/setup_adbkeyboard.sh
```

The keyboard installer downloads the separately licensed
[ADBKeyboard](https://github.com/senzhk/ADBKeyBoard) APK, verifies its pinned
SHA-256, and installs and enables it on this phone.
Some Android versions block this legacy APK through Play Protect. In that case,
ASCII search names can use `--allow-no-adbkeyboard`; Chinese names still require
a compatible keyboard. The Appium version collector does not depend on ADBKeyboard.
The APK is not bundled. The expected checksum is
`e698adea5633135a067b038f9a0cf41baa4de09888713a81593fb2b9682cdc59`.

Install Node.js 26+ and the pinned reconstruction dependency using the main
guide. This collector imports the bundled [shared core](../MiniZooCollectionCore/README.md).
The [Docker guide](../docs/DOCKER.md) provides an alternative installation.

## Run one acquisition

Supply your own UTF-8 CSV with a `name` column. Include `appid`, `description`,
`developer`, `icon_url` and `app_version` when available; `--name-column` selects
a differently named column. Discovery's `catalog.csv` is accepted directly.

```sh
python3 minizoo.py collection \
  --serial DEVICE_A --csv private/input/initial.csv --limit 1 \
  --output collection_results/initial/trial-001
```

Use a new output directory for a new run; an existing `manifest.csv` is rejected
before device access. `--start` and `--limit` select rows, not a resume point.
If a search for A opens B, runtime evidence identifies B: B's packages and
observed metadata are retained while A remains unresolved. Search input stays
separate in `queried_*` fields.

| Option | Use |
| --- | --- |
| `--open-method search` | Default text search. |
| `--open-method auto` / `qr` | Try or require available Mini Program QR/code assets. |
| `--manual-before-type --manual-open-result` | Allow manual UI actions in an interactive terminal. |
| `--prepare-only` | Prepare metadata/QR assets without a phone; public HTTP sources may still be queried. |
| `--mini-programs-row-y 0.435` | Adjust the Mini Programs row for a different Discover menu layout; default is `0.515`. Also accepts `WECHAT_MINIPROGRAMS_ROW_Y`. |
| `--allow-no-adbkeyboard` | Use the system keyboard for ASCII-only search names when ADBKeyboard is unavailable. |
| `--frida PATH` | Select a Frida executable; `WECHAT_FRIDA` is also supported. |

All methods retain the same identity checks. Cache deletion is rejected. The
retired `--packages-per-app` option is ignored: every available runtime-bound
component is eligible for capture. An unknown screen stops navigation.

## Local results

```text
collection_results/initial/trial-001/
  manifest.csv
  package_history.csv
  package_metadata.csv
  package_metadata_evidence/
  attempt_records/
  attempts/<attempt>/
    before/messages.json
    after/messages.json
    opened_app_evidence.json
    evidence.json
    packages/
    package_metadata.json
    processing.json
```

Public fields come from the observed AppID's runtime or device metadata,
including categories and registered developer when available. Missing or
conflicting values remain explicit. Static extraction and reconstruction finish
before the next search. Coverage is complete only when original configuration,
runtime inventory and acquired components agree; a decompilation failure keeps
valid original bytes. See the [metadata format](README_package_metadata.md).

Saved packages can be inspected independently:

```sh
python3 minizoo.py inspect /absolute/path/package.wxapkg
```

## Recovering an interrupted run

Acquisition time, metadata, hashes and identity evidence are saved before
reconstruction. Stop the writer, then recover its durable records offline:

```sh
python3 minizoo.py collection --recover-output collection_results/initial/trial-001
```

Recovery revalidates evidence and bytes, preserves the previous manifest and
rebuilds CSV records without duplicating an attempt. It does not open another
app, invent missing fields or replace acquisition dates with recovery time.
Keep the complete acquisition directory and its original paths together.

## Optional batch collection

Single-device collection needs no scheduler. The remaining multi-device
supervisor and local consolidation tools are documented in the
[automation guide](automation/README.md). They preserve separate workers,
checkpoints and original evidence. Legacy retry daemons, external rating
scrapers and the old manual merger are no longer part of this release.
