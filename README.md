# MiniZoo Scraper

![Mini-app discovery, initial collection, consolidation and version revisit workflow](docs/images/mini-app-workflow.png)

Collect WeChat Mini Programs and their original `.wxapkg` packages into
`collection_results/` inside this project. The public release contains local
collection and verification tools only. It has no Google Drive uploader, cloud
publication command, cloud credentials or rclone dependency.

| Module | Purpose | Guide |
| --- | --- | --- |
| Discovery | Search keywords, record AppIDs and public metadata, checkpoint the query queue. | [Docker and discovery](docs/DOCKER.md) |
| Initial collection | Open catalogue entries through ADB and Frida; retain packages and runtime evidence. | [Initial collector](scraper/README.md) |
| Version collection | Revisit known AppIDs through Appium, compare versions and update a working CSV. | [Version collector](version_scraper/README.md) |

Both collectors use [MiniZooCollectionCore](MiniZooCollectionCore/README.md) for
package selection, identity checks, metadata extraction and reconstruction.
Discovery is optional when a catalogue already exists. Use a separate phone and
state directory for each simultaneous role. Mini Program packages run inside
WeChat; they are not Android APKs.

## Requirements and setup

Use Python 3.12, Android Platform Tools, rooted Android phones with WeChat
installed and logged in, and matching Frida client/server versions. Initial
collection uses ADBKeyboard; version navigation uses Appium. Native macOS OCR
uses Apple Vision; the Linux container uses Tesseract.

For Docker, follow the [Docker guide](docs/DOCKER.md). Default commands print
help and do not start collection:

```sh
docker compose build
docker run --rm --network none minizoo-scraper:public
```

For native setup, follow the [initial](scraper/README.md#installation) or
[version](version_scraper/README.md#installation) collector guide. Initialize
only the external components needed by the selected workflow:

```sh
# Reconstruction for either collector:
git submodule update --init version_scraper/vendor/wxappUnpacker
# Optional keyword discovery:
git submodule update --init scraper/MiniCrawler
```

For source archives without Git metadata, clone the dependencies into those
same directories and check out the exact revisions in
[PROVENANCE.md](docs/PROVENANCE.md). Dependency installation does not configure
phones or start a collection.

## Local output

Defaults for native commands and Docker Compose use this layout:

```text
MiniZooScraper/
  collection_results/                 # Ignored by Git and Docker builds
    discovery/                        # Query database and discovered catalogue
    initial/                          # Initial runs and their evidence
      active_runs/                    # Optional multi-device supervisor runs
      all_mini_apps/                  # Locally consolidated packages and CSVs
    automation/                       # Supervisor locks, logs and retry state
    version/
      apps.csv                        # Working baseline; input stays untouched
      output/
        collections/<appid>/<attempt>/
        checkpoints/
        reports/
        state.sqlite3
```

Each acquisition retains original packages, available metadata, runtime evidence
and reconstruction results. Failed or partial acquisitions remain local for
inspection. Reports contain local file paths and integrity checks. Stopping a
collector or removing a container does not delete its acquired files.

Copy the example configurations before editing device serials and input paths.
Version-collector relative paths resolve from the configuration file's folder,
so its default `config.json` belongs beside `config.example.json`. Docker Compose
binds the host project's `collection_results/` to `/data` in the container.
Explicit output overrides are supported; use separate directories for independent
runs and preserve checkpoint paths when resuming.

Real catalogues, device/account identifiers, screenshots, logs and configuration
are private data. Keep them under ignored `collection_results/` or `private/`.
Only the example CSV is synthetic. `.gitignore` cannot protect an already tracked
file or a forced add: inspect staged files and commit author emails before a
public commit. Use a GitHub noreply email where required. Local results are not
anonymized merely because they are excluded from Git.

## Evidence and validation

Runtime observations must corroborate package ownership. Search labels, static
AppID references and hashes alone do not establish ownership or release versions.
A version baseline advances only after identity, version, complete coverage and
artifact checks pass. Missing components and reconstruction failures remain
explicit; valid original bytes are retained. See the
[shared-core policy](MiniZooCollectionCore/README.md).

Run the offline suites separately:

```sh
(cd scraper && python3.12 -B -m unittest discover -s . -p 'test_*.py')
(cd version_scraper && python3.12 -B -m unittest discover -s tests)
python3.12 -B -m unittest discover -s docker -p 'test_*.py'
```

Tests use synthetic fixtures and mocked devices. Integration tests require the
pinned decompiler, OCR dependencies or discovery adapter; missing prerequisites
are reported as skips. A live acquisition is a separate check on an idle phone.

## License and citation

Project-authored code uses the [MIT License](LICENSE). External components and
Mini Program packages retain their own rights; see [provenance](docs/PROVENANCE.md).
Author credit is in [CITATION.cff](CITATION.cff). The associated preprint is in
preparation; no arXiv identifier has been assigned here.
