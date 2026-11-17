"""Frozen transfer plans, read-only preflight, and journaled additive execution."""

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from collections import Counter, deque
from contextlib import closing, contextmanager
from datetime import datetime, timezone

from ytmusicapi import YTMusic

from spottoyt.archive import prepare_private_storage, private_root
from spottoyt.auth import PlaylistNotReady, SetupError, TimedSession, TransferPaused, Vault, quiet_library_logs, verify_browser_account
from spottoyt.playlist_completion import complete_playlist, inspect_completion
from spottoyt.matching import choose_archive, extract_songs, similarity
from spottoyt.podcasts import extract_podcasts
from spottoyt.review import RUN_ID, ReviewStore, list_reports, load_report, validate_target

PREFLIGHT_SECONDS = 1800


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def transfer_root():
    root = prepare_private_storage() / "transfers"
    root.mkdir(exist_ok=True)
    return root


def write_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def plan_folder(plan_id):
    if not isinstance(plan_id, str) or not RUN_ID.fullmatch(plan_id):
        raise SetupError("Choose a valid saved transfer plan ID.")
    root = transfer_root().resolve()
    path = (root / plan_id).resolve()
    if path.parent != root or not path.is_dir():
        raise SetupError("Transfer plan is missing or outside private storage.")
    return path


def approvals_digest(store, report):
    approved = [{"source_key": key, **{field: row.get(field) for field in
                ("status", "target_id", "target_kind", "origin", "source_row_hash", "source_archive")}}
                for key, row in sorted(store.decisions(report).items())]
    return digest(approved)


