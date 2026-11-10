"""Read loaded WeChat metadata with Frida; verify actual package bytes with ADB.

No search, navigation, hooks, cache deletion, or private message access. The UI
remains controlled by Appium. A filename/version suffix never supplies identity.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import struct
import subprocess
import sys
import uuid

import shutil
from . import artifacts, public_metadata


class EvidenceError(RuntimeError):
    pass


def read_messages(text):
    messages = []
    for line in text.splitlines():
        try:
            if "message: " in line and " data: " in line:
                envelope = ast.literal_eval(line.split("message: ", 1)[1].rsplit(" data: ", 1)[0])
            else:
                envelope = json.loads(line)
            payload = envelope.get("payload", envelope)
            if isinstance(payload, dict) and payload.get("kind", "").startswith(("runtime-", "metadata-")):
                messages.append(payload)
        except (ValueError, SyntaxError, AttributeError):
            continue
    return messages


def canonical_path(path):
    if not isinstance(path, str):
        raise EvidenceError("Runtime package path missing")
    path = path.replace("/data/user/0/com.tencent.mm/", "/data/data/com.tencent.mm/", 1)
    if (not path.startswith(artifacts.CACHE_ROOT + "/") or ".." in PurePosixPath(path).parts
            or any(c in path for c in "\x00\r\n") or not path.endswith(".wxapkg")):
        raise EvidenceError("Runtime package outside scoped WeChat cache")
    return path


def named_package(record, object_path):
    fields = {}
    provenance = {}
    for name in ("pkgPath", "md5", "pkgVersion"):
        values = {str(v["value"]) for v in record.get("values", [])
                  if v.get("path") in (object_path + "." + name, object_path + ".toString()." + name)}
        for module in record.get("modules", []):
            if module.get("path") == object_path and module.get("fields", {}).get(name) is not None:
                values.add(str(module["fields"][name]))
        if len(values) != 1:
            raise EvidenceError("Missing/conflicting named package field: " + name)
        fields[name] = values.pop()
        matched = [v for v in record["values"] if v.get("path") in
                   (object_path + "." + name, object_path + ".toString()." + name)]
        provenance[name] = [{"path": v["path"], "declaring_class": v.get("declaring_class", "")}
                            for v in matched]
        for module in record.get("modules", []):
            if module.get("path") == object_path and name in module.get("provenance", {}):
                provenance[name].append({"path": object_path + "." + name,
                                         "declaring_class": module["provenance"][name].rsplit(".", 1)[0]})
    if not re.fullmatch(r"[0-9a-fA-F]{32}", fields["md5"]) or not re.fullmatch(r"0|[1-9]\d*", fields["pkgVersion"]):
        raise EvidenceError("Invalid named package checksum/version")
    return {"remote_path": canonical_path(fields["pkgPath"]), "md5": fields["md5"].lower(),
            "version": fields["pkgVersion"], "metadata_object": object_path,
            "version_provenance": provenance["pkgVersion"]}


def owning_config(record, package_path, appid):
    """Find the package's actual enclosing SysConfig with its own exact AppID."""
    owners = [obj for obj in record.get("objects", [])
              if "AppBrandSysConfig" in obj.get("class_name", "")
              and package_path.startswith(obj.get("path", "") + ".")]
    for owner in sorted(owners, key=lambda obj: len(obj["path"]), reverse=True):
        prefix = owner["path"] + "."
        if any(value.get("value") == appid and value.get("path", "").startswith(prefix)
               and "." not in value["path"][len(prefix):]
               and "[" not in value["path"][len(prefix):]
               for value in record.get("values", [])):
            return owner
    return None


