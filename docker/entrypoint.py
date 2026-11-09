#!/usr/bin/env python3
"""Small command aliases; starting the image alone never contacts a device."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
HELP = """MiniZoo Scraper container

  discovery ARGS    AppID/metadata discovery (use --help for arguments)
  collection ARGS   Initial collector (use --help for arguments)
  version ARGS      Version collector (use --help for arguments)
  COMMAND ARGS      Run a tool, e.g. python3, frida or adb

No acquisition starts by default. See docs/DOCKER.md for host ADB/Appium,
read-only configuration/input mounts, persistent output and offline checks.
"""


def main():
    args = sys.argv[1:]
    if not args or args[0] in {"help", "--help", "-h"}:
        print(HELP)
        return
    aliases = {
        "discovery": ROOT / "docker" / "discovery.py",
        "collection": ROOT / "scraper" / "wechat_csv_scraper.py",
        "version": ROOT / "version_scraper" / "main.py",
    }
    if args[0] in aliases:
        args = [sys.executable, str(aliases[args[0]]), *args[1:]]
    os.execvp(args[0], args)


if __name__ == "__main__":
    main()
