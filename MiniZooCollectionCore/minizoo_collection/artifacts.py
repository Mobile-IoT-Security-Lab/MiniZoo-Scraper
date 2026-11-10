"""Read package bytes, validate runtime evidence, and run a real decompiler.

Appium controls the UI; this module only reads the existing Android cache. A
changed file is a candidate, never proof of its owning AppID or release.

The evidence JSON must come from a trusted adapter reading *loaded package*
metadata. Merely writing
the expected values into that JSON does not establish identity.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import uuid


CACHE_ROOT = "/data/data/com.tencent.mm/MicroMsg/appbrand/pkg"
SHA256 = re.compile(r"[0-9a-f]{64}")
APPID = re.compile(r"wx[0-9a-f]{16}")
SEMVER = re.compile(
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


class ArtifactError(RuntimeError):
    """Collection, evidence, or decompilation could not be verified."""


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remote_path(value):
    if not isinstance(value, str):
        raise ArtifactError("Remote package path must be a string")
    path = PurePosixPath(value)
    if (not value.startswith(CACHE_ROOT + "/") or ".." in path.parts
            or any(c in value for c in "\r\n\x00") or path.suffix != ".wxapkg"):
        raise ArtifactError("Invalid cache package path: " + value)
    return value


def _read_device(serial, command, adb):
    if not isinstance(serial, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", serial):
        raise ArtifactError("Invalid device serial")
    try:
        result = subprocess.run(
            [adb, "-s", serial, "exec-out", "su -c " + shlex.quote(command)],
            capture_output=True, check=True, timeout=120,
        )
        return result.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise ArtifactError("Unable to read package cache on " + serial) from exc


def inventory(serial, adb="adb"):
    """Return remote paths and actual hashes without modifying device cache."""
    command = "find " + shlex.quote(CACHE_ROOT) + " -type f -name '*.wxapkg' -exec sha256sum {} +"
    try:
        output = _read_device(serial, command, adb).decode("utf-8", "strict")
    except UnicodeError as exc:
        raise ArtifactError("Invalid UTF-8 cache inventory") from exc
    result = {}
    for line in output.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if match is None:
            raise ArtifactError("Malformed device hash inventory")
        digest, remote = match.groups()
        _remote_path(remote)
        if remote in result:
            raise ArtifactError("Duplicate path in device inventory")
        result[remote] = digest
    return result


def _directory(path):
    path = Path(path).absolute()
    if path.resolve() != path:
        raise ArtifactError("Artifact directories must not contain symlinks")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _immutable(path, data):
    """Publish atomically without overwriting an existing, different object."""
    path = Path(path)
    if path.is_symlink():
        raise ArtifactError("Refusing artifact symlink: " + str(path))
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != data:
                raise ArtifactError("Existing artifact differs: " + str(path))
    finally:
        temporary.unlink(missing_ok=True)


def capture(serial, before, collection_dir, adb="adb"):
    """Preserve new/changed cache files; hashes alone do not assign ownership.

    Raw copies and the before/after inventories survive a later failure. A hash
    mismatch is saved as a diagnostic and never returned as a verified package.
    """
    root = _directory(collection_dir)
    packages_dir = _directory(root / "packages")
    logs = _directory(root / "logs")
    after = inventory(serial, adb=adb)
    receipt_id = uuid.uuid4().hex
    _immutable(logs / ("cache-" + receipt_id + ".json"), json.dumps(
        {"serial": serial, "before": before, "after": after},
        ensure_ascii=False, indent=2,
    ).encode("utf-8"))
    collected = []
    errors = []
    for remote, expected in after.items():
        if before.get(remote) == expected:
            continue
        try:
            content = _read_device(serial, "cat -- " + shlex.quote(_remote_path(remote)), adb)
            actual = hashlib.sha256(content).hexdigest()
            if not content or actual != expected:
                _immutable(logs / ("unverified-" + actual + ".wxapkg"), content)
                raise ArtifactError("Cache changed during copy: " + remote)
            local = packages_dir / (actual + ".wxapkg")
            _immutable(local, content)
            collected.append({"remote_path": remote, "sha256": actual,
                              "bytes": len(content), "local_path": str(local)})
        except ArtifactError as exc:
            errors.append(str(exc))
    _immutable(logs / ("capture-" + receipt_id + ".json"), json.dumps(
        {"packages": collected, "errors": errors}, ensure_ascii=False, indent=2,
    ).encode("utf-8"))
    if errors:
        raise ArtifactError("; ".join(errors))
    return collected


def capture_selected(serial, remote_paths, collection_dir, adb="adb"):
    """Copy explicitly selected cached packages; ownership still needs evidence.

    Unlike cache-delta capture, this also handles packages prefetched by search.
    The caller must independently bind the bytes to the active runtime release.
    """
    if not isinstance(remote_paths, (list, tuple)):
        raise ArtifactError("Selected package paths must be a list")
    # Validate every path before reading anything from the phone.
    paths = list(dict.fromkeys(_remote_path(remote) for remote in remote_paths))
    root = _directory(collection_dir)
    packages_dir = _directory(root / "packages")
    logs = _directory(root / "logs")
    collected, errors = [], []
    for remote in paths:
        try:
            content = _read_device(serial, "cat -- " + shlex.quote(remote), adb)
            if not content:
                raise ArtifactError("Selected package is empty: " + remote)
            checksum = hashlib.sha256(content).hexdigest()
            local = packages_dir / (checksum + ".wxapkg")
            _immutable(local, content)
            collected.append({"remote_path": remote, "sha256": checksum,
                              "bytes": len(content), "local_path": str(local)})
        except ArtifactError as error:
            errors.append(str(error))
    _immutable(logs / ("capture-selected-" + uuid.uuid4().hex + ".json"), json.dumps(
        {"serial": serial, "remote_paths": paths, "packages": collected,
         "errors": errors, "attribution": "requires_independent_runtime_evidence"},
        ensure_ascii=False, indent=2,
    ).encode("utf-8"))
    if errors:
        raise ArtifactError("; ".join(errors))
    return collected


def validate_evidence(evidence_path, expected_appid, packages, attempt_id):
    """Validate a trusted runtime adapter's evidence against the captured bytes.

    Required: attempt_id, appid, source, active_loaded_release=true,
    package_binding='runtime_metadata', coverage ('complete'/'partial'), and a
    packages list of {remote_path, sha256, role ('main'/'subpackage'), loaded}.
    Main must be loaded. Cached subpackages require an explicit active-release
    membership and a runtime MD5 matching the actual bytes.
    Complete coverage requires expected_remote_paths; partial requires a list of
    coverage_limits. Optional version requires version_kind and version_source.
    Returned packages include verified local paths and roles for decompile().
    """
    try:
        evidence = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArtifactError("Runtime package evidence is missing or invalid") from exc
    if not isinstance(evidence, dict):
        raise ArtifactError("Runtime evidence must be an object")
    replayed = None
    if evidence.get("collection_schema") == "minizoo_acquisition_v1":
        from .runtime import verify_observations, EvidenceError
        try:
            replayed = verify_observations(evidence, evidence_path, packages)
        except (EvidenceError, OSError, ValueError, KeyError) as error:
            raise ArtifactError("Runtime observations failed replay: " + str(error)) from error
    if evidence.get("acquisition_kind") == "component_recovery" or "component_recovery" in evidence:
        return validate_component_recovery(evidence_path, expected_appid, packages, attempt_id)
    if (not isinstance(expected_appid, str) or not APPID.fullmatch(expected_appid)
            or evidence.get("appid") != expected_appid
            or not attempt_id or evidence.get("attempt_id") != attempt_id):
        raise ArtifactError("AppID or attempt mismatch in runtime evidence")
    if (evidence.get("active_loaded_release") is not True
            or evidence.get("package_binding") != "runtime_metadata"
            or not isinstance(evidence.get("source"), str)
            or not evidence["source"].strip()):
        raise ArtifactError("No verified active-release runtime package binding")
    captured = {}
    for package in packages:
        remote = _remote_path(package["remote_path"])
        digest = package.get("sha256", "")
        path = Path(package["local_path"])
        if (remote in captured or not isinstance(digest, str)
                or not SHA256.fullmatch(digest) or not path.is_file()
                or path.is_symlink() or path.resolve() != path.absolute()
                or path.stat().st_size <= 0 or path.stat().st_size != package.get("bytes")
                or sha256_file(path) != digest):
            raise ArtifactError("Captured package is missing, changed, or duplicated")
        captured[remote] = package
    entries = evidence.get("packages")
    if not isinstance(entries, list) or not entries:
        raise ArtifactError("No runtime package entries")
    checked = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ArtifactError("Malformed runtime package entry")
        remote = _remote_path(entry.get("remote_path"))
        if (remote in seen or remote not in captured
                or entry.get("sha256") != captured[remote]["sha256"]
                or entry.get("role") not in ("main", "subpackage")):
            raise ArtifactError("Runtime package does not match captured active bytes")
        if entry.get("loaded") is not True:
            runtime_md5 = entry.get("runtime_md5", "")
            if (entry["role"] != "subpackage" or entry.get("loaded") is not False
                    or entry.get("active_release_member") is not True
                    or entry.get("availability") != "cache_verified"
                    or not isinstance(runtime_md5, str) or not re.fullmatch(r"[0-9a-f]{32}", runtime_md5)
                    or hashlib.md5(Path(captured[remote]["local_path"]).read_bytes()).hexdigest() != runtime_md5):
                raise ArtifactError("Cached subpackage lacks verified active-release membership")
        seen.add(remote)
        details = {key: entry[key] for key in ("loaded", "load_status", "active_release_member", "availability",
                                              "module_root", "module_roots", "runtime_md5", "binding_method",
                                              "release_download_md5_match") if key in entry}
        checked.append(dict(captured[remote], role=entry["role"], **details))
    if sum(item["role"] == "main" for item in checked) != 1:
        raise ArtifactError("Exactly one verified main package is required")
    coverage = evidence.get("coverage")
    limits = evidence.get("coverage_limits", [])
    if coverage == "complete":
        expected = evidence.get("expected_remote_paths")
        if (not isinstance(expected, list) or not all(isinstance(p, str) for p in expected)
                or len(set(expected)) != len(expected) or set(expected) != seen or limits):
            raise ArtifactError("Complete collection lacks an exact component inventory")
    elif coverage == "partial":
        if not isinstance(limits, list) or not limits or not all(isinstance(v, str) and v.strip() for v in limits):
            raise ArtifactError("Partial collection requires explicit coverage limits")
    else:
        raise ArtifactError("Unknown collection coverage")
    version = evidence.get("version")
    kind = evidence.get("version_kind")
    version_source = evidence.get("version_source")
    if version not in (None, ""):
        if isinstance(version, bool) or not isinstance(version, (int, str)):
            raise ArtifactError("Invalid mini-app release version")
        version = str(version)
        valid = ((kind == "integer" and re.fullmatch(r"0|[1-9]\d*", version))
                 or (kind == "semver" and SEMVER.fullmatch(version)))
        if not valid or not isinstance(version_source, str) or not version_source.strip():
            raise ArtifactError("Release version kind or runtime source is unverified")
    else:
        version, kind, version_source = None, None, None
    public_fields = {}
    if replayed is not None:
        public_fields["observed_metadata"] = replayed["metadata"]
        if "public_metadata_status" in replayed:
            public_fields["public_metadata_status"] = replayed["public_metadata_status"]
        if "category_fallback" in evidence:
            public_fields["category_fallback"] = evidence["category_fallback"]
    return {"appid": expected_appid, "attempt_id": attempt_id, "version": version,
            **public_fields,
            "version_kind": kind, "source": evidence["source"],
            "version_source": version_source, "coverage": coverage,
            "coverage_limits": limits, "packages": checked,
            "runtime_binding": {key: evidence[key] for key in (
                "binding_method", "runtime_md5", "runtime_process_id", "runtime_observed_at"
            ) if key in evidence},
            "declared_modules": evidence.get("declared_modules", []),
            "package_config_evidence": evidence.get("package_config_evidence", []),
            "evidence_path": str(Path(evidence_path).absolute()),
            "evidence_sha256": sha256_file(evidence_path)}


def validate_component_recovery(evidence_path, expected_appid, packages, attempt_id):
    """Validate archived bytes against an unchanged, typed parent runtime proof."""
    from tools.runtime_evidence import (read_messages, select_binding, declared_modules,
                                        package_config_roots, EvidenceError)
    try:
        root = Path(evidence_path).absolute().parent
        evidence = json.loads(Path(evidence_path).read_text())
        recovery = evidence["component_recovery"]
        if evidence.get("acquisition_kind") != "component_recovery" or recovery.get("schema_version") != 1:
            raise ArtifactError("Unsupported component recovery proof")

        def checked_copy(reference):
            path = Path(reference["path"])
            if (not path.is_absolute() or root not in path.parents or path.resolve() != path
                    or not path.is_file() or sha256_file(path) != reference["sha256"]):
                raise ArtifactError("Component recovery proof copy changed or escaped collection")
            return path

        parent_path = checked_copy(recovery["parent_manifest"])
        parent_evidence_path = checked_copy(recovery["parent_evidence"])
        parent = json.loads(parent_path.read_text())
        original = json.loads(parent_evidence_path.read_text())
        if (parent.get("acquisition_kind") == "component_recovery" or "component_recovery" in original
                or original.get("acquisition_kind") == "component_recovery"):
            raise ArtifactError("Nested component recovery is not supported")
        if (parent.get("identity_verified") is not True or parent.get("appid") != expected_appid
                or parent.get("attempt_id") != attempt_id
                or parent.get("evidence_sha256") != recovery["parent_evidence"]["sha256"]):
            raise ArtifactError("Parent identity or evidence checksum differs")
        captured = {}
        for package in packages:
            path = Path(package["local_path"])
            remote = package["remote_path"]
            if (remote in captured or root not in path.parents or path.resolve() != path
                    or not path.is_file() or path.stat().st_size != package["bytes"]
                    or path.stat().st_size <= 0 or sha256_file(path) != package["sha256"]):
                raise ArtifactError("Recovered package bytes missing, changed, or duplicated")
            captured[remote] = package
        parent_packages = []
        for package in parent["packages"]:
            copied = captured.get(package["remote_path"])
            if copied is None or {k: v for k, v in copied.items() if k != "local_path"} != {
                    k: v for k, v in package.items() if k != "local_path"}:
                raise ArtifactError("Original package record changed in component recovery")
            parent_packages.append(copied)
        proof = validate_evidence(parent_evidence_path, expected_appid, parent_packages, attempt_id)
        for field in ("version", "version_kind", "source", "version_source", "coverage", "coverage_limits"):
            if proof.get(field) != parent.get(field):
                raise ArtifactError("Parent manifest differs from runtime evidence: " + field)
        changed = {"packages", "expected_remote_paths", "coverage", "coverage_limits", "declared_modules",
                   "acquisition_kind", "component_recovery"}
        if {k: v for k, v in evidence.items() if k not in changed} != {
                k: v for k, v in original.items() if k not in changed}:
            raise ArtifactError("Original runtime identity, timestamp, or metadata changed")

        # Rebuild the declaration from the original raw typed runtime objects.
        sources = recovery.get("runtime_sources", [])
        expected_sources = {(s["path"], s["sha256"]) for s in original.get("metadata_sources", [])}
        if (not expected_sources or len(sources) != len(expected_sources)
                or {(s["original_path"], s["sha256"]) for s in sources} != expected_sources):
            raise ArtifactError("Original typed runtime sources missing or substituted")
        messages = []
        for source in sources:
            messages.extend(read_messages(checked_copy(source).read_text()))
        binding = select_binding(messages, expected_appid, attempt_id)
        runtime = binding["runtime"]
        if (binding["main"]["version"] != proof["version"]
                or binding["main"]["md5"] != original.get("runtime_md5")
                or runtime.get("process_id") != original.get("runtime_process_id")
                or runtime.get("observed_at") != original.get("runtime_observed_at")):
            raise ArtifactError("Parent typed runtime binding changed")
        declared, _ = declared_modules(binding)
        typed = {m["module_root"]: m for m in declared}
        original_modules = {m["module_root"]: m for m in original.get("declared_modules", [])}
        if len(original_modules) != len(original.get("declared_modules", [])):
            raise ArtifactError("Duplicate parent module declarations")
        main = next(p for p in proof["packages"] if p["role"] == "main")
        roots, config_proof = package_config_roots(main["local_path"])
        if config_proof != original.get("package_config_evidence"):
            raise ArtifactError("Original root configuration checksum changed")

        additions, recovered_roots = [], set()
        declarations = [dict(m) for m in original.get("declared_modules", [])]
        modules = recovery.get("modules", [])
        if not modules:
            raise ArtifactError("Component recovery has no modules")
        for item in modules:
            module_root = item["module_root"]
            declaration, runtime_declaration = original_modules.get(module_root), typed.get(module_root)
            if (module_root in recovered_roots or module_root not in roots or not declaration
                    or declaration.get("status") != "missing" or declaration.get("binding_error")
                    or not runtime_declaration or any(declaration.get(k) != v for k, v in runtime_declaration.items())
                    or declaration.get("version") != proof["version"]):
                raise ArtifactError("Recovered module lacks exact parent release declaration")
            record = next((m for m in binding["config"].get("modules", [])
                           if m.get("path") == declaration["metadata_object"]), {})
            if record.get("class_name") != "com.tencent.mm.plugin.appbrand.appcache.ModulePkgInfo":
                raise ArtifactError("Recovered module declaration is not a typed ModulePkgInfo")
            sha = item["sha256"]
            remote = "sha256:" + sha
            if not SHA256.fullmatch(sha) or remote not in captured:
                raise ArtifactError("Recovered package requires its exact SHA256 identifier")
            package = captured[remote]
            if (package["sha256"] != sha or package["bytes"] != item["bytes"]
                    or package.get("origin_path") != item["origin_path"]
                    or not isinstance(item["origin_path"], str) or not Path(item["origin_path"]).is_absolute()
                    or item.get("md5") != declaration["md5"]
                    or hashlib.md5(Path(package["local_path"]).read_bytes()).hexdigest() != declaration["md5"]):
                raise ArtifactError("Recovered bytes do not match the declared module checksum")
            entry = {"remote_path": remote, "sha256": sha, "role": "subpackage", "loaded": False,
                     "load_status": "not_attested", "active_release_member": True,
                     "availability": "archive_verified", "runtime_md5": declaration["md5"],
                     "module_root": module_root, "module_roots": [module_root],
                     "metadata_object": declaration["metadata_object"],
                     "binding_method": "parent_runtime_inventory",
                     "release_download_md5_match": declaration["release_download_md5_match"],
                     "origin_path": item["origin_path"]}
            if any(e["remote_path"] == remote for e in additions):
                raise ArtifactError("Multiple recovered roots with identical bytes require explicit merging")
            additions.append(entry); recovered_roots.add(module_root)
            for module in declarations:
                if module["module_root"] == module_root:
                    module.update(status="archive_verified", remote_path=remote, sha256=sha)
        expected_entries = original["packages"] + additions
        expected_paths = [e["remote_path"] for e in expected_entries]
        limits = [v for v in original["coverage_limits"]
                  if v not in {"Declared subpackage unavailable in cache: " + r for r in recovered_roots}]
        if (evidence["packages"] != expected_entries or set(captured) != set(expected_paths)
                or evidence["expected_remote_paths"] != expected_paths
                or evidence["declared_modules"] != declarations or evidence["coverage_limits"] != limits
                or evidence["coverage"] != ("partial" if limits else "complete")):
            raise ArtifactError("Recovered inventory or coverage differs from justified changes")
        checked = proof["packages"] + [dict(captured[e["remote_path"]], **{
            k: v for k, v in e.items() if k not in ("remote_path", "sha256")}) for e in additions]
        return dict(proof, packages=checked, coverage=evidence["coverage"], coverage_limits=limits,
                    declared_modules=declarations, evidence_path=str(Path(evidence_path).absolute()),
                    evidence_sha256=sha256_file(evidence_path), acquisition_kind="component_recovery")
    except (OSError, ValueError, KeyError, TypeError, StopIteration, EvidenceError) as error:
        raise ArtifactError("Invalid component recovery evidence: " + str(error)) from error


def _verify_decompiled(output, role):
    files = []
    for path in output.rglob("*"):
        if path.is_symlink():
            raise ArtifactError("Decompiler produced a symlink")
        if path.is_file():
            files.append(path)
    scripts = [p for p in files if p.suffix == ".js" and len(p.read_bytes().strip()) >= 16]
    if not scripts:
        raise ArtifactError("Decompiler produced no meaningful JavaScript")
    if role == "main":
        if not any(p.name in ("app-service.js", "app.js", "game.js") for p in scripts):
            raise ArtifactError("Main decompilation lacks app-service.js, app.js, or game.js")
        configs = [p for p in files if p.name in ("app.json", "app-config.json", "game.json")]
        valid_config = False
        for path in configs:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                valid_config |= isinstance(value, dict) and bool(value)
            except (ValueError, UnicodeError):
                pass
        if not valid_config:
            raise ArtifactError("Main decompilation lacks a valid application configuration")
    return [{"path": str(p), "bytes": p.stat().st_size, "sha256": sha256_file(p)}
            for p in sorted(files)]


def _resume_decompiled(receipt_path, package, appid, command_hash, decompiled):
    """A receipt permits reuse only after verifying every saved output again."""
    if receipt_path.is_symlink():
        raise ArtifactError("Decompilation receipt must not be a symlink")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        output = Path(receipt["output"])
        if (receipt.get("appid") != appid or receipt.get("command_sha256") != command_hash
                or receipt.get("package_sha256") != package["sha256"]
                or receipt.get("role") != package["role"] or receipt.get("status") != "verified"
                or receipt.get("method") != "configured_external_decompiler"
                or output.parent != decompiled or not output.is_dir()
                or output.resolve() != output.absolute()):
            raise ArtifactError("Saved decompilation provenance does not match")
        if _verify_decompiled(output, package["role"]) != receipt["files"]:
            raise ArtifactError("Saved decompiled files changed; originals preserved")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ArtifactError("Invalid saved decompilation receipt: " + str(receipt_path)) from exc
    return receipt


def decompile(packages, collection_dir, appid, command, timeout=300):
    """Run an explicitly configured decompiler, preserving separate outputs/logs.

    `command` is an argv list with {package}, {output}, optional {appid} and
    {main_output} (empty for main, verified main directory for subpackages); no
    shell is used. It must reconstruct application files. Unpacking alone is
    intentionally not provided as a successful fallback. Per-package receipts
    allow interrupted collections to reuse already verified outputs.
    """
    if (not isinstance(command, list) or not command
            or not all(isinstance(arg, str) and arg for arg in command)
            or not any("{package}" in arg for arg in command)
            or not any("{output}" in arg for arg in command)):
        raise ArtifactError("Configure a real decompiler argv with {package} and {output}")
    if not APPID.fullmatch(appid):
        raise ArtifactError("Invalid decompilation AppID")
    if (not packages or sum(p.get("role") == "main" for p in packages) != 1
            or any(p.get("role") not in ("main", "subpackage") for p in packages)):
        raise ArtifactError("Decompiler requires verified main/subpackage roles")
    root = _directory(collection_dir)
    logs = _directory(root / "logs")
    decompiled = _directory(root / "decompiled")
    checkpoints = _directory(root / "checkpoints")
    base_command_hash = hashlib.sha256(json.dumps(command, ensure_ascii=False).encode()).hexdigest()
    needs_main = any("{main_output}" in argument for argument in command)
    main_output, main_sha256 = "", None
    results = []
    for package in sorted(packages, key=lambda item: item["role"] != "main"):
        command_hash = base_command_hash
        if needs_main and package["role"] == "subpackage":
            # A resumed subpackage must use the same verified main package.
            command_hash = hashlib.sha256((base_command_hash + main_sha256).encode()).hexdigest()
        source = Path(package["local_path"])
        if source.is_symlink() or source.resolve() != source.absolute() or sha256_file(source) != package["sha256"]:
            raise ArtifactError("Package changed before decompilation")
        receipt_path = checkpoints / ("decompile-" + package["sha256"] + "-" +
                                      package["role"] + "-" + command_hash[:12] + ".json")
        if receipt_path.exists() or receipt_path.is_symlink():
            receipt = _resume_decompiled(receipt_path, package, appid, command_hash, decompiled)
            results.append(receipt)
            if package["role"] == "main":
                main_output, main_sha256 = receipt["output"], package["sha256"]
            continue
        run_id = package["sha256"] + "-" + uuid.uuid4().hex[:8]
        output = decompiled / run_id
        output.mkdir()
        log = logs / ("decompile-" + run_id + ".log")
        arguments = [arg.replace("{package}", str(source.absolute()))
                     .replace("{output}", str(output)).replace("{appid}", appid)
                     .replace("{main_output}", main_output)
                     for arg in command]
        try:
            with log.open("xb") as stream:
                process = subprocess.run(arguments, cwd=output, stdout=stream,
                                         stderr=subprocess.STDOUT, timeout=timeout)
            if process.returncode:
                raise ArtifactError("Decompiler failed; see " + str(log))
        except (OSError, subprocess.SubprocessError) as exc:
            raise ArtifactError("Decompiler unavailable or timed out; see " + str(log)) from exc
        files = _verify_decompiled(output, package["role"])
        receipt = {"appid": appid, "command_sha256": command_hash,
                   "package_sha256": package["sha256"], "role": package["role"],
                   "output": str(output), "log": str(log), "files": files,
                   "status": "verified", "method": "configured_external_decompiler"}
        _immutable(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2).encode())
        results.append(receipt)
        if package["role"] == "main":
            main_output, main_sha256 = receipt["output"], package["sha256"]
    return results
