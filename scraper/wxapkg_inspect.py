#!/usr/bin/env python3
import argparse
import hashlib
import struct
from pathlib import Path


def inspect(path: Path):
    b = path.read_bytes()
    print(f"file: {path}")
    print(f"size: {len(b)}")
    print(f"sha256: {hashlib.sha256(b).hexdigest()}")
    if len(b) < 18 or b[0] != 0xBE or b[13] != 0xED:
        print("wxapkg: header not recognized")
        return 1
    count = struct.unpack(">I", b[14:18])[0]
    print(f"wxapkg: yes")
    print(f"entries: {count}")
    off = 18
    for i in range(count):
        if off + 4 > len(b):
            print(f"truncated table at entry {i+1}")
            return 2
        name_len = struct.unpack(">I", b[off:off+4])[0]
        off += 4
        name = b[off:off+name_len].decode("utf-8", "replace")
        off += name_len
        file_off = struct.unpack(">I", b[off:off+4])[0]
        file_size = struct.unpack(">I", b[off+4:off+8])[0]
        off += 8
        print(f"{i+1:03d} {file_size:8d} {file_off:8d} {name}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    args = ap.parse_args()
    rc = 0
    for f in args.files:
        rc |= inspect(Path(f))
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