def select_binding(messages, appid, attempt_id):
    records = [r for r in messages if r.get("kind") == "runtime-package-metadata"
               and r.get("attempt_id") == attempt_id]
    active = []
    for record in records:
        identity = any(v.get("value") == appid and v.get("path", "").count(".") == 1
                       and v.get("declaring_class") == "com.tencent.mm.plugin.appbrand.AppBrandRuntime"
                       for v in record.get("values", []))
        focused = any(v.get("window_focus") is True and
                      (v.get("shown") is True or v.get("source") == "android.app.Activity"
                       and v.get("finishing") is False and v.get("destroyed") is False)
                      for v in record.get("lifecycle", []))
        if identity and focused:
            active.append(record)
    unique = {(r.get("process_id"), r["objects"][0]["identity_hash"]): r for r in active}
    if len(unique) != 1:
        raise EvidenceError("Exactly one visible, focused runtime for the requested AppID is required")
    runtime = next(iter(unique.values()))
    runtime_hashes = {str(v.get("value", "")).lower() for v in runtime.get("values", [])
                      if re.search(r"WxaVersion[^.]*Info", v.get("declaring_class", ""))
                      and re.fullmatch(r"[0-9a-fA-F]{32}", str(v.get("value", "")))}
    runtime_versions = {str(v.get("value", "")) for v in runtime.get("values", [])
                        if re.search(r"[.$]WxaVersionInfo$", v.get("declaring_class", ""))
                        and v.get("type") == "int"
                        and re.fullmatch(r"0|[1-9]\d*", str(v.get("value", "")))}
    candidates = {}
    for record in records:
        if record.get("process_id") != runtime.get("process_id"):
            continue
        if not (record is runtime or "AppBrandSysConfig" in record.get("runtime_class", "")):
            continue
        for obj in record.get("objects", []):
            if not obj.get("class_name", "").endswith(".WxaPkgWrappingInfo"):
                continue
            owner = owning_config(record, obj["path"], appid)
            if owner is None:
                continue
            package = named_package(record, obj["path"])
            if package["version"] not in runtime_versions:
                continue
            direct = record is runtime
            # Otherwise join two typed runtime records by the exact owning
            # AppID, process, and content MD5 already held in active InitConfig.
            if not direct and package["md5"] not in runtime_hashes:
                continue
            key = (package["remote_path"], package["md5"], package["version"])
            if key in candidates:
                previous_package, previous, previous_method, _ = candidates[key]
                def inventory_signature(metadata, package_path):
                    return sorted((str(m.get("fields", {}).get("name")), str(m.get("fields", {}).get("md5")),
                                   str(m.get("fields", {}).get("pkgVersion")))
                                  for m in metadata.get("modules", [])
                                  if m.get("path") == package_path or m.get("path", "").startswith(package_path + "."))
                if inventory_signature(previous, previous_package["metadata_object"]) != inventory_signature(record, package["metadata_object"]):
                    raise EvidenceError("Conflicting runtime inventories for the same main release")
                if previous_method == "runtime_object_graph":
                    continue
            proof = {"config_path": owner["path"], "config_identity_hash": owner.get("identity_hash"),
                     "package_path": obj["path"], "package_identity_hash": obj.get("identity_hash")}
            candidates[key] = (package, record, "runtime_object_graph" if direct else "active_runtime_version_info_content_md5", proof)
    if len(candidates) != 1:
        raise EvidenceError("No unique active-release package binding")
    package, config, method, proof = next(iter(candidates.values()))
    return {"main": package, "runtime": runtime, "config": config, "method": method, "binding_proof": proof}


def module_root(value):
    if not isinstance(value, str) or not value or "\\" in value or any(c in value for c in "\r\n\x00"):
        raise EvidenceError("Invalid declared module root")
    value = value.rstrip("/")
    if value.startswith("/"):
        value = value[1:]
    if not value or value.startswith("/") or any(p in ("", ".", "..") for p in value.split("/")):
        raise EvidenceError("Unsafe declared module root")
    return value


def declared_modules(binding):
    """Return the named host-module list, with explicit reasons it is incomplete."""
    config, main = binding["config"], binding["main"]
    schema = {s["path"]: s for s in config.get("schema", [])}
    collections = []
    limits = []
    for collection in config.get("collections", []):
        declaration = schema.get(collection["path"], {})
        owner = collection.get("declaring_class", declaration.get("declaring_class", ""))
        field_type = collection.get("field_type", declaration.get("type", ""))
        if (collection["path"].startswith(main["metadata_object"] + ".")
                and owner.endswith(".WxaPkgWrappingInfo")
                and field_type in ("java.util.List", "java.util.LinkedList", "java.util.ArrayList")):
            collections.append(collection)
    if not collections:
        limits.append("Runtime host-module inventory unavailable")
    hashes = {str(v.get("value", "")).lower() for v in binding["runtime"].get("values", [])
              if re.search(r"WxaVersion[^.]*Info", v.get("declaring_class", ""))
              and re.fullmatch(r"[0-9a-fA-F]{32}", str(v.get("value", "")))}
    modules = {}
    for collection in collections:
        prefix = collection["path"]
        records = [m for m in config.get("modules", [])
                   if re.fullmatch(re.escape(prefix) + r"\[\d+\]", m.get("path", ""))]
        if (collection.get("truncated") is not False or collection.get("inspected") != collection.get("size")
                or len(records) != collection.get("size") or collection.get("metadata_items") != collection.get("size")):
            limits.append("Runtime module list incomplete: " + prefix)
        for record in records:
            try:
                fields = record["fields"]
                root = module_root(fields.get("name"))
                md5 = str(fields.get("md5", "")).lower()
                version = str(fields.get("pkgVersion", ""))
                if not re.fullmatch(r"[0-9a-f]{32}", md5) or version != main["version"]:
                    raise EvidenceError("Module does not match the bound parent release version")
                path = canonical_path(fields["pkgPath"]) if fields.get("pkgPath") else None
                module = {"module_root": root, "md5": md5, "version": version,
                          "runtime_path": path, "metadata_object": record["path"],
                          "provenance": record.get("provenance", {}),
                          "binding_method": "parent_runtime_inventory",
                          "release_download_md5_match": md5 in hashes}
                if root in modules and (modules[root]["md5"], modules[root]["version"]) != (md5, version):
                    raise EvidenceError("Conflicting module declaration: " + root)
                modules[root] = module
            except (EvidenceError, KeyError) as error:
                limits.append(record["path"] + ": " + str(error))
    if config.get("scan_limits") or binding["runtime"].get("scan_limits"):
        limits.append("Runtime metadata scan reached its configured bound")
    for collection in binding["runtime"].get("collections", []):
        if collection.get("truncated"):
            limits.append("Active release metadata list truncated: " + collection["path"])
    return list(modules.values()), limits


