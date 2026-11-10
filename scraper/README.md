# Collect your first mini app

Open a named Mini Program in WeChat and save its packages and metadata locally.
**Input:** a CSV with a `name` column. **Output:** `collection_results/initial/`.

## Quick start with Docker

First complete [installation](../README.md#1-install) and
[phone setup](../docs/DOCKER.md#connect-phones). Run commands from the repository
root in the terminal where you set `DEVICE_SERIAL`.

### 1. Add the app you want to collect

Create `private/input/initial.csv` with this structure. Replace the second line
with the **exact name of a real Mini Program you can find in WeChat**:

```csv
name
YOUR_MINI_APP_NAME
```

Add more names on separate lines when needed. A discovery catalogue is also
accepted. Optional columns include `appid`, `description`, `developer`,
`icon_url` and `app_version`.

### 2. Prepare the keyboard once

```sh
docker compose run --rm -e ANDROID_SERIAL="$DEVICE_SERIAL" collector bash scraper/setup_adbkeyboard.sh
```

This installs and enables ADBKeyboard on the selected phone. If Android blocks
it, ASCII-only names can use `--allow-no-adbkeyboard` in the next command.
Chinese names require a compatible keyboard.

### 3. Collect one app

Keep the phone unlocked with WeChat open:

```sh
docker compose run --rm collection \
  --serial "$DEVICE_SERIAL" --csv /input/initial.csv --limit 1
```

### 4. Open the result

Open the newest **`collection_results/initial/csv_batch_*/`** folder:

| File or folder | What to look for |
| --- | --- |
| `manifest.csv` | The app attempted and its acquisition, metadata and reconstruction outcomes. |
| `attempts/` | Saved packages and evidence for each attempt. |
| `package_metadata.csv` | Metadata extracted from the saved packages. |

If your input comes from discovery, keep its `appid` and `raw_json` columns.
The collector can then recover a missing category as **Mini game** from WeChat's
explicit game flag, only when the app that opened has that same AppID. No extra
command is needed. A name-only CSV has no search evidence for this fallback.

A finished command can contain a failed or partial acquisition: read the outcome
in `manifest.csv`. Missing metadata remains explicit. If another app opens,
its observed identity is recorded separately from the requested app.

To collect more rows, change `--limit 1` to the desired count. Each run creates
a new output folder and starts from the first CSV row; `--start N` skips N rows.
Press Ctrl-C once and wait for saved state before disconnecting the phone.

## Options and recovery

| Option | When to use it |
| --- | --- |
| `--mini-programs-row-y 0.435` | The Mini Programs row is at a different height in the Discover menu; default is `0.515`. |
| `--allow-no-adbkeyboard` | ADBKeyboard is unavailable and search names contain only ASCII characters. |
| `--output /data/initial/my-run` | Choose a new result directory explicitly. Existing manifests are refused. |
| `--prepare-only --open-method search` | Prepare input and a manifest without a phone or network. |
| `--open-method auto` or `qr` | Try or require available Mini Program QR/code assets. |
| `--name-column COLUMN` | The input uses a column other than `name`. |

For an interrupted run, stop its writer and recover the saved records. Replace
`YOUR_RUN` with the actual folder name:

```sh
docker compose run --rm collection --recover-output /data/initial/YOUR_RUN
```

Recovery reads existing evidence, preserves original packages and rebuilds the
CSV records. Keep the full acquisition folder at its original path. Coverage
and reconstruction outcomes are explained in the [metadata guide](README_package_metadata.md).
For optional scheduling and merging, see [batch collection](automation/README.md).

## Native usage

Follow the [native installation](../docs/NATIVE.md), including reconstruction
tools. With the phone prepared and the same CSV input:

```sh
ANDROID_SERIAL="$DEVICE_SERIAL" bash scraper/setup_adbkeyboard.sh
python3 minizoo.py collection \
  --serial "$DEVICE_SERIAL" --csv private/input/initial.csv --limit 1
```

All options: `python3 minizoo.py collection --help`.
