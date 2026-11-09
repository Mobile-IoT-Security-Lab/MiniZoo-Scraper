# MiniZoo shared collection core

Both the initial collector and the version collector import this package. Their
queues and UI navigation remain separate; package selection, runtime evidence,
coverage checks, static extraction and decompilation use the same implementation.
This is a support library for the paper's Mini App Crawler and Mini App Version
Crawler, not a separate discovery or scheduling service.

## Run each processing stage independently

From the repository root:

```sh
python3 minizoo.py inspect /absolute/package.wxapkg
python3 minizoo.py decompile --package /absolute/package.wxapkg --output collection_results/reconstruction-trial
python3 minizoo.py metadata --help
```

Inspection uses only Python and never executes package code. Reconstruction
requires Node.js 26+ and the pinned wxappUnpacker installation from the
[main guide](../README.md#install-only-what-you-use). It preserves original bytes
and refuses a populated output directory. Metadata extraction can run on an
existing verified reconstruction without opening WeChat. Each stage reports its
own outcome; it does not start discovery, acquisition or scheduling.

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
   collector starts its next search. Scan verified reconstructed root manifests
   for explicitly declared app metadata. Static AppID references are not host IDs.

`coverage=complete` requires agreement between the original main configuration,
the runtime inventory and all retrieved components. An unavailable or truncated
inventory is partial coverage. Original files and historical proofs are retained.

## Outcomes and outputs

Each acquisition contains `evidence.json`, hashed before/after observations,
`package_metadata.json`, `package_metadata.csv`, and `processing.json`. Metadata
sidecars retain parser identity and byte-level evidence. Reconstruction logs and
outputs remain in the acquisition directory.

The post-decompilation metadata report has its own status and provenance. It is
linked from `processing.json`; a missing declaration or failed extraction does
not change the acquisition, coverage or reconstruction result.

Package acquisition, coverage, metadata parsing and decompilation are separate
outcomes. Decompilation failure keeps valid packages available. The version
collector retains its conservative baseline-update checks: a newer comparable
runtime version, verified identity and artifacts, and complete coverage.

## Recovering missing metadata

During acquisition, both collectors read public attributes for the AppID
identified from the active runtime. Besides name, description and icon, this
includes the service taxonomy in `dynamicInfo.NewCategories` and the developer
in `registerSource.RegisterBody`. A bounded, read-only cache lookup supplements
loaded `WxaAttributes` objects. Raw fields and their identity/provenance are
retained and checked when the evidence is replayed. Search keywords never
supply an app category, and a component's version never supplies its host's
release version.

Metadata is saved with the acquired bytes before reconstruction. Its private
status distinguishes available, missing, invalid and conflicting fields.
Unavailable attributes do not discard packages or stop the catalogue from
retaining the app. A later acquisition can obtain newly available attributes;
an absent later field must not erase a previously verified value. These checks
cannot create a public description that the platform does not supply or
reconstruct an acquisition date that was never recorded.

Static extraction reads package members directly. After successful reconstruction,
`minizoo_collection.decompiled_metadata` also reads a bounded set of root JSON
manifests from the verified main package. It never executes the reconstructed
JavaScript. Explicit app declarations can supply `name`, `description`,
`category`, `developer` and `icon_url` when the declaration's AppID agrees with
the acquisition. Generic project names, page titles, npm authors, SDK versions,
plugins and subpackages are excluded.

The report binds declarations to the original package SHA-256, reconstruction
receipt and source-file hashes. Catalogue consolidation replays that proof and
the runtime identity evidence, fills only absent fields after other supported
sources, and retains conflicts privately. App-declared values are a fallback,
not a claim that WeChat supplied them. Existing frozen acquisitions can be
inspected read-only; their original receipts and manifests must not be rewritten.

Reconstruction is not required to calculate hashes or read an available manifest.
Neither static reader can create absent metadata, recover an unrecorded collection
time or turn a generic version string into a verified release code.

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

To inspect an existing verified reconstruction without opening WeChat, run from
the repository root and choose a new output path:

```sh
python3 minizoo.py metadata extract \
  /absolute/acquisition/processing.json \
  --appid wx0123456789abcdef --output /absolute/recovery/decompiled-metadata.json
```

Replace the example AppID and paths with the acquisition's actual values. The
command requires the original package and reconstruction receipts at their saved
paths, refuses to overwrite a report, and returns its hash. Its `verify`
subcommand replays a report; use `--help` for arguments. This inspection alone
does not establish runtime ownership or modify a public catalogue.

The initial collector continues exporting its existing run-level CSVs for
compatibility. Its historical `opened_app_v1` receipts are replayed under their
original policy; old numeric package limits are never retroactively removed from
historical evidence. New invocations ignore the retired `--packages-per-app`
option so old launchers cannot truncate acquisitions.

## Installation

The release bundles this directory alongside `scraper/` and `version_scraper/`.
Their bootstrap modules discover this layout automatically. Direct scripts accept
`MINIZOO_CORE_PATH` for a separate installation; the common launcher selects
this checkout’s core. No process, device connection, or scheduler starts on import.

The default reconstruction command uses the existing pinned wxappUnpacker and
Node.js 26 permission mode. The command may be configured as an argument list;
there is no shell interpolation. The Android cache is never deleted.

Acquisition primitives were extracted from the MiniZoo collectors on 3 November
2026. See [provenance](../docs/PROVENANCE.md) for dependency licenses. All processing
commands are also available through the `collector` service in the
[Docker guide](../docs/DOCKER.md#inspect-or-reconstruct-saved-data-independently).