def package_config_roots(path):
    """Read only the root config from the original WXAPKG; this is not decompilation."""
    data = Path(path).read_bytes()
    if len(data) < 18 or data[0] != 0xBE or data[13] != 0xED:
        raise EvidenceError("Main package configuration unavailable: unsupported container")
    index_size, body_size = struct.unpack_from(">II", data, 5)
    end = 14 + index_size
    count = struct.unpack_from(">I", data, 14)[0]
    if count > 100000 or end + body_size != len(data) or end < 18:
        raise EvidenceError("Invalid main package index")
    configs, offset = [], 18
    for _ in range(count):
        if offset + 4 > end:
            raise EvidenceError("Truncated package index")
        size = struct.unpack_from(">I", data, offset)[0]
        offset += 4
        if size > 16384 or offset + size + 8 > end:
            raise EvidenceError("Invalid package member")
        name = data[offset:offset + size].decode("utf-8")
        offset += size
        position, length = struct.unpack_from(">II", data, offset)
        offset += 8
        if position < end or position + length > len(data):
            raise EvidenceError("Package member exceeds container")
        if name.lstrip("/") not in ("app-config.json", "app.json", "game.json"):
            continue
        config = json.loads(data[position:position + length].decode("utf-8-sig"))
        if not isinstance(config, dict) or not (isinstance(config.get("pages"), list) or name.lstrip("/") == "game.json"):
            raise EvidenceError("Unrecognized root application configuration")
        declarations = config.get("subPackages", config.get("subpackages", []))
        if not isinstance(declarations, list):
            raise EvidenceError("Unrecognized subpackage declarations")
        roots = [module_root(item["root"]) for item in declarations if isinstance(item, dict) and "root" in item]
        if len(roots) != len(declarations) or len(set(roots)) != len(roots):
            raise EvidenceError("Malformed or duplicated config roots")
        configs.append((name, set(roots), hashlib.sha256(data[position:position + length]).hexdigest()))
    if offset != end or not configs or any(c[1] != configs[0][1] for c in configs):
        raise EvidenceError("Missing/conflicting root package configuration")
    return configs[0][1], [{"member": n, "sha256": digest} for n, _, digest in configs]


def cache_md5_inventory(serial, adb="adb"):
    command = "find " + shlex.quote(artifacts.CACHE_ROOT) + " -type f -name '*.wxapkg' -exec md5sum {} +"
    result = {}
    for line in artifacts._read_device(serial, command, adb).decode("utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{32})  (.+)", line)
        if match is None:
            raise EvidenceError("Invalid scoped cache MD5 inventory")
        digest, path = match.groups()
        path = canonical_path(path)
        if path in result:
            raise EvidenceError("Duplicate cache path")
        result[path] = digest.lower()
    return result


