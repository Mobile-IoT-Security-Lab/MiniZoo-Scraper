"""Protected CSV and persistent queues. Only this module writes apps.csv."""

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, value):
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


class FileLock:
    """Process lock: one writer per workspace."""

    def __init__(self, path):
        self.path = Path(path)
        self.stream = None

    def __enter__(self):
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+")
        try:
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            self.stream = None
            raise RuntimeError("Another run is already using " + str(self.path))
        return self

    def __exit__(self, *args):
        if self.stream:
            self.stream.close()
            self.stream = None


SEMVER = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?\Z"
)


def compare_versions(candidate, baseline, kind="integer"):
    """-1, 0, 1; None when the two identifiers cannot be compared."""
    if not isinstance(candidate, str) or not isinstance(baseline, str):
        return None
    if kind == "integer":
        if not re.fullmatch(r"[0-9]+", candidate) or not re.fullmatch(r"[0-9]+", baseline):
            return None
        left, right = int(candidate), int(baseline)
        return (left > right) - (left < right)
    if kind != "semver":
        return None
    left, right = SEMVER.fullmatch(candidate), SEMVER.fullmatch(baseline)
    if not left or not right:
        return None
    for match in (left, right):
        for identifier in (match.group(4) or "").split("."):
            if identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0"):
                return None
    left_core = tuple(int(left.group(i)) for i in (1, 2, 3))
    right_core = tuple(int(right.group(i)) for i in (1, 2, 3))
    if left_core != right_core:
        return (left_core > right_core) - (left_core < right_core)
    left_pre, right_pre = left.group(4), right.group(4)
    if left_pre == right_pre:
        return 0
    if left_pre is None or right_pre is None:
        return 1 if left_pre is None else -1
    left_parts, right_parts = left_pre.split("."), right_pre.split(".")
    for a, b in zip(left_parts, right_parts):
        if a == b:
            continue
        if a.isdigit() and b.isdigit():
            return (int(a) > int(b)) - (int(a) < int(b))
        if a.isdigit() != b.isdigit():
            return -1 if a.isdigit() else 1
        return (a > b) - (a < b)
    return (len(left_parts) > len(right_parts)) - (len(left_parts) < len(right_parts))


def read_csv_bytes(data, encoding=None, schema=None):
    if encoding is None:
        if data.startswith(b"\xef\xbb\xbf"):
            encoding = "utf-8-sig"
        elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
            encoding = "utf-16"
        else:
            encoding = "utf-8"
    text = data.decode(encoding, errors="strict")
    if schema is None:
        dialect = csv.Sniffer().sniff(text[:65536], delimiters=",;\t|")
        schema = {"encoding": encoding, "delimiter": dialect.delimiter,
                  "quotechar": dialect.quotechar or '"', "doublequote": True,
                  "escapechar": dialect.escapechar, "skipinitialspace": False,
                  "lineterminator": "\r\n" if "\r\n" in text else "\n"}
    reader = csv.DictReader(io.StringIO(text, newline=""), **{
        key: schema[key] for key in ("delimiter", "quotechar", "doublequote", "escapechar", "skipinitialspace")})
    columns = reader.fieldnames
    if not columns or len(columns) != len(set(columns)) or not {"name", "appid"}.issubset(columns):
        raise ValueError("CSV requires unique name and appid columns")
    rows = list(reader)
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError("CSV has an inconsistent field count")
    schema = dict(schema, columns=columns)
    return rows, schema