def create_plan(reports=None, also_save_to_library=False, allow_partial_playlists=False, strict_target_metadata=False):
    if reports is None:
        reports = [available[0] for kind in ("music", "podcasts") if (available := list_reports(kind)[0])]
    store = ReviewStore()
    try:
        approved = store.preview(reports)["approved_items"]
        if not approved:
            raise SetupError("No approved matches in the selected reports. Review matches before preparing a transfer.")
        sources = {row["source_archive"] for row in approved}
        if len(sources) != 1:
            raise SetupError("Approved reports use different source archives. Select reports from one snapshot.")
        source_id = sources.pop()
        if any(report["data"].get("source_archive") != source_id for report in reports):
            raise SetupError("Select music and podcast reports from the same source snapshot.")
        source = choose_archive(private_root() / "archives" / source_id)
        songs, skipped_songs = extract_songs(source)
        podcasts, skipped_podcasts = extract_podcasts(source)
        original = {row["source_key"]: row for row in songs + podcasts}
        selected_results = {row["source_key"]: row for report in reports for row in report["data"]["results"]}
        resolved = {}
        for report in reports:
            for key, decision in store.decisions(report).items():
                resolved[key] = decision
        approved_by_key = {}
        for row in approved:
            key = row["source_key"]
            if key not in original:
                raise SetupError("An approved item is no longer present in the source archive.")
            current = original[key]
            report_row = selected_results[key]
            if (current.get("song", current.get("podcast")) != report_row.get("song", report_row.get("podcast")) or
                    current["occurrences"] != report_row["occurrences"]):
                raise SetupError("Source metadata/occurrences changed. Re-archive and re-review before transfer.")
            validate_target(row["target_id"], row["target_kind"])
            approved_by_key[key] = row
        operations, target_evidence = [], {}
        for key, row in approved_by_key.items():
            report_row = selected_results[key]
            candidate = next((item for item in report_row["candidates"]
                              if item.get("video_id", item.get("target_id")) == row["target_id"]), {})
            target_evidence[row["target_kind"] + ":" + row["target_id"]] = {
                "kind": row["target_kind"], "target_id": row["target_id"], "origin": row["origin"],
                "require_unchanged_metadata": strict_target_metadata,
                "expected_title": candidate.get("title"), "expected_duration_seconds": candidate.get("duration_seconds")}
            collections = {ref["collection"] for ref in row["occurrences"]}
            common = {"source_key": key, "title": row["source_title"], "target_id": row["target_id"], "target_kind": row["target_kind"]}
            if "liked_songs" in collections:
                operations.append({**common, "action": "like_song"})
                if also_save_to_library:
                    operations.append({**common, "action": "save_song"})
            if "saved_shows" in collections:
                operations.append({**common, "action": "save_show"})
            if "saved_episodes" in collections:
                operations.append({**common, "action": "save_episode"})
        by_occurrence = {}
        for key, entry in original.items():
            for ref in entry["occurrences"]:
                by_occurrence[(ref["collection"], ref["position"])] = key
        held_playlists = []
        database = source / "archive.sqlite3"
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            playlists = [json.loads(row[0]) for row in db.execute("SELECT payload FROM records WHERE collection='playlists' ORDER BY position")]
            for playlist in playlists:
                source_playlist = playlist.get("id")
                if not isinstance(source_playlist, str) or not re.fullmatch(r"[A-Za-z0-9]+", source_playlist):
                    continue
                collection = "playlist_items/" + source_playlist
                coverage = db.execute("SELECT status FROM coverage WHERE collection=?", (collection,)).fetchone()
                items = db.execute("SELECT position,payload FROM records WHERE collection=? ORDER BY position", (collection,)).fetchall()
                targets, occurrences, omitted, pending = [], [], 0, 0
                for position, _ in items:
                    key = by_occurrence.get((collection, position))
                    if key in approved_by_key:
                        targets.append(approved_by_key[key]["target_id"])
                        occurrences.append({'source_position': position, 'video_id': approved_by_key[key]['target_id']})
                    elif key and resolved.get(key, {}).get("status") == "skipped":
                        omitted += 1
                    else:
                        pending += 1
                if not coverage or coverage[0] != "complete" or pending and not allow_partial_playlists or pending and not targets:
                    held_playlists.append({"source_id": source_playlist, "title": playlist.get("name", "Spotify playlist"),
                                           "approved_entries": len(targets), "skipped_entries": omitted, "unresolved_entries": pending,
                                           "reason": "incomplete_archive_or_unresolved_entries"})
                    continue
                marker = "[spottoyt:" + hashlib.sha256(source_playlist.encode()).hexdigest()[:20] + "]"
                title = str(playlist.get("name") or "Spotify playlist")[:120]
                operations.append({"action": "playlist", "source_id": source_playlist, "title": title,
                                   "marker": marker, "video_ids": targets, "skipped_entries": omitted,
                                   "unresolved_entries": pending, "privacy": "PRIVATE"})
                if allow_partial_playlists:
                    operations[-1].update(occurrences=occurrences, source_signature=digest([
                        [position, by_occurrence.get((collection, position)) or digest(json.loads(payload))]
                        for position, payload in items]))
            gaps = {name: db.execute("SELECT COUNT(*) FROM records WHERE collection=?", (name,)).fetchone()[0]
                    for name in ("saved_albums", "followed_artists", "saved_audiobooks")}
        # One idempotent state change per target/action; playlist occurrences are never deduplicated.
        unique, normalized_operations = set(), []
        for operation in operations:
            identity = (operation["action"], operation.get("target_id", operation.get("source_id")))
            if identity not in unique:
                unique.add(identity)
                normalized_operations.append({"op_id": str(len(normalized_operations)), **operation})
        required_targets = {operation["target_id"] for operation in normalized_operations if "target_id" in operation}
        required_targets.update(identifier for operation in normalized_operations if operation["action"] == "playlist"
                                for identifier in operation["video_ids"])
        plan = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(), "source_archive": source_id,
                "source_sha256": file_digest(database),
                "bindings": [{"kind": report["kind"], "run_id": report["run_id"], "report_hash": report["fingerprint"],
                              "approvals_hash": approvals_digest(store, report)} for report in reports],
                "operations": normalized_operations, "targets": [target for target in target_evidence.values() if target["target_id"] in required_targets], "held_playlists": held_playlists,
                "approved_targets_without_operations": [target for target in target_evidence.values() if target["target_id"] not in required_targets],
                "unmatched_entity_counts": gaps, "unsupported_states": ["history", "playback_progress", "social_user_follows"],
                "review_summary": [{"kind": report["kind"], "run_id": report["run_id"],
                                    "decisions": dict(Counter(store.decisions(report).get(row["source_key"], {}).get("status", "unreviewed")
                                                              for row in report["data"]["results"]))} for report in reports],
                "unsearched_music": max((report["data"].get("unselected_songs", 0) +
                                         sum(row["status"] in ("pending", "not_cached") for row in report["data"]["results"])
                                         for report in reports if report["kind"] == "music"), default=0),
                "also_save_to_library": also_save_to_library, "allow_partial_playlists": allow_partial_playlists,
                "strict_target_metadata": strict_target_metadata,
                "source_skipped_occurrences": len(skipped_songs) + len(skipped_podcasts)}
        identifier = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        folder = transfer_root() / identifier
        folder.mkdir()
        plan["plan_id"] = identifier
        write_json(folder / "plan.json", plan)
        (folder / "plan.sha256").write_text(file_digest(folder / "plan.json"), encoding="ascii")
        write_plan_preview(folder, plan)
        return folder
    finally:
        store.close()


