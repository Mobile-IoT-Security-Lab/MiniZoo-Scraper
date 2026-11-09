# Mini App Discovery

Find WeChat AppIDs and public metadata from keywords. This module has its own
SQLite queue and page checkpoints. It does not start either package collector.
All commands below run from the repository root.

## Test independently

```sh
python3 minizoo.py discovery --help
python3 minizoo.py test discovery
```

The default tests exercise persistence, metadata, pagination, query expansion,
pauses, device binding and interrupted requests using a synthetic adapter.
They do not require a phone, Frida, MiniCrawler or a network connection.

## Install the live adapter

Use the shared Python environment in the [main guide](../README.md#install-only-what-you-use).
Only this module needs the separately maintained MiniCrawler source:

```sh
git submodule update --init discovery/vendor/MiniCrawler
python3 minizoo.py test discovery --integration
```

The adapter verifies the pinned source files before loading them. The upstream
checkout has no license file at the pinned revision and is not bundled into the
container; see [provenance](../docs/PROVENANCE.md). Set `MINICRAWLER_SOURCE` or
`--source` to use that exact revision from another directory.

## Initialize and inspect a queue

Create `private/input/queries.txt` with one intended search term per line. For
an illustrative local setup:

```sh
mkdir -p private/input
printf '%s\n' weather travel > private/input/queries.txt
python3 minizoo.py discovery init --queries private/input/queries.txt
python3 minizoo.py discovery status
```

`init` needs the adapter source but does not contact a phone. Repeating it adds
unseen seeds without resetting progress. The database defaults to
`collection_results/discovery/data.db`. Override it before the subcommand:

```sh
python3 minizoo.py discovery --database collection_results/discovery-trial/data.db init --queries private/input/queries.txt
```

Use the same `--database` for every later command on that queue.

## Run one query on a phone

The authorized rooted phone needs WeChat logged in, its Mini Program search
page open and a matching Frida server running. Select its serial from
`adb devices -l`:

```sh
python3 minizoo.py discovery run --serial DEVICE_A --max-queries 1
python3 minizoo.py discovery status
```

This contacts the selected phone. One completed query is the default limit;
`--max-queries 0` explicitly requests an unbounded queue. The default cap is five
pages per query. A queue is bound to its first serial and permits one writer.
Use different phones and databases for concurrent roles.

## Results, restart and limits

`collection_results/discovery/catalog.csv` is the operational catalogue for
initial collection; it is not a package-release CSV. The private database also
retains raw responses, query cookies, cursors and errors. Missing metadata does
not erase a previous observation. App names and Chinese 2-4-character substrings
expand the queue; keywords are never substituted for native categories.

Ctrl-C/SIGTERM saves the current bounded request. Run again with the same paths
to continue pending work. A capped query retains its cursor:

```sh
python3 minizoo.py discovery resume --include-capped
python3 minizoo.py discovery run --serial DEVICE_A --max-pages 10 --max-queries 1
```

Ten consecutive genuine empty queries or an explicit rate-limit/verification
response pause discovery. Transport, schema and callback errors preserve the
cursor and stop the run; they are not empty results. Inspect the phone before
explicitly using `resume`. Nothing restarts automatically.

For containers, use the independent `discovery` service in the [Docker guide](../docs/DOCKER.md).
