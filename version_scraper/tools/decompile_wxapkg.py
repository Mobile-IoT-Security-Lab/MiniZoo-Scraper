#!/usr/bin/env python3
"""Adapt wxappUnpacker to --package/--output without modifying originals.

Restore JS/JSON/WXML/WXSS; incomplete results exit with code 2.
The upstream tool is pinned to PINNED_COMMIT and installed separately.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import sys


PINNED_COMMIT = "694ca3874e67b89dd709f3d577c85a553bd45a0c"
DEFAULT_TOOL = os.environ.get("WXAPP_TOOL_ROOT", str(Path(__file__).resolve().parents[1] / "vendor" / "wxappUnpacker"))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def package_index(path):
    data = path.read_bytes()
    if len(data) < 18 or data[0] != 0xBE or data[13] != 0xED:
        raise ValueError("Unsupported package: an unencrypted Android wxapkg is required")
    index_size, body_size = struct.unpack_from(">II", data, 5)
    if 14 + index_size + body_size != len(data):
        raise ValueError("Inconsistent package length")
    count = struct.unpack_from(">I", data, 14)[0]
    if count > 100000:
        raise ValueError("Too many files in the package")
    offset, result = 18, {}
    for _ in range(count):
        size = struct.unpack_from(">I", data, offset)[0]
        offset += 4
        if not 0 < size <= 65536 or offset + size + 8 > 14 + index_size:
            raise ValueError("Package index out of bounds")
        name = data[offset:offset + size].decode("utf-8").lstrip("/")
        offset += size
        position, length = struct.unpack_from(">II", data, offset)
        offset += 8
        if (not name or ".." in PurePosixPath(name).parts or "\\" in name
                or position < 14 + index_size or position + length > len(data)):
            raise ValueError("Invalid package path or contents")
        result[name] = hashlib.sha256(data[position:position + length]).hexdigest()
    if offset != 14 + index_size:
        raise ValueError("Inconsistent package index")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--tool-root", default=DEFAULT_TOOL)
    parser.add_argument("--node", default="node")
    parser.add_argument("--main-dir", help="Main package output used to restore a subpackage")
    args = parser.parse_args(argv)
    package, output, tool = (Path(value).resolve() for value in (args.package, args.output, args.tool_root))
    if not package.is_file() or not (tool / "wuWxapkg.js").is_file():
        raise ValueError("Package or decompiler not found")
    commit = subprocess.check_output(["git", "-C", str(tool), "rev-parse", "HEAD"], text=True).strip()
    if commit != PINNED_COMMIT:
        raise ValueError("Decompiler revision differs from the verified commit")
    node = shutil.which(args.node)
    if not node:
        raise ValueError("Node.js unavailable")
    node_version = subprocess.check_output([node, "--version"], text=True).strip()
    if int(node_version.lstrip("v").split(".")[0]) < 26:
        raise ValueError("This wrapper requires Node.js 26+ to restrict network access too")
    original = package_index(package)
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Output already populated: preserved; choose a new directory")
    scratch = output.parent / (output.name + "-work")
    scratch.mkdir(exist_ok=False)
    staged = scratch / "package.wxapkg"
    shutil.copyfile(package, staged)
    before = digest(package)
    preload = Path(__file__).with_name("serial_writes.cjs").resolve()
    compatibility = Path(__file__).with_name("decompiler_compat.cjs").resolve()
    if not preload.is_file() or not compatibility.is_file():
        raise ValueError("Decompiler preload missing")
    argv = [node, "--permission", "--allow-fs-read=" + str(tool),
            "--allow-fs-read=" + str(scratch), "--allow-fs-write=" + str(scratch),
            "--allow-fs-read=" + str(preload), "--require", str(preload),
            "--allow-fs-read=" + str(compatibility), "--require", str(compatibility),
            str(tool / "wuWxapkg.js")]
    context = None
    baseline = {}
    if args.main_dir:
        main_dir = Path(args.main_dir).resolve()
        if not main_dir.is_dir():
            raise ValueError("Main package directory missing")
        if any(path.is_symlink() for path in main_dir.rglob("*")):
            raise ValueError("Main directory contains symbolic links")
        context = scratch / "main-context"
        shutil.copytree(main_dir, context, symlinks=False)
        baseline = {str(p.relative_to(context)): digest(p) for p in context.rglob("*") if p.is_file()}
        argv.append("-s=" + str(context))
    argv.append(str(staged))
    environment = {"PATH": os.path.dirname(node) + ":/usr/bin:/bin",
                   "LANG": "en_US.UTF-8", "TMPDIR": str(scratch),
                   "WXAPP_VM_TIMEOUT_MS": "10000", "WXAPP_VM_BUFFER_LIMIT_MB": "32",
                   "WXAPP_TOOL_ROOT": str(tool), "WXAPP_COMPAT_AUDIT": str(scratch / "expected-outputs.jsonl")}
    log = scratch / "decompiler.log"
    with log.open("wb") as stream:
        result = subprocess.run(argv, cwd=scratch, env=environment, stdout=stream,
                                stderr=subprocess.STDOUT, timeout=240)
    generated, files = [], []
    candidates = {}
    # The tool leaves original resources in the subpackage and writes
    # restored code into a main-package copy: retain both without changing the main.
    sources = [scratch / "package"] + ([context] if context else [])
    for restored in sources:
        if not restored.is_dir():
            continue
        for path in sorted(restored.rglob("*")):
            if path.is_symlink():
                raise ValueError("Decompiler produced a symbolic link")
            if not path.is_file():
                continue
            relative = str(path.relative_to(restored))
            checksum = digest(path)
            if restored == context and baseline.get(relative) == checksum:
                continue
            candidates[relative] = (path, checksum)
    for relative, (path, checksum) in sorted(candidates.items()):
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        files.append({"path": relative, "sha256": checksum, "bytes": path.stat().st_size})
        if original.get(relative) != checksum:
            generated.append(relative)
    text = log.read_text(encoding="utf-8", errors="replace")
    errors = []
    if result.returncode:
        errors.append("wxappUnpacker exit=" + str(result.returncode))
    if re.search(r"error on |(?:^|\n)\s*err:|\b(?:ReferenceError|SyntaxError|TypeError|RangeError):", text):
        errors.append("Reconstruction errors in the log")
    if any(name.endswith(".ori.js") for name in generated):
        errors.append("Some WXML files remain in compiled .ori.js form")
    if not any(name.endswith(".js") and name not in ("app-service.js", "page-frame.js", "app-wxss.js") for name in generated):
        errors.append("No reconstructed JavaScript module")
    if context is None:
        config = output / ("game.json" if "game.js" in original else "app.json")
        if not config.is_file():
            errors.append("Reconstructed app/game configuration missing")
        else:
            try:
                value = json.loads(config.read_text(encoding="utf-8"))
                if not isinstance(value, dict) or not value:
                    raise ValueError("Configuration is empty or not an object")
            except (UnicodeError, ValueError):
                errors.append("Reconstructed app/game configuration is invalid JSON")
    has_templates = any(PurePosixPath(name).name in ("app-wxss.js", "page-frame.js", "page-frame.html") for name in original)
    if has_templates and not any(name.endswith(".wxml") for name in generated):
        errors.append("No WXML reconstructed from the bundle")
    expected_outputs = []
    audit = scratch / "expected-outputs.jsonl"
    if audit.is_file():
        seen = set()
        for line in audit.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            target = Path(item["path"])
            match = next((base for base in sources if base in target.parents), None)
            if match is None or item["kind"] not in ("js", "wxml", "wxss"):
                raise ValueError("Decompiler inventory outside the collection")
            relative = str(target.relative_to(match))
            key = (item["kind"], relative)
            if key in seen:
                continue
            seen.add(key)
            inherited = match == context and relative in baseline and target.is_file() and digest(target) == baseline[relative]
            present = relative in candidates or inherited
            expected_outputs.append({"kind": item["kind"], "path": relative,
                                     "present": present, "verified_main_context": inherited})
            if not present:
                errors.append("Artifact declared in the bundle but not reconstructed: " + relative)
    if not expected_outputs:
        errors.append("Reconstructed-module inventory missing")
    if digest(package) != before:
        raise ValueError("Original package changed during execution")
    receipt = {"tool": "threecha/wxappUnpacker", "commit": commit, "node": node_version,
               "serial_write_preload_sha256": digest(preload),
               "compatibility_preload_sha256": digest(compatibility),
               "input": str(package), "input_sha256": before, "input_unchanged": True,
               "status": "partial" if errors else "restored", "errors": errors,
               "log": str(log), "output": str(output), "files": files,
               "reconstructed_files": generated,
               "expected_outputs": expected_outputs,
               "limits": ["Restoration cannot recover comments, original names or code absent from the package.",
                          "Restoration does not verify package AppID or version."]}
    (scratch / "report.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": receipt["status"], "files": len(files), "reconstructed": len(generated),
                      "errors": errors, "report": str(scratch / "report.json")}, ensure_ascii=False))
    return 2 if errors else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(type(error).__name__ + ": " + str(error), file=sys.stderr)
        sys.exit(1)
