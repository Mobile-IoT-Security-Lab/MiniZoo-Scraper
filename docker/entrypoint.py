#!/usr/bin/env python3
"""Small command aliases; starting the image alone never contacts a device."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import minizoo


def main():
    args = sys.argv[1:]
    if not args or args[0] in {*minizoo.COMMANDS, "test", "help", "--help", "-h"}:
        return minizoo.main(args)
    os.execvp(args[0], args)


if __name__ == "__main__":
    raise SystemExit(main())
