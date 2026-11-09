# MiniZoo Scraper

![Mini-app discovery, collection and version workflow](docs/images/mini-app-workflow.png)

Collect WeChat Mini Programs and their original `.wxapkg` packages into
`collection_results/` inside this project. Each module runs independently.
The public release saves locally and contains no Google Drive uploader.

## Try it without a phone

From the repository root, with Python 3.12:

```sh
python3.12 minizoo.py --help
python3.12 minizoo.py test
```

The Python tests use the standard library, synthetic files and mocked phones.
You do not need Frida, Appium, Docker, credentials or a dataset. Tests requiring
Node.js, OCR or optional upstream dependencies report explicit skips. Nothing
starts collecting when the launcher or a module is invoked without arguments.

## Independent modules

Run every command below from the repository root. Append `--help` to see the
selected module's complete options.

| Module | Start | Test without a phone | Guide |
| --- | --- | --- | --- |
| Discovery | `python3 minizoo.py discovery` | `python3 minizoo.py test discovery` | [Discovery](discovery/README.md) |
| Initial collection | `python3 minizoo.py collection` | `python3 minizoo.py test collection` | [Initial collector](scraper/README.md) |
| Version collection | `python3 minizoo.py version` | `python3 minizoo.py test version` | [Version collector](version_scraper/README.md) |
| Package inspection, reconstruction and metadata | `python3 minizoo.py inspect`, `decompile`, `metadata` | `python3 minizoo.py test core` | [Shared core](MiniZooCollectionCore/README.md) |

`python3 minizoo.py test cli` checks independent command startup, private config
generation, input preservation and error propagation. GitHub Actions runs each
suite separately on Linux and macOS. [Testing details](docs/TESTING.md) distinguish
unit tests, optional dependency checks and bounded tests on a real phone.

Discovery is optional if you already have a catalogue. Initial collection does
not need the version collector or Appium. Version collection does not start
discovery or the initial collector. Offline inspection needs only a saved package.
The collectors share one implementation of identity checks, package acquisition,
metadata extraction and reconstruction; navigation and queues remain independent.

## Install only what you use

For live work, create one environment at the repository root:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

Live modules require Android Platform Tools, an authorized rooted Android phone
with WeChat logged in, and matching Frida client/server versions. Follow the
module guide for its specific prerequisites: the discovery adapter, ADBKeyboard,
or Appium and OCR. Use a separate phone and state directory for simultaneous roles.

For reconstruction in either collector, install Node.js 26+ and the pinned tool:

```sh
git submodule update --init version_scraper/vendor/wxappUnpacker
npm ci --prefix version_scraper/vendor/wxappUnpacker --ignore-scripts --no-audit --no-fund
```

Discovery installs its own external adapter separately. Source ZIP downloads do
not contain submodules; use the revisions in [provenance](docs/PROVENANCE.md).
For a container installation, follow [Docker](docs/DOCKER.md).

## Project layout and local results

```text
minizoo.py                      # Common launcher and per-module test runner
discovery/                      # Discovery, its tests and optional adapter
scraper/                        # Initial collection; optional batch automation
version_scraper/                # Version collection, adapters and tests
MiniZooCollectionCore/           # Shared engine and its own tests
tests/                          # Launcher/configuration tests
tools/                          # Private version-config generator
docker/                         # Container entry point and smoke checks
private/                        # Your configs and inputs; ignored by Git
collection_results/             # All default outputs; ignored by Git
  discovery/                    # SQLite query queue and catalog.csv
  initial/                      # Runs, evidence and optional consolidation
  version/                      # Working CSV, packages, SQLite and checkpoints
  automation/                   # Optional initial-collector supervisor state
```

Inputs, configurations, acquired packages, device identifiers, screenshots and
logs stay outside the tracked source. Do not force-add them. The bundled example
CSV is synthetic. Check staged files before any public commit.

Configuration generation never overwrites an existing file. Version paths are
resolved from the config file's folder; explicit CLI paths keep the caller's
working directory. Preserve existing output paths when resuming saved evidence.
A module does not automatically launch another module or schedule later runs.

## Evidence and metadata

Runtime identity and checksums must corroborate package ownership. Multiple hashes
do not by themselves prove multiple versions. A version baseline advances only
after identity, version, complete coverage and artifact checks pass.

Public metadata is saved before decompilation. Missing values remain explicit;
search keywords are not categories and export times are not acquisition times.
Static app declarations are a separate, provenance-checked fallback. Partial
acquisitions and reconstruction failures preserve valid original bytes. See the
[core policy](MiniZooCollectionCore/README.md#recovering-missing-metadata) and
[interrupted-run recovery](scraper/README.md#recovering-an-interrupted-run).

## License and citation

Project-authored code uses the [MIT License](LICENSE). External components and
mini-app packages retain their own rights; see [provenance](docs/PROVENANCE.md).
Author credit is in [CITATION.cff](CITATION.cff). The associated preprint is in
preparation; no arXiv identifier is assigned here.
