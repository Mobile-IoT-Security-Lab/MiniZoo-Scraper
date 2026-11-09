# Collect newer versions

Revisit known Mini Programs and compare their observed versions with a saved baseline.
**Input:** AppID, name and previous version. **Output:** `collection_results/version/`.

## Quick start with Docker

First complete [installation](../README.md#1-install-and-try-it) and
[phone setup](../docs/DOCKER.md#connect-phones). Run commands from the repository
root in the terminal where you set `DEVICE_SERIAL`.

### 1. Add the apps to revisit

Create `private/input/apps.csv` with these columns:

```csv
appid,name,previous_app_version
wx0123456789abcdef,Example Mini App,1
```

The row above is synthetic: replace it with a real AppID, name and known previous
version. Keep the header. Add one app per row.

### 2. Start Appium

Appium controls navigation for this module. Install and start its container:

```sh
docker compose build appium
docker compose up -d --wait appium
```

For later runs, only the second command is needed. If port 4723 is already in
use, see [Appium configuration](../docs/DOCKER.md#appium-configuration).

### 3. Create the queue once

```sh
docker compose run --rm -v "$PWD/private/config:/generated" collector \
  configure-version --container --serial "$DEVICE_SERIAL" --csv /input/apps.csv \
  --output /generated/version.json
docker compose run --rm version --config /config/version.json init
```

This saves configuration in `private/config/version.json` and initializes local
state. It preserves the source CSV. For later runs, keep those files and skip
this step; configuration generation refuses to overwrite existing settings.

### 4. Check the phone and collect one app

Keep WeChat open, then check the prerequisites:

```sh
docker compose run --rm version --config /config/version.json doctor
```

Continue only when `blockers` is empty:

```sh
docker compose run --rm version --config /config/version.json run --max-items 1
docker compose run --rm version --config /config/version.json status
```

### 5. Read the results and continue

Open **`collection_results/version/output/reports/`** for outcomes and
**`collection_results/version/output/collections/`** for saved acquisitions.
The working baseline is `collection_results/version/apps.csv`.

Run the same `run` command again to process the next pending item. Increase
`--max-items` for a larger batch; failed attempts also count toward this limit.
A completed command can contain per-app failures, so inspect `status` or the reports.

Press Ctrl-C once and wait for the checkpoint. When finished with this module:

```sh
docker compose stop appium
```

## Try without a phone

After building the collector image:

```sh
docker run --rm --network none minizoo-scraper:public example version
```

Expect `PASSED: version`. The synthetic example checks queue initialization,
baseline preservation and reports without Appium or a phone. See
[example output](../docs/DOCKER.md#immediate-examples) to keep its files.

## Results, restart and limits

Use one collector per phone and output directory. The version baseline advances
only after a newer comparable version has verified identity, complete package
coverage, intact artifacts and successful reconstruction. Partial acquisitions
and missing metadata remain explicit. Keep the complete output tree at its
original paths when resuming.

Ten consecutive distinct empty searches or a rate-limit response pause the queue.
Respect the saved waiting period and inspect the phone before requesting `resume`.
The `run` command never silently clears a pause.

Use the same `--config /config/version.json` with these commands:

| Command | Purpose |
| --- | --- |
| `status` | Read queue progress and per-outcome counts. |
| `report` | Write current JSON/CSV reports. |
| `resume` | Clear an eligible pause or retry pending local processing. |
| `retry --appids APPID` | Requeue selected outcomes while preserving previous attempts. |

Stop acquisition before `report`, `resume` or `retry`. The [example configuration](config.example.json)
describes pacing and adapter options. Unknown fields are rejected. `load_wait`
controls the wait after opening an app; its default is 8 seconds.

## Native usage

Follow the [native installation](../docs/NATIVE.md), including reconstruction
tools. Install a JDK and the pinned Appium dependencies:

```sh
npm ci --prefix version_scraper --no-audit --no-fund
```

On macOS, install Xcode Command Line Tools and compile OCR:

```sh
swiftc version_scraper/tools/ocr.swift -o version_scraper/tools/ocr
```

On Linux, install Tesseract with English and Simplified Chinese language data,
then `python3 -m pip install -r docker/requirements.txt`. Configuration generation
selects the platform's OCR automatically.

With your CSV and selected phone, configure and initialize once:

```sh
python3 minizoo.py configure-version --serial "$DEVICE_SERIAL" --csv private/input/apps.csv
python3 minizoo.py version --config private/config/version.json init
```

Keep native and Docker configuration/output paths separate. Start
`./version_scraper/start_appium.sh` in another terminal, then:

```sh
python3 minizoo.py version --config private/config/version.json doctor
python3 minizoo.py version --config private/config/version.json run --max-items 1
python3 minizoo.py version --config private/config/version.json status
```

Offline example: `python3 minizoo.py example version`.
All commands: `python3 minizoo.py version --help`.
