# Docker

The three Compose services match the paper's components:

| Service | Role | Persistent state |
| --- | --- | --- |
| `discovery` | Find AppIDs and metadata through WeChat's search bridge | `/data/discovery/data.db`, including the keyword queue and page cursors |
| `collection` | Acquire the first available packages of catalogue entries | The explicit `--output` directory under `/data` |
| `version` | Revisit identified apps and compare release versions | `/data/version`, configured by `docker/configure.py` |

The image packages the first-party discovery runner, both collectors, their
shared engine, Python 3.12, Node.js 26, Frida 17.18.0 / frida-tools 14.11.0, an
ADB client, Tesseract with English and Simplified Chinese data, Pillow
13.0.0 and the pinned wxappUnpacker source. Discovery additionally requires an
explicit read-only mount of the pinned MiniCrawler source. It is not copied
into the image. Starting the image or any service without arguments prints help
and exits; `docker compose up` does not start scraping. The `collector` service
remains available as a general tool entrypoint for older commands.

Phones connect to the host, where the ADB server and Appium run. The container runs the Python
collectors and processing tools. The setup uses **host networking**, available
in Docker Engine on Linux and as an option in Docker Desktop on macOS/Windows.
The device checks documented below used macOS. Host networking keeps
ADB device serials and Frida's localhost tunnels visible in the same TCP network.
It does not install WeChat, root a phone, sign in, bypass rate limits or provide
direct macOS USB passthrough.

## Build and check without a phone

Run these commands at the repository root:

```sh
git submodule update --init scraper/MiniCrawler
mkdir -p private/config private/input collection_results
cp docker/env.example .env
# Edit .env: storage paths and numeric UID/GID from `id -u` / `id -g`.
docker compose build
docker run --rm --network none minizoo-scraper:public
docker run --rm --network none --memory 1g --cpus 2 \
  --mount "type=bind,src=$PWD/scraper/MiniCrawler,dst=/opt/minicrawler,readonly" \
  minizoo-scraper:public python3 docker/smoke.py --discovery-source /opt/minicrawler
docker run --rm --network none minizoo-scraper:public discovery --help
docker run --rm --network none minizoo-scraper:public collection --help
docker run --rm --network none minizoo-scraper:public version --help
```

If `.env` already exists, edit it instead of replacing it. The commands assume
a Git checkout with its submodule reference intact. MiniCrawler must resolve to
`5cd8dff0ea25f8bd52f4898954b8bd8d76ef6958`. The adapter validates hashes of the
three source files it uses and reports a clear error for missing or modified
files. Read [dependency provenance](PROVENANCE.md): this separately obtained
source has no license file at that revision and is outside the project's MIT
license scope. The initial and version collectors do not require this mount.

The build downloads dependencies, including wxappUnpacker at commit
`694ca3874e67b89dd709f3d577c85a553bd45a0c`. Its source and GPL license remain in
`/opt/wxappUnpacker`; the project's MIT license does not replace dependency
licenses. The external MiniCrawler source is excluded from the image. Python and
Node major/minor lines are selected by image tags; the Dockerfile is not a
byte-for-byte reproducible lock of operating-system packages.

The offline smoke check exercises all three CLI imports, configuration
generation, the shared core, dependency versions, the actual OCR executable on
a synthetic blank image, OCR geometry tests and the decompiler checkout. With
`--discovery-source`, it also uses the real pinned MiniCrawler parser on
synthetic responses: metadata persistence, name/Chinese 2–4-gram queue growth,
page-cursor recovery, caps, callback correlation, selected device serials,
ten-empty-query pauses and explicit resume. Device subprocesses are mocked in
these tests. The smoke check never connects to ADB, Appium or external storage.

## Prepare the host

