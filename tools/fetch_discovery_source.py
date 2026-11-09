#!/usr/bin/env python3
"""Fetch only the three pinned discovery adapter files into private storage."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DESTINATION = ROOT / "private" / "dependencies" / "MiniCrawler"
SOURCE_FILES = {"pixel_crawler.py", "pixel_batch_crawler.py", "frida_direct_query.js"}
MAX_SOURCE_BYTES = 2 * 1024 * 1024
SOURCE_HOST = "raw.githubusercontent.com"


class SourceFetchError(RuntimeError):
    pass


def load_manifest(path=None):
    """Read the runner's pins without importing or executing either project."""
    values = {}
    tree = ast.parse(Path(path or ROOT / "discovery/main.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in ("SOURCE_COMMIT", "SOURCE_HASHES"):
                    if target.id in values:
                        raise SourceFetchError("Discovery source pins are repeated")
                    values[target.id] = ast.literal_eval(node.value)
    commit, hashes = values.get("SOURCE_COMMIT"), values.get("SOURCE_HASHES")
    if (not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit)
            or not isinstance(hashes, dict) or set(hashes) != SOURCE_FILES
            or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                   for value in hashes.values())):
        raise SourceFetchError("Invalid discovery source pins")
    return commit, hashes


def public_url(url):
    value = urllib.parse.urlsplit(url)
    return (value.scheme == "https" and value.netloc == SOURCE_HOST
            and value.username is None and value.password is None
            and not value.query and not value.fragment)


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        if not public_url(new_url):
            raise SourceFetchError("Discovery download redirected outside its public HTTPS source")
        return super().redirect_request(request, response, code, message, headers, new_url)


def download_bytes(url):
    if not public_url(url):
        raise SourceFetchError("Discovery download requires the pinned public HTTPS source")
    # No credential provider, cookie jar, or authenticated proxy is configured.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), PublicRedirect())
    request = urllib.request.Request(url, headers={"User-Agent": "MiniZoo-Scraper-Source-Fetcher"})
    try:
        with opener.open(request, timeout=30) as response:
            if response.status != 200 or not public_url(response.url):
                raise SourceFetchError("Unexpected discovery download response")
            content = response.read(MAX_SOURCE_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise SourceFetchError(f"Discovery source download failed (HTTP {error.code})") from None
    except (urllib.error.URLError, OSError):
        raise SourceFetchError("Discovery source download failed; check network access") from None
    if not content or len(content) > MAX_SOURCE_BYTES:
        raise SourceFetchError("Discovery source download has an invalid size")
    return content


def safe_destination(value):
    path = Path(os.path.abspath(Path(value).expanduser()))
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise SourceFetchError("Discovery source destination must not use symlinks")
    return path


def matches_file(path, expected):
    if path.is_symlink():
        raise SourceFetchError("Discovery source files must not be symlinks")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return False
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_BYTES:
            return False
        return hashlib.file_digest(stream, "sha256").hexdigest() == expected


def fetch_source(destination=DEFAULT_DESTINATION, *, download=download_bytes):
    commit, hashes = load_manifest()
    destination = safe_destination(destination)
    if destination.exists():
        metadata = safe_destination(destination / "MetadataCrawler")
        if destination.is_dir() and all(matches_file(metadata / name, digest)
                                       for name, digest in hashes.items()):
            return {"status": "unchanged", "commit": commit, "files": len(hashes)}
        raise SourceFetchError("Destination already exists with missing or different files; choose an empty destination")
    destination.parent.mkdir(parents=True, exist_ok=True)
    safe_destination(destination)
    with tempfile.TemporaryDirectory(prefix=".minizoo-discovery-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "source"
        metadata = staged / "MetadataCrawler"
        metadata.mkdir(parents=True)
        for name, expected in hashes.items():
            url = f"https://{SOURCE_HOST}/luca959/MiniCrawler/{commit}/MetadataCrawler/{name}"
            content = download(url)
            if (not isinstance(content, bytes) or not content or len(content) > MAX_SOURCE_BYTES
                    or hashlib.sha256(content).hexdigest() != expected):
                raise SourceFetchError("Discovery source checksum mismatch: " + name)
            with (metadata / name).open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        safe_destination(destination)
        if destination.exists():
            raise SourceFetchError("Discovery destination appeared during download; retry with an empty destination")
        # Publish the complete verified directory in one operation. Failures
        # before this point leave no partially accepted source at destination.
        os.replace(staged, destination)
    return {"status": "installed", "commit": commit, "files": len(hashes)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    args = parser.parse_args()
    try:
        result = fetch_source(args.destination)
    except SourceFetchError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, SyntaxError):
        print("Could not prepare discovery source; check the destination and source pins", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
