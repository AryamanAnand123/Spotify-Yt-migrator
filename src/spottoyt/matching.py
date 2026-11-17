"""Conservative, explainable music matching. No account mutations or LLM calls."""

import csv
import hashlib
import json
import math
import os
import re
import sqlite3
import time
import unicodedata
import uuid
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from ytmusicapi import YTMusic
from requests import RequestException

from spottoyt.archive import prepare_private_storage, private_root
from spottoyt.auth import SetupError, TimedSession, quiet_library_logs

SCORING_VERSION = 1
CACHE_VERSION = 1
VARIANTS = ("live", "remix", "acoustic", "instrumental", "karaoke", "remaster", "sped up", "slowed", "cover")


def text(value):
    return value if isinstance(value, str) else ""


def normalized(value):
    value = unicodedata.normalize("NFKC", text(value)).casefold()
    # Combining marks carry vowel/other distinctions in many writing systems.
    value = "".join(char if char.isalnum() or unicodedata.category(char).startswith("M") else " " for char in value)
    return " ".join(value.split())


def similarity(left, right):
    a, b = normalized(left), normalized(right)
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def variants(value):
    value = normalized(value)
    return {kind for kind in VARIANTS if re.search(r"\b" + kind + (r"(?:ed)?\b" if kind == "remaster" else r"\b"), value)}


def positive_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def choose_archive(path=None):
    root = (private_root() / "archives").resolve()
    if path:
        selected = Path(path).resolve()
        database = selected / "archive.sqlite3"
        if (root not in selected.parents or not database.is_file() or
                root not in database.resolve().parents):
            raise SetupError("Choose an existing archive folder inside private Spottoyt storage.")
        return selected
    for candidate in sorted(root.glob("*"), reverse=True):
        database = candidate / "archive.sqlite3"
        if not database.is_file() or root not in database.resolve().parents:
            continue
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            meta = {key: json.loads(value) for key, value in db.execute("SELECT * FROM metadata")}
            if meta.get("source") == "spotify_api" and meta.get("state") in ("complete", "completed_with_gaps"):
                return candidate
    raise SetupError("No Spotify API archive found. Run export-spotify first.")


def extract_songs(path):
    """Deduplicate searches, retaining every liked/playlist occurrence as a source reference."""
    songs, skipped = {}, []
    database = (path / "archive.sqlite3").resolve()
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        meta = {key: json.loads(value) for key, value in db.execute("SELECT * FROM metadata")}
        if meta.get("schema_version") != 1 or meta.get("source") != "spotify_api":
            raise SetupError("Music matching currently accepts version-1 Spotify API archives only.")
        if meta.get("state") not in ("complete", "completed_with_gaps"):
            raise SetupError("Choose a finished API snapshot; interrupted archives may omit whole song collections.")
        for collection, position, payload in db.execute("SELECT * FROM records ORDER BY collection, position"):
            if collection != "liked_songs" and not collection.startswith("playlist_items/"):
                continue
            wrapper = json.loads(payload)
            item = wrapper.get("item") if "item" in wrapper else wrapper.get("track")
            ref = {"collection": collection, "position": position}
            if not isinstance(item, dict):
                skipped.append({"source": ref, "reason": "unavailable_or_missing_item"})
                continue
            if item.get("type") == "episode":
                skipped.append({"source": ref, "reason": "podcast_requires_separate_matching"})
                continue
            if item.get("is_local") or wrapper.get("is_local"):
                skipped.append({"source": ref, "reason": "local_file_not_searched"})
                continue
            artists = [artist["name"] for artist in item.get("artists", [])
                       if isinstance(artist, dict) and text(artist.get("name"))]
            title = text(item.get("name"))
            if not title or not artists:
                skipped.append({"source": ref, "reason": "insufficient_song_metadata"})
                continue
            album = item.get("album") or {}
            song = {"title": title, "artists": artists, "album": text(album.get("name")),
                    "duration_ms": item.get("duration_ms"),
                    "explicit": item.get("explicit") if isinstance(item.get("explicit"), bool) else None,
                    "spotify_uri": text(item.get("uri")),
                    "isrc": (item.get("external_ids") or {}).get("isrc")}
            key = song["spotify_uri"] or "metadata:" + hashlib.sha256(json.dumps(song, sort_keys=True).encode()).hexdigest()
            if key not in songs:
                songs[key] = {"source_key": key, "song": song, "occurrences": []}
            songs[key]["occurrences"].append(ref)
    return list(songs.values()), skipped


