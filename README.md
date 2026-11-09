# MiniZoo Scraper

![Mini-app discovery, collection and version workflow](docs/images/mini-app-workflow.png)

Collect WeChat Mini Programs and their original `.wxapkg` packages into
`collection_results/` inside this project. Each module runs independently.
The public release saves locally and contains no Google Drive uploader.

## Start with Docker

Docker supplies Python, Frida, OCR and reconstruction tools. A separate Appium
container supplies the navigation server, JDK and Android SDK for versioning.
From the repository root:

```sh
docker compose build collection appium
docker run --rm --network none minizoo-scraper:public --help
mkdir -p private/config private/input collection_results
cp docker/env.example .env
```

If `.env` already exists, edit it instead of replacing it. Set `LOCAL_UID` and
`LOCAL_GID` to your `id -u` and `id -g`. The [Docker guide](docs/DOCKER.md) covers
phone setup, independent module commands and persistent local results.

On macOS, Docker Desktop uses the host ADB server for the USB connection. On
Linux, the optional `compose.linux-usb.yaml` also runs that server in Docker.
WeChat and Frida's device-side server still run on your authorized Android phones.
Starting a module without arguments prints help and does not start collection.

## Independent modules

| Module | Docker command | Native command | Guide |
| --- | --- | --- | --- |
| Discovery | `docker compose run --rm discovery --help` | `python3 minizoo.py discovery` | [Discovery](discovery/README.md) |
| Initial collection | `docker compose run --rm collection --help` | `python3 minizoo.py collection` | [Initial collector](scraper/README.md) |
| Version collection | `docker compose run --rm version --help` | `python3 minizoo.py version` | [Version collector](version_scraper/README.md) |
| Package inspection | `docker compose run --rm collector inspect --help` | `python3 minizoo.py inspect` | [Shared core](MiniZooCollectionCore/README.md) |
| Reconstruction | `docker compose run --rm collector decompile --help` | `python3 minizoo.py decompile` | [Shared core](MiniZooCollectionCore/README.md) |
| Metadata | `docker compose run --rm collector metadata --help` | `python3 minizoo.py metadata` | [Metadata](scraper/README_package_metadata.md) |

Run commands from the repository root. Discovery additionally mounts its pinned
external adapter as explained in its guide. It is optional when you already have
a catalogue. Initial collection does not require Appium. Version collection uses
Appium but does not start discovery or initial collection. Processing saved
packages needs no phone. Each module has its own input, output and checkpoints.

The collectors share identity checks, package acquisition, metadata extraction
and reconstruction; navigation and queues remain independent. Optional multi-phone
batches are described in the [automation guide](scraper/automation/README.md).

## Install only what you use

For native execution instead of Docker, create one environment at the root:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 minizoo.py --help
```

Live modules require Android Platform Tools, an authorized rooted Android phone
with WeChat logged in, and matching Frida client/server versions. Follow the
module guide for its specific prerequisites: the discovery adapter, ADBKeyboard,
or Appium and OCR. Use a separate phone and state directory for simultaneous roles.

For native reconstruction, install Node.js 26+ and the pinned tool:

```sh
git submodule update --init version_scraper/vendor/wxappUnpacker
npm ci --prefix version_scraper/vendor/wxappUnpacker --ignore-scripts --no-audit --no-fund
```

Docker already includes the reconstruction dependency. Discovery installs its
external adapter separately. Source ZIP downloads do not contain submodules;
use the revisions in [provenance](docs/PROVENANCE.md).

## Project layout and local results

```text
minizoo.py                      # Common launcher
discovery/                      # Discovery and optional external adapter
scraper/                        # Initial collection and optional batches
version_scraper/                # Version collection and navigation adapters
MiniZooCollectionCore/          # Shared acquisition and processing engine
tools/                         # Private version-config generator
docker/                        # Collector and Appium container setup
compose.yaml                   # Independently started services
compose.linux-usb.yaml          # Optional Linux USB server
private/                       # Your configs, inputs and keys; ignored by Git
collection_results/            # Default outputs; ignored by Git
  discovery/                   # SQLite query queue and catalog.csv
  initial/                     # Acquisitions, evidence and consolidation
  version/                     # Working CSV, packages, SQLite and checkpoints
  automation/                  # Optional supervisor state
```

Inputs, configurations, acquired packages, device identifiers, screenshots and
logs stay outside the tracked source and Docker build context. Do not force-add
them. The bundled example CSV is synthetic. Check staged files before publication.

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
