#!/usr/bin/env python3
"""Independent MiniZoo commands. Help does not contact phones."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
COMMANDS = {
    "example": ("tools/run_examples.py", "Run checked offline examples with synthetic inputs"),
    "discovery": ("discovery/main.py", "Search and checkpoint AppIDs"),
    "collection": ("scraper/wechat_csv_scraper.py", "Collect packages from a CSV"),
    "version": ("version_scraper/main.py", "Revisit AppIDs and compare versions"),
    "inspect": (":minizoo_collection.inspect_package", "Read a package without running its code"),
    "decompile": (":minizoo_collection.tools.decompile_wxapkg", "Reconstruct one saved package"),
    "metadata": (":minizoo_collection.decompiled_metadata", "Extract or verify reconstructed app metadata"),
    "configure-version": ("tools/configure_version.py", "Create private version-collector configuration"),
}


def environment():
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env["MINIZOO_CORE_PATH"] = str(ROOT / "MiniZooCollectionCore")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT / "MiniZooCollectionCore"), env.get("PYTHONPATH")]))
    return env


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"help", "--help", "-h"}:
        print(__doc__ + "\n\nUsage: python3 minizoo.py COMMAND [arguments]\n")
        for command, (_, description) in COMMANDS.items():
            print(f"  {command:19} {description}")
        print("\nUse COMMAND --help for options. Results default to collection_results/.")
        return 0
    if args[0] not in COMMANDS:
        print(f"Unknown command: {args[0]}. Use --help.", file=sys.stderr)
        return 2
    target, _ = COMMANDS[args[0]]
    entry = ["-m", target[1:]] if target.startswith(":") else [str(ROOT / target)]
    # Keep the caller's cwd for explicit paths and forward signals to the module.
    os.execve(sys.executable, [sys.executable, "-B", *entry, *(args[1:] or ["--help"])], environment())


if __name__ == "__main__":
    raise SystemExit(main())
