# Docker setup and troubleshooting

Start with [installation](../README.md#1-install). This page
connects that installation to a real phone. Commands use the default project
folders and run from the repository root on macOS or Linux.

## Connect phones

You need an already rooted Android phone with USB debugging enabled and WeChat
logged in. Keep it unlocked. Use one phone for each simultaneous collection role.

### 1. Install ADB and authorize USB

Install [Android Platform Tools](https://developer.android.com/tools/releases/platform-tools),
extract the archive and add its `platform-tools` folder to your terminal's `PATH`.
Check that `adb version` works. Connect the phone by USB and accept its debugging
authorization dialog:

```sh
adb devices -l
```

The phone must appear with status **`device`**. Copy its serial from the first
column, replace `REPLACE_WITH_SERIAL` below, and keep using this terminal:

```sh
export DEVICE_SERIAL="REPLACE_WITH_SERIAL"
adb -s "$DEVICE_SERIAL" shell su -c id
```

The last command must report **`uid=0`**. This guide assumes the phone is already
rooted; grant its root prompt if shown.

### 2. Prepare the local folders and Docker connection

```sh
mkdir -p private/config private/input collection_results
export LOCAL_UID="$(id -u)" LOCAL_GID="$(id -g)"
```

On Docker Desktop 4.34+, enable **Settings → Resources → Network → Enable host
networking**, then apply the change. Docker Engine on Linux supports this
network mode directly. See [Docker's instructions](https://docs.docker.com/engine/network/drivers/host/).

Check that the container sees the same phone:

```sh
docker compose run --rm collector adb -s "$DEVICE_SERIAL" get-state
```

**Expected result:** `device`. The host ADB server handles USB on both macOS and
Linux. Keep it running while collecting.

### 3. Check Frida

MiniZoo uses Frida **17.18.0**. A matching Frida Server must run as root on the phone:

```sh
docker compose run --rm collector frida-ps -D "$DEVICE_SERIAL"
```

**Expected result:** a process list. If this works, reuse the existing server.
Otherwise, follow the setup below.

<details>
<summary>Install Frida Server on a phone that does not have it</summary>

Check the phone's architecture:

```sh
adb -s "$DEVICE_SERIAL" shell getprop ro.product.cpu.abi
```

Download the matching Android `frida-server` from the
[17.18.0 release](https://github.com/frida/frida/releases/tag/17.18.0): for
`arm64-v8a`, choose `frida-server-17.18.0-android-arm64.xz`.
Extract it and save the resulting binary as `private/input/frida-server`.
With no existing server running on this phone, install and start it:

```sh
adb -s "$DEVICE_SERIAL" push private/input/frida-server /data/local/tmp/frida-server
adb -s "$DEVICE_SERIAL" shell "su -c 'chmod 755 /data/local/tmp/frida-server'"
adb -s "$DEVICE_SERIAL" shell "su -c '/data/local/tmp/frida-server >/dev/null 2>&1 &'"
```

Run the process-list check again. After a phone reboot, start the server again.
See the [Frida Android guide](https://frida.re/docs/android/) for other architectures
or device-specific installation issues.

</details>

### 4. Start the task you need

Keep WeChat open and follow **one** guide:

- [Collect a mini app by name](../scraper/README.md#quick-start-with-docker).
- [Discover AppIDs by keyword](../discovery/README.md#quick-start-with-docker).
- [Revisit known apps for new versions](../version_scraper/README.md#quick-start-with-docker).

In a new terminal, repeat the three `export` assignments above. The folders,
installed keyboard and saved collection state remain available.

## Common problems

| What you see | What to do |
| --- | --- |
| `adb: command not found` | Add the extracted Platform Tools folder to `PATH`. |
| Phone is `unauthorized` or `offline` | Unlock it, accept USB debugging, check the cable and run `adb devices -l` again. |
| `su` fails or does not show `uid=0` | Check root access on the phone. |
| Host sees the phone but Docker does not | Enable Docker Desktop host networking and keep the host ADB server running. |
| Frida cannot list processes | Check the selected serial and the server version; the server must be running as root. |
| A bind-mount source is missing | Run the folder-creation command; discovery also needs its adapter-install step. |
| Permission denied writing results | Set `LOCAL_UID` and `LOCAL_GID` as above and use folders owned by your user. |
| Keyboard installation is blocked | For ASCII names, add `--allow-no-adbkeyboard`; Chinese names need a compatible keyboard. |
| A collector pauses after empty searches or a verification screen | Inspect the phone and respect the saved waiting period before using `resume`. |

## Appium configuration

Only version collection needs Appium. Its guide includes build/start commands.
If port 4723 is already used, either reuse a suitable existing server or choose a
free port before starting this project's Appium container:

```sh
export APPIUM_PORT=14723
docker compose up -d --wait appium
```

Also pass `--appium-url http://127.0.0.1:14723` when generating the version
configuration. For existing configuration, update `appium_url` to match.
Use `docker compose logs --tail 30 appium` to inspect startup errors.
Stop this project's Appium after collection with `docker compose stop appium`.

## Folder mapping and custom settings

The default commands use these paths:

| In the project | In a container | Access |
| --- | --- | --- |
| `private/input/` | `/input` | Read-only input files. |
| `private/config/` | `/config` | Read-only configuration. |
| `collection_results/` | `/data` | Writable results. |
| `private/dependencies/MiniCrawler/` | `/opt/minicrawler` | Read-only discovery adapter. |

For custom locations, copy `docker/env.example` to `.env` only if `.env` does not
already exist, then edit it. Use writable locations and keep existing outputs at
their original paths. These settings and collected data are excluded from Git
and Docker builds. No `.env` is needed for the default setup above.

The shared image contains Python 3.12, Node 26, Frida, Tesseract and the pinned
reconstruction tool. Appium's separate image includes its driver, JDK and Android
SDK. Its build downloads Google's SDK and accepts its licenses. The collector
builds for the host architecture; Appium uses `linux/amd64` for Google's SDK,
including through emulation on Apple Silicon. See [provenance](PROVENANCE.md).

## Inspect or reconstruct saved data independently

Follow the [saved-package guide](../MiniZooCollectionCore/README.md). Keep the
original output tree: container receipts refer to `/data/...`. For native replay,
use identical absolute paths on host and container from the start of acquisition.
Do not rewrite saved paths or evidence afterward.

## Linux USB inside Docker

This optional setup replaces the host ADB server. It is for Docker Engine on
Linux; the standard host-ADB setup above is the recommended first path.

Finish any active collection before switching USB ownership. Only one ADB
server can own the phones and port 5037:

```sh
mkdir -p private/adb
adb kill-server
docker compose -f compose.yaml -f compose.linux-usb.yaml up -d --wait adb
docker compose run --rm collector adb devices -l
```

Accept the phone's authorization prompt. The service keeps keys under
`private/adb` and uses Linux USB device access. When all collectors finish:

```sh
docker compose -f compose.yaml -f compose.linux-usb.yaml stop adb
```
