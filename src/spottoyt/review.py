"""Offline review storage. Decisions never modify source reports or accounts."""

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from spottoyt.archive import prepare_private_storage, private_root
from spottoyt.auth import SetupError

REPORT_FOLDERS = {"music": "matching", "podcasts": "podcast-matching"}
RUN_ID = re.compile(r"\d{8}T\d{6}Z-[a-f0-9]{8}")


def load_report(kind, run_id):
    if kind not in REPORT_FOLDERS or not RUN_ID.fullmatch(run_id):
        raise SetupError("Choose a valid local review report.")
    root = private_root().resolve()
    path = (root / REPORT_FOLDERS[kind] / run_id / "report.json").resolve()
    if root not in path.parents or not path.is_file() or path.stat().st_size > 50 * 1024 * 1024:
        raise SetupError("Review report is missing, outside private storage, or too large.")
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("results"), list):
            raise ValueError
        seen = set()
        for result in data["results"]:
            key = result["source_key"]
            if not isinstance(key, str) or key in seen:
                raise ValueError
            seen.add(key)
            metadata = result["song" if kind == "music" else "podcast"]
            if not isinstance(metadata, dict) or not isinstance(metadata.get("title"), str):
                raise ValueError
            if kind == "podcasts" and metadata.get("kind") not in ("show", "episode"):
                raise ValueError
            if not isinstance(result.get("candidates"), list):
                raise ValueError
            for candidate in result["candidates"]:
                if not isinstance(candidate, dict):
                    raise ValueError
                validate_target(candidate.get("video_id", candidate.get("target_id")),
                                "song" if kind == "music" else metadata["kind"])
    except (ValueError, TypeError, KeyError):
        raise SetupError("This report has an unsupported or damaged structure. Regenerate it before review.") from None
    return {"kind": kind, "run_id": run_id, "fingerprint": hashlib.sha256(raw).hexdigest(), "data": data}


def list_reports(kind):
    reports, errors = [], 0
    for path in sorted((private_root() / REPORT_FOLDERS[kind]).glob("*/report.json"), reverse=True):
        try:
            reports.append(load_report(kind, path.parent.name))
        except (SetupError, OSError):
            errors += 1
    return reports, errors


def inventories():
    rows = []
    root = private_root().resolve()
    for path in sorted((root / "archives").glob("*/inventory.json"), reverse=True):
        if root not in path.resolve().parents:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["metadata"]["schema_version"] != 1:
                continue
            rows.append({"id": path.parent.name, "data": data})
        except (ValueError, KeyError, TypeError, OSError):
            continue
    return rows


def source_kind(report, result):
    return "song" if report["kind"] == "music" else result["podcast"]["kind"]


def validate_target(identifier, kind):
    if not isinstance(identifier, str) or kind not in ("song", "episode", "show"):
        raise SetupError("The selected YouTube Music target has an invalid type or identifier.")
    if kind == "show":
        valid = re.fullmatch(r"MPSP[A-Za-z0-9_-]{8,100}", identifier)
    else:
        valid = re.fullmatch(r"[A-Za-z0-9_-]{11}", identifier)
    if not valid:
        raise SetupError("The selected YouTube Music target has an invalid identifier.")
    return identifier


def listen_url(identifier, kind):
    validate_target(identifier, kind)
    return ("https://music.youtube.com/browse/" if kind == "show" else "https://music.youtube.com/watch?v=") + identifier


def manual_target(url, kind):
    try:
        parsed = urlsplit(url.strip())
        if (parsed.scheme != "https" or parsed.hostname not in {"music.youtube.com", "youtube.com", "www.youtube.com", "youtu.be"}
                or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443) or parsed.fragment):
            raise ValueError
        query = parse_qs(parsed.query)
        if set(query) - {"v", "list", "si", "feature"} or any(len(values) != 1 for values in query.values()):
            raise ValueError
        if kind == "show":
            if parsed.path.startswith("/browse/"):
                identifier = parsed.path.removeprefix("/browse/")
            elif parsed.path == "/playlist":
                value = query.get("list", [""])[0]
                identifier = value if value.startswith("MPSP") else "MPSP" + value
            else:
                raise ValueError
        else:
            if parsed.hostname == "youtu.be":
                identifier = parsed.path.removeprefix("/")
            elif parsed.path == "/watch":
                identifier = query.get("v", [""])[0]
            else:
                raise ValueError
        return validate_target(identifier, kind)
    except (ValueError, SetupError):
        raise SetupError("Enter a valid HTTPS YouTube Music show link or episode/song watch link for this item. "
                         "Credentials and unrelated URL parameters are not accepted.") from None


