"""Private, versioned snapshots: SQLite checkpoints plus portable JSON reports."""

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from spottoyt.auth import SetupError
from spottoyt.config import data_directory

SCHEMA_VERSION = 1


def private_root():
    root = data_directory().resolve()
    project = Path(__file__).resolve().parents[2]
    if root == project or project in root.parents or any("onedrive" in part.lower() for part in root.parts):
        raise SetupError("Archive storage must be outside the project and OneDrive. "
                         "Remove SPOTTOYT_DATA_DIR to use the private default.")
    return root


def prepare_private_storage():
    root = private_root()
    root.mkdir(parents=True, exist_ok=True)
    # This machine also has a Git repository at the user-profile level. Protect
    # private archives from accidental inclusion there, not just in this project.
    ignore = root / ".gitignore"
    if not ignore.exists():
        with ignore.open("x", encoding="utf-8") as output:
            output.write("# Spottoyt private account data: never publish.\n*\n")
    elif "*" not in ignore.read_text(encoding="utf-8").splitlines():
        raise SetupError("Private storage has a conflicting .gitignore; refusing to archive without protection.")
    return root


class Archive:
    def __init__(self, path):
        self.path = Path(path)
        self.db = sqlite3.connect(self.path / "archive.sqlite3")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS records (
                collection TEXT NOT NULL, position INTEGER NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(collection, position));
            CREATE TABLE IF NOT EXISTS coverage (
                collection TEXT PRIMARY KEY, status TEXT NOT NULL, expected INTEGER,
                detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS imports (
                sha256 TEXT PRIMARY KEY, filename TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS original_files (
                sha256 TEXT PRIMARY KEY, content BLOB NOT NULL);
        """)

    @classmethod
    def create(cls, source):
        root = prepare_private_storage() / "archives"
        root.mkdir(parents=True, exist_ok=True)
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        path = root / name
        path.mkdir(mode=0o700)
        archive = cls(path)
        with archive.db:
            for key, value in {"schema_version": SCHEMA_VERSION, "source": source,
                               "created_at": datetime.now(timezone.utc).isoformat(),
                               "state": "in_progress"}.items():
                archive.db.execute("INSERT INTO metadata VALUES (?, ?)", (key, json.dumps(value)))
        return archive

    def coverage(self, collection, status, expected=None, detail=""):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO coverage VALUES (?,?,?,?)",
                            (collection, status, expected, detail))

    def append(self, collection, items):
        start = self.db.execute("SELECT COUNT(*) FROM records WHERE collection=?", (collection,)).fetchone()[0]
        with self.db:
            self.db.executemany("INSERT INTO records VALUES (?,?,?)",
                                ((collection, start + index, json.dumps(item, ensure_ascii=False))
                                 for index, item in enumerate(items)))

    def items(self, collection):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT payload FROM records WHERE collection=? ORDER BY position", (collection,))]

    def import_blob(self, filename, raw, payload, status):
        digest = hashlib.sha256(raw).hexdigest()
        with self.db:
            result = self.db.execute("INSERT OR IGNORE INTO imports VALUES (?,?,?,?)",
                                     (digest, filename, json.dumps(payload, ensure_ascii=False), status))
            if result.rowcount:
                self.db.execute("INSERT INTO original_files VALUES (?,?)", (digest, raw))
        return result.rowcount > 0

    def finish(self, state):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('state',?)", (json.dumps(state),))
        self.write_reports()

    def write_reports(self):
        meta = {key: json.loads(value) for key, value in self.db.execute("SELECT * FROM metadata")}
        coverage = []
        for collection, status, expected, detail in self.db.execute("SELECT * FROM coverage ORDER BY collection"):
            count = self.db.execute("SELECT COUNT(*) FROM records WHERE collection=?", (collection,)).fetchone()[0]
            coverage.append({"collection": collection, "status": status, "captured": count,
                             "expected": expected, "detail": detail})
        names = [row[0] for row in self.db.execute("SELECT DISTINCT collection FROM records ORDER BY collection")]
        imports = [{"sha256": digest, "filename": name, "status": status, "data": json.loads(payload)}
                   for digest, name, payload, status in self.db.execute("SELECT * FROM imports ORDER BY filename")]
        snapshot = {"metadata": meta, "coverage": coverage,
                    "collections": {name: self.items(name) for name in names}, "account_files": imports}
        inventory = {"metadata": meta, "coverage": coverage,
                     "account_files": [{k: v for k, v in item.items() if k != "data"} for item in imports]}
        for filename, data in (("snapshot.json", snapshot), ("inventory.json", inventory)):
            temp = self.path / (filename + ".tmp")
            with temp.open("w", encoding="utf-8") as output:
                json.dump(data, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, self.path / filename)
        lines = ["# Spotify archive inventory", "", f"State: {meta['state']}", "",
                 "| Collection | Captured | Expected | Status |", "|---|---:|---:|---|"]
        lines.extend(f"| {row['collection']} | {row['captured']} | {row['expected'] if row['expected'] is not None else 'unknown'} | {row['status']} |" for row in coverage)
        lines.extend(["", "History and social data require Spotify account-data downloads.",
                      "Counts reflect returned records, not independent proof of full account coverage.",
                      "This archive is private and is not encrypted by Spottoyt."])
        (self.path / "inventory.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def close(self):
        self.db.close()