def clean_candidate(item):
    if not isinstance(item, dict) or item.get("resultType") not in (None, "song"):
        return None
    identifier = text(item.get("videoId"))
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", identifier):
        return None
    album = item.get("album") or {}
    duration = item.get("duration_seconds")
    if not positive_number(duration):
        parts = text(item.get("duration")).split(":")
        duration = None
        if 2 <= len(parts) <= 3 and all(part.isdigit() for part in parts):
            duration = 0
            for part in parts:
                duration = duration * 60 + int(part)
    return {"video_id": identifier, "title": text(item.get("title")),
            "artists": [artist["name"] for artist in item.get("artists", [])
                        if isinstance(artist, dict) and text(artist.get("name"))],
            "album": text(album.get("name")), "duration_seconds": duration,
            "explicit": item.get("isExplicit") if isinstance(item.get("isExplicit"), bool) else None,
            "available": item.get("isAvailable") is not False}


def score_candidate(song, candidate):
    title = similarity(song["title"], candidate["title"])
    artists = min((max((similarity(source, target) for target in candidate["artists"]), default=0)
                   for source in song["artists"]), default=0)
    album = similarity(song["album"], candidate["album"])
    source_duration = song.get("duration_ms")
    target_duration = candidate.get("duration_seconds")
    delta = abs(source_duration / 1000 - target_duration) if positive_number(source_duration) and positive_number(target_duration) else None
    duration = max(0, 1 - delta / 30) if delta is not None else 0
    mismatch = variants(song["title"] + " " + song["album"]) != variants(candidate["title"] + " " + candidate["album"])
    explicit_known = song["explicit"] is not None and candidate["explicit"] is not None
    explicit_mismatch = explicit_known and song["explicit"] != candidate["explicit"]
    score = max(0, (45 * title + 30 * artists + 15 * duration + 10 * album) -
                (30 if mismatch else 0) - (20 if explicit_mismatch else 0))
    reasons = []
    if title < .95:
        reasons.append("title_differs")
    if artists < .95:
        reasons.append("artist_missing_or_differs")
    if delta is None:
        reasons.append("duration_missing")
    elif delta > max(2, source_duration / 1000 * .015):
        reasons.append("duration_differs")
    if album < .85:
        reasons.append("album_missing_or_differs")
    if mismatch:
        reasons.append("recording_version_differs")
    if explicit_mismatch:
        reasons.append("explicit_status_differs")
    if not explicit_known:
        reasons.append("explicit_status_unverified")
    if not candidate["available"]:
        reasons.append("unavailable")
        score = 0
    return {**candidate, "score": round(score, 2), "reasons": reasons,
            "duration_delta_seconds": round(delta, 2) if delta is not None else None,
            "strong_metadata_match": not reasons and score >= 95}


def rank(song, candidates):
    ranked = sorted((score_candidate(song, candidate) for candidate in candidates), key=lambda c: (-c["score"], c["video_id"]))
    if not ranked:
        return {"status": "unmatched", "candidates": [], "reason": "no_song_candidates"}
    first = ranked[0]
    margin = first["score"] - ranked[1]["score"] if len(ranked) > 1 else None
    strong = first["strong_metadata_match"] and (margin is None or margin >= 5)
    return {"status": "strong_suggestion" if strong else "needs_review", "candidates": ranked,
            "reason": "metadata_agrees" if strong else "close_alternatives" if first["strong_metadata_match"] else "metadata_uncertain",
            "margin": round(margin, 2) if margin is not None else None}


class SearchCache:
    def __init__(self, path, namespace="songs"):
        self.namespace = namespace
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS searches (key TEXT PRIMARY KEY, created REAL NOT NULL, payload TEXT NOT NULL)")

    def get(self, query):
        key = hashlib.sha256(f"{CACHE_VERSION}:{self.namespace}:{query}".encode()).hexdigest()
        row = self.db.execute("SELECT created,payload FROM searches WHERE key=?", (key,)).fetchone()
        if row:
            payload = json.loads(row[1])
            ttl = 7 * 86400 if payload else 86400
            if time.time() - row[0] < ttl:
                return payload
        return None

    def put(self, query, candidates):
        key = hashlib.sha256(f"{CACHE_VERSION}:{self.namespace}:{query}".encode()).hexdigest()
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO searches VALUES (?,?,?)", (key, time.time(), json.dumps(candidates)))


def csv_safe(value):
    value = text(value)
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")) else value


