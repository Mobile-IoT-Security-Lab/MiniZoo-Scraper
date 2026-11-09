#!/usr/bin/env python3
"""Independent MiniZoo commands. Help and offline tests do not contact phones."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
COMMANDS = {
    "discovery": ("discovery/main.py", "Search and checkpoint AppIDs"),
    "collection": ("scraper/wechat_csv_scraper.py", "Collect packages from a CSV"),
    "version": ("version_scraper/main.py", "Revisit AppIDs and compare versions"),
    "inspect": (":minizoo_collection.inspect_package", "Read a package without running its code"),
    "decompile": (":minizoo_collection.tools.decompile_wxapkg", "Reconstruct one saved package"),
    "metadata": (":minizoo_collection.decompiled_metadata", "Extract or verify reconstructed app metadata"),
    "configure-version": ("tools/configure_version.py", "Create private version-collector configuration"),
}
SUITES = {
    "discovery": ("discovery", "tests"),
    "collection": ("scraper", "."),
    "version": ("version_scraper", "tests"),
    "core": ("MiniZooCollectionCore", "tests"),
    "cli": (".", "tests"),
}


def environment():
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env["MINIZOO_CORE_PATH"] = str(ROOT / "MiniZooCollectionCore")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT / "MiniZooCollectionCore"), env.get("PYTHONPATH")]))
    return env


def test(argv):
    parser = argparse.ArgumentParser(prog="minizoo.py test", description="Offline suites: synthetic inputs and mocked phones.")
    parser.add_argument("module", choices=["all", *SUITES], nargs="?", default="all")
    parser.add_argument("--integration", action="store_true", help="Also require and test installed MiniCrawler/decompiler dependencies")
    args = parser.parse_args(argv)
    env = environment()
    if args.integration:
        if args.module in {"all", "discovery"}:
            source = Path(env.get("MINICRAWLER_SOURCE", ROOT / "discovery/vendor/MiniCrawler")).resolve()
            if not (source / "MetadataCrawler/pixel_crawler.py").is_file():
                parser.error("Initialize discovery/vendor/MiniCrawler before using --integration")
            env["MINICRAWLER_SOURCE"] = str(source)
        if args.module in {"all", "core"}:
            tool = Path(env.get("WXAPP_TOOL_ROOT", ROOT / "version_scraper/vendor/wxappUnpacker")).resolve()
            if not (tool / "node_modules/esprima").is_dir():
                parser.error("Initialize wxappUnpacker and install its npm dependencies before using --integration")
            import shutil
            if not shutil.which("node"):
                parser.error("Node.js is required for --integration")
            try:
                node = subprocess.check_output(["node", "--version"], text=True).strip()
                revision = subprocess.check_output(["git", "-C", str(tool), "rev-parse", "HEAD"], text=True).strip()
            except (OSError, subprocess.SubprocessError) as exc:
                parser.error(f"Cannot check the reconstruction dependency: {exc}")
            if int(node.lstrip("v").split('.')[0]) < 26 or revision != "694ca3874e67b89dd709f3d577c85a553bd45a0c":
                parser.error("Integration tests require Node.js 26+ and the pinned wxappUnpacker revision")
            env["WXAPP_TOOL_ROOT"] = str(tool)
    else:
        env.pop("MINICRAWLER_SOURCE", None)
    selected = list(SUITES) if args.module == "all" else [args.module]
    failed = []
    for name in selected:
        directory, tests = SUITES[name]
        print(f"\n=== {name} ===", flush=True)
        result = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", tests, "-p", "test_*.py"],
                                cwd=ROOT / directory, env=env)
        if result.returncode:
            failed.append(name)
    print("\n" + ("FAIL: " + ", ".join(failed) if failed else "PASS: " + ", ".join(selected)), flush=True)
    return int(bool(failed))


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"help", "--help", "-h"}:
        print(__doc__ + "\n\nUsage: python3 minizoo.py COMMAND [arguments]\n")
        for command, (_, description) in COMMANDS.items():
            print(f"  {command:19} {description}")
        print("  test [MODULE]       Run all suites or discovery/collection/version/core/cli")
        print("\nUse COMMAND --help for options. Results default to collection_results/.")
        return 0
    if args[0] == "test":
        return test(args[1:])
    if args[0] not in COMMANDS:
        print(f"Unknown command: {args[0]}. Use --help.", file=sys.stderr)
        return 2
    target, _ = COMMANDS[args[0]]
    entry = ["-m", target[1:]] if target.startswith(":") else [str(ROOT / target)]
    # Keep the caller's cwd for explicit paths and forward signals to the module.
    os.execve(sys.executable, [sys.executable, "-B", *entry, *(args[1:] or ["--help"])], environment())


if __name__ == "__main__":
    raise SystemExit(main())