1. Install Docker Engine on Linux, or Docker Desktop **4.34 or later**. On Docker
   Desktop enable **Settings → Resources → Network → Enable host networking**.
   Apply this setting before starting acquisition. The supplied Compose file
   depends on this option; ordinary bridge networking is insufficient for the
   localhost ADB/Frida tunnels used here. See [Docker's host-networking
   documentation](https://docs.docker.com/engine/network/drivers/host/).
2. Follow the [native device and Appium setup](../version_scraper/README.md).
   The host needs Android SDK/ADB, Java, Node.js and the pinned Appium 3.9.0 /
   UiAutomator2 8.7.3 dependencies. The logged-in, rooted phone needs a compatible
   Frida server and ADBKeyboard. Keep each phone assigned to one collector.
   Before starting, wake and unlock the phone, close its notification shade,
   and bring WeChat to the foreground. For discovery, open the Mini Program
   search page. An obscured or unrecognized screen stops navigation safely.
3. With no conflicting session, start the normal host ADB server, then the host
   Appium server:

   ```sh
   adb start-server
   npm ci --prefix version_scraper
   ./version_scraper/start_appium.sh
   ```

   Leave Appium running on `127.0.0.1:4723`. An existing correctly configured
   server can be reused; do not restart ADB or Appium underneath an active run.
   With host networking, `127.0.0.1:5037` reaches the host ADB server and Frida's
   default ADB connection also reaches that server. Never run `adb kill-server`
   from a container while other collectors use the host server.

Compose limits each collector to two CPUs and 2 GiB RAM. Running several
containers requires additional host memory; these limits are starting values,
not a measured minimum for every workload. The image runs as an unprivileged
UID and does not need `--privileged` or a USB device mount.

## Private inputs and persistent state

Edit `.env` for your storage paths. On macOS/Linux set `LOCAL_UID` and `LOCAL_GID` to
the output of `id -u` and `id -g`, so the container can write the data directory.
Use a dedicated data directory on your intended disk. Configurations and inputs
are mounted read-only; packages, decompilations and checkpoints go under `/data`,
which is a persistent host bind mount. Removing a container leaves them intact.
The default writable host directory is `./collection_results/` inside this
project. It and `private/`, `.env`, credentials and datasets are excluded from
Git and the image build context.

## Discovery: seed, inspect and run

Supply a UTF-8 keyword file with one query per line in `private/input/queries.txt`.
For an offline setup check, two illustrative seeds are sufficient:

```sh
printf '%s\n' weather travel > private/input/queries.txt
docker compose run --rm discovery init --queries /input/queries.txt
docker compose run --rm discovery status
```

These commands create and inspect `/data/discovery/data.db`; they do not contact
a phone. Repeating `init` adds previously unseen seeds without resetting the
database. The example words are not the historical paper's keyword corpus.
Use your intended query file before starting an actual collection.

Prepare a separate discovery phone with WeChat's Mini Program search page open
and a matching Frida server running. The following command contacts that phone:

```sh
docker compose run --rm discovery run --serial YOUR_DISCOVERY_DEVICE --max-queries 1
```

`--max-queries 1` is the default; use `--max-queries 0` explicitly for an unbounded
queue. Each completed query expands the queue with observed app names and
Chinese 2–4-grams. Results are deduplicated by AppID in SQLite. The adapter checks
the returned query, offset and request ID before accepting a response. It
exports an operational input catalogue to `/data/discovery/catalog.csv`; this
contains discovery fields and is separate from the public package-release CSV.
It does not automatically merge or publish a release or launch another role.

Page cursors, cookies and raw responses stay in the private database. A run is
bound to its first selected serial. Only one writer can use that database. The
default cap is five pages per query; queries with more pages are marked `capped`
and retain their next-page cursor. To continue them deliberately:

```sh
docker compose run --rm discovery resume --include-capped
docker compose run --rm discovery run --serial YOUR_DISCOVERY_DEVICE --max-pages 10 --max-queries 1
```

Ten consecutive genuine empty queries pause discovery and requeue those queries.
Transport failures, unexpected response schemas, stale callbacks and pagination
errors stop and preserve the current cursor; they are not treated as empty
searches. Explicit rate-limit/verification messages in returned search responses
also pause the run. This pinned bridge does not scrape visible UI warnings.
After inspecting the phone and addressing the cause, clear the pause with
`docker compose run --rm discovery resume`, then explicitly run again. There is
no automatic restart. Ctrl-C/SIGTERM stops after the current bounded request
and saves the checkpoint.

## Version acquisition: configure, initialize and run

Copy your version catalogue to `private/input/apps.csv`. It needs `appid`, `name`
and `previous_app_version` columns. Generate a configuration for the versioning
phone:

```sh
python3 docker/configure.py --serial YOUR_VERSION_DEVICE --csv private/input/apps.csv
docker compose run --rm version --config /config/version.json init
docker compose run --rm version --config /config/version.json status
```

The generator checks the columns, counts rows, points the paths at the mounts,
and selects Tesseract for local collection. It refuses to overwrite an existing
configuration. It uses the actual CSV basename under `/input`; mount that same
file. Once initialized, keep the same input, configuration and data
paths to preserve checkpoint identity. Do not attach a native production
checkpoint to a container with different path names.

`init` and `status` manage local state. The next commands contact the selected
phone and, for `run`, start acquisition:

```sh
docker compose run --rm version --config /config/version.json doctor
docker compose run --rm version --config /config/version.json run --max-items 1
```

After inspecting that first result, omit `--max-items 1` for the complete queue.
Use `resume` only when intentionally resuming a paused run. The ten-consecutive-
empty-search stop and persistent checkpoints remain active inside Docker.

## Initial acquisition

Put its catalogue at `private/input/initial.csv` and
assign a different phone when versioning runs simultaneously:

```sh
docker compose run --rm collection \
  --csv /input/initial.csv --serial YOUR_INITIAL_DEVICE --frida frida \
  --output /data/initial/run-001 --limit 1
```

This starts one initial acquisition. Set an appropriate `--limit` and a new run
directory for a larger batch; consult the [initial collector](../scraper/README.md)
for its recovery and consolidation workflow. Avoid sharing a phone or writable
output directory between concurrent workers. The version collector handles
Ctrl-C as a stop request; allow it to finish saving state. The initial collector
preserves completed rows and any copied files, but an interrupted current
attempt may lack a manifest row. Inspect that directory and use its documented
fresh-run/recovery workflow before continuing.

## OCR and platform limits

The native macOS path uses Apple Vision. Linux containers instead run
`version_scraper/tools/ocr_tesseract.py`, which emits the same
`{"width": ..., "height": ..., "nodes": [...]}` JSON format with pixel bounds,
line text and confidence values. It reads the original screenshot and a
grayscale contrast/inversion pass so dim text on dark WeChat screens remains
readable. Pixel geometry and original screenshot bytes are preserved; the
collector merges overlapping readings and keeps its 0.8 confidence threshold.
Pillow is required by the Tesseract adapter and is installed from
`docker/requirements.txt`; native Apple Vision does not use it. English and
Simplified Chinese are installed;
`--language` and `--psm` can be added to `ocr_command` for another layout. OCR
chooses UI controls only. Runtime AppID/version/package evidence still controls
acceptance. Tesseract confidence and recognition behavior differ from Apple
Vision, so inspect a one-item device run before scaling up.

The image does not contain a complete Android SDK, Java or an Appium server:
those remain host services. Direct USB forwarding on Docker Desktop is a
separate, optional mechanism and is not used by this setup; see [Docker's USB/IP
documentation](https://docs.docker.com/desktop/features/usbip/).

## Local storage and retention

There is no cloud storage client or publication command in the public image.
Compose mounts the project directory `collection_results/` at `/data` and puts
initial runs under `/data/initial`, version results under `/data/version`,
discovery state under `/data/discovery`, and automation state under
`/data/automation`. Original packages, metadata, checkpoints and reports remain
in that host folder after a container exits. Configuration and input mounts
remain read-only. Back up the entire local collection folder separately when
needed, preserving acquisition paths and evidence.

## Validation scope

Bounded Linux ARM64 container checks on macOS exercised offline help, persistent
synthetic queues, configuration generation, OCR, the pinned discovery adapter
and both collectors. Separate live checks used rooted Android 16 phones:
discovery and version acquisition with WeChat 8.0.72, and initial acquisition
with WeChat 8.0.79. Initial acquisition retained partial coverage; version
acquisition achieved complete coverage against an isolated synthetic baseline.
These checks do not certify a continuous campaign or other host/device releases.

Run the offline smoke check above and a bounded acquisition on your intended
device before a larger run. Keep the phone awake and unlocked, its notification
shade closed, and WeChat visible. Unrecognized screens stop navigation; OCR
success alone does not verify the identity or completeness of acquired packages.

When native host tools must replay container-produced evidence, bind the output
directory at the **same absolute path** on host and container and use that path
in the configuration. This preserves absolute references inside hash-checked
receipts. Do not rewrite receipt paths after collection. The simpler `/data`
mapping is appropriate when all processing stays in the container.
