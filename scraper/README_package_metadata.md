# Metadata extracted from packages

`wechat_csv_scraper.py` automatically calls
`wxapkg_metadata_csv.append_collected_package_metadata()` after acquisition and
hashing. Export is synchronous: CSV rows and evidence are written before the next
app search. The exporter reads bytes through `static_wxapkg_reader.py` without
executing package code, making network requests, or operating the phone.

## Query context and static observations

`package_metadata.csv` keeps the following information separate:

- `queried_*` and `query_record_json`: the catalogue record used to search.
- `extracted_*_json`: information found in the package bytes.
- SHA-256, size, local/remote path, source run and parser status.
- Evidence sidecar path and parser hash/version.

The base `appid`, `name`, `developer` and `app_version` columns are populated only
with verified runtime evidence for the opened app. Without that evidence they
remain empty and `package_attribution=UNASSIGNED`. The `extracted_*` fields always
represent static observations. `certified_release=false` distinguishes content
attribution from certification of a release.

| Columns | Meaning |
| --- | --- |
| `extracted_host_appids_json` | AppIDs declared in recognized root manifests; these are not signed attestations. |
| `extracted_candidate_appids_json`, `extracted_reference_appids_json` | References that may identify SDKs, plugins or other apps. |
| `extracted_names_json`, `extracted_developers_json` | Extracted names, titles and developer declarations. |
| `extracted_app_versions_json` | Declared app versions without runtime verification. |
| `extracted_sdk_versions_json`, `extracted_generic_versions_json` | SDK versions and generic strings, separate from app versions. |
| `extracted_domains_json`, `extracted_plugin_ids_json` | Referenced domains and plugins. |
| `all_extracted_fields_json` | All extracted groups, including fields not promoted to dedicated columns. |

Sidecars under `package_metadata_evidence/<sha256>/` preserve member, offset,
snippet and provenance. An AppID in a filename, folder or query is not identity
evidence. Encrypted, malformed or oversized inputs receive explicit statuses;
missing identity is never filled from the searched name.

## Preservation and compatibility

The static parser derives from the earlier metadata-recovery audit. It does not
import that audit's inventory, database or CLI. The reference source SHA-256 is
`d795bd3fac9d6e4a6ed3e9b79f173a40af7a699a067ae85f5070c4dfe3e364e4`.
See [provenance](../docs/PROVENANCE.md) for its place in the release.

Existing manifests and history are not rewritten. New
`collection_schema=opened_app_v1` rows separate the query from the observed app.
`package_history.csv` and `package_metadata.csv` inherit identity only after
runtime evidence replay and byte verification. Unverified acquisitions retain
empty identity/version fields and cannot create new canonical associations.

Replaying the same SHA/provenance pair does not duplicate a row; acquisitions of
the same content with different provenance remain distinct. Symlinks, mismatched
hashes and incompatible CSV schemas are rejected. The exporter assumes one
writer per run; it is not a transactional database.

## API

```python
from wxapkg_metadata_csv import append_collected_package_metadata

written = append_collected_package_metadata(
    csv_path,
    local_paths,
    query_record,
    source_run=run_id,
    remote_sources=ordered_remote_paths,
)
```

Pass existing preserved files. Remote paths must align with the local path list.
When `query_record['sha256']` contains expected hashes separated by `;`, they are
checked before export.

From the repository root, inspect a package or test the modules independently:

```bash
python3 minizoo.py inspect /absolute/path/package.wxapkg
python3 minizoo.py test collection
python3 minizoo.py test core
```

The suite exercises search, runtime observation, acquisition, manifest creation,
extraction, merge and catalogue export with mocks and synthetic files. Offline
tests and replay of previously saved observations do not replace a fresh live
check of an updated runtime probe.

## Identifying the app that opened

`opened_app_probe.js` does not receive the searched AppID. It identifies one
foreground `AppBrandRuntime` and links its AppID, path, checksum and version
through `AppBrandSysConfig`/`WxaPkgWrappingInfo`. The collector copies the declared
package and calculates MD5 and SHA-256. A second observation confirms the same
runtime after copying. Compatible Frida CLI/server versions are required; select
the executable using `--frida` or `WECHAT_FRIDA`.

Available public fields come from the runtime and loaded `WxaAttributes` indexed
by the observed AppID. These are on-device records, not a fresh server response.
Absent or conflicting values remain unknown. `opened_app_evidence.json` preserves
the observations and their hashes; later stages repeat the checks before
attributing content.

When a search for A opens B, the canonical association is
`B/<sha256>.wxapkg`. A retains an `opened_different_app` attempt and remains due
for collection. AppID/SHA-256 deduplication preserves distinct supported
associations. Historical canonical files are not retroactively certified.

## Shared acquisition engine

`minizoo_core_bootstrap.py` discovers the bundled `../MiniZooCollectionCore/`.
Direct scripts support `MINIZOO_CORE_PATH`; the common launcher selects this checkout’s bundled core. Package acquisition,
evidence replay, static parsing and reconstruction are shared with the version
collector; initial navigation uses ADB and ADBKeyboard, while version navigation
uses Appium.

The core retrieves the main package and every available subpackage corroborated
by runtime checksums and inventory, without a package-count limit. Coverage is
complete only when the original configuration, runtime inventory and acquired
components agree. Otherwise coverage remains partial and missing components are
recorded explicitly.

Each acquisition writes `package_metadata.json`, `package_metadata.csv` and
`processing.json` before the next search. Run-level exports remain compatible.
Decompilation failure retains valid original packages. Historical acquisitions
keep their original evidence and validation policy.
