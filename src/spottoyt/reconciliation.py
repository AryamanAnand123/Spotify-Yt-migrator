"""Independent destination reads and explicit migration gaps; never account writes."""

import json
import sqlite3
import uuid
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone

from spottoyt.archive import prepare_private_storage, private_root
from spottoyt.auth import SetupError
from spottoyt.matching import choose_archive, extract_songs
from spottoyt.review import RUN_ID, load_report
from spottoyt.playlist_completion import inspect_completion
from spottoyt.transfers import (Destination, execution_lock, file_digest,
                               load_plan, plan_folder, write_json)


def readonly(path):
    return closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True))


def history_summary(db, library_uris):
    kinds, music, episodes = Counter(), set(), set()
    overlap_events, overlap_uris, missing_timestamps = 0, set(), 0
    first = last = None
    for payload, in db.execute("SELECT payload FROM records WHERE collection='history'"):
        event = json.loads(payload).get("event")
        if not isinstance(event, dict):
            kinds["unrecognized_event"] += 1
            missing_timestamps += 1
            continue
        track, episode = event.get("spotify_track_uri"), event.get("spotify_episode_uri")
        if isinstance(track, str) and track.startswith("spotify:track:"):
            kinds["music"] += 1
            music.add(track)
            if track in library_uris:
                overlap_events += 1
                overlap_uris.add(track)
        elif isinstance(episode, str) and episode.startswith("spotify:episode:"):
            kinds["podcast"] += 1
            episodes.add(episode)
        else:
            kinds["without_supported_spotify_uri"] += 1
        try:
            value = datetime.fromisoformat(event.get("ts", "").replace("Z", "+00:00"))
            if value.tzinfo is None:
                raise ValueError
            value = value.astimezone(timezone.utc)
            first = min(first, value) if first else value
            last = max(last, value) if last else value
        except (ValueError, TypeError, AttributeError):
            missing_timestamps += 1
    return {"event_kinds": dict(kinds), "unique_music_uris": len(music), "unique_episode_uris": len(episodes),
            "current_library_overlap_events": overlap_events, "current_library_overlap_unique_tracks": len(overlap_uris),
            "unique_history_tracks_outside_current_library": len(music - library_uris),
            "first_event_utc": first.isoformat() if first else None, "last_event_utc": last.isoformat() if last else None,
            "events_without_timezone_aware_timestamp": missing_timestamps,
            "interpretation": "Past plays are not current saved items. Outside-library tracks are preserved, not automatically liked or added to playlists."}


def download_inventory(archive_id, library_uris=frozenset()):
    if not isinstance(archive_id, str) or not RUN_ID.fullmatch(archive_id):
        raise SetupError("Choose a valid account-download archive ID.")
    # choose_archive validates the resolved path against private storage.
    path = choose_archive(private_root() / "archives" / archive_id)
    with readonly(path / "archive.sqlite3") as db:
        meta = {key: json.loads(value) for key, value in db.execute("SELECT * FROM metadata")}
        if meta.get("source") != "spotify_account_download" or meta.get("state") == "interrupted_or_partial":
            raise SetupError("Choose a finished Spotify account-download archive.")
        if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise SetupError("Account-download database integrity check failed.")
        import hashlib
        files = db.execute("SELECT i.sha256,i.filename,i.status,o.content FROM imports i LEFT JOIN original_files o USING(sha256)").fetchall()
        for checksum, _, _, raw in files:
            if not isinstance(raw, bytes) or hashlib.sha256(raw).hexdigest() != checksum:
                raise SetupError("Imported original-file checksum verification failed.")
        return {"archive_id": path.name, "state": meta["state"], "database_sha256": file_digest(path / "archive.sqlite3"),
                "files": [{"filename": name, "sha256": checksum, "status": status} for checksum, name, status, _ in files],
                "collections": dict(db.execute("SELECT collection,COUNT(*) FROM records GROUP BY collection")),
                "history_summary": history_summary(db, set(library_uris)),
                "history_policy": "Archived only. Listening events do not imply current likes and are not replayed on YouTube.",
                "social_policy": "Archived only. Spotify user follows are not YouTube subscriptions."}