def verified_content(package, expected_md5):
    local = Path(package["local_path"])
    if not local.is_file() or local.is_symlink() or local.resolve() != local.absolute():
        raise EvidenceError("Invalid captured package path")
    content = local.read_bytes()
    if (hashlib.md5(content).hexdigest() != expected_md5
            or hashlib.sha256(content).hexdigest() != package["sha256"]
            or len(content) != package["bytes"]):
        raise EvidenceError("Runtime checksum does not match captured package bytes")
    return content


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def write_text(path, value):
    """Persist raw observations before downstream parsing or decompilation."""
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def finish_evidence(binding, packages, serial, appid, attempt_id, collection_dir, adb="adb", *, capture_fn=None, cache_fn=None, record_cache=True):
    capture_fn = capture_fn or artifacts.capture_selected
    cache_fn = cache_fn or cache_md5_inventory
    main = binding["main"]
    existing = {canonical_path(p["remote_path"]): p for p in packages}
    if len(existing) != len(packages):
        raise EvidenceError("Duplicate captured package paths")
    if main["remote_path"] not in existing:
        selected = capture_fn(serial, [main["remote_path"]], collection_dir, adb=adb)
        existing.update({p["remote_path"]: p for p in selected})
    package = existing[main["remote_path"]]
    verified_content(package, main["md5"])
    modules, limits = declared_modules(binding)
    config_evidence, config_roots = [], None
    try:
        config_roots, config_evidence = package_config_roots(package["local_path"])
        if config_roots != {m["module_root"] for m in modules}:
            limits.append("Runtime module roots do not equal package app-config roots")
    except (EvidenceError, ValueError, UnicodeError, struct.error) as error:
        limits.append(str(error))
    for module in modules:
        if config_roots is None or module["module_root"] not in config_roots:
            module["binding_error"] = "Module root is not corroborated by the original package configuration"
    entries = [{"remote_path": package["remote_path"], "sha256": package["sha256"],
                "role": "main", "loaded": True, "active_release_member": True,
                "runtime_md5": main["md5"], "availability": "runtime_path_verified"}]
    by_md5 = {}
    for path, captured in existing.items():
        content = Path(captured["local_path"]).read_bytes()
        if hashlib.sha256(content).hexdigest() != captured["sha256"] or len(content) != captured["bytes"]:
            raise EvidenceError("Previously captured package changed")
        by_md5.setdefault(hashlib.md5(content).hexdigest(), []).append(path)
    unresolved = [m for m in modules if not m.get("binding_error") and m["md5"] not in by_md5]
    if unresolved:
        try:
            cache = cache_fn(serial, adb)
        except (EvidenceError, artifacts.ArtifactError, OSError, subprocess.SubprocessError) as error:
            cache = {}
            limits.append("Cache inventory unavailable: " + str(error))
        receipt = Path(collection_dir) / "logs" / ("module-cache-" + uuid.uuid4().hex + ".json")
        if record_cache:
            write_json(receipt, {"attempt_id": attempt_id, "device": serial, "md5_by_path": cache})
        selected_paths = set()
        for module in unresolved:
            matches = sorted(p for p, digest in cache.items() if digest == module["md5"])
            if matches:
                chosen = module["runtime_path"] if module["runtime_path"] in matches else matches[0]
                selected_paths.add(chosen)
        for selected_path in sorted(selected_paths):
            try:
                captured_items = capture_fn(serial, [selected_path], collection_dir, adb=adb)
            except (artifacts.ArtifactError, OSError) as error:
                limits.append("Subpackage copy unavailable: " + selected_path + ": " + str(error))
                continue
            for captured in captured_items:
                existing[captured["remote_path"]] = captured
                digest = hashlib.md5(Path(captured["local_path"]).read_bytes()).hexdigest()
                by_md5.setdefault(digest, []).append(captured["remote_path"])
    declarations = []
    entry_by_path = {e["remote_path"]: e for e in entries}
    for module in modules:
        if module.get("binding_error"):
            declarations.append(dict(module, status="metadata_unverified"))
            limits.append(module["module_root"] + ": " + module["binding_error"])
            continue
        available = sorted(by_md5.get(module["md5"], []))
        declaration = dict(module, status="missing")
        if not available:
            limits.append("Declared subpackage unavailable in cache: " + module["module_root"])
        else:
            path = module["runtime_path"] if module["runtime_path"] in available else available[0]
            captured = existing[path]
            verified_content(captured, module["md5"])
            declaration.update(status="verified", remote_path=path, sha256=captured["sha256"])
            if path in entry_by_path:
                entry_by_path[path].setdefault("module_roots", []).append(module["module_root"])
            else:
                entry = {"remote_path": path, "sha256": captured["sha256"], "role": "subpackage",
                         "loaded": False, "load_status": "not_attested", "active_release_member": True,
                         "availability": "cache_verified", "runtime_md5": module["md5"],
                         "module_root": module["module_root"], "module_roots": [module["module_root"]],
                         "metadata_object": module["metadata_object"],
                         "binding_method": module["binding_method"],
                         "release_download_md5_match": module["release_download_md5_match"]}
                entries.append(entry)
                entry_by_path[path] = entry
        declarations.append(declaration)
    runtime = binding["runtime"]
    evidence = {
        "attempt_id": attempt_id, "appid": appid, "active_loaded_release": True,
        "package_binding": "runtime_metadata",
        "source": ("Visible AppBrandRuntime object graph + AppBrandSysConfigWC/WxaPkgWrappingInfo"
                   if binding["method"] == "runtime_object_graph" else
                   "Visible AppBrandRuntime/AppBrandInitConfig content MD5 + AppBrandSysConfigWC/WxaPkgWrappingInfo"),
        "version": main["version"], "version_kind": "integer",
        "version_source": "; ".join(v["declaring_class"] + ":" + v["path"] for v in main["version_provenance"]),
        "coverage": "partial" if limits else "complete", "coverage_limits": sorted(set(limits)),
        "packages": entries, "expected_remote_paths": [e["remote_path"] for e in entries],
        "declared_modules": declarations, "package_config_evidence": config_evidence,
        "binding_method": binding["method"], "runtime_md5": main["md5"],
        "binding_proof": binding.get("binding_proof", {}),
        "runtime_process_id": runtime["process_id"], "runtime_observed_at": runtime["observed_at"],
        "runtime_lifecycle": runtime["lifecycle"],
        "package_metadata_object": main["metadata_object"],
        "runtime_collections": binding["config"].get("collections", []),
        "runtime_modules": binding["config"].get("modules", []),
    }
    return evidence, list(existing.values())


