# Testing individual modules

Run from the repository root with Python 3.12. A single command runs all suites:

```sh
python3 minizoo.py test
```

Each suite runs in its own process and working directory. A failed suite returns
a nonzero overall exit code; the remaining suites still run so you see all
failures. Imports and offline tests do not start a device connection or collection.

| Command | Coverage |
| --- | --- |
| `python3 minizoo.py test discovery` | Synthetic search responses, persistent queues, page cursors, metadata retention, pauses and device binding. |
| `python3 minizoo.py test collection` | Initial acquisition, identity, durable metadata, recovery, isolation and optional batch consolidation. |
| `python3 minizoo.py test version` | Version comparison, baseline protection, pauses, retry/resume, OCR/navigation contracts and local state. |
| `python3 minizoo.py test core` | Runtime evidence, original-byte preservation, coverage, metadata provenance and reconstruction contracts. |
| `python3 minizoo.py test cli` | Independent startup from another directory, private configuration, input preservation and failure reporting. |

The Python-only checks need no pip install. Node serialization tests run when
Node is available. OCR and upstream reconstruction tests explicitly skip if
their dependencies are absent. GitHub Actions tests each module on Linux and
macOS with Python 3.12 and Node 26; it does not connect phones or upload data.

## Optional adapter and reconstruction integration

Install only the selected dependency. These checks remain offline and use
synthetic inputs, but exercise the pinned upstream implementation:

```sh
git submodule update --init discovery/vendor/MiniCrawler
python3 minizoo.py test discovery --integration

git submodule update --init version_scraper/vendor/wxappUnpacker
npm ci --prefix version_scraper/vendor/wxappUnpacker --ignore-scripts --no-audit --no-fund
python3 minizoo.py test core --integration
```

The integration option fails clearly when prerequisites are missing. Discovery
verifies the adapter's source hashes. Core checks Node 26+ and the pinned Git
revision before testing reconstruction. `MINICRAWLER_SOURCE` and `WXAPP_TOOL_ROOT`
can point to existing installations of those exact revisions. See
[provenance](PROVENANCE.md) for license scope.

For packaged dependencies and actual OCR, follow the offline container smoke
check in [Docker](DOCKER.md#build-and-test-without-a-phone).

## Bounded checks on real phones

Offline success does not certify a new WeChat release or a complete campaign.
After preparing an idle authorized phone and the relevant module, use a new
trial output and the smallest explicit limit:

```sh
python3 minizoo.py discovery run --serial DEVICE_A --max-queries 1
python3 minizoo.py collection --serial DEVICE_B --csv private/input/initial.csv --limit 1 --output collection_results/initial/trial-001
python3 minizoo.py version --config private/config/version.json run --max-items 1
```

Run only the module you intend to test; these are independent alternatives,
not a script to execute automatically. Discovery needs an initialized queue;
versioning needs its private config, initialization and Appium. A phone must
not belong to two simultaneous collectors. Check captured identity, package
hashes, coverage and reconstruction status before increasing limits. A partial
capture is useful preserved data, not a verified complete release.

The previous public release had bounded Android 16 tests with WeChat 8.0.72
(discovery/versioning) and 8.0.79 (initial collection). Those results do not
establish that every future modification has been tested live. The modular
refactor is checked with the independent offline suites and container smoke.
