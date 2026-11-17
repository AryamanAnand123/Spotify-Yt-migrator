"""Music matching behavior with synthetic metadata and no network or accounts."""

import hashlib
import json
from unittest.mock import Mock

import pytest

from spottoyt.archive import Archive
from spottoyt.auth import SetupError
from spottoyt.matching import (SearchCache, choose_archive, clean_candidate, csv_safe,
                              extract_songs, match_music, normalized, rank, score_candidate)


@pytest.fixture
def song():
    return {"title": "Example Song", "artists": ["Example Artist"], "album": "Example Album",
            "duration_ms": 200000, "explicit": False}


@pytest.fixture
def candidate():
    return {"video_id": "abcdefghijk", "title": "Example Song", "artists": ["Example Artist"],
            "album": "Example Album", "duration_seconds": 200, "explicit": False, "available": True}


@pytest.fixture
def source(monkeypatch, tmp_path):
    monkeypatch.setenv("SPOTTOYT_DATA_DIR", str(tmp_path / "private"))
    archive = Archive.create("spotify_api")
    first = {"type": "track", "uri": "spotify:track:first", "name": "Example Song", "artists": [{"name": "Example Artist"}],
             "album": {"name": "Example Album"}, "duration_ms": 200000, "explicit": False}
    second = {**first, "uri": "spotify:track:second", "name": "Other Song"}
    archive.append("liked_songs", [{"track": first}, {"track": second}])
    archive.append("playlist_items/abc", [{"item": first}, {"item": first}, {"item": None},
                                          {"item": {"type": "episode"}}, {"item": {**first, "is_local": True}}])
    archive.finish("completed_with_gaps")
    archive.close()
    return archive.path


def test_exact_metadata_is_suggestion_not_approval(song, candidate):
    result = rank(song, [candidate])
    assert result["status"] == "strong_suggestion"
    assert result["candidates"][0]["score"] == 100
    assert "approved" not in result


def test_unicode_vowel_marks_are_not_discarded():
    assert normalized("कल") != normalized("कला")
    assert normalized("Café") == normalized("Cafe\u0301")


@pytest.mark.parametrize("changes,reason", [
    ({"title": "Example Song (Live)"}, "recording_version_differs"),
    ({"title": "Example Song (Remix)"}, "recording_version_differs"),
    ({"album": "Example Album (Remastered)"}, "recording_version_differs"),
    ({"artists": ["Cover Performer"]}, "artist_missing_or_differs"),
    ({"duration_seconds": 250}, "duration_differs"),
    ({"duration_seconds": None}, "duration_missing"),
    ({"explicit": True}, "explicit_status_differs"),
    ({"explicit": None}, "explicit_status_unverified"),
    ({"available": False}, "unavailable"),
])
def test_conflicting_or_missing_metadata_needs_review(song, candidate, changes, reason):
    result = rank(song, [{**candidate, **changes}])
    assert result["status"] == "needs_review"
    assert reason in result["candidates"][0]["reasons"]


def test_equal_alternatives_are_ambiguous(song, candidate):
    result = rank(song, [candidate, {**candidate, "video_id": "zyxwvutsrqp"}])
    assert result["status"] == "needs_review"
    assert result["reason"] == "close_alternatives"


def test_featured_artist_cannot_be_silently_lost(song, candidate):
    assert not score_candidate({**song, "artists": ["Example Artist", "Featured Artist"]}, candidate)["strong_metadata_match"]


def test_candidate_sanitization_removes_service_tokens():
    cleaned = clean_candidate({"videoId": "abcdefghijk", "title": "Example", "duration": "3:20",
                               "feedbackTokens": {"add": "SECRET"}, "authorization": "SECRET"})
    assert cleaned["duration_seconds"] == 200
    assert "SECRET" not in str(cleaned)
    assert clean_candidate({"videoId": "javascript:bad"}) is None


def test_source_dedup_keeps_occurrences_and_archive_unchanged(source):
    before = hashlib.sha256((source / "archive.sqlite3").read_bytes()).hexdigest()
    songs, skipped = extract_songs(source)
    assert len(songs) == 2
    assert len(songs[0]["occurrences"]) == 3
    assert len(skipped) == 3
    assert hashlib.sha256((source / "archive.sqlite3").read_bytes()).hexdigest() == before


def test_default_archive_does_not_pick_synthetic_download(source):
    other = Archive.create("spotify_account_download")
    other.close()
    assert choose_archive() == source