CACHE_ROOT = artifacts.CACHE_ROOT
RUNTIME_CLASS = "com.tencent.mm.plugin.appbrand.AppBrandRuntime"
APPID_RE = re.compile(r"wx[0-9a-f]{16}")
METADATA_FIELDS = ("appid", "name", "developer", "username", "description", "category", "icon_url", "app_version")
_select_binding = select_binding


def focused(record):
    return any(item.get("window_focus") is True and (
        item.get("shown") is True or item.get("source") == "android.app.Activity"
        and item.get("finishing") is False and item.get("destroyed") is False)
        for item in record.get("lifecycle", []))


def select_opened_binding(messages, attempt_id):
    """Discover one focused runtime first; the requested AppID is never an input."""
    if any(m.get("class_name") == RUNTIME_CLASS and (
            m.get("kind") == "metadata-probe-error"
            or m.get("kind") == "metadata-class-complete" and m.get("instances", 0) > 6)
           for m in messages):
        raise EvidenceError("Focused runtime enumeration was incomplete")
    active = {}
    for record in messages:
        if (record.get("kind") != "runtime-package-metadata"
                or record.get("attempt_id") != attempt_id or not focused(record)):
            continue
        ids = {v.get("value") for v in record.get("values", [])
               if v.get("declaring_class") == RUNTIME_CLASS
               and v.get("path", "").count(".") == 1
               and APPID_RE.fullmatch(str(v.get("value", "")))}
        roots = [obj for obj in record.get("objects", []) if obj.get("path") == "runtime"]
        if len(ids) != 1 or len(roots) != 1 or not roots[0].get("identity_hash"):
            continue
        key = (record.get("process_id"), roots[0]["identity_hash"])
        active.setdefault(key, set()).update(ids)
    if len(active) != 1 or len(next(iter(active.values()))) != 1:
        raise EvidenceError("Exactly one focused runtime with an observed AppID is required")
    appid = next(iter(next(iter(active.values()))))
    binding = _select_binding(messages, appid, attempt_id)
    binding["appid"] = appid
    binding["public_metadata"] = public_metadata.select_records(
        messages, appid, attempt_id, binding["runtime"]["process_id"])
    binding["public_metadata_schema"] = binding["runtime"].get("metadata_schema")
    binding["metadata"] = runtime_metadata(binding)
    return binding


def runtime_metadata(binding):
    aliases = {"appName": "name", "nickName": "name", "nickname": "name",
               "userName": "username", "username": "username",
               "developer": "developer", "developerName": "developer",
               "description": "description", "signature": "description",
               "iconUrl": "icon_url", "appIconUrl": "icon_url"}
    values = {}
    # Only metadata from the selected runtime graph or its bound SysConfig.
    for record in (binding["runtime"], binding["config"]):
        for value in record.get("values", []):
            owner = value.get("declaring_class", "")
            name = value.get("path", "").rsplit(".", 1)[-1]
            if name not in aliases or not re.search(r"AppBrand(?:InitConfig|SysConfig)", owner):
                continue
            text = value.get("value")
            if isinstance(text, str) and text.strip() and len(text) <= 4096:
                values.setdefault(aliases[name], set()).add(text)
    if binding.get("public_metadata_schema") == public_metadata.SCHEMA:
        result, report = public_metadata.decode_records(binding.get("public_metadata", []), values)
        binding["public_metadata_status"] = report
        result.update(appid=binding["appid"], app_version=binding["main"]["version"])
        return result
    # Preserve the original projection when replaying historical acquisitions.
    result = {key: next(iter(items)) for key, items in values.items() if len(items) == 1}
    cached = {}
    allowed = {"name": "field_nickname", "username": "field_username",
               "description": "field_signature", "icon_url": "field_brandIconURL"}
    for record in binding.get("public_metadata", []):
        for key, field in allowed.items():
            value = record.get("fields", {}).get(key)
            proof = record.get("provenance", {}).get(key, {})
            if (isinstance(value, str) and value.strip() and len(value) <= 4096
                    and proof.get("field") == field and proof.get("value") == value):
                cached.setdefault(key, set()).add(value)
    for key, items in cached.items():
        if key not in values and len(items) == 1:
            result[key] = next(iter(items))
    result.update(appid=binding["appid"], app_version=binding["main"]["version"])
    return result


