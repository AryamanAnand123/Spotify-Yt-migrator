"""Read-only public podcast matching, with source provenance and explicit gaps."""

import csv
import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
import time
import uuid
from collections import Counter
from datetime import datetime, timezone

from ytmusicapi import YTMusic

from spottoyt.archive import prepare_private_storage
from spottoyt.auth import SetupError, TimedSession, quiet_library_logs
from spottoyt.matching import (SearchCache, choose_archive, csv_safe, normalized,
                              positive_number, similarity, text)


def date_value(value):
    value = text(value).strip()
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    # Relative dates and year/month-only dates are not promoted to exact days.
    return None


def duration_value(value):
    if positive_number(value):
        return value, "seconds"
    value = text(value).strip().lower()
    if re.fullmatch(r"\d+:\d{2}(?::\d{2})?", value):
        parts = [int(part) for part in value.split(":")]
        if any(part >= 60 for part in parts[1:]):
            return None, "unknown"
        seconds = 0
        for part in parts:
            seconds = seconds * 60 + part
        return seconds, "seconds"
    pattern = r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b"
    matches = re.findall(pattern, value)
    if not matches or re.sub(pattern, "", value).strip(" ,"):
        return None, "unknown"
    seconds = sum(float(number) * (3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1)
                  for number, unit in matches)
    return seconds, "seconds" if any(unit.startswith("s") for _, unit in matches) else "rounded"


def episode_number(value):
    match = re.search(r"(?:\bep(?:isode)?\.?\s*|#)(\d+)\b|^\s*(\d+)[.:]\s", text(value), re.I)
    return int(next(group for group in match.groups() if group)) if match else None


def podcast_id(value):
    value = text(value)
    if value.startswith("MPSP"):
        value = value[4:]
    return "MPSP" + value if re.fullmatch(r"[A-Za-z0-9_-]{8,100}", value) else ""


