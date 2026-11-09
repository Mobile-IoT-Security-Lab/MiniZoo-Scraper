# Docker

Discovery, initial collection, version collection and saved-package processing
use one image and start independently. Appium has a separate image containing
its Android driver, JDK and SDK. On Linux, an optional ADB service handles USB;
on macOS, only the ADB USB server remains on the host. WeChat and Frida's server
run on the authorized phones. All collected results stay on your local disk.
Run the following commands from the repository root.

## Build

```sh
docker compose build collection appium
docker run --rm --network none minizoo-scraper:public --help
```

Build only `collection` if you do not use version navigation. Discovery, collection,
versioning and processing share `minizoo-scraper:public`. It contains Python 3.12,
Node 26, pinned Frida tools, Tesseract with English and Simplified Chinese,
Pillow and the pinned wxappUnpacker. The Appium image installs the versions locked
in `version_scraper/package-lock.json`, JDK 21, Android API 36 and Build Tools
36.0.0. Its build downloads Google's SDK and accepts the SDK licenses.

The collector image builds for the host architecture. The Appium image uses
`linux/amd64` for Google's Linux SDK binaries; Docker Desktop on Apple Silicon
runs that image through x86-64 emulation. No emulator or Android Studio is needed.

The optional MiniCrawler adapter remains an external read-only mount. Install it
only for discovery:

```sh
python3 tools/fetch_discovery_source.py
```

Its source hashes are checked against the pinned revision. Other modules do not
require it. See [provenance](PROVENANCE.md) for dependency licenses and notices.

## Immediate examples

After building `collection`, run the checked examples without phones, settings
or network access:

```sh
docker run --rm --network none minizoo-scraper:public example all
```