def review_gaps(plan):
    summaries = []
    for binding in plan["bindings"]:
        report = load_report(binding["kind"], binding["run_id"])
        if report["fingerprint"] != binding["report_hash"]:
            raise SetupError("A bound matching report changed; reconciliation cannot trust its gap counts.")
        # Report status is frozen evidence, not a claim about current decisions.
        summaries.append({"kind": binding["kind"], "run_id": binding["run_id"],
                          "matching_status_counts": dict(Counter(row["status"] for row in report["data"]["results"])),
                          "unselected": report["data"].get("unselected_songs", 0)})
    return {"matching_reports": summaries, "review_summary": plan.get("review_summary", []), "held_playlists": plan["held_playlists"],
            "approved_targets_without_operations": plan.get("approved_targets_without_operations", []),
            "unmatched_entities": plan["unmatched_entity_counts"], "unsupported_states": plan["unsupported_states"],
            "unsearched_music": plan["unsearched_music"], "source_skipped_occurrences": plan["source_skipped_occurrences"],
            "scope": "Verifies this frozen plan only. Held playlists, unresolved matches and unsupported states are not migrated."}


def list_verifications():
    """Load checksum-checked saved reports offline; never initialize a destination."""
    root = (private_root() / "verification").resolve()
    reports, errors = [], 0
    for path in sorted(root.glob("*/report.json"), reverse=True):
        try:
            if (not RUN_ID.fullmatch(path.parent.name) or root not in path.resolve().parents or
                    path.stat().st_size > 50 * 1024 * 1024 or
                    file_digest(path) != path.with_suffix(".sha256").read_text().strip()):
                raise ValueError
            report = json.loads(path.read_text(encoding="utf-8"))
            if report.get("schema_version") != 1 or not isinstance(report.get("operations"), list):
                raise ValueError
            reports.append({"run_id": path.parent.name, "data": report})
        except (ValueError, OSError, TypeError):
            errors += 1
    return reports, errors