def operation_counts(plan):
    return {action: sum(op["action"] == action for op in plan["operations"])
            for action in ("like_song", "save_song", "save_show", "save_episode", "playlist")}


def write_plan_preview(folder, plan):
    counts = operation_counts(plan)
    lines = ["# Transfer preview", "", "No account writes have been performed by plan creation or preflight.", "",
             f"Plan: {plan['plan_id']}", "", "| Action | Planned |", "|---|---:|"]
    lines.extend(f"| {action} | {count} |" for action, count in counts.items())
    lines.extend(["", f"Playlists held for unresolved/incomplete data: {len(plan['held_playlists'])}",
                  f"Songs outside the current matching report: {plan['unsearched_music']}",
                  "", "Album saves, artist follows, audiobooks, history and playback progress are not migrated by this plan.",
                  "See plan.json for source/target details. Preflight and explicit execution authorization are required."])
    (folder / "preview.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_plan(identifier, validate_reviews=True):
    folder = plan_folder(identifier)
    if file_digest(folder / "plan.json") != (folder / "plan.sha256").read_text(encoding="ascii").strip():
        raise SetupError("Transfer plan changed. Prepare a new plan; edited plan files are not executable.")
    plan = json.loads((folder / "plan.json").read_text(encoding="utf-8"))
    if plan.get("schema_version") != 1 or plan.get("plan_id") != identifier:
        raise SetupError("Unsupported transfer plan.")
    source = choose_archive(private_root() / "archives" / plan["source_archive"])
    if file_digest(source / "archive.sqlite3") != plan["source_sha256"]:
        raise SetupError("Source archive changed. Prepare a new plan.")
    if validate_reviews:
        store = ReviewStore()
        try:
            for binding in plan["bindings"]:
                report = load_report(binding["kind"], binding["run_id"])
                if report["fingerprint"] != binding["report_hash"] or approvals_digest(store, report) != binding["approvals_hash"]:
                    raise SetupError("Approved choices or reports changed. Prepare a new transfer plan.")
        finally:
            store.close()
    return plan


class Destination:
    """Only additive operations exist in this adapter. Raw responses stay in memory."""
    def __init__(self, client=None, credential_hash=None, pause=time.sleep, browser=False):
        quiet_library_logs()
        if client is None and os.environ.get('SPOTTOYT_DEMO') == '1':
            from spottoyt.demo import DemoClient
            client = DemoClient(private_root())
            credential_hash = 'synthetic-demo-no-credentials'
            pause = lambda _: None
        if client is None:
            if browser:
                config = Vault().get("youtube-browser")
                headers = config["headers"]
                credential_hash = digest({"cookie": headers["cookie"], "authuser": headers.get("x-goog-authuser", "0")})
                client = YTMusic(auth=headers, requests_session=TimedSession())
            else:
                from spottoyt.youtube_official import YouTubeOfficial
                client = YouTubeOfficial()
                credential_hash = client.credential_hash
        self.client, self.credential_hash, self.pause = client, credential_hash, pause
        self.calls = 0

    def call(self, method, *args, **kwargs):
        if self.calls:
            self.pause(.5)
        self.calls += 1
        return getattr(self.client, method)(*args, **kwargs)

    def account(self):
        self.calls += 1
        info = verify_browser_account(self.client)
        handle = info.get("channelHandle")
        if not isinstance(handle, str) or not handle.startswith("@"):
            raise SetupError("Destination account handle could not be verified. No account writes are allowed.")
        return {"channel_handle": handle, "account_name": info.get("accountName"), "account_hash": digest(handle.casefold()),
                "credential_hash": self.credential_hash}

    def target(self, target):
        identifier, kind = target["target_id"], target["kind"]
        validate_target(identifier, kind)
        if kind == "show":
            detail = self.call("get_podcast", identifier, limit=1)
            if not isinstance(detail.get("saved"), bool):
                raise SetupError("Destination show save state is unknown.")
            result = {"target_id": identifier, "kind": kind, "title": detail.get("title"), "saved": detail["saved"]}
        else:
            song = self.call("get_song", identifier)
            if song.get("playabilityStatus", {}).get("status") != "OK" or song.get("videoDetails", {}).get("videoId") != identifier:
                raise SetupError("Target playback availability could not be verified.")
            details = song["videoDetails"]
            result = {"target_id": identifier, "kind": kind, "title": details.get("title"),
                      "duration_seconds": int(details["lengthSeconds"]) if str(details.get("lengthSeconds", "")).isdigit() else None}
            if kind == "song":
                tracks = self.call("get_watch_playlist", videoId=identifier, limit=1).get("tracks", [])
                track = next((item for item in tracks if item.get("videoId") == identifier), None)
                if not track or track.get("likeStatus") not in ("LIKE", "INDIFFERENT", "DISLIKE"):
                    raise SetupError("Destination song rating state could not be verified.")
                result.update(like_status=track["likeStatus"], in_library=track.get("inLibrary"))
            else:
                detail = self.call("get_episode", identifier)
                if not isinstance(detail.get("saved"), bool):
                    raise SetupError("Destination episode save state is unknown.")
                result["saved"] = detail["saved"]
        if target.get("origin") != "manual_metadata_unverified":
            warnings = []
            expected = target.get("expected_title")
            if expected and similarity(expected, result.get("title")) < .95:
                warnings.append('Destination title differs from the saved candidate metadata')
            duration = target.get("expected_duration_seconds")
            if isinstance(duration, (int, float)) and result.get("duration_seconds") is not None:
                if abs(duration - result["duration_seconds"]) > max(2, duration * .015):
                    warnings.append('Destination duration differs from the saved candidate metadata')
            if warnings and target.get('require_unchanged_metadata', True):
                raise SetupError('Destination title or duration differs from the reviewed candidate. Review it again before transfer.')
            if warnings:
                result['metadata_warnings'] = warnings
        return result

    def playlist_state(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", identifier):
            raise SetupError("Invalid destination playlist identifier.")
        data = self.call("get_playlist", identifier, limit=None)
        tracks = data.get("tracks")
        if not isinstance(tracks, list) or any(not re.fullmatch(r"[A-Za-z0-9_-]{11}", str(item.get("videoId", ""))) for item in tracks):
            raise SetupError("Destination playlist has unreadable/unavailable entries; refusing to append.")
        if isinstance(data.get("trackCount"), int) and data["trackCount"] != len(tracks):
            raise PlaylistNotReady("Destination playlist pagination/count has not converged or is incomplete.")
        return {"id": identifier, "title": data.get("title"), "privacy": data.get("privacy"),
                "description": data.get("description", ""), "video_ids": [item["videoId"] for item in tracks]}


class Ledger:
    def __init__(self):
        self.db = sqlite3.connect(transfer_root() / "journal.sqlite3")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS operations(plan_id TEXT,op_id TEXT,state TEXT,PRIMARY KEY(plan_id,op_id));
          CREATE TABLE IF NOT EXISTS playlists(account_hash TEXT,source_id TEXT,destination_id TEXT,PRIMARY KEY(account_hash,source_id));
          CREATE TABLE IF NOT EXISTS playlist_progress(account_hash TEXT,source_id TEXT,payload TEXT,PRIMARY KEY(account_hash,source_id));
        """)

    def state(self, plan_id, op_id):
        row = self.db.execute("SELECT state FROM operations WHERE plan_id=? AND op_id=?", (plan_id, op_id)).fetchone()
        return row[0] if row else "pending"

    def mark(self, plan_id, op_id, state):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO operations VALUES (?,?,?)", (plan_id, op_id, state))

    def mapping(self, account, source):
        row = self.db.execute("SELECT destination_id FROM playlists WHERE account_hash=? AND source_id=?", (account, source)).fetchone()
        return row[0] if row else None

    def remember(self, account, source, destination):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO playlists VALUES (?,?,?)", (account, source, destination))

    def playlist_progress(self, account, source):
        row = self.db.execute('SELECT payload FROM playlist_progress WHERE account_hash=? AND source_id=?', (account, source)).fetchone()
        return json.loads(row[0]) if row else None

    def save_playlist_progress(self, account, source, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO playlist_progress VALUES (?,?,?)', (account, source, json.dumps(value)))

    def close(self):
        self.db.close()


def find_playlist(destination, operation):
    found = []
    playlists = destination.call("get_library_playlists", limit=None)
    if not isinstance(playlists, list) or len(playlists) > 2000:
        raise SetupError("Destination playlist inventory is too large or unreadable.")
    for row in playlists:
        if row.get("owned") is True and row.get("title") == operation["title"]:
            state = destination.playlist_state(row.get("playlistId"))
            if operation["marker"] in state["description"]:
                found.append(state)
    if len(found) > 1:
        raise SetupError("Multiple destination playlists have this transfer marker. Resolve the ambiguity before resuming.")
    return found[0] if found else None


def check_playlist_prefix(state, operation):
    if state["privacy"] != "PRIVATE" or operation["marker"] not in state["description"]:
        raise SetupError("Destination playlist privacy/provenance changed. No writes are allowed.")
    current, desired = state["video_ids"], operation["video_ids"]
    if current != desired[:len(current)]:
        raise SetupError("Destination playlist differs from the planned sequence. Refusing to reorder, delete or duplicate entries.")


def preflight(identifier, destination=None, progress=lambda _: None, resume_reads=False):
    plan = load_plan(identifier)
    destination = destination or Destination()
    result = {"schema_version": 1, "plan_hash": file_digest(plan_folder(identifier) / "plan.json"),
              "created_at_epoch": time.time(), "state": "checking", "targets": [], "playlist_checks": []}
    ledger = Ledger()
    try:
        result["account"] = destination.account()
        path = plan_folder(identifier) / 'preflight.json'
        if resume_reads and path.is_file() and path.with_suffix('.sha256').is_file() and file_digest(path) == path.with_suffix('.sha256').read_text().strip():
            previous = json.loads(path.read_text(encoding='utf-8'))
            prefix = previous.get('targets', [])
            anchor = previous.get('target_checks_completed_at_epoch', previous.get('completed_at_epoch',
                                  path.stat().st_mtime if previous.get('state') == 'ready' else previous.get('created_at_epoch', 0)))
            if (previous.get('account') == result['account'] and previous.get('plan_hash') == result['plan_hash'] and
                    0 <= time.time() - anchor <= PREFLIGHT_SECONDS and
                    [(row.get('target_id'), row.get('kind')) for row in prefix] ==
                    [(row['target_id'], row['kind']) for row in plan['targets'][:len(prefix)]]):
                result['targets'] = prefix
                result['created_at_epoch'] = previous['created_at_epoch']
                result['target_checks_completed_at_epoch'] = anchor
                progress(f"Reusing {len(prefix)} checksum-verified destination reads within the original 30-minute window")
        for target in plan["targets"][len(result['targets']):]:
            checked = destination.target(target)
            if checked.get("like_status") == "DISLIKE":
                raise SetupError("An approved target is currently disliked on YouTube. Resolve that conflict before transfer.")
            result["targets"].append(checked)
            progress(f"Destination target {len(result['targets'])}/{len(plan['targets'])}: checked")
        for operation in plan["operations"]:
            if operation["action"] == "save_song":
                checked = next(row for row in result["targets"] if row["target_id"] == operation["target_id"])
                if not isinstance(checked.get("in_library"), bool):
                    raise SetupError("Song library-save state is unknown; prepare a likes-only plan or resolve destination metadata.")
            if operation["action"] == "playlist":
                mapped = ledger.mapping(result["account"]["account_hash"], operation["source_id"])
                state = destination.playlist_state(mapped) if mapped else find_playlist(destination, operation)
                if state:
                    if 'occurrences' in operation:
                        inspect_completion(state, operation, ledger.playlist_progress(result['account']['account_hash'], operation['source_id']))
                    else:
                        check_playlist_prefix(state, operation)
                elif ledger.state(identifier, operation["op_id"]) in ("in_flight", "uncertain"):
                    raise SetupError("A previous playlist creation is uncertain and its marker was not found. Wait and reconcile before creating again.")
                result["playlist_checks"].append({"op_id": operation["op_id"], "destination_id": state["id"] if state else None,
                                                  "existing_entries": len(state["video_ids"]) if state else 0})
        result["state"] = "ready"
        result['completed_at_epoch'] = time.time()
        result.setdefault('target_checks_completed_at_epoch', result['completed_at_epoch'])
    except Exception as error:
        result["state"] = "blocked"
        result["reason"] = str(error) if isinstance(error, SetupError) else "Destination read failed; sensitive response details omitted."
    finally:
        result["read_calls"] = destination.calls
        ledger.close()
        path = plan_folder(identifier) / "preflight.json"
        write_json(path, result)
        path.with_suffix(".sha256").write_text(file_digest(path), encoding="ascii")
    return result


@contextmanager
def execution_lock():
    path = transfer_root() / "execution.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    locked = False
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise SetupError("Another transfer is running. Wait for it to finish before resuming.") from None
        yield
    finally:
        if locked:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def ensure_playlist(plan, operation, destination, ledger, account):
    mapped = ledger.mapping(account, operation["source_id"])
    state = destination.playlist_state(mapped) if mapped else find_playlist(destination, operation)
    if not state:
        if ledger.state(plan["plan_id"], operation["op_id"]) in ("in_flight", "uncertain"):
            raise SetupError("Playlist creation outcome is unknown. No marker was found; wait and reconcile before retrying creation.")
        ledger.mark(plan["plan_id"], operation["op_id"], "in_flight")
        identifier = destination.call("create_playlist", operation["title"], "Imported with Spottoyt. Reviewed items only; more may be added later. " + operation["marker"], privacy_status="PRIVATE")
        if not isinstance(identifier, str):
            raise SetupError("Playlist creation did not return a verified identifier.")
        ledger.remember(account, operation["source_id"], identifier)
        for attempt in range(4):
            try:
                state = destination.playlist_state(identifier)
                break
            except PlaylistNotReady:
                if attempt == 3:
                    raise
                destination.pause(2 ** attempt)
    ledger.remember(account, operation["source_id"], state["id"])
    return state


def execute_playlist(plan, operation, destination, ledger, account, max_inserts=None):
    state = ensure_playlist(plan, operation, destination, ledger, account)
    if 'occurrences' in operation:
        return complete_playlist(plan, operation, destination, ledger, account, state, max_inserts=max_inserts)
    check_playlist_prefix(state, operation)
    while len(state["video_ids"]) < len(operation["video_ids"]):
        start = len(state["video_ids"])
        batch = operation["video_ids"][start:start + 50]
        ledger.mark(plan["plan_id"], operation["op_id"], "in_flight")
        response = destination.call("add_playlist_items", state["id"], batch, duplicates=True)
        if not isinstance(response, dict) or not str(response.get("status", "")).endswith("SUCCEEDED"):
            raise SetupError("Playlist append response was not successful. Resume only after reading the current sequence.")
        state = destination.playlist_state(state["id"])
        check_playlist_prefix(state, operation)
        if len(state["video_ids"]) != start + len(batch):
            raise SetupError("Playlist append count is uncertain. No automatic write retry was attempted.")
    return True


def execute_state_change(operation, destination, evidence):
    action, identifier = operation['action'], operation['target_id']
    if action == 'like_song':
        tracks = destination.call('get_watch_playlist', videoId=identifier, limit=1).get('tracks', [])
        current = next((row for row in tracks if row.get('videoId') == identifier), {})
        if current.get('likeStatus') == 'LIKE':
            return
        if current.get('likeStatus') == 'DISLIKE':
            raise SetupError('Target is disliked; resolve the conflict before changing its rating.')
    target = evidence
    before = destination.target(target)
    action, identifier = operation["action"], operation["target_id"]
    if action == "like_song":
        if before["like_status"] == "LIKE":
            return
        if before["like_status"] == "DISLIKE":
            raise SetupError("Target was disliked after preflight; resolve the conflict first.")
        destination.call("rate_song", identifier, "LIKE")
    elif action == "save_show":
        if before["saved"]:
            return
        destination.call("rate_playlist", identifier, "LIKE")
    elif action == "save_episode":
        if before["saved"]:
            return
        destination.call("add_playlist_items", "SE", [identifier], duplicates=False)
    elif action == "save_song":
        if before.get("in_library") is True:
            return
        tracks = destination.call("get_watch_playlist", videoId=identifier, limit=1).get("tracks", [])
        track = next((row for row in tracks if row.get("videoId") == identifier), {})
        token = (track.get("feedbackTokens") or {}).get("add")
        if track.get("inLibrary") is not False or not isinstance(token, str):
            raise SetupError("Song library-save token/state unavailable; nothing was sent.")
        destination.call("edit_song_library_status", [token])
    else:
        raise SetupError("Unsupported transfer action.")
    after = destination.target(target)
    successful = after.get("like_status") == "LIKE" if action == "like_song" else after.get("in_library") is True if action == "save_song" else after.get("saved") is True
    if not successful:
        raise SetupError("Destination write could not be verified. No automatic write retry was attempted.")


def execute(identifier, confirmed=False, destination=None, progress=lambda _: None):
    if confirmed is not True:
        raise SetupError("Review the concrete plan and explicitly authorize execution first.")
    with execution_lock():
        plan = load_plan(identifier)
        folder = plan_folder(identifier)
        if not (folder / "preflight.json").exists():
            raise SetupError("Run read-only preflight before executing this plan.")
        checked = json.loads((folder / "preflight.json").read_text(encoding="utf-8"))
        if (not (folder / "preflight.sha256").exists() or
                file_digest(folder / "preflight.json") != (folder / "preflight.sha256").read_text(encoding="ascii").strip()):
            raise SetupError("Preflight was modified. Run it again before execution.")
        if (checked.get("state") != "ready" or checked.get("plan_hash") != file_digest(folder / "plan.json") or
                not 0 <= time.time() - checked.get('completed_at_epoch', checked.get("created_at_epoch", 0)) <= PREFLIGHT_SECONDS):
            raise SetupError("Preflight is blocked, stale or mismatched. Run it again before execution.")
        destination = destination or Destination()
        try:
            account = destination.account()
            if account != checked["account"]:
                raise SetupError("Destination account/session changed since preflight. Run preflight again.")
        except Exception as error:
            checked["state"] = "blocked"
            checked["reason"] = "Destination account/session verification failed at execution. Reconnect if needed and run preflight again. No writes started."
            write_json(folder / "preflight.json", checked)
            (folder / "preflight.sha256").write_text(file_digest(folder / "preflight.json"), encoding="ascii")
            if isinstance(error, SetupError):
                raise
            raise SetupError(checked["reason"]) from None
        ledger = Ledger()
        result = {"plan_id": identifier, "state": "in_progress", "completed": 0, "total": len(plan["operations"])}
        try:
            # Establish all private copies before spending the batch on song likes
            # or filling one large playlist. Each creation remains journaled.
            for operation in plan['operations']:
                if operation['action'] == 'playlist' and 'occurrences' in operation:
                    state = ensure_playlist(plan, operation, destination, ledger, account['account_hash'])
                    progress_state = inspect_completion(state, operation, ledger.playlist_progress(account['account_hash'], operation['source_id']))
                    ledger.save_playlist_progress(account['account_hash'], operation['source_id'], progress_state)
                    progress('Private playlist copy ready; occurrence checkpoints saved')
            queue = deque(plan['operations'])
            while queue:
                operation = queue.popleft()
                try:
                    if operation["action"] == "playlist":
                        complete = execute_playlist(plan, operation, destination, ledger, account["account_hash"],
                                                    max_inserts=1 if 'occurrences' in operation else None)
                        if not complete:
                            ledger.mark(identifier, operation['op_id'], 'pending')
                            queue.append(operation)
                            progress('Playlist occurrence verified and checkpointed; continuing across partial copies')
                            continue
                    else:
                        ledger.mark(identifier, operation["op_id"], "in_flight")
                        evidence = next(target for target in plan["targets"]
                                        if target["target_id"] == operation["target_id"] and target["kind"] == operation["target_kind"])
                        execute_state_change(operation, destination, evidence)
                    ledger.mark(identifier, operation["op_id"], "verified")
                    result["completed"] += 1
                    write_json(folder / "execution.json", result)
                    progress(f"Operation {result['completed']}/{result['total']}: verified")
                except (KeyboardInterrupt, Exception) as error:
                    ledger.mark(identifier, operation["op_id"], "pending" if isinstance(error, TransferPaused) else "uncertain")
                    raise
            result["state"] = "verified"
        except TransferPaused as error:
            ledger.mark(identifier, operation['op_id'], 'pending')
            result['state'] = 'paused'
            result['reason'] = str(error)
        except (KeyboardInterrupt, Exception):
            result["state"] = "stopped_uncertain"
            raise SetupError(f"Transfer stopped safely. Inspect {folder / 'execution.json'}. "
                             "Do not blindly repeat writes; the same plan reconciles destination state when resumed.") from None
        finally:
            if hasattr(destination.client, 'writes_attempted'):
                result['write_attempts'] = destination.client.writes_attempted
                result['batch_write_limit'] = destination.client.write_limit
                result['write_attempts_by_endpoint'] = dict(destination.client.write_counts)
            ledger.close()
            write_json(folder / "execution.json", result)
        return result
