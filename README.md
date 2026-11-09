# MiniZoo Scraper

![Mini-app discovery, collection and version workflow](docs/images/mini-app-workflow.png)

Collection tools for WeChat Mini Programs: discover apps, acquire their original
`.wxapkg` packages and revisit apps for newer versions. Each module runs independently.

Results are saved inside `collection_results/`. Keep configuration and input
files under `private/`. Both directories stay outside the tracked source.

## Quick start with Docker

From the repository root, build the shared collector image and check its commands:

```sh
docker compose build collection
docker run --rm --network none minizoo-scraper:public --help
```

This opens help without contacting a phone. For live collection, follow the
[Docker guide](docs/DOCKER.md) to connect an authorized rooted Android phone,
prepare local inputs and select one of the modules below. WeChat must be logged
in and the phone must run a matching Frida server. Version collection also uses
the separate Appium service; discovery needs its pinned external adapter.

## Choose a module

| Module | Native command | Guide |
| --- | --- | --- |
| Mini App Discovery | `python3 minizoo.py discovery` | [Discovery guide](discovery/README.md) |
| Mini App Crawler | `python3 minizoo.py collection` | [Initial collection guide](scraper/README.md) |
| Mini App Version Crawler | `python3 minizoo.py version` | [Version collection guide](version_scraper/README.md) |

To try each command's help with Docker, no phone, input files or local mounts
are needed:

```sh
docker run --rm --network none minizoo-scraper:public discovery --help
docker run --rm --network none minizoo-scraper:public collection --help
docker run --rm --network none minizoo-scraper:public version --help
```

For a first live run, prepare the phone and inputs in the selected guide, then
use `--max-queries 1` for discovery, `--limit 1` for initial collection or
`--max-items 1` for version collection. Use a separate phone and output directory
for simultaneous roles. Optional batch collection and local consolidation are
in the [batch guide](scraper/automation/README.md).

Saved packages can be processed without a phone:

```sh
python3 minizoo.py inspect /absolute/package.wxapkg
python3 minizoo.py decompile --help
python3 minizoo.py metadata --help
```

For Docker, use `docker compose run --rm collector COMMAND` and paths under
`/data/`. The [shared core guide](MiniZooCollectionCore/README.md) describes these
processing stages and their output formats.

## Install only what you use

For native execution, create the shared Python environment:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 minizoo.py --help
```

Live collection requires Android Platform Tools and the phone prerequisites
above. Follow the selected module's guide for keyboard, Appium or OCR setup.
For native reconstruction, install Node.js 26+ and the pinned unpacker:

```sh
git submodule update --init version_scraper/vendor/wxappUnpacker
npm ci --prefix version_scraper/vendor/wxappUnpacker --ignore-scripts --no-audit --no-fund
```

Docker includes the reconstruction dependency. Source ZIPs omit submodules;
see [dependency provenance](docs/PROVENANCE.md) for the required revision.

## Local data and evidence

Keep settings, input catalogues and keys in `private/`; outputs go to
`collection_results/`. Both folders are excluded from Git and Docker builds.
The bundled example CSV is synthetic. Before publishing source changes, run
`python3 tools/check_publication.py`; GitHub also checks for exposed secrets.

Package ownership is checked against the observed AppID. Consolidation verifies
bytes and deduplicates AppID/SHA-256 pairs. Multiple hashes alone do not establish
multiple versions. A version baseline advances only after identity, version,
complete coverage, file integrity and decompilation checks pass.

Available metadata and acquisition evidence are retained with the packages;
missing fields remain explicit. Partial acquisitions and failed reconstruction
preserve original bytes. Keep the complete output tree when resuming and inspect
per-app outcomes. See the selected module's guide for recovery and platform limits.

## License and citation

Project-authored code is [MIT licensed](LICENSE). External dependencies and
mini-app packages retain their own rights; see [provenance](docs/PROVENANCE.md).
Cite the software using [CITATION.cff](CITATION.cff).