def binding_signature(binding):
    root = next(o for o in binding["runtime"]["objects"] if o["path"] == "runtime")
    main = binding["main"]
    return (binding["appid"], binding["runtime"]["process_id"], root["identity_hash"],
            main["remote_path"], main["md5"], main["version"])


def resolve_frida(value=""):
    candidates = [value] if value else ["frida"]
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
    raise EvidenceError("Frida CLI unavailable: set --frida or WECHAT_FRIDA")


class RuntimeProbe:
    def __init__(self, serial, frida, adb="adb"):
        if not serial or any(c.isspace() for c in serial):
            raise EvidenceError("An explicit device serial is required for runtime observations")
        self.serial, self.frida, self.adb = serial, frida, adb

    def observe(self, attempt_id, directory, pid=None):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=False)
        source = Path(__file__).with_name("opened_app_probe.js").read_text(encoding="utf-8")
        script = directory / "probe.js"
        write_text(script, "globalThis.PROBE_ATTEMPT_ID = " + json.dumps(attempt_id) + ";\n" + source)
        if pid is None:
            process = subprocess.run(
                [self.adb, "-s", self.serial, "shell", "pidof", "com.tencent.mm:appbrand"]
                + ["com.tencent.mm:appbrand" + str(i) for i in range(10)],
                capture_output=True, text=True, timeout=15)
            pids = sorted({int(p) for p in process.stdout.split() if p.isdigit() and int(p) > 0})
        else:
            pids = [int(pid)]
        if not pids:
            raise EvidenceError("No running Mini Program process")
        messages, failures = [], []
        for process_id in pids:
            try:
                process = subprocess.run(
                    [self.frida, "-D", self.serial, "-p", str(process_id), "-q",
                     "--no-auto-reload", "-t", "12", "-l", str(script)],
                    capture_output=True, text=True, timeout=25)
                output = process.stdout + "\n" + process.stderr
                write_text(directory / (str(process_id) + ".log"), output)
                records = read_messages(output)
                if process.returncode or not any(r.get("kind") == "metadata-probe-complete" for r in records):
                    failures.append(process_id)
                else:
                    messages.extend(records)
            except subprocess.TimeoutExpired as error:
                def decoded(value):
                    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
                write_text(directory / (str(process_id) + ".log"),
                           decoded(error.stdout) + "\n" + decoded(error.stderr))
                failures.append(process_id)
        write_json(directory / "messages.json", messages)
        if failures:
            raise EvidenceError("Runtime observation incomplete for processes: " + str(failures))
        try:
            binding = select_opened_binding(messages, attempt_id)
        except EvidenceError:
            return messages  # The caller reports the original identity failure.
        if binding.get("public_metadata_status", {}).get("missing_fields"):
            messages.extend(self.cached_metadata(binding, attempt_id, directory))
            write_json(directory / "messages.json", messages)
        return messages

    def cached_metadata(self, binding, attempt_id, directory):
        """Read public cache in an already-running WeChat main process only.

        The focused runtime supplies the identity. This optional observation
        cannot replace its AppID, version or package bindings.
        """
        context = {"appid": binding["appid"], "attempt_id": attempt_id,
                   "runtime_process_id": binding["runtime"]["process_id"]}
        unavailable = dict(context, kind="metadata-main-cache-unavailable")
        try:
            process = subprocess.run([self.adb, "-s", self.serial, "shell", "pidof", "com.tencent.mm"],
                                     capture_output=True, text=True, timeout=15)
            pids = {int(value) for value in process.stdout.split() if value.isdigit() and int(value) > 0}
            if process.returncode or len(pids) != 1:
                return [dict(unavailable, reason="Existing WeChat main PID unavailable or ambiguous")]
            pid = pids.pop()
            source = Path(__file__).with_name("cached_public_metadata_probe.js").read_text(encoding="utf-8")
            script = directory / "cached_public_metadata_probe.js"
            write_text(script, "globalThis.CACHED_METADATA_CONTEXT = " + json.dumps(context) + ";\n" + source)
            process = subprocess.run([self.frida, "-D", self.serial, "-p", str(pid), "-q",
                "--no-auto-reload", "-t", "12", "-l", str(script)],
                capture_output=True, text=True, timeout=25)
            output = process.stdout + "\n" + process.stderr
            write_text(directory / ("main-metadata-" + str(pid) + ".log"), output)
            records = read_messages(output)
            if process.returncode or not any(r.get("kind") == "metadata-probe-complete" for r in records):
                return [dict(unavailable, reason="Public cache observation incomplete")]
            # Check the real process PID, declared process name, runtime context
            # and completed table query again during offline replay.
            return [record for record in records if record.get("process_id") == pid
                    and record.get("metadata_source") == "wechat_main_cache"
                    and record.get("runtime_process_id") == context["runtime_process_id"]
                    and record.get("appid") == context["appid"]
                    and record.get("attempt_id") == attempt_id]
        except subprocess.TimeoutExpired as error:
            def decoded(value):
                return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
            write_text(directory / "main-metadata-timeout.log", decoded(error.stdout) + "\n" + decoded(error.stderr))
            return [dict(unavailable, reason="Public cache observation timed out")]
        except subprocess.SubprocessError as error:
            return [dict(unavailable, reason=type(error).__name__)]