def verify_transfer(identifier, account_archives=(), destination=None, progress=lambda _: None):
    with execution_lock():
        # Later review choices do not rewrite the historical transfer plan.
        plan = load_plan(identifier, validate_reviews=False)
        folder = plan_folder(identifier)
        songs, _ = extract_songs(choose_archive(private_root() / "archives" / plan["source_archive"]))
        library_uris = {row['song']['spotify_uri'] for row in songs if row['song']['spotify_uri']}
        downloads = [download_inventory(value, library_uris) for value in account_archives]
        gaps = review_gaps(plan)
        checked_path = folder / "preflight.json"
        if (not checked_path.is_file() or not checked_path.with_suffix(".sha256").is_file() or
                file_digest(checked_path) != checked_path.with_suffix(".sha256").read_text().strip()):
            raise SetupError("A checksum-verified preflight is required to identify the expected account.")
        checked = json.loads(checked_path.read_text(encoding="utf-8"))
        if checked.get("plan_hash") != file_digest(folder / "plan.json") or not checked.get("account", {}).get("account_hash"):
            raise SetupError("Preflight does not identify this plan's destination account.")
        root = prepare_private_storage() / "verification"
        root.mkdir(exist_ok=True)
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        output = root / run_id
        output.mkdir()
        names = [item['filename'].casefold() for download in downloads for item in download['files']]
        download_coverage = {"history_files_present": any(name.startswith(('streaminghistory', 'streaming_history', 'endsong')) for name in names),
                             "library_files_present": any(name.startswith('yourlibrary') for name in names),
                             "playlist_files_present": any(name.startswith('playlist') for name in names),
                             "social_follow_files_present": any(name.startswith('follow') for name in names),
                             "account_data_completeness": "not_verified"}
        report = {"schema_version": 1, "plan_id": identifier, "plan_sha256": file_digest(folder / "plan.json"),
                  "source_archive": plan["source_archive"], "created_at": datetime.now(timezone.utc).isoformat(),
                  "state": "checking", "operations": [], "gaps": gaps, "account_downloads": downloads, "download_coverage": download_coverage,
                  "account_writes": 0, "complete_account_migration": False}
        destination = destination or Destination()
        try:
            account = destination.account()
            if account["account_hash"] != checked["account"]["account_hash"]:
                raise SetupError("Destination account differs from the frozen plan. No destination items were inspected.")
            report["account_hash"] = account["account_hash"]
            journal = private_root() / "transfers" / "journal.sqlite3"
            with readonly(journal) as db:
                for operation in plan["operations"]:
                    action = operation["action"]
                    item = {"op_id": operation["op_id"], "action": action, "status": "unverified"}
                    row = db.execute("SELECT state FROM operations WHERE plan_id=? AND op_id=?", (identifier, operation["op_id"])).fetchone()
                    item["journal_state"] = row[0] if row else "pending"
                    if action == "playlist":
                        mapping = db.execute("SELECT destination_id FROM playlists WHERE account_hash=? AND source_id=?",
                                             (account["account_hash"], operation["source_id"])).fetchone()
                        item["source_id"] = operation["source_id"]
                        item["expected_entries"] = len(operation["video_ids"])
                        if not mapping:
                            item["status"] = "not_transferred"
                        else:
                            state = destination.playlist_state(mapping[0])
                            item.update(destination_id=mapping[0], observed_entries=len(state["video_ids"]),
                                        private=state["privacy"] == "PRIVATE", provenance_matches=operation["marker"] in state["description"],
                                        title_matches=state["title"] == operation["title"],
                                        sequence_matches=state["video_ids"] == operation["video_ids"])
                            item["status"] = "verified" if all(item[key] for key in ("private", "provenance_matches", "sequence_matches", "title_matches")) else "destination_differs"
                            if 'occurrences' in operation:
                                saved = db.execute('SELECT payload FROM playlist_progress WHERE account_hash=? AND source_id=?',
                                                   (account['account_hash'], operation['source_id'])).fetchone()
                                try:
                                    inspect_completion(state, operation, json.loads(saved[0]) if saved else None)
                                    item['checkpoint_matches'] = True
                                    item['remaining_entries'] = len(operation['video_ids']) - len(state['video_ids'])
                                    if item['status'] != 'verified':
                                        item['status'] = 'partial_verified'
                                except SetupError:
                                    item['checkpoint_matches'] = False
                                    item['status'] = 'destination_differs'
                    elif action == "like_song":
                        item["target_id"] = operation["target_id"]
                        tracks = destination.call("get_watch_playlist", videoId=operation["target_id"], limit=1).get("tracks", [])
                        state = next((row for row in tracks if row.get("videoId") == operation["target_id"]), {})
                        item["status"] = "verified" if state.get("likeStatus") == "LIKE" else "destination_differs" if state.get("likeStatus") in ("INDIFFERENT", "DISLIKE") else "unknown"
                    else:
                        item["status"] = "unsupported_by_official_verifier"
                    report["operations"].append(item)
                    progress(f"Verification {len(report['operations'])}/{len(plan['operations'])}: {item['status']}")
            statuses = {row['status'] for row in report['operations']}
            report['state'] = ('verified_plan_with_gaps' if statuses <= {'verified'} else 'verified_progress_with_gaps'
                               if statuses <= {'verified', 'partial_verified'} else 'completed_with_differences')
        except (KeyboardInterrupt, Exception) as error:
            report["state"] = "blocked_or_partial"
            report["reason"] = str(error) if isinstance(error, SetupError) else "Destination read failed; sensitive details omitted. No account writes or automatic retries."
        report["counts"] = dict(Counter(row["status"] for row in report["operations"]))
        report["unchecked_operations"] = len(plan["operations"]) - len(report["operations"])
        write_json(output / "report.json", report)
        (output / "report.sha256").write_text(file_digest(output / "report.json"), encoding="ascii")
        lines = ["# Destination verification and migration gaps", "", f"Plan: {identifier}", f"State: {report['state']}",
                 f"Verified operations: {report['counts'].get('verified', 0)}/{len(plan['operations'])}",
                 f"Unchecked operations: {report['unchecked_operations']}", "Account writes: 0", "",
                 "This is not a complete account migration.", f"Held playlists: {len(plan['held_playlists'])}",
                 f"Approved targets without executable operations (held playlists): {len(plan.get('approved_targets_without_operations', []))}",
                 f"Unsearched music: {plan['unsearched_music']}", "History, playback progress and Spotify social follows are archive-only.",
                 "Album and artist entity mappings remain unsupported.", "", "## Imported Spotify downloads"]
        for download in downloads:
            lines.append(f"- {download['archive_id']}: {len(download['files'])} checksum-verified JSON files; {download['collections'].get('history', 0)} history events.")
        if not any(download_coverage[key] for key in ('library_files_present', 'playlist_files_present', 'social_follow_files_present')):
            lines.append("Separate Account data library/playlist/follow files were not identified in the selected imports.")
        lines.extend(["", "See private report.json for exact differences, frozen matching counts and source provenance."])
        (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return output
