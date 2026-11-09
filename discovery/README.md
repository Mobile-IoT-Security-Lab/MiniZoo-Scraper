# Discover mini apps

Search WeChat by keyword and save AppIDs and available public metadata.
**Input:** one search term per line. **Output:** `collection_results/discovery/catalog.csv`.

## Quick start with Docker

First complete [installation](../README.md#1-install-and-try-it) and
[phone setup](../docs/DOCKER.md#connect-phones). Run commands from the repository
root in the terminal where you set `DEVICE_SERIAL`.

### 1. Install the discovery adapter once

```sh
mkdir -p private/dependencies
docker compose run --rm -v "$PWD/private/dependencies:/dependencies" collector \
  python3 tools/fetch_discovery_source.py --destination /dependencies/MiniCrawler
```

Expect `"status": "installed"` or `"status": "unchanged"`. This downloads three
files and verifies their pinned checksums. Only discovery needs this adapter.

### 2. Add your search terms

Create `private/input/queries.txt`, for example:

```text
weather
travel
```

Initialize the local queue:

```sh
docker compose run --rm discovery init --queries /input/queries.txt
```

### 3. Run one query

On the phone, open **WeChat's Mini Program search page**, then run:

```sh
docker compose run --rm discovery run --serial "$DEVICE_SERIAL" --max-queries 1
docker compose run --rm discovery status
```

Open **`collection_results/discovery/catalog.csv`** to see the discovered apps.
The status shows the number of apps and pending queries. A query may return no
apps; a paused or failed request needs attention before continuing.

Run the same command again to continue the queue. `--max-queries 5` processes
up to five queries; `--max-queries 0` explicitly requests an unbounded run.

## Try without a phone

After building the image:

```sh
docker run --rm --network none minizoo-scraper:public example discovery
```

Expect `PASSED: discovery`. The synthetic example checks catalogue updates,
metadata preservation and CSV export. See [example output](../docs/DOCKER.md#immediate-examples)
to keep its files. It does not perform a live search.

## Results, restart and limits

The queue is bound to its first phone and permits one writer. Keep the same
phone and database when continuing. Each query is capped at five pages by default.
To continue capped queries after inspecting the phone:

```sh
docker compose run --rm discovery resume --include-capped
docker compose run --rm discovery run --serial "$DEVICE_SERIAL" --max-pages 10 --max-queries 1
```

Ctrl-C saves the current request. Ten consecutive genuine empty queries or a
rate-limit/verification response pause discovery. Other request errors retain
the cursor and stop the run. Nothing restarts automatically.

`catalog.csv` can be used as initial-collection input. The database also retains
responses and cursors. Missing metadata preserves earlier values; search terms
are never used as categories. The adapter is separately maintained and is not
bundled; see [provenance](../docs/PROVENANCE.md).

## Native usage

Follow the [native installation](../docs/NATIVE.md), then use the same query file:

```sh
python3 tools/fetch_discovery_source.py
python3 minizoo.py discovery init --queries private/input/queries.txt
python3 minizoo.py discovery run --serial "$DEVICE_SERIAL" --max-queries 1
python3 minizoo.py discovery status
```

Offline example: `python3 minizoo.py example discovery`.
Options: `python3 minizoo.py discovery --help`.
For a separate queue, pass `--database PATH` before the subcommand on every call.
