#!/usr/bin/env python3
"""Offline container checks. No ADB server, phone, Appium or external storage is contacted."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def run(*args, cwd=ROOT):
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    if result.returncode:
        print(result.stdout + result.stderr, file=sys.stderr)
        raise SystemExit(result.returncode)
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery-source", type=Path, help="Also test the external pinned MiniCrawler integration")
    args = parser.parse_args()
    if args.discovery_source:
        sys.path.insert(0, str(ROOT / "discovery"))
        from main import load_source
        load_source(args.discovery_source)
        os.environ["MINICRAWLER_SOURCE"] = str(args.discovery_source)
    assert sys.version_info[:2] == (3, 12)
    for name in ("adb", "frida", "node", "tesseract"):
        assert shutil.which(name), name
    assert int(run("node", "--version").lstrip("v").split(".")[0]) == 26
    assert importlib.metadata.version("frida") == "17.18.0"
    assert importlib.metadata.version("frida-tools") == "14.11.0"
    assert importlib.metadata.version("Pillow") == "13.0.0"
    languages = run("tesseract", "--list-langs").splitlines()
    assert "eng" in languages and "chi_sim" in languages
    for alias in ("discovery", "collection", "version"):
        assert "usage:" in run(sys.executable, str(ROOT / "docker/entrypoint.py"), alias, "--help").lower()
    sys.path.insert(0, str(ROOT / "MiniZooCollectionCore"))
    from minizoo_collection import processing, runtime
    assert processing and runtime
    run(sys.executable, "minizoo.py", "test", *( ["--integration"] if args.discovery_source else []))
    # A synthetic blank PGM exercises the actual Tesseract process and JSON adapter.
    with tempfile.TemporaryDirectory() as tmp:
        fixture = Path(tmp) / "blank.pgm"
        fixture.write_bytes(b"P5\n200 100\n255\n" + b"\xff" * 20000)
        ocr = json.loads(run(sys.executable, str(ROOT / "version_scraper/tools/ocr_tesseract.py"), str(fixture)))
        assert ocr == {"width": 200, "height": 100, "nodes": []}, ocr
    commit = run("git", "-c", "safe.directory=/opt/wxappUnpacker", "-C", "/opt/wxappUnpacker", "rev-parse", "HEAD")
    assert commit == "694ca3874e67b89dd709f3d577c85a553bd45a0c"
    assert list(Path("/opt/wxappUnpacker").glob("LICENSE*"))
    print("PASS: Python/Node/Frida pins, OCR, all three CLI imports, metadata capture/replay, static post-decompilation recovery, interrupted-run recovery, bootstrap/response tests, shared core and pinned decompiler source.")
    print("External discovery checkpoint/response integration: " + ("PASS" if args.discovery_source else "not requested (use --discovery-source /opt/minicrawler)."))


if __name__ == "__main__":
    main()