def extract_podcasts(path):
    entries, skipped = {}, []
    with closing(sqlite3.connect((path / "archive.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
        meta = {key: json.loads(value) for key, value in db.execute("SELECT * FROM metadata")}
        if (meta.get("source") != "spotify_api" or meta.get("schema_version") != 1 or
                meta.get("state") not in ("complete", "completed_with_gaps")):
            raise SetupError("Choose a finished version-1 Spotify API archive for podcast matching.")
        for collection, position, raw in db.execute("SELECT * FROM records ORDER BY collection,position"):
            if collection not in ("saved_shows", "saved_episodes") and not collection.startswith("playlist_items/"):
                continue
            wrapper = json.loads(raw)
            item = wrapper.get("show") if collection == "saved_shows" else wrapper.get("episode") if collection == "saved_episodes" else wrapper.get("item", wrapper.get("track"))
            if collection.startswith("playlist_items/") and (not isinstance(item, dict) or item.get("type") != "episode"):
                continue
            ref = {"collection": collection, "position": position}
            if not isinstance(item, dict) or not text(item.get("name")):
                skipped.append({"source": ref, "reason": "missing_podcast_metadata"})
                continue
            kind = "show" if collection == "saved_shows" else "episode"
            show = item if kind == "show" else item.get("show") or {}
            data = {"kind": kind, "title": item["name"], "show_title": text(show.get("name")),
                    "publisher": text(show.get("publisher")), "spotify_uri": text(item.get("uri")),
                    "show_uri": text(show.get("uri")), "duration_ms": item.get("duration_ms") if kind == "episode" else None,
                    "release_date": date_value(item.get("release_date")),
                    "episode_number": episode_number(item["name"]) if kind == "episode" else None,
                    "source_resume_point_present": isinstance(item.get("resume_point"), dict)}
            key = data["spotify_uri"] or "metadata:" + hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
            key = kind + ":" + key
            if key not in entries:
                entries[key] = {"source_key": key, "podcast": data, "occurrences": []}
            entries[key]["occurrences"].append(ref)
    return list(entries.values()), skipped


def clean_podcast(item, kind):
    if not isinstance(item, dict) or item.get("resultType") not in (None, "podcast" if kind == "show" else "episode"):
        return None
    identifier = podcast_id(item.get("browseId") or item.get("podcastId")) if kind == "show" else text(item.get("videoId"))
    if not identifier or kind == "episode" and not re.fullmatch(r"[A-Za-z0-9_-]{11}", identifier):
        return None
    parent = item.get("podcast") if isinstance(item.get("podcast"), dict) else {}
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    duration, precision = duration_value(item.get("duration_seconds") or item.get("duration"))
    return {"target_id": identifier, "kind": kind, "title": text(item.get("title")),
            "show_title": text(item.get("title")) if kind == "show" else text(parent.get("name")),
            "show_id": identifier if kind == "show" else podcast_id(parent.get("id")),
            "publisher": text(author.get("name")), "release_date": date_value(item.get("date")),
            "duration_seconds": duration, "duration_precision": precision,
            "available": item.get("isAvailable") is not False, "live": item.get("live") is True,
            "episode_number": episode_number(item.get("title")) if kind == "episode" else None}


def clean_show_detail(raw):
    if not isinstance(raw, dict):
        raise SetupError("Unexpected podcast detail response.")
    author = raw.get("author") if isinstance(raw.get("author"), dict) else {}
    return {"title": text(raw.get("title")), "publisher": text(author.get("name"))}


def clean_episode_detail(raw):
    if not isinstance(raw, dict):
        raise SetupError("Unexpected episode detail response.")
    duration, precision = duration_value(raw.get("duration_seconds") or raw.get("duration"))
    return {"title": text(raw.get("title")), "release_date": date_value(raw.get("date")),
            "show_id": podcast_id(raw.get("playlistId")),
            "duration_seconds": duration, "duration_precision": precision}


def score_podcast(source, candidate):
    title = similarity(source["title"], candidate["title"])
    publisher = similarity(source["publisher"], candidate["publisher"])
    reasons = []
    if title < .95:
        reasons.append("title_differs")
    if publisher < .9:
        reasons.append("publisher_missing_or_differs")
    delta = None
    if source["kind"] == "show":
        score = 70 * title + 30 * publisher
    else:
        show = similarity(source["show_title"], candidate["show_title"])
        if show < .95:
            reasons.append("show_identity_missing_or_differs")
        if positive_number(source.get("duration_ms")) and positive_number(candidate.get("duration_seconds")):
            delta = abs(source["duration_ms"] / 1000 - candidate["duration_seconds"])
        if delta is None:
            reasons.append("duration_missing")
        elif delta > max(10, source["duration_ms"] / 1000 * .01):
            reasons.append("duration_differs")
        if candidate["duration_precision"] == "rounded":
            reasons.append("duration_is_rounded")
        same_date = source.get("release_date") is not None and candidate.get("release_date") == source["release_date"]
        if not same_date:
            reasons.append("release_date_missing_or_differs")
        if source.get("episode_number") != candidate.get("episode_number"):
            reasons.append("episode_number_missing_or_differs")
        duration_score = max(0, 1 - delta / 120) if delta is not None else 0
        score = 45 * title + 25 * show + 10 * publisher + 10 * duration_score + 10 * same_date
    if candidate.get("live"):
        reasons.append("live_broadcast_needs_review")
    if not candidate["available"]:
        reasons.append("unavailable")
        score = 0
    return {**candidate, "score": round(score, 2), "reasons": reasons,
            "duration_delta_seconds": round(delta, 2) if delta is not None else None,
            "strong_metadata_match": not reasons and score >= 95}


def rank_podcasts(source, candidates):
    ordered = sorted((score_podcast(source, item) for item in candidates), key=lambda c: (-c["score"], c["target_id"]))
    if not ordered:
        return {"status": "unmatched", "candidates": [], "reason": "no_usable_catalog_candidates"}
    margin = ordered[0]["score"] - ordered[1]["score"] if len(ordered) > 1 else None
    strong = ordered[0]["strong_metadata_match"] and (margin is None or margin >= 5)
    return {"status": "strong_suggestion" if strong else "needs_review", "candidates": ordered,
            "margin": margin, "reason": "metadata_agrees" if strong else "metadata_uncertain_or_ambiguous"}


class BudgetReached(Exception):
    pass


class Catalog:
    def __init__(self, cache, budget, offline, searcher=None, pause=time.sleep):
        self.cache, self.budget, self.offline = cache, budget, offline
        self.client, self.pause = searcher, pause
        self.calls = self.hits = 0

    def read(self, key, request, sanitize):
        cached = self.cache.get(key)
        if cached is not None:
            self.hits += 1
            return cached
        if self.offline:
            return None
        if self.calls >= self.budget:
            raise BudgetReached
        if self.client is None:
            self.client = YTMusic(requests_session=TimedSession())
        if self.calls:
            self.pause(1)
        self.calls += 1
        result = sanitize(request(self.client))
        self.cache.put(key, result)
        return result


def search_results(raw, kind):
    if not isinstance(raw, list):
        raise SetupError("Unexpected podcast search response.")
    unique = {}
    for item in raw[:10]:
        candidate = clean_podcast(item, kind)
        if candidate:
            unique.setdefault(candidate["target_id"], candidate)
    return list(unique.values())


def enrich(candidate, catalog):
    if candidate["kind"] == "episode":
        identifier = candidate["target_id"]
        detail = catalog.read("episode:" + identifier, lambda client: client.get_episode(identifier), clean_episode_detail)
        if detail:
            for key, value in detail.items():
                if value is not None and value != "" and value != "unknown":
                    candidate[key] = value
            candidate["episode_number"] = episode_number(candidate["title"])
    show_id = candidate["show_id"]
    if show_id:
        detail = catalog.read("show:" + show_id, lambda client: client.get_podcast(show_id, limit=1), clean_show_detail)
        if detail:
            candidate["show_title"] = detail["title"] or candidate["show_title"]
            candidate["publisher"] = detail["publisher"] or candidate["publisher"]
            if candidate["kind"] == "show":
                candidate["title"] = candidate["show_title"]


def write_reports(folder, report):
    temp = folder / "report.json.tmp"
    with temp.open("w", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temp, folder / "report.json")
    with (folder / "review.csv.tmp").open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["kind", "source_key", "source_title", "source_show", "source_publisher", "source_date", "source_duration_ms",
                         "item_status", "candidate_rank", "target_title", "target_show", "target_publisher", "target_date", "target_duration_seconds",
                         "duration_precision", "score", "reasons", "listen_url"])
        for result in report["results"]:
            source = result["podcast"]
            for index, candidate in enumerate(result.get("candidates") or [{}], 1):
                identifier = candidate.get("target_id", "")
                url = ("https://music.youtube.com/browse/" if source["kind"] == "show" else "https://music.youtube.com/watch?v=") + identifier if identifier else ""
                values = [source["kind"], result["source_key"], source["title"], source["show_title"], source["publisher"], str(source["release_date"]),
                          str(source["duration_ms"]), result["status"], str(index), candidate.get("title", ""), candidate.get("show_title", ""),
                          candidate.get("publisher", ""), str(candidate.get("release_date", "")), str(candidate.get("duration_seconds", "")),
                          candidate.get("duration_precision", ""), str(candidate.get("score", "")), "; ".join(candidate.get("reasons", [])), url]
                writer.writerow([csv_safe(value) for value in values])
    os.replace(folder / "review.csv.tmp", folder / "review.csv")
    summary = {key: value for key, value in report.items() if key not in ("results", "skipped_occurrences")}
    summary["statuses"] = dict(Counter(result["status"] for result in report["results"]))
    (folder / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def match_podcasts(archive=None, limit=20, request_budget=20, offline=False, progress=lambda _: None, searcher=None, pause=time.sleep):
    if limit < 0 or request_budget < 0:
        raise SetupError("Limits must be zero or positive.")
    source = choose_archive(archive)
    entries, skipped = extract_podcasts(source)
    selected = entries[:limit] if limit else entries
    root = prepare_private_storage() / "podcast-matching"
    root.mkdir(exist_ok=True)
    folder = root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    folder.mkdir()
    cache = SearchCache(root / "catalog-cache.sqlite3", namespace="podcasts-v1")
    catalog = Catalog(cache, request_budget, offline, searcher, pause)
    report = {"schema_version": 1, "scoring_version": 1, "source_archive": source.name,
              "state": "in_progress", "total_unique_items": len(entries), "selected_items": len(selected),
              "unselected_items": len(entries) - len(selected), "skipped_occurrences": skipped,
              "network_reads": 0, "cache_hits": 0, "offline": offline,
              "destination_capabilities": {"show_library_save": "documented_not_write_tested",
                                           "saved_episode": "documented_not_write_tested",
                                           "playback_position": "no_mapping_implemented", "history": "no_mapping_implemented"},
              "results": [{**entry, "status": "pending", "candidates": []} for entry in selected]}

    def checkpoint():
        report.update(network_reads=catalog.calls, cache_hits=catalog.hits)
        write_reports(folder, report)

    checkpoint()
    progress(f"Private podcast reports: {folder}")
    progress(f"Selected {len(selected)} of {len(entries)} unique shows/episodes")
    try:
        quiet_library_logs()
        for index, result in enumerate(report["results"]):
            item = result["podcast"]
            query = item["title"] + " " + (item["publisher"] if item["kind"] == "show" else item["show_title"])
            kind_filter = "podcasts" if item["kind"] == "show" else "episodes"
            candidates = []
            try:
                candidates = catalog.read(kind_filter + ":" + query,
                                          lambda client: client.search(query, filter=kind_filter, limit=10, ignore_spelling=True),
                                          lambda raw: search_results(raw, item["kind"]))
                if candidates is None:
                    result["status"] = "not_cached"
                    continue
                if not candidates and normalized(query) != normalized(item["title"]):
                    candidates = catalog.read(kind_filter + ":" + item["title"],
                                              lambda client: client.search(item["title"], filter=kind_filter, limit=10, ignore_spelling=True),
                                              lambda raw: search_results(raw, item["kind"]))
                    if candidates is None:
                        result["status"] = "not_cached"
                        continue
                # Enrich only the three best preliminary candidates; other results remain reviewable.
                candidates.sort(key=lambda c: -score_podcast(item, c)["score"])
                for candidate in candidates[:3]:
                    enrich(candidate, catalog)
                result.update(rank_podcasts(item, candidates))
            except BudgetReached:
                if candidates:
                    result.update(rank_podcasts(item, candidates))
                report["state"] = "request_budget_reached"
                break
            checkpoint()
            progress(f"Podcast item {index + 1}/{len(selected)}: {result['status']} (titles omitted)")
        if report["state"] == "in_progress":
            report["state"] = "completed_selected_items"
    except (KeyboardInterrupt, Exception):
        report["state"] = "interrupted_or_failed"
        checkpoint()
        raise SetupError(f"Podcast matching stopped; completed results remain at {folder}. "
                         "No account changes or automatic retries; successful reads are cached.") from None
    finally:
        cache.db.close()
    checkpoint()
    return folder