def row_fingerprint(result):
    return hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def policy_action(result):
    """Music uses the requested 85/100 score alone; podcast policy stays strict."""
    candidates = result.get("candidates", [])
    if result.get("status") == "unmatched" and not candidates:
        return "skipped", None
    if "song" in result and result.get("status") in ("strong_suggestion", "needs_review") and candidates:
        top = candidates[0]
        score = top.get("score")
        if top.get("available") is not False and type(score) in (int, float) and 85 <= score <= 100:
            return "approved", top.get("video_id", top.get("target_id"))
    elif result.get("status") == "strong_suggestion" and candidates:
        top = candidates[0]
        score = top.get("score")
        supported = (top.get("strong_metadata_match") is True and not top.get("reasons") and
                     top.get("available") is not False and type(score) in (int, float) and 95 <= score <= 100)
        if len(candidates) > 1:
            runner_score = candidates[1].get("score")
            supported = supported and type(runner_score) in (int, float) and score - runner_score >= 5
        if supported:
            return "approved", top.get("video_id", top.get("target_id"))
    return None, None


class ReviewStore:
    def __init__(self):
        root = prepare_private_storage() / "review"
        root.mkdir(exist_ok=True)
        self.path = root / "decisions.sqlite3"
        self.db = sqlite3.connect(self.path)
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS decisions (
            report_hash TEXT NOT NULL, source_key TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(report_hash,source_key));
          CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, report_hash TEXT NOT NULL, source_key TEXT NOT NULL,
            action TEXT NOT NULL, created_at TEXT NOT NULL);
        """)

    def decisions(self, report):
        return {key: json.loads(payload) for key, payload in self.db.execute(
            "SELECT source_key,payload FROM decisions WHERE report_hash=?", (report["fingerprint"],))}

    def policy_plan(self, report):
        current = load_report(report["kind"], report["run_id"])
        if current["fingerprint"] != report["fingerprint"]:
            raise SetupError("This report changed. Reload before applying the bulk review policy.")
        existing = self.decisions(current)
        counts = {"approve": 0, "skip": 0, "review": 0, "pending": 0, "preserved": 0}
        changes = []
        for result in current["data"]["results"]:
            if result["source_key"] in existing:
                counts["preserved"] += 1
                continue
            action, target = policy_action(result)
            if action:
                if target:
                    validate_target(target, source_kind(current, result))
                changes.append((result, action, target))
                counts["approve" if action == "approved" else "skip"] += 1
            elif result["status"] in ("pending", "not_cached"):
                counts["pending"] += 1
            else:
                counts["review"] += 1
        return counts, changes

    def apply_policy(self, report):
        """Atomic explicit batch: keep manual choices; no per-item listening claim."""
        counts, changes = self.policy_plan(report)
        applied = {"approved": 0, "skipped": 0}
        now = datetime.now(timezone.utc).isoformat()
        with self.db:
            for result, action, target in changes:
                decision = {"status": action, "target_id": target, "target_kind": source_kind(report, result),
                            "origin": ("bulk_score_85" if report['kind'] == 'music' else "bulk_strong_match") if action == "approved" else "bulk_missing_match",
                            "note": "Applied requested bulk policy (music score >=85/100, irrespective of metadata warnings); no listening verification claimed.",
                            "reviewed_at": now, "source_row_hash": row_fingerprint(result),
                            "source_archive": report["data"].get("source_archive"), "run_id": report["run_id"]}
                inserted = self.db.execute("INSERT OR IGNORE INTO decisions VALUES (?,?,?)",
                                           (report["fingerprint"], result["source_key"], json.dumps(decision)))
                if inserted.rowcount:
                    applied[action] += 1
                    self.db.execute("INSERT INTO events(report_hash,source_key,action,created_at) VALUES (?,?,?,?)",
                                    (report["fingerprint"], result["source_key"], "bulk_" + action, now))
        return {**counts, **applied}

    def save(self, report, key, action, candidate_id=None, manual_url=None, note="", confirmed=False):
        current = load_report(report["kind"], report["run_id"])
        if current["fingerprint"] != report["fingerprint"]:
            raise SetupError("This report changed while you were reviewing it. Reload and compare before saving.")
        result = next((row for row in current["data"]["results"] if row["source_key"] == key), None)
        if result is None or action not in {"approved", "skipped", "deferred", "clear"}:
            raise SetupError("Choose a valid item and review action.")
        if not isinstance(note, str) or len(note) > 1000:
            raise SetupError("Keep the review note to 1000 characters or fewer.")
        kind = source_kind(current, result)
        target = origin = None
        if action == "approved":
            if confirmed is not True:
                raise SetupError("Confirm that you checked the recording or show before approving it.")
            if candidate_id and manual_url:
                raise SetupError("Choose either a candidate or a manual link.")
            if manual_url:
                target = manual_target(manual_url, kind)
                origin = "manual_metadata_unverified"
            else:
                candidate = next((row for row in result["candidates"] if row.get("video_id", row.get("target_id")) == candidate_id), None)
                if not candidate or candidate.get("available") is False:
                    raise SetupError("Select an available candidate, or provide a manually verified link.")
                target = validate_target(candidate_id, kind)
                origin = "report_candidate"
        now = datetime.now(timezone.utc).isoformat()
        decision = {"status": action, "target_id": target, "target_kind": kind, "origin": origin,
                    "note": note.strip(), "reviewed_at": now, "source_row_hash": row_fingerprint(result),
                    "source_archive": current["data"].get("source_archive"), "run_id": current["run_id"]}
        with self.db:
            if action == "clear":
                self.db.execute("DELETE FROM decisions WHERE report_hash=? AND source_key=?", (current["fingerprint"], key))
            else:
                self.db.execute("INSERT OR REPLACE INTO decisions VALUES (?,?,?)", (current["fingerprint"], key, json.dumps(decision)))
            self.db.execute("INSERT INTO events(report_hash,source_key,action,created_at) VALUES (?,?,?,?)",
                            (current["fingerprint"], key, action, now))

    def preview(self, reports):
        approved = []
        for report in reports:
            current = load_report(report["kind"], report["run_id"])
            if current["fingerprint"] != report["fingerprint"]:
                raise SetupError("A report changed. Reload before viewing the approved preview.")
            decisions = self.decisions(report)
            for result in report["data"]["results"]:
                decision = decisions.get(result["source_key"])
                if decision and decision["status"] == "approved" and decision["source_row_hash"] == row_fingerprint(result):
                    approved.append({"source_key": result["source_key"], "source_title": result.get("song", result.get("podcast"))["title"],
                                     "report_hash": report["fingerprint"], "occurrences": result.get("occurrences", []), **decision})
        return {"schema_version": 1, "purpose": "review_only_no_transfer", "approved_items": approved,
                "requires_destination_revalidation": True}

    def close(self):
        self.db.close()


def review_command(port=8501):
    import sys

    if not 1024 <= port <= 65535:
        raise SetupError("Choose a local port between 1024 and 65535.")
    return [sys.executable, "-X", "utf8", "-m", "streamlit", "run", str(Path(__file__).with_name("ui.py")),
            "--server.address", "127.0.0.1", "--server.port", str(port),
            "--server.allowedHosts", "127.0.0.1",
            "--server.headless", "true", "--browser.gatherUsageStats", "false",
            "--server.enableCORS", "true", "--server.enableXsrfProtection", "true",
            "--server.enableStaticServing", "false", "--server.fileWatcherType", "none",
            "--client.showErrorDetails", "none", "--client.disableDataExport", "true"]
