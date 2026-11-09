# Provenance and licensing

This repository combines a discovery runner, the initial collector, the version
collector, and their shared acquisition core. Operational datasets, device
configurations, credentials and checkpoints are not part of the software release.

## Project software

The original code and documentation contributed to this project are distributed
under the [MIT License](../LICENSE), using the standard
[Open Source Initiative license text](https://opensource.org/license/mit).
The license does not relicense independently maintained dependencies, WeChat,
Android applications, or collected Mini Program packages.

The first-party Docker discovery runner supplies persistent query scheduling
and process control. Its device search adapter is loaded at runtime from an
external MiniCrawler checkout mounted read-only. That checkout is not copied
into the container image and is not covered by the project's MIT grant.

The initial collector originated in
[WeChatMiniAppScraper](https://github.com/luca959/WeChatMiniAppScraper), with local
source revision `be2f02653a98a4ccd42270f3a175bf873365fa3b` plus subsequent working
changes. Its automation, runtime attribution, static metadata extraction and
tests are included. The earlier repository history was not imported into this
combined repository.

The version collector originated in the working project `WeChatScraperVersion`.
It includes Appium/ADB collection, Frida evidence readers, conservative version
comparison, package preservation, local reports and persistent acquisition checkpoints.

`MiniZooCollectionCore/` contains the common acquisition, runtime evidence,
coverage, static parsing and reconstruction implementation used by both package
collectors. Queue selection and UI navigation remain separate. Portability and
English-language changes in this release preserve the original package bytes,
schema identifiers and evidence semantics.

## Separately maintained dependencies

| Component | Pinned source or installation | License treatment |
| --- | --- | --- |
| MiniCrawler | Optional submodule `scraper/MiniCrawler`, revision `5cd8dff0ea25f8bd52f4898954b8bd8d76ef6958` from [luca959/MiniCrawler](https://github.com/luca959/MiniCrawler) | No LICENSE file is present at this revision. It is not covered by this repository's MIT grant and is not bundled into the release archive or container. |
| wxappUnpacker | Submodule `version_scraper/vendor/wxappUnpacker`, revision `694ca3874e67b89dd709f3d577c85a553bd45a0c` from [threecha/wxappUnpacker](https://github.com/threecha/wxappUnpacker) | Retains its upstream GNU GPL v3 license and notices. It is invoked as a separate tool; the MIT license does not replace its terms. |
| Appium and UiAutomator2 | Exact versions in `version_scraper/package-lock.json` | Keep their own package licenses and dependency notices. Installed `node_modules` are not committed. |
| Frida and frida-tools | Exact versions in `version_scraper/requirements.txt` | Keep their upstream package licenses. Device-side installation is separate. |
| ADBKeyboard | Installed separately using `scraper/setup_adbkeyboard.sh` | The APK is not part of this release; retain the upstream license and installation terms. |
| Android SDK platform tools and optional OCR dependencies | Installed by the user or the documented container build | Their licenses remain separate from the project MIT license. |

The Git submodule references preserve dependency provenance; initializing a
submodule does not change its license. The container documentation identifies
which components are installed and which remain external.

## Data and attribution

Author names are retained in `CITATION.cff`; contact addresses are omitted.

Only synthetic example CSVs and test fixtures are included. Original `.wxapkg`
files, device screenshots, acquisition logs, database files, private directory
IDs and credentials are excluded. Mini Program packages and platform content
retain their respective third-party rights; the software MIT license does not
grant rights over a collected corpus.

Prerequisites and bounded validation scope are documented in the collector
guides and [Docker guide](DOCKER.md). No arXiv identifier, DOI, publication status
or container/device test result is implied where none has been obtained.