class Store:
    def __init__(self, config):
        self.config = config
        self.source = Path(config["source_csv"]).resolve()
        if Path(config["work_csv"]).is_symlink() or Path(config["output_dir"]).is_symlink():
            raise ValueError("Working copy and output cannot be symbolic links")
        self.work = Path(config["work_csv"]).resolve()
        self.output = Path(config["output_dir"]).resolve()
        if self.source == self.work or (self.source.exists() and self.work.exists()
                                        and os.path.samefile(self.source, self.work)):
            raise ValueError("Source and working copy must differ")
        self.manifest_path = self.output / "initial_manifest.json"
        self.backup = self.output / "checkpoints" / "apps.initial.csv"
        self.version_column = config.get("version_column", "previous_app_version")
        self.version_kind = config.get("version_kind", "integer")
        self.lock = threading.RLock()
        self.conn = None
        self.recovered_decisions = []

    def open_readonly(self):
        """Read state/reports without recovering tasks or modifying the checkpoint."""
        self.initial = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if self.initial.get("source_csv") != str(self.source) or self.initial.get("work_csv") != str(self.work):
            raise ValueError("Manifest incompatible with configured paths")
        self.schema = self.initial["schema"]
        uri = (self.output / "state.sqlite3").as_uri() + "?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA query_only=ON")
        return self

    def initialize(self):
        with self.lock:
            self._check_destinations()
            data = self.source.read_bytes()
            source_hash = hashlib.sha256(data).hexdigest()
            rows, schema = read_csv_bytes(data, self.config.get("encoding"))
            counts = Counter(row["appid"] for row in rows if row["appid"].strip())
            duplicates = {key: count for key, count in counts.items() if count > 1}
            anomalies = {"expected_records": self.config.get("expected_records"),
                         "actual_records": len(rows), "distinct_appids": len(counts),
                         "duplicate_appids": duplicates,
                         "missing_rows": [i for i, row in enumerate(rows)
                                          if not row["appid"].strip() or not row["name"].strip()],
                         "invalid_appid_rows": [i for i, row in enumerate(rows)
                                                if not re.fullmatch(r"wx[0-9a-f]{16}", row["appid"])],
                         "version_column_missing": self.version_column not in schema["columns"]}
            self.output.mkdir(parents=True, exist_ok=True)
            for directory in ("collections", "reports", "checkpoints", "logs"):
                (self.output / directory).mkdir(exist_ok=True)
            if self.manifest_path.exists():
                self.initial = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                expected = {"source_csv": str(self.source), "source_sha256": source_hash,
                            "work_csv": str(self.work), "schema": schema,
                            "version_column": self.version_column, "version_kind": self.version_kind}
                if any(self.initial.get(key) != value for key, value in expected.items()):
                    raise ValueError("Incompatible provenance/configuration: CSV unchanged")
                if not self.work.exists() and self.initial.get("work_sha256") != source_hash:
                    raise ValueError("Updated working copy missing: automatic restoration refused")
            else:
                if self.work.exists():
                    raise ValueError("apps.csv exists without a verifiable manifest; refusing to overwrite it")
                self.initial = {"created_at": now(), "source_csv": str(self.source),
                                "source_sha256": source_hash, "work_csv": str(self.work),
                                "work_sha256": source_hash, "record_count": len(rows),
                                "schema": schema, "anomalies": anomalies,
                                "version_column": self.version_column, "version_kind": self.version_kind,
                                "configuration": self._public_config()}
                write_json(self.manifest_path, self.initial)
            self.schema = schema
            self.source_rows = rows
            if not self.work.exists():
                atomic_write(self.work, data)
            self._verify_work()
            if self.backup.is_symlink() or (self.backup.exists() and os.path.samefile(self.source, self.backup)):
                raise ValueError("The initial backup must be independent of the source")
            if not self.backup.exists():
                atomic_write(self.backup, data)
            if sha256(self.backup) != source_hash:
                raise ValueError("Initial backup differs from source")
            self.conn = sqlite3.connect(str(self.output / "state.sqlite3"), check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.conn.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS tasks (
                    row_index INTEGER PRIMARY KEY, appid TEXT NOT NULL, name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    manifest TEXT, error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS collections (
                    id INTEGER PRIMARY KEY, manifest TEXT UNIQUE NOT NULL,
                    manifest_sha256 TEXT NOT NULL, recorded_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS csv_decisions (
                    collection_id TEXT PRIMARY KEY, row_index INTEGER NOT NULL,
                    appid TEXT NOT NULL, result TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS task_history (
                    id INTEGER PRIMARY KEY, row_index INTEGER NOT NULL,
                    appid TEXT NOT NULL, status TEXT NOT NULL, manifest TEXT,
                    attempts INTEGER NOT NULL, error TEXT NOT NULL, requeued_at TEXT NOT NULL);
            """)
            with self.conn:
                for decision in self.recovered_decisions:
                    self._save_decision(decision)
                missing_rows = set(anomalies["missing_rows"])
                invalid_appids = set(anomalies["invalid_appid_rows"])
                for index, row in enumerate(rows):
                    error = "" if index not in missing_rows else "Missing name/appid"
                    if index in invalid_appids:
                        error = "Invalid AppID format: expected wx followed by 16 hexadecimal digits"
                    if row["appid"] in duplicates:
                        error = "Duplicate AppID: ambiguous association"
                    status = "invalid_record" if error else "pending"
                    self.conn.execute("INSERT OR IGNORE INTO tasks(row_index,appid,name,status,error,updated_at) VALUES(?,?,?,?,?,?)",
                                      (index, row["appid"], row["name"], status, error, now()))
                self.conn.execute("UPDATE tasks SET status='pending',updated_at=? WHERE status='running'", (now(),))
            if self.recovered_decisions:
                write_json(self.manifest_path, self.initial)
                self.recovered_decisions.clear()
            recorded = self.conn.execute("SELECT row_index,appid,name FROM tasks ORDER BY row_index").fetchall()
            if [(r["appid"], r["name"]) for r in recorded] != [(r["appid"], r["name"]) for r in rows]:
                raise ValueError("SQLite checkpoint incompatible with source list")
            return dict(self.initial)

    def _public_config(self):
        # Do not copy credentials or tokens into publishable manifests.
        keys = ("source_csv", "work_csv", "output_dir", "version_column", "version_kind",
                "expected_records", "encoding", "devices", "pacing")
        return {key: self.config[key] for key in keys if key in self.config}

    def _verify_work(self):
        self._check_destinations()
        actual = sha256(self.work)
        pending = self.initial.get("pending_write")
        if pending:
            if actual not in (pending["before_sha256"], pending["after_sha256"]):
                raise ValueError("CSV changed outside the pipeline during an update")
            self.initial["work_sha256"] = actual
            decision = pending.get("decision")
            if actual == pending["after_sha256"] and decision:
                if self.conn is None:
                    self.recovered_decisions.append(decision)
                else:
                    with self.conn:
                        self._save_decision(decision)
            self.initial.pop("pending_write")
            # The journal remains persisted on disk until SQLite opens.
            if self.conn is not None or not decision or actual != pending["after_sha256"]:
                write_json(self.manifest_path, self.initial)
        elif actual != self.initial["work_sha256"]:
            raise ValueError("CSV changed outside the pipeline: unverifiable provenance")
        rows, schema = read_csv_bytes(self.work.read_bytes(), self.schema["encoding"], self.schema)
        if schema != self.schema or len(rows) != len(self.source_rows):
            raise ValueError("Working-copy schema or record count changed")
        if [row["appid"] for row in rows] != [row["appid"] for row in self.source_rows]:
            raise ValueError("Working-copy order or AppIDs changed")
        return rows

    def _check_destinations(self):
        if self.work.is_symlink() or self.output.is_symlink():
            raise ValueError("Working copy and output cannot be symbolic links")
        if self.work.exists() and os.path.samefile(self.source, self.work):
            raise ValueError("Source and working copy must differ")

    def claim(self):
        with self.lock, self.conn:
            row = self.conn.execute("SELECT * FROM tasks WHERE status='pending' ORDER BY row_index LIMIT 1").fetchone()
            if row is None:
                return None
            self.conn.execute("UPDATE tasks SET status='running',attempts=attempts+1,updated_at=? WHERE row_index=?",
                              (now(), row["row_index"]))
            return dict(self.conn.execute("SELECT * FROM tasks WHERE row_index=?", (row["row_index"],)).fetchone())

    def save_task(self, row_index, status, manifest=None, error=""):
        with self.lock, self.conn:
            self.conn.execute("UPDATE tasks SET status=?,manifest=COALESCE(?,manifest),error=?,updated_at=? WHERE row_index=?",
                              (status, str(manifest) if manifest is not None else None, error, now(), row_index))

    def record_collection(self, manifest_path):
        """Pin a completed local manifest without changing its bytes or location."""
        with self.lock, self.conn:
            local = Path(manifest_path).absolute()
            if (local.is_symlink() or local.resolve() != local or not local.is_file()
                    or self.output not in local.parents):
                raise ValueError("Collection manifest missing or outside the workspace")
            path, checksum = str(local), sha256(local)
            existing = self.conn.execute("SELECT * FROM collections WHERE manifest=?", (path,)).fetchone()
            if existing is not None:
                if existing["manifest_sha256"] != checksum:
                    raise ValueError("Manifest changed after local registration")
                return existing["id"]
            cursor = self.conn.execute("INSERT INTO collections(manifest,manifest_sha256,recorded_at) VALUES(?,?,?)",
                                       (path, checksum, now()))
            return cursor.lastrowid

    def collections(self):
        """Read pinned manifests and report missing or changed local files."""
        with self.lock:
            records = [dict(row) for row in self.conn.execute("SELECT * FROM collections ORDER BY id")]
            for record in records:
                record["integrity_error"] = ""
                path = Path(record["manifest"])
                if path.is_symlink() or path.resolve() != path or not path.is_file():
                    record["integrity_error"] = "Collection manifest missing or path invalid"
                elif sha256(path) != record["manifest_sha256"]:
                    record["integrity_error"] = "Manifest changed after local registration"
            return records

    def requeue(self, appids):
        """Reopen explicitly selected outcomes while preserving all local collections."""
        if (not isinstance(appids, (list, tuple)) or not appids
                or any(not isinstance(appid, str) or not re.fullmatch(r"wx[0-9a-f]{16}", appid) for appid in appids)
                or len(set(appids)) != len(appids)):
            raise ValueError("Specify a nonempty list of valid, unique AppIDs")
        allowed = {"partial", "not_found", "ambiguous", "collection_failed", "decompilation_failed",
                   "search_failed", "retry_exhausted", "rate_limited", "checkpoint_failed", "csv_conflict"}
        with self.lock, self.conn:
            selected = []
            for appid in appids:
                matches = self.conn.execute("SELECT * FROM tasks WHERE appid=?", (appid,)).fetchall()
                if len(matches) != 1:
                    raise ValueError("AppID missing or duplicated in dataset: " + appid)
                row = dict(matches[0])
                if row["status"] not in allowed:
                    raise ValueError("State cannot be requeued for " + appid + ": " + row["status"])
                selected.append(row)
            results = []
            for row in selected:
                timestamp = now()
                cursor = self.conn.execute(
                    "INSERT INTO task_history(row_index,appid,status,manifest,attempts,error,requeued_at) VALUES(?,?,?,?,?,?,?)",
                    (row["row_index"], row["appid"], row["status"], row["manifest"], row["attempts"], row["error"], timestamp))
                self.conn.execute("UPDATE tasks SET status='pending',manifest=NULL,error='',updated_at=? WHERE row_index=?",
                                  (timestamp, row["row_index"]))
                results.append({"appid": row["appid"], "row_index": row["row_index"],
                                "previous_status": row["status"], "previous_manifest": row["manifest"],
                                "history_id": cursor.lastrowid})
            return {"count": len(results), "appids": list(appids), "rows": results,
                    "history_ids": [row["history_id"] for row in results],
                    "history_db": str(self.output / "state.sqlite3")}


    def _verify_artifacts(self, manifest):
        if not manifest.get("identity_verified") or not manifest.get("artifacts_verified"):
            return False
        if manifest.get("coverage") != "complete":
            return False
        if not manifest.get("packages") or not manifest.get("decompiled"):
            return False
        for artifact in manifest["packages"] + manifest["decompiled"]:
            path = Path(artifact["local_path"])
            if not path.is_file() or path.is_symlink() or self.output not in path.resolve().parents:
                return False
            if artifact.get("bytes") is not None and path.stat().st_size != artifact["bytes"]:
                return False
            if sha256(path) != artifact.get("sha256"):
                return False
        return True

    def apply_collection(self, manifest):
        """Reread the baseline and recheck hashes before an atomic write."""
        with self.lock:
            rows = self._verify_work()
            index = manifest.get("row_index")
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(rows):
                raise ValueError("Invalid record index")
            row = rows[index]
            previous = row.get(self.version_column, "")
            version = manifest.get("version")
            result = {"previous_version": previous, "version": version}
            duplicate = manifest.get("appid") in self.initial["anomalies"]["duplicate_appids"]
            if row["appid"] != manifest.get("appid") or duplicate or not re.fullmatch(r"wx[0-9a-f]{16}", row["appid"]):
                return dict(result, status="ambiguous")
            if not self._verify_artifacts(manifest):
                return dict(result, status="artifacts_unverified")
            # Rehashing packages may take time: reread the row here.
            rows = self._verify_work()
            row = rows[index]
            previous = row.get(self.version_column, "")
            result["previous_version"] = previous
            collection_id = manifest.get("collection_id")
            if collection_id:
                recorded = self.conn.execute("SELECT * FROM csv_decisions WHERE collection_id=?", (collection_id,)).fetchone()
                if recorded:
                    prior = json.loads(recorded["result"])
                    if recorded["row_index"] != index or recorded["appid"] != row["appid"] or prior["version"] != version:
                        raise ValueError("collection_id reused for different data")
                    return prior
            if not previous:
                return dict(result, status="baseline_missing")
            if manifest.get("version_kind") != self.version_kind:
                return dict(result, status="version_unknown")
            comparison = compare_versions(version, previous, self.version_kind)
            if comparison is None:
                return dict(result, status="version_unknown")
            if comparison <= 0:
                return dict(result, status="unchanged" if comparison == 0 else "older")
            row[self.version_column] = version
            metadata = manifest.get("metadata", {})
            for column in self.config.get("version_metadata_columns", []):
                if column in row and column not in ("appid", "name", self.version_column):
                    row[column] = ""
            for column, value in metadata.items():
                if column in row and column not in ("appid", self.version_column):
                    row[column] = str(value) if value is not None else ""
            stream = io.StringIO(newline="")
            writer = csv.DictWriter(stream, fieldnames=self.schema["columns"], **{
                key: self.schema[key] for key in ("delimiter", "quotechar", "doublequote", "escapechar", "lineterminator")})
            writer.writeheader()
            writer.writerows(rows)
            data = stream.getvalue().encode(self.schema["encoding"])
            decision = {"collection_id": collection_id, "row_index": index,
                        "appid": row["appid"], "result": dict(result, status="updated")}
            # The journal enables recovery after a crash between replace and commit.
            self.initial["pending_write"] = {"before_sha256": self.initial["work_sha256"],
                                              "after_sha256": hashlib.sha256(data).hexdigest(),
                                              "row_index": index, "created_at": now(),
                                              "decision": decision if collection_id else None}
            write_json(self.manifest_path, self.initial)
            atomic_write(self.work, data)
            if collection_id:
                with self.conn:
                    self._save_decision(decision)
            self.initial["work_sha256"] = self.initial.pop("pending_write")["after_sha256"]
            write_json(self.manifest_path, self.initial)
            return dict(result, status="updated")

    def _save_decision(self, decision):
        self.conn.execute("INSERT OR IGNORE INTO csv_decisions(collection_id,row_index,appid,result,updated_at) VALUES(?,?,?,?,?)",
                          (decision["collection_id"], decision["row_index"], decision["appid"],
                           json.dumps(decision["result"], ensure_ascii=False), now()))

    def report(self):
        with self.lock:
            tasks = [dict(row) for row in self.conn.execute("SELECT * FROM tasks ORDER BY row_index")]
            collections = self.collections()
            current_source_hash = sha256(self.source)
            decisions = [dict(row) for row in self.conn.execute("SELECT * FROM csv_decisions ORDER BY updated_at")]
            for decision in decisions:
                decision["result"] = json.loads(decision["result"])
            return {"generated_at": now(), "records": self.initial["record_count"],
                    "distinct_appids": self.initial["anomalies"]["distinct_appids"],
                    "anomalies": self.initial["anomalies"],
                    "task_counts": dict(Counter(task["status"] for task in tasks)),
                    "collection_count": len(collections),
                    "source_sha256": current_source_hash,
                    "source_unchanged": current_source_hash == self.initial["source_sha256"],
                    "paths": {"source_csv": str(self.source), "work_csv": str(self.work),
                              "backup": str(self.backup), "output": str(self.output),
                              "checkpoint": str(self.output / "state.sqlite3")},
                    "tasks": tasks, "collections": collections, "csv_decisions": decisions}

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None
