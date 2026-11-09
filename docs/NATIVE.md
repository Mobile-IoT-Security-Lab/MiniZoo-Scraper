# Install without Docker

Use this only if you want to install the dependencies directly on macOS or Linux.
For the recommended Docker setup, start with the [main guide](../README.md).
Run commands from the repository root.

## Try the examples first

With Python 3.12+, no additional packages are needed for these offline examples:

```sh
python3 minizoo.py example all
```

Expect four `PASSED` lines. Results and `result.json` are saved in a new folder
under `collection_results/examples/`. Select `discovery`, `collection`, `version`
or `core` in place of `all` to run one example.

## Install the shared Python environment

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 minizoo.py --help
```

Activate `.venv` again in each new terminal. Live collection also needs Android
Platform Tools and the [phone prerequisites](DOCKER.md#connect-phones); skip the
Docker networking and container commands when using native execution. After
selecting `DEVICE_SERIAL`, verify the connection with `frida-ps -D "$DEVICE_SERIAL"`;
a working server returns a process list.

## Install reconstruction tools

For package reconstruction, install Node.js 26+ and the pinned unpacker:

```sh
git submodule update --init version_scraper/vendor/wxappUnpacker
npm ci --prefix version_scraper/vendor/wxappUnpacker --ignore-scripts --no-audit --no-fund
```

Use a Git clone for this setup. Source ZIPs omit the submodule; its required
revision is listed in [provenance](PROVENANCE.md).

## Start the module you need

- [Discovery: adapter, input and run command](../discovery/README.md#native-usage).
- [Initial collection: keyboard and run command](../scraper/README.md#native-usage).
- [Version collection: Appium, OCR and run commands](../version_scraper/README.md#native-usage).
- [Saved-package processing](../MiniZooCollectionCore/README.md#native-usage).
