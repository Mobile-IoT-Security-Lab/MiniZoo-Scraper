# MiniZoo shared collection core

Both the initial collector and the version collector import this package. Their
queues and UI navigation remain separate; package selection, runtime evidence,
coverage checks, static extraction and decompilation use the same implementation.
This is a support library for the paper's Mini App Crawler and Mini App Version
Crawler, not a separate discovery or scheduling service.

## Acquisition policy

1. Observe the uniquely focused Mini Program and identify its actual AppID and
   runtime version. A version-revisit target must match this observed AppID.
2. Copy the runtime-bound main package. Read its original configuration and the
   runtime module inventory; match available subpackages by runtime MD5.
3. Retrieve every corroborated available component. There is no package-count
   limit. Missing components are recorded and do not discard the valid main.
4. Observe the runtime again. Reject attribution if identity, version or the
   declared module inventory changed. Save both observations and their hashes.
5. Extract static metadata and attempt decompilation synchronously, before the
   collector starts its next search. Static AppID references are not host IDs.

`coverage=complete` requires agreement between the original main configuration,
the runtime inventory and all retrieved components. An unavailable or truncated
inventory is partial coverage. Original files and historical proofs are retained.

## Outcomes and outputs

Each acquisition contains `evidence.json`, hashed before/after observations,
`package_metadata.json`, `package_metadata.csv`, and `processing.json`. Metadata
sidecars retain parser identity and byte-level evidence. Reconstruction logs and
outputs remain in the acquisition directory.

Package acquisition, coverage, metadata parsing and decompilation are separate
outcomes. Decompilation failure keeps valid packages available. The version
collector retains its conservative baseline-update checks: a newer comparable
runtime version, verified identity and artifacts, and complete coverage.

## Recovering missing metadata

Static extraction reads package members directly. Full JavaScript reconstruction
can help inspect application behavior, but is not needed to calculate hashes or
read an available manifest. Neither operation creates metadata that was absent
from the captured package.

| Public field | Recoverable evidence and limits |
| --- | --- |
| `sha256`, `size` | Calculated directly from the original package bytes. |
| `appid` | A root-manifest declaration is useful evidence; SDK, plugin and other referenced AppIDs do not identify the package owner. Attribution must pass the applicable runtime or historical-evidence checks. |
| `name`, `developer` | Declared values may be present. Screen titles, sample configuration and UI dictionary labels are candidates, not automatically verified app metadata. |
| `version_code` | An app-specific declaration may support a version when corroborated by acquisition evidence. SDK versions, build labels and default values such as `1.0.0` are not verified release codes. |
| `description`, `category`, `icon_url` | May be absent or unrelated to public app metadata. Runtime/search metadata tied to the observed AppID is normally needed. A local icon asset does not supply its public URL. |
| `collection_time` | Requires an acquisition record or equivalent historical evidence. Internal build times, file modification times and decompilation time are not collection times. |

The static reader already records extracted candidates, their package member and
offset, and source hashes. Recovery must retain that provenance and resolve
conflicts before changing a public catalogue. Unsupported public values remain
`null`; the presence of a string alone does not justify filling a missing cell.
See the [static metadata format](../scraper/README_package_metadata.md) for the separate candidate and verification outputs.

The initial collector continues exporting its existing run-level CSVs for
compatibility. Its historical `opened_app_v1` receipts are replayed under their
original policy; old numeric package limits are never retroactively removed from
historical evidence. New invocations ignore the retired `--packages-per-app`
option so old launchers cannot truncate acquisitions.

## Installation and tests

The release bundles this directory alongside `scraper/` and `version_scraper/`.
Their bootstrap modules discover this layout automatically. Set
`MINIZOO_CORE_PATH` to use a separate installation. No process, device connection, or scheduler starts on import.

The default reconstruction command uses the existing pinned wxappUnpacker and
Node.js 26 permission mode. The command may be configured as an argument list;
there is no shell interpolation. The Android cache is never deleted.

The acquisition primitives and metadata reader were extracted from the existing
MiniZoo collectors on 3 November 2026. Run regression tests through the two
collector suites documented in the [main README](../README.md#evidence-and-validation).
See [provenance](../docs/PROVENANCE.md) for source origins and license scope.