PIPELINE_SCHEMA = "minizoo_acquisition_v1"


def _inventory_signature(binding):
    modules, limits = declared_modules(binding)
    return sorted((m["module_root"], m["md5"], m["version"]) for m in modules), sorted(limits)


def collect_runtime(probe, attempt_id, collection_dir, *, expected_appid=None,
                    capture_fn=None, cache_fn=None, pid=None, search_metadata=None):
    """One policy for both queues: collect every corroborated, available component.

    Save runtime-bound main/subpackages and their evidence locally.
    Navigation and baseline updates are handled by the calling collector.
    """
    root = Path(collection_dir).absolute()
    root.mkdir(parents=True, exist_ok=True)
    observations = root / "runtime_observations" / uuid.uuid4().hex
    before = probe.observe(attempt_id, observations / "before", pid=pid)
    binding = select_opened_binding(before, attempt_id)
    if expected_appid is not None and binding["appid"] != expected_appid:
        raise EvidenceError("Opened AppID differs from the requested version target")
    evidence, captured = finish_evidence(
        binding, [], probe.serial, binding["appid"], attempt_id, root,
        getattr(probe, "adb", "adb"), capture_fn=capture_fn, cache_fn=cache_fn)
    after = probe.observe(attempt_id, observations / "after", pid=binding["runtime"]["process_id"])
    final = select_opened_binding(after, attempt_id)
    if (binding_signature(binding) != binding_signature(final)
            or _inventory_signature(binding) != _inventory_signature(final)):
        raise EvidenceError("Focused app, release or module inventory changed during acquisition")
    metadata = binding["metadata"]
    if binding.get("public_metadata_schema") == public_metadata.SCHEMA:
        if final.get("public_metadata_schema") != public_metadata.SCHEMA:
            raise EvidenceError("Public metadata probe schema changed during acquisition")
        metadata, report = public_metadata.combine(binding, final)
        # Save only bounded native records, not arbitrary columns from the input CSV.
        search_metadata = ([record for row in search_metadata
                            if (record := public_metadata.search_record(row))]
                           if isinstance(search_metadata, list) and len(search_metadata) <= 64 else [])
        metadata, report, fallback = public_metadata.apply_search_category(metadata, report, search_metadata)
        evidence["public_metadata_status"] = report
        if fallback:
            source = observations / "search_metadata.json"
            write_json(source, search_metadata)
            evidence["category_fallback"] = dict(fallback, observation={
                "path": str(source.relative_to(root)), "sha256": artifacts.sha256_file(source)})
    evidence.update(collection_schema=PIPELINE_SCHEMA, acquisition_policy="all_available_components",
                    device=probe.serial, observed_metadata=metadata, observations={})
    evidence["metadata_sources"] = [
        {"path": str(path), "sha256": artifacts.sha256_file(path)}
        for path in sorted((observations / "before").glob("*.log"))]
    for phase in ("before", "after"):
        path = observations / phase / "messages.json"
        evidence["observations"][phase] = {"path": str(path.relative_to(root)),
                                          "sha256": artifacts.sha256_file(path)}
    # Exclude unrelated cache candidates, while retaining their original files.
    selected = {item["remote_path"] for item in evidence["packages"]}
    packages = [item for item in captured if item["remote_path"] in selected]
    verify_observations(evidence, root / "evidence.json", packages)
    return evidence, packages