def write_match_reports(folder, report):
    temp = folder / "report.json.tmp"
    with temp.open("w", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temp, folder / "report.json")
    csv_temp = folder / "review.csv.tmp"
    with csv_temp.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["source_key", "source_title", "source_artists", "source_album", "source_duration_ms", "source_explicit",
                         "song_status", "candidate_rank", "target_title", "target_artists", "target_album", "target_duration_seconds",
                         "target_explicit", "score", "reasons", "listen_url"])
        for result in report["results"]:
            for index, candidate in enumerate(result.get("candidates") or [None], start=1):
                candidate = candidate or {}
                values = [result["source_key"], result["song"]["title"], "; ".join(result["song"]["artists"]),
                          result["song"]["album"], str(result["song"].get("duration_ms", "")), str(result["song"]["explicit"]), result["status"],
                          str(index), candidate.get("title", ""), "; ".join(candidate.get("artists", [])), candidate.get("album", ""),
                          str(candidate.get("duration_seconds", "")), str(candidate.get("explicit", "")), str(candidate.get("score", "")),
                          "; ".join(candidate.get("reasons", [])),
                          "https://music.youtube.com/watch?v=" + candidate["video_id"] if candidate.get("video_id") else ""]
                writer.writerow([csv_safe(value) for value in values])
    os.replace(csv_temp, folder / "review.csv")
    counts = dict(Counter(row["status"] for row in report["results"]))
    summary = {key: value for key, value in report.items() if key not in {"results", "skipped_occurrences"}}
    summary["statuses"] = counts
    (folder / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def match_music(archive=None, limit=20, request_budget=20, offline=False, progress=lambda _: None, searcher=None, pause=time.sleep):
    if limit < 0 or request_budget < 0:
        raise SetupError("Limits must be zero or positive.")
    source = choose_archive(archive)
    songs, skipped = extract_songs(source)
    selected = songs[:limit] if limit else songs
    root = prepare_private_storage() / "matching"
    root.mkdir(exist_ok=True)
    folder = root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    folder.mkdir()
    cache = SearchCache(root / "search-cache.sqlite3")
    report = {"schema_version": 1, "scoring_version": SCORING_VERSION, "source_archive": source.name,
              "state": "in_progress", "total_unique_songs": len(songs), "selected_songs": len(selected),
              "unselected_songs": len(songs) - len(selected), "skipped_occurrences": skipped,
              "network_searches": 0, "cache_hits": 0, "offline": offline,
              "results": [{**song, "status": "pending", "candidates": []} for song in selected]}
    write_match_reports(folder, report)
    progress(f"Private match reports: {folder}")
    progress(f"Selected {len(selected)} of {len(songs)} unique songs; skipped {len(skipped)} non-searchable occurrences")
    try:
        quiet_library_logs()
        for index, result in enumerate(report["results"]):
            song = result["song"]
            query = song["title"] + " " + " ".join(song["artists"])
            candidates = cache.get(query)
            if candidates is not None:
                report["cache_hits"] += 1
            elif offline:
                result["status"] = "not_cached"
                continue
            elif report["network_searches"] >= request_budget:
                report["state"] = "request_budget_reached"
                break
            else:
                if searcher is None:
                    # Public catalog queries need no account cookie and cannot change a library.
                    searcher = YTMusic(requests_session=TimedSession())
                if report["network_searches"]:
                    pause(1)
                report["network_searches"] += 1
                raw = searcher.search(query, filter="songs", limit=10, ignore_spelling=True)
                if not isinstance(raw, list):
                    raise SetupError("Unexpected YouTube Music search response.")
                unique = {}
                for item in raw[:10]:
                    candidate = clean_candidate(item)
                    if candidate:
                        unique.setdefault(candidate["video_id"], candidate)
                candidates = list(unique.values())
                cache.put(query, candidates)
            result.update(rank(song, candidates))
            write_match_reports(folder, report)
            progress(f"Song {index + 1}/{len(selected)}: {result['status']} (titles omitted)")
        if report["state"] == "in_progress":
            report["state"] = "completed_selected_sample"
    except (KeyboardInterrupt, Exception) as error:
        report["state"] = "interrupted_or_failed"
        report["failure_category"] = ("network" if isinstance(error, RequestException) else "local_io" if isinstance(error, OSError)
                                      else "interrupted" if isinstance(error, KeyboardInterrupt) else "response_or_local_processing")
        status = getattr(getattr(error, "response", None), "status_code", None) if isinstance(error, RequestException) else None
        if type(status) is int and 100 <= status <= 599:
            report["http_status"] = status
        write_match_reports(folder, report)
        raise SetupError(f"Matching stopped; completed results remain at {folder}. "
                         "No account changes or automatic retries. A rerun reuses cached successful searches.") from None
    finally:
        cache.db.close()
    write_match_reports(folder, report)
    return folder