def test_partial_source_not_selected_or_silently_matched(source):
    partial = Archive.create("spotify_api")
    partial.finish("interrupted_or_partial")
    partial.close()
    assert choose_archive() == source
    with pytest.raises(SetupError, match="finished"):
        extract_songs(partial.path)


def test_cache_expiry_including_empty_searches(tmp_path, monkeypatch):
    from spottoyt import matching
    monkeypatch.setattr(matching.time, "time", lambda: 1000)
    cache = SearchCache(tmp_path / "cache.sqlite3")
    cache.put("query", [])
    assert cache.get("query") == []
    monkeypatch.setattr(matching.time, "time", lambda: 1000 + 86401)
    assert cache.get("query") is None
    cache.db.close()


def test_match_run_uses_songs_filter_and_cached_rerun(source):
    searcher = Mock()
    searcher.search.return_value = [{"videoId": "abcdefghijk", "resultType": "song", "title": "Example Song",
                                    "artists": [{"name": "Example Artist"}], "album": {"name": "Example Album"},
                                    "duration_seconds": 200, "isExplicit": False}]
    path = match_music(source, limit=1, searcher=searcher, pause=lambda _: None)
    assert searcher.search.call_args.kwargs == {"filter": "songs", "limit": 10, "ignore_spelling": True}
    report = json.loads((path / "report.json").read_text())
    assert report["network_searches"] == 1
    assert report["unselected_songs"] == 1
    assert report["results"][0]["status"] == "strong_suggestion"
    import csv
    with (path / "review.csv").open(encoding="utf-8-sig", newline="") as data:
        review = list(csv.DictReader(data))
    assert review[0]["source_album"] == "Example Album"
    assert review[0]["target_album"] == "Example Album"
    assert review[0]["target_duration_seconds"] == "200"
    assert review[0]["candidate_rank"] == "1"
    assert review[0]["song_status"] == "strong_suggestion"
    assert review[0]["listen_url"] == "https://music.youtube.com/watch?v=abcdefghijk"
    cached = match_music(source, limit=1, offline=True)
    assert json.loads((cached / "report.json").read_text())["cache_hits"] == 1
    assert searcher.search.call_count == 1


def test_offline_cache_miss_never_initializes_network(source, monkeypatch):
    from spottoyt import matching
    network = Mock(side_effect=AssertionError("network forbidden"))
    monkeypatch.setattr(matching, "YTMusic", network)
    path = match_music(source, offline=True)
    network.assert_not_called()
    report = json.loads((path / "report.json").read_text())
    assert all(result["status"] == "not_cached" for result in report["results"])


def test_budget_limits_requests_and_leaves_pending_results(source):
    searcher = Mock()
    searcher.search.return_value = []
    path = match_music(source, limit=0, request_budget=1, searcher=searcher)
    report = json.loads((path / "report.json").read_text())
    assert report["state"] == "request_budget_reached"
    assert [row["status"] for row in report["results"]] == ["unmatched", "pending"]
    assert searcher.search.call_count == 1


def test_failed_request_saves_partial_progress_without_body(source):
    searcher = Mock()
    searcher.search.side_effect = [[], RuntimeError("SECRET-ERROR")]
    with pytest.raises(SetupError) as failure:
        match_music(source, limit=0, searcher=searcher, pause=lambda _: None)
    assert "SECRET" not in str(failure.value)
    folders = list((source.parent.parent / "matching").glob("*/report.json"))
    report = json.loads(folders[0].read_text())
    assert report["state"] == "interrupted_or_failed"
    assert report["results"][0]["status"] == "unmatched"
    assert report["results"][1]["status"] == "pending"
    assert report["failure_category"] == "response_or_local_processing"


def test_rate_limit_records_only_safe_status_and_does_not_retry(source):
    from requests import HTTPError
    response = Mock(status_code=429, text='SECRET-PROVIDER-BODY')
    searcher = Mock()
    searcher.search.side_effect = HTTPError('SECRET-ERROR', response=response)
    with pytest.raises(SetupError):
        match_music(source, limit=0, searcher=searcher, pause=lambda _: None)
    report_path = next((source.parent.parent / 'matching').glob('*/report.json'))
    report = json.loads(report_path.read_text())
    assert report['failure_category'] == 'network'
    assert report['http_status'] == 429
    assert 'SECRET' not in report_path.read_text()
    searcher.search.assert_called_once()


@pytest.mark.parametrize("value", ["=HYPERLINK(example)", "+formula", "-formula", "@formula", "  =formula", "\tformula"])
def test_csv_formula_cells_are_neutralized(value):
    assert csv_safe(value).startswith("'")
