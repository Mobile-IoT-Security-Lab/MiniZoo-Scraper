"""Read static metadata and byte evidence without a phone, network or decompiler."""
import argparse
import json
from pathlib import Path
import sys

from . import artifacts, static_reader


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    args = parser.parse_args(argv)
    try:
        path = args.package.expanduser().absolute()
        if path.resolve() != path:
            raise ValueError("Use an original package path without symlinks")
        size = path.stat().st_size
        if size > static_reader.MAX_PACKAGE:
            raise ValueError("Package exceeds the static reader size limit")
        report, evidence = static_reader.parse_one((artifacts.sha256_file(path), size, str(path)))
        print(json.dumps({"package": report, "evidence": evidence}, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "plaintext_parsed" else 1
    except (OSError, ValueError) as exc:
        print(f"Cannot inspect package: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
