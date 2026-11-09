# Docker

The container uses the same `minizoo.py` launcher as native execution. Discovery,
initial collection and versioning are separate Compose services. Invoking a
service without arguments prints help; `docker compose up` does not start a run.
Run all commands below from the repository root.

## Build and test without a phone

```sh
docker compose build
docker run --rm --network none minizoo-scraper:public
docker run --rm --network none minizoo-scraper:public test
docker run --rm --network none minizoo-scraper:public test core
docker run --rm --network none minizoo-scraper:public python3 docker/smoke.py
```

No private config, phone or dataset is needed. The image supplies Python 3.12,
Node 26, the pinned Frida tools, Tesseract, Pillow and wxappUnpacker. The smoke
check also checks installed dependency versions, the actual OCR executable on a
synthetic image, all public commands and each offline suite. Dependency downloads
happen during image build, not during these tests.

Discovery's external MiniCrawler source is not bundled. To also exercise its
real parser and checkpoint integration on synthetic responses:

```sh
git submodule update --init discovery/vendor/MiniCrawler
docker run --rm --network none \
  --mount "type=bind,src=$PWD/discovery/vendor/MiniCrawler,dst=/opt/minicrawler,readonly" \
  minizoo-scraper:public python3 docker/smoke.py --discovery-source /opt/minicrawler
```

The adapter checks source hashes against the pinned revision. Initial collection,
versioning and offline unit tests do not require this mount. The decompiler's
upstream GPL license remains in `/opt/wxappUnpacker`; MiniCrawler has separate
[provenance and license limitations](PROVENANCE.md).

## Prepare host services for live work

Phones connect to the host. The container uses host networking for the host ADB
server and Frida's localhost tunnels. On Linux, use Docker Engine. On Docker
Desktop 4.34+, enable **Settings > Resources > Network > Enable host networking**
and apply it before acquisition. See [Docker host networking](https://docs.docker.com/engine/network/drivers/host/).

Prepare the rooted, authorized, logged-in phone with a compatible Frida server.
Initial collection needs [ADBKeyboard](../scraper/README.md#installation).
Versioning needs the host JDK, Android SDK and Appium from the
[version guide](../version_scraper/README.md#installation):

```sh
adb start-server
npm ci --prefix version_scraper --no-audit --no-fund
./version_scraper/start_appium.sh
```

Keep Appium at `127.0.0.1:4723`. Reuse a correctly configured running server;
do not restart ADB or Appium under another collector. Appium is needed only for
version navigation. Keep phones awake and unlocked with WeChat visible; discovery
also needs its Mini Program search page open.

## Local files and mounts

```sh
mkdir -p private/config private/input collection_results
cp docker/env.example .env
```

If `.env` already exists, edit it instead of overwriting it. Set `LOCAL_UID` and
`LOCAL_GID` to `id -u` and `id -g`. Defaults use these mounts:

| Host path | Container path | Use |
| --- | --- | --- |
| `private/config/` | `/config` | Read-only private settings. |
| `private/input/` | `/input` | Read-only catalogues and seed files. |
| `collection_results/` | `/data` | Writable results and checkpoints. |
| `discovery/vendor/MiniCrawler/` | `/opt/minicrawler` | Read-only adapter, discovery only. |

All data and private settings are excluded from Git and the Docker build context.
Compose refuses missing bind-mount sources. The container runs without privileges,
limited to two CPUs and 2 GiB RAM per service. Removing a container preserves the
host output folder. Simultaneous roles need separate phones and output paths.

## Discovery independently

Put one intended keyword per line in `private/input/queries.txt`, initialize the
adapter above, then seed the queue without contacting a phone:

```sh
docker compose run --rm discovery init --queries /input/queries.txt
docker compose run --rm discovery status
```

To contact the prepared discovery phone for one query:

```sh
docker compose run --rm discovery run --serial DEVICE_A --max-queries 1
```

Output is `/data/discovery/data.db` and `catalog.csv`. The
[discovery guide](../discovery/README.md#results-restart-and-limits) explains
caps, checkpoints and explicit resume. No other service is launched.

## Initial collection independently

Put your catalogue in `private/input/initial.csv`; it needs a `name` column:

```sh
docker compose run --rm collection \
  --serial DEVICE_B --csv /input/initial.csv --limit 1 \
  --output /data/initial/trial-001
```

Use a fresh output for each new run. For an interrupted run, stop its writer and
use `collection --recover-output /data/initial/trial-001`. This recovery reads
saved evidence and does not contact the phone. See the [initial guide](../scraper/README.md).

## Version collection independently

Put a CSV with `appid,name,previous_app_version` in `private/input/apps.csv`:

```sh
python3 minizoo.py configure-version --container --serial DEVICE_C --csv private/input/apps.csv
docker compose run --rm version --config /config/version.json init
docker compose run --rm version --config /config/version.json status
```

Generation and initialization preserve the input and do not contact a phone.
The `--container` option selects `/input`, `/data` and Tesseract. Keep that config
separate from native collection settings. To check the prepared phone and run
one acquisition:

```sh
docker compose run --rm version --config /config/version.json doctor
docker compose run --rm version --config /config/version.json run --max-items 1
```

Results stay under `/data/version/`. Version comparisons, pause/resume and
baseline checks are described in the [version guide](../version_scraper/README.md).
Press Ctrl-C once and wait for saved state. Unrecognized screens stop navigation.

## Inspect or reconstruct saved data independently

The optional tool service exposes the same offline processing commands:

```sh
docker compose run --rm collector inspect /data/path/package.wxapkg
docker compose run --rm collector decompile --package /data/path/package.wxapkg --output /data/reconstruction-trial
docker compose run --rm collector metadata --help
```

Choose actual saved paths and a fresh reconstruction output. No collection
service is started. Reconstruction never overwrites original packages.

The simple `/data` mapping assumes all evidence replay happens in the container.
To replay container-produced receipts with native tools, bind outputs at the
**same absolute path** on host and container and use that path in the config
before acquisition. Preserve paths and hash-bound receipts when resuming; do not
rewrite them after the fact. See [testing scope](TESTING.md) for what offline and
bounded device checks establish.
