# Mini Program version collector

Revisit known AppIDs, save original packages and runtime evidence locally, and
compare observed versions against a working CSV baseline. Appium navigates the
phone, ADB reads its cache, and Frida verifies the opened AppID and release.
The [shared core](../MiniZooCollectionCore/README.md) verifies and processes the
packages. This public collector has no cloud transfer functionality.

Run setup commands from `version_scraper/`. Relative data paths in configuration
resolve from the configuration file's folder, independently of the launch folder.
Keep `config.json` beside the template unless you adjust its relative paths.
For Linux containers, see [Docker](../docs/DOCKER.md).

## Installation

Use Python **3.12**, Node.js **26+**, npm 10+, Android Platform Tools (`adb`), a
JDK and an authorized Android phone with WeChat already installed and logged in.
Reading the cache requires root and a Frida server compatible with the client;
this project does not install root. The native `visual` adapter uses Apple Vision
and requires macOS with Xcode Command Line Tools. The
[Docker guide](../docs/DOCKER.md) supplies the Linux Tesseract adapter and explains
which device services remain on the host. The `native` UI mode requires
appropriate selectors for the installed WeChat version.

Initialize the pinned decompiler from the **repository root**, then install:

```bash
git submodule update --init version_scraper/vendor/wxappUnpacker
cd version_scraper
npm ci
npm ci --prefix vendor/wxappUnpacker
python3.12 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
swiftc tools/ocr.swift -o tools/ocr
cp config.example.json config.json
```

For a source archive without `.git`, use the [pinned dependency revisions](../docs/PROVENANCE.md) before installation.

The Swift command is for native macOS. On Linux, skip that command and install
Tesseract with the `eng` and `chi_sim` language data. The Tesseract adapter also
requires Pillow for its dark-screen contrast/inversion pass. Install its pinned
Python dependency into the same virtual environment, from `version_scraper/`:

```bash
python3 -m pip install -r ../docker/requirements.txt
```

Then set `ocr_command` in `config.json` to:

```json
["python3", "{project_dir}/tools/ocr_tesseract.py"]
```

Docker installs these OCR dependencies automatically. Native Apple Vision does
not require Pillow. The adapter preserves screenshot bytes and pixel geometry;
see the [Docker validation results](../docs/DOCKER.md#validation-scope) for the
bounded device checks and remaining platform limits.

Edit `config.json` before collecting:

| Setting | Meaning |
| --- | --- |
| `source_csv` | Read-only input with `appid`, `name` and the configured baseline column. `examples/apps.csv` has one synthetic row solely to show the schema. |
| `work_csv`, `output_dir` | Working copy and results, defaulting to `../collection_results/version/` inside the project. |
| `expected_records` | Expected input row count. |
| `devices` | ADB serial, a distinct name and `system_port` for each phone. `account` groups devices that share account-level pauses. |
| Adapter commands | Argument lists for installed Python, Frida, OCR and decompiler executables. `{project_dir}` expands to the code directory; placeholders such as `{appid}` and `{serial}` are filled for each acquisition. |
| `volume_mount`, `volume_uuid` | Optional checks for a particular mounted macOS volume. |

`config.json`, `.venv/`, `collection_results/`, compiled OCR and installed npm
dependencies are excluded from Git. The core is discovered at
`../MiniZooCollectionCore/`; `MINIZOO_CORE_PATH` can override its location.

## Collection and checkpoints

Edit your local `config.json` with the real phone serial and input catalogue.
The bundled example has one synthetic row and does not represent an actual app
queue. Start the local Appium server in its own terminal:

```sh
./start_appium.sh
```

Then, from the activated Python environment:

```sh
python3 -B main.py --config config.json init
python3 -B main.py --config config.json doctor
python3 -B main.py --config config.json run --devices android1 --max-items 1
python3 -B main.py --config config.json status
```

`init` creates a working CSV and persistent state without contacting a phone.
`doctor` checks the input, device/root access, Appium and configured adapters.
`run` continues pending work; omit `--max-items` for the full queue after checking
a bounded result. The limit includes failed attempts. `status` is read-only.

| Command | Effect |
| --- | --- |
| `run` | Continue pending acquisition, resuming saved local processing first. |
| `resume` | Explicitly resume a rate-limit pause; otherwise also retry failed reconstruction. |
| `retry --appids wx0123456789abcdef` | Requeue selected outcomes while retaining old attempts. |
| `report` | Write local `reports/latest.json` and `reports/results.csv`. |
| `probe` | Inspect configured devices and retain local diagnostics. |

Press Ctrl+C once and wait for the checkpoint to be saved. An output lock stops
two processes from modifying the same state. Use an otherwise idle phone for
`probe`; stop acquisition before `retry`, `resume` or `report`.

## Files retained locally

The default output root is `../collection_results/version/`:

```text
apps.csv                             # Working baseline
output/
  initial_manifest.json
  state.sqlite3
  checkpoints/                       # Original CSV, account and pause state
  reports/                           # Human-readable status and device diagnostics
  collections/<appid>/<attempt>/
    manifest.json
    packages/                        # Original .wxapkg bytes
    evidence.json                    # Identity, release and coverage evidence
    package_metadata.json
    processing.json
    logs/
    ...                              # Reconstructed files and ZIP when successful
```

Paths in reports point to these local files. Completed manifests are pinned by
SHA-256 in SQLite; missing or changed manifests are reported. Before accepting a
resumed completed acquisition, package hashes, reconstruction artifacts and the
archive are verified again. Keep the whole output tree and its original absolute
paths together when resuming. The input CSV is preserved; only the working copy
can advance its baseline.

Incomplete packages, ambiguous identities, failed decompilations and diagnostics
remain in their attempt directory. They do not become verified complete releases.
A newer baseline requires verified identity, a comparable version, complete
coverage and verified artifacts. Partial coverage remains explicit. Static
metadata or a search card alone cannot authorize a baseline update.

## Pause and retry behavior

Ten consecutive distinct apps with genuinely empty search results pause the
configured devices (`empty_search_limit: 10`). The counter survives restarts;
a nonempty result resets it. Repeated attempts for the same app do not count as
new apps. Explicit rate-limit responses stop collection immediately. The pause
reason and minimum wait are retained under `output/checkpoints/`.

`run` reports a persisted pause without starting new searches. Resume deliberately
after the recorded wait. Interrupted searches remain pending and do not consume
the retry budget. Completed acquisitions and original package bytes stay intact.
Use `retry --appids ...` followed by `run` for terminal partial or ambiguous
outcomes; each new attempt retains the previous history.

## Tests

```sh
python3 -B -m unittest discover -s tests
```

The offline suite covers acquisition with simulated phones, identity/coverage
checks, preservation of original bytes, baseline updates, restart behavior,
local manifest integrity and operation from another working directory. It does
not perform a live acquisition or certify all WeChat releases.
