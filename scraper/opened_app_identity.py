"""Bind the actually focused Mini Program to its package bytes.

Typed runtime readers adapted from the project's version collector. Discovery
never receives the CSV's requested AppID; original logs and package bytes are
retained. No device navigation, cache deletion or package JavaScript execution.
"""
import ast
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path, PurePosixPath

CACHE_ROOT = "/data/data/com.tencent.mm/MicroMsg/appbrand/pkg"
SCHEMA = "opened_app_v1"
RUNTIME_CLASS = "com.tencent.mm.plugin.appbrand.AppBrandRuntime"
APPID_RE = re.compile(r"wx[0-9a-f]{16}")
METADATA_FIELDS = ("appid", "name", "category", "developer", "username", "description", "icon_url", "app_version")

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
    if (not path.startswith(CACHE_ROOT + "/") or ".." in PurePosixPath(path).parts
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


def _select_binding(messages, appid, attempt_id):
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
    binding["public_metadata"] = [
        m for m in messages if m.get("kind") == "runtime-public-metadata"
        and m.get("appid") == appid and m.get("attempt_id") == attempt_id
        and m.get("process_id") == binding["runtime"]["process_id"]
        and m.get("metadata_class") == "com.tencent.mm.plugin.appbrand.config.WxaAttributes"
        and m.get("identity_field", {}).get("field") == "field_appId"
        and m.get("identity_field", {}).get("value") == appid]
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


def write_json(path, value):
    """Publish a durable, complete receipt without replacing existing evidence."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
        script.write_text("globalThis.PROBE_ATTEMPT_ID = " + json.dumps(attempt_id) + ";\n" + source,
                          encoding="utf-8")
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
                (directory / (str(process_id) + ".log")).write_text(output, encoding="utf-8")
                records = read_messages(output)
                if process.returncode or not any(r.get("kind") == "metadata-probe-complete" for r in records):
                    failures.append(process_id)
                else:
                    messages.extend(records)
            except subprocess.TimeoutExpired:
                failures.append(process_id)
        write_json(directory / "messages.json", messages)
        if failures:
            raise EvidenceError("Runtime observation incomplete for processes: " + str(failures))
        return messages


class PackageUnavailable(EvidenceError):
    pass


def package_declarations(binding, maximum=None):
    if maximum is not None and maximum < 1:
        raise EvidenceError("packages-per-app must be positive")
    declarations = [dict(binding["main"], role="main")]
    modules, limits = declared_modules(binding)
    seen = {binding["main"]["remote_path"]}
    for module in modules:
        path = module.get("runtime_path")
        if not path:
            limits.append("No runtime cache path for module: " + module["module_root"])
            continue
        if maximum is not None and len(declarations) >= maximum:
            limits.append("Configured package limit reached")
            break
        if path in seen:
            continue
        seen.add(path)
        declarations.append(dict(module, remote_path=path, role="subpackage"))
    return declarations, sorted(set(limits))




def confined_file(root, relative):
    root = Path(root).absolute()
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()) or any(
            p.is_symlink() for p in (path, *path.parents) if p == root or root in p.parents):
        raise EvidenceError("Evidence or package path escapes run or uses a symlink")
    if not path.is_file():
        raise EvidenceError("Evidence or package file is missing")
    return path


def load_verified_receipt(record, run_dir):
    """Replay the typed binding and hash captured bytes before any downstream attribution."""
    run_dir = Path(run_dir).absolute()
    if record.get("collection_schema") != SCHEMA or record.get("package_attribution") != "runtime_verified":
        raise EvidenceError("No runtime-verified collection receipt")
    evidence = confined_file(run_dir, record.get("runtime_evidence_path", ""))
    data = evidence.read_bytes()
    if hashlib.sha256(data).hexdigest() != record.get("runtime_evidence_sha256"):
        raise EvidenceError("Runtime evidence checksum mismatch")
    receipt = json.loads(data)
    if (receipt.get("schema") != SCHEMA or receipt.get("attempt_id") != record.get("attempt_id")
            or receipt.get("device_serial") != record.get("device_serial")):
        raise EvidenceError("Runtime receipt does not belong to this attempt/device")
    bindings = []
    for phase in ("before", "after"):
        raw = confined_file(evidence.parent, receipt[phase]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != receipt.get(phase + "_sha256"):
            raise EvidenceError("Runtime observation checksum mismatch")
        messages = json.loads(raw)
        bindings.append(select_opened_binding(messages, receipt["attempt_id"]))
    before, after = bindings
    if (binding_signature(before) != binding_signature(after)
            or receipt.get("appid") != before["appid"] or record.get("appid") != before["appid"]
            or receipt.get("metadata") != before["metadata"]):
        raise EvidenceError("Runtime identity or metadata differs from captured app")
    for phase in bindings:
        declarations, _ = package_declarations(phase, receipt["maximum_packages"])
        valid = {(d["remote_path"], d["md5"], d["role"]) for d in declarations}
        if any((p["remote_source"], p["md5"], p["role"]) not in valid for p in receipt["packages"]):
            raise EvidenceError("Package does not belong to the observed runtime inventory")
    packages = []
    for item in receipt["packages"]:
        path = confined_file(evidence.parent, item["local_file"])
        raw = path.read_bytes()
        if (hashlib.sha256(raw).hexdigest() != item["sha256"]
                or hashlib.md5(raw).hexdigest() != item["md5"] or len(raw) != item["bytes"]):
            raise EvidenceError("Captured bytes differ from runtime-bound receipt")
        packages.append(dict(item, path=path))
    if not packages or sum(p["role"] == "main" for p in packages) != 1:
        raise EvidenceError("Receipt must contain exactly one bound main package")
    expected = [(str(p["path"].relative_to(Path(run_dir))), p["sha256"], p["remote_source"]) for p in packages]
    recorded = list(zip(*(record.get(k, "").split(";") for k in ("local_files", "sha256", "remote_sources"))))
    if expected != recorded or any(len(record.get(k, "").split(";")) != len(packages)
                                   for k in ("local_files", "sha256", "remote_sources")):
        raise EvidenceError("Manifest package list differs from runtime-bound receipt")
    return receipt, packages


# Historical receipts retain the parser/policy under which they were captured.
# New acquisitions from either queue use the same shared implementation.
_load_legacy_receipt = load_verified_receipt
import minizoo_core_bootstrap
from minizoo_collection import runtime as _runtime, artifacts as _artifacts
EvidenceError = _runtime.EvidenceError
RuntimeProbe = _runtime.RuntimeProbe


class PackageUnavailable(EvidenceError):
    pass


def collect_opened_app(probe, attempt_id, directory, pull=None, maximum=None, *, search_metadata=None):
    """Collect all available components. The old numeric argument is ignored."""
    directory = Path(directory).absolute()
    directory.mkdir(parents=True, exist_ok=False)

    def capture(serial, paths, root, adb="adb"):
        if pull is None:
            return _artifacts.capture_selected(serial, paths, root, adb)
        temporary = directory / "copy_attempts" / uuid.uuid4().hex
        copied = pull(paths, temporary)
        remotes = {canonical_path(remote) for remote, _ in copied}
        result = []
        for index, remote in enumerate(paths, 1):
            source = temporary / (str(index).zfill(2) + "_" + PurePosixPath(remote).name)
            if remote not in remotes or not source.is_file() or source.is_symlink():
                raise PackageUnavailable("Runtime-bound package could not be retrieved")
            data = source.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            target = directory / "packages" / (digest + ".wxapkg")
            target.parent.mkdir(parents=True, exist_ok=True)
            _artifacts._immutable(target, data)
            result.append({"remote_path": remote, "local_path": str(target),
                           "sha256": digest, "bytes": len(data)})
        return result

    evidence, captured = _runtime.collect_runtime(probe, attempt_id, directory, capture_fn=capture,
                                                  search_metadata=search_metadata)
    common_path = directory / "evidence.json"
    write_json(common_path, evidence)
    checked = _artifacts.validate_evidence(common_path, evidence["appid"], captured, attempt_id)
    packages = [{"remote_source": p["remote_path"],
                 "local_file": str(Path(p["local_path"]).relative_to(directory)),
                 "sha256": p["sha256"], "md5": hashlib.md5(Path(p["local_path"]).read_bytes()).hexdigest(),
                 "bytes": p["bytes"], "role": p["role"], "runtime_md5": p["runtime_md5"]}
                for p in checked["packages"]]
    receipt = {"schema": SCHEMA, "attempt_id": attempt_id, "device_serial": probe.serial,
               "appid": evidence["appid"], "metadata": evidence["observed_metadata"],
               "package_attribution": "runtime_verified", "observed_at": evidence["runtime_observed_at"],
               "collection_time": dt.datetime.now(dt.timezone.utc).isoformat(),
               "packages": packages, "maximum_packages": None,
               "acquisition_policy": "all_available_components",
               "coverage": evidence["coverage"], "coverage_limits": evidence["coverage_limits"],
               "common_evidence_path": "evidence.json", "common_evidence_sha256": _artifacts.sha256_file(common_path)}
    if "public_metadata_status" in evidence:
        receipt["public_metadata_status"] = evidence["public_metadata_status"]
    for phase in ("before", "after"):
        receipt[phase] = evidence["observations"][phase]["path"]
        receipt[phase + "_sha256"] = evidence["observations"][phase]["sha256"]
    path = directory / "opened_app_evidence.json"
    write_json(path, receipt)
    return path, receipt


def load_verified_receipt(record, run_dir):
    evidence = confined_file(run_dir, record.get("runtime_evidence_path", ""))
    receipt = json.loads(evidence.read_text())
    if "common_evidence_path" not in receipt:
        return _load_legacy_receipt(record, run_dir)
    if (record.get("collection_schema") != SCHEMA or record.get("package_attribution") != "runtime_verified"
            or _artifacts.sha256_file(evidence) != record.get("runtime_evidence_sha256")
            or receipt.get("schema") != SCHEMA or receipt.get("attempt_id") != record.get("attempt_id")
            or receipt.get("device_serial") != record.get("device_serial")):
        raise EvidenceError("Runtime receipt does not belong to this attempt/device or changed")
    common_path = confined_file(evidence.parent, receipt["common_evidence_path"])
    if _artifacts.sha256_file(common_path) != receipt.get("common_evidence_sha256"):
        raise EvidenceError("Common evidence checksum mismatch")
    common = json.loads(common_path.read_text())
    if (common.get("appid") != record.get("appid") or receipt.get("appid") != record.get("appid")
            or common.get("observed_metadata") != receipt.get("metadata")
            or common.get("runtime_observed_at") != receipt.get("observed_at")
            or common.get("device") != record.get("device_serial")
            or receipt.get("coverage") != common.get("coverage")
            or receipt.get("coverage_limits") != common.get("coverage_limits")
            or receipt.get("public_metadata_status") != common.get("public_metadata_status")):
        raise EvidenceError("Runtime receipt identity, metadata or coverage differs")
    if "collection_time" in receipt:
        try:
            timestamp = dt.datetime.fromisoformat(receipt["collection_time"].replace("Z", "+00:00"))
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError("Timestamp has no timezone")
        except (ValueError, TypeError, AttributeError) as error:
            raise EvidenceError("Invalid recorded collection time") from error
    packages = []
    for item in receipt["packages"]:
        path = confined_file(evidence.parent, item["local_file"])
        if hashlib.md5(path.read_bytes()).hexdigest() != item["md5"]:
            raise EvidenceError("Captured bytes differ from runtime-bound receipt")
        packages.append(dict(item, path=path))
    converted = [{"remote_path": p["remote_source"], "local_path": str(p["path"]),
                  "sha256": p["sha256"], "bytes": p["bytes"]} for p in packages]
    try:
        checked = _artifacts.validate_evidence(common_path, receipt["appid"], converted, receipt["attempt_id"])
    except _artifacts.ArtifactError as error:
        raise EvidenceError(str(error)) from error
    if [(p["remote_source"], p["role"]) for p in packages] != [(p["remote_path"], p["role"]) for p in checked["packages"]]:
        raise EvidenceError("Runtime package roles differ")
    expected = [(str(p["path"].relative_to(Path(run_dir))), p["sha256"], p["remote_source"]) for p in packages]
    fields = ("local_files", "sha256", "remote_sources")
    recorded = list(zip(*(record.get(k, "").split(";") for k in fields)))
    if expected != recorded or any(len(record.get(k, "").split(";")) != len(packages) for k in fields):
        raise EvidenceError("Manifest package list differs from runtime-bound receipt")
    return receipt, packages