Replace `all` with `discovery`, `collection`, `version` or `core` to run one.
Each prints `PASSED` or `FAILED`, checks its synthetic outputs and returns a
nonzero exit code if a check fails. See the [example table](../README.md#immediate-examples)
for the covered stages. Live acquisition requires the setup below.

The command above stores temporary results inside the container. To keep the
artifacts and `result.json` inside the project:

```sh
mkdir -p collection_results
docker run --rm --network none --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$(pwd)/collection_results,dst=/data" \
  minizoo-scraper:public example all
```

Results appear in a new `collection_results/examples/all-*/` folder. These
examples do not need the live discovery adapter, Appium, Frida or USB access.

## Local files and configuration

```sh
mkdir -p private/config private/input collection_results
cp docker/env.example .env
```

Edit an existing `.env` instead of overwriting it. Set `LOCAL_UID` and `LOCAL_GID`
to the values printed by `id -u` and `id -g`. These control ownership of result
files. Defaults keep all inputs and results inside the project:

| Host path | Container path | Access |
| --- | --- | --- |
| `private/config/` | `/config` | Read-only settings. |
| `private/input/` | `/input` | Read-only catalogues and seed files. |
| `collection_results/` | `/data` | Writable results and checkpoints. |
| `private/dependencies/MiniCrawler/` | `/opt/minicrawler` | Read-only, discovery only. |
| `private/adb/` | `/root/.android` | Authorization keys, optional Linux ADB service only. |

Private settings, keys and collected data are excluded from Git and the Docker
build context. Missing bind-mount sources cause an error. Collection services run
as the configured user with two CPUs and 2 GiB RAM each. Removing a container
preserves the host output folder. Appium uses its own unprivileged user and does
not mount your inputs or results. Services start only when explicitly requested.

## Connect phones

All services share host networking so ADB, Appium and Frida's loopback tunnels
can communicate. On Docker Desktop 4.34+, enable **Settings > Resources > Network
> Enable host networking** and apply it. On Linux, use Docker Engine. See
[Docker host networking](https://docs.docker.com/engine/network/drivers/host/).

Prepare authorized rooted Android phones with USB debugging, WeChat logged in
and a Frida server matching the client version in `requirements.txt`. Keep them
awake and unlocked. Each simultaneous role needs a separate phone and output.

### macOS or an existing host ADB server

Install Android Platform Tools on the host, connect and authorize the phones:

```sh
adb start-server
adb devices -l
docker compose run --rm collector adb devices -l
```

Docker Desktop does not provide direct USB passthrough; the native ADB server
handles that connection. The containers connect to its loopback port 5037.
Reuse an already running server; do not restart ADB underneath active collection.
This host ADB approach also works on Linux.

### Linux: USB inside Docker

The additional service uses Linux USB device access, runs as root for that access
and keeps authorization keys under `private/adb`. It does not use privileged mode.
First finish any existing acquisition. If you are switching from native ADB,
stop that idle server before starting the container; only one can own port 5037
and the connected phones.

```sh
mkdir -p private/adb
# Only when replacing an idle native ADB server:
adb kill-server
docker compose -f compose.yaml -f compose.linux-usb.yaml up -d --wait adb
docker compose run --rm collector adb devices -l
```

Accept the authorization dialog on each phone if prompted. The `/dev/bus/usb`
mount and USB device cgroup rule are specific to Linux Engine and are not a
macOS Docker Desktop configuration. Other module commands stay the same.

To stop this USB service after all collectors finish:

```sh
docker compose -f compose.yaml -f compose.linux-usb.yaml stop adb
```

### Initial-collector keyboard

Initial collection requires ADBKeyboard. Install it on the selected phone using
the image, with its APK saved under local results:

```sh
docker compose run --rm -e ANDROID_SERIAL=DEVICE_B collector bash scraper/setup_adbkeyboard.sh
```

The installer downloads the APK, verifies its pinned checksum, and installs and
enables it only on that phone. Android may block this legacy APK. The initial
collector supports `--allow-no-adbkeyboard` for ASCII-only names; the version
collector uses Appium instead. See the [initial guide](../scraper/README.md#installation).

### Start Appium for version navigation

```sh
docker compose up -d --wait appium
docker compose logs --tail 30 appium
```

The server listens on loopback port 4723. Starting it creates no phone session.
The container supplies Java, the Android SDK, Appium and UiAutomator2; none of
these need native installation. It uses the shared ADB server and is configured
not to kill it. Required tool versions are described in
[UiAutomator2 prerequisites](https://appium.github.io/appium-uiautomator2-driver/latest/getting-started/).

If another Appium server already owns port 4723, reuse it or set `APPIUM_PORT`
to an unused port in `.env`. When generating version configuration, pass the
matching `--appium-url http://127.0.0.1:PORT`. A native Appium setup remains
available in the [version guide](../version_scraper/README.md#installation).

## Discovery independently

Initialize the adapter above and put one intended keyword per line in
`private/input/queries.txt`. Seed the queue without contacting a phone:

```sh
docker compose run --rm discovery init --queries /input/queries.txt
docker compose run --rm discovery status
```

With the discovery phone showing WeChat's Mini Program search page, run one query:

```sh
docker compose run --rm discovery run --serial DEVICE_A --max-queries 1
```

Results are `/data/discovery/data.db` and `catalog.csv`. The [discovery
guide](../discovery/README.md#results-restart-and-limits) explains caps,
checkpoints and explicit resume. No other collection service is launched.

## Initial collection independently

Put a catalogue containing a `name` column in `private/input/initial.csv`:

```sh
docker compose run --rm collection \
  --serial DEVICE_B --csv /input/initial.csv --limit 1 \
  --output /data/initial/run-001
```

If the Discover menu places Mini Programs on another row, pass its vertical
screen fraction, for example `--mini-programs-row-y 0.435`.

Choose a fresh output for every new run. For an interrupted run, stop its writer
and use `collection --recover-output /data/initial/run-001`. Recovery reads saved
evidence and does not contact the phone. See the [initial guide](../scraper/README.md).

## Version collection independently

Put a CSV with `appid,name,previous_app_version` in `private/input/apps.csv`.
Generate a private config entirely through Docker; only this command temporarily
mounts the configuration folder for writing:

```sh
docker compose run --rm -v "$PWD/private/config:/generated" collector \
  configure-version --container --serial DEVICE_C --csv /input/apps.csv \
  --output /generated/version.json
docker compose run --rm version --config /config/version.json init
docker compose run --rm version --config /config/version.json status
```

Generation refuses to overwrite an existing config. Initialization preserves the
input CSV and does not contact a phone. Container configuration selects `/input`,
`/data`, Tesseract and the bundled reconstruction tool. Keep it separate from
native settings.

Start Appium as above. With the version phone showing WeChat, inspect prerequisites
and collect at most one queued app:

```sh
docker compose run --rm version --config /config/version.json doctor
docker compose run --rm version --config /config/version.json run --max-items 1
```

Results stay under `/data/version/`. See the [version guide](../version_scraper/README.md)
for comparisons, baseline checks and pause/resume. Press Ctrl-C once and wait
for saved state. Unknown screens stop navigation. When version collection has
finished, stop its Appium service with `docker compose stop appium`.

## Inspect or reconstruct saved data independently

The optional `collector` service also exposes offline processing:

```sh
docker compose run --rm collector inspect /data/path/package.wxapkg
docker compose run --rm collector decompile --package /data/path/package.wxapkg --output /data/reconstruction-001
docker compose run --rm collector metadata --help
```

Choose actual saved paths and a fresh reconstruction output. No phone connection
or collection service is needed. Reconstruction preserves original packages.
Optional multi-phone supervision and consolidation can also run in this image;
see [batch collection](../scraper/automation/README.md#run-in-docker).

The simple `/data` mapping assumes evidence replay happens in the container.
To replay container-produced receipts with native tools, bind outputs at the
**same absolute path** on host and container and configure that path before
acquisition. Preserve paths and hash-bound receipts when resuming; do not rewrite
them after the fact.