def _confined(root, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise EvidenceError("Observation path must be relative to the collection")
    path = root / relative
    if (not path.resolve().is_relative_to(root.resolve()) or not path.is_file()
            or any(p.is_symlink() for p in (path, *path.parents) if p == root or root in p.parents)):
        raise EvidenceError("Observation missing or outside the collection")
    return path


def verify_observations(evidence, evidence_path, packages):
    """Replay observations offline; historical evidence keeps its original policy."""
    root = Path(evidence_path).absolute().parent
    bindings = []
    for phase in ("before", "after"):
        proof = evidence.get("observations", {}).get(phase, {})
        path = _confined(root, proof.get("path"))
        if artifacts.sha256_file(path) != proof.get("sha256"):
            raise EvidenceError("Runtime observation checksum mismatch")
        bindings.append(select_opened_binding(json.loads(path.read_text()), evidence["attempt_id"]))
    first, last = bindings
    metadata = first["metadata"]
    if first.get("public_metadata_schema") == public_metadata.SCHEMA:
        if last.get("public_metadata_schema") != public_metadata.SCHEMA:
            raise EvidenceError("Public metadata probe schema changed during acquisition")
        metadata, report = public_metadata.combine(first, last)
        if "category_fallback" in evidence:
            fallback = evidence["category_fallback"]
            if not isinstance(fallback, dict) or not isinstance(fallback.get("observation"), dict):
                raise EvidenceError("Invalid category fallback reference")
            reference = fallback["observation"]
            source = _confined(root, reference.get("path"))
            if artifacts.sha256_file(source) != reference.get("sha256"):
                raise EvidenceError("Category source checksum mismatch")
            metadata, report, replay = public_metadata.apply_search_category(
                metadata, report, json.loads(source.read_text()))
            if replay is None or dict(replay, observation=reference) != fallback:
                raise EvidenceError("Category fallback differs from native search evidence")
        if evidence.get("public_metadata_status") != report:
            raise EvidenceError("Public metadata status differs from saved observations")
        first = dict(first, metadata=metadata, public_metadata_status=report)
    elif "public_metadata_status" in evidence or "category_fallback" in evidence:
        raise EvidenceError("Public metadata status lacks versioned observations")
    if (binding_signature(first) != binding_signature(last)
            or _inventory_signature(first) != _inventory_signature(last)
            or first["appid"] != evidence.get("appid")
            or first["main"]["version"] != evidence.get("version")
            or first["metadata"] != evidence.get("observed_metadata")):
        raise EvidenceError("Recorded evidence differs from the observed runtime")

    def no_copy(*args, **kwargs):
        raise EvidenceError("Required runtime package missing from saved acquisition")

    replay, _ = finish_evidence(first, packages, evidence.get("device", ""),
                                first["appid"], evidence["attempt_id"], root,
                                capture_fn=no_copy, cache_fn=lambda *args: {}, record_cache=False)
    def signature(entries):
        return sorted((p["remote_path"], p["sha256"], p["role"], p.get("runtime_md5"),
                       tuple(sorted(p.get("module_roots", [])))) for p in entries)
    if signature(replay["packages"]) != signature(evidence.get("packages", [])):
        raise EvidenceError("Runtime package membership differs from saved acquisition")
    if evidence.get("coverage") == "complete" and replay["coverage"] != "complete":
        raise EvidenceError("Complete coverage is not supported by the original package and runtime")
    return first


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--appid", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--packages-json", type=Path, required=True)
    parser.add_argument("--collection-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frida", required=True)
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--pid", type=int)
    args = parser.parse_args(argv)
    if not APPID_RE.fullmatch(args.appid):
        raise EvidenceError("Invalid AppID")
    collection = (args.collection_dir or args.packages_json.parent).resolve()
    # Collectors stage optional native search records beside the package list.
    search_path = collection / "search_metadata.json"
    search_metadata = (json.loads(_confined(collection, search_path.name).read_text())
                       if search_path.exists() else None)
    evidence, packages = collect_runtime(RuntimeProbe(args.serial, args.frida, args.adb),
                                         args.attempt_id, collection,
                                         expected_appid=args.appid, pid=args.pid, search_metadata=search_metadata)
    write_json(args.packages_json, packages)
    write_json(args.output, evidence)
    print(json.dumps({"appid": evidence["appid"], "version": evidence["version"],
                      "coverage": evidence["coverage"], "packages": len(packages)}))
