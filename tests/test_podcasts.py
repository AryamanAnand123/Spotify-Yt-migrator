"""Podcast identity, parsing, bounded catalog access and offline safety."""

import hashlib
import json
from unittest.mock import Mock

import pytest

from spottoyt.archive import Archive
from spottoyt.auth import SetupError
from spottoyt.matching import SearchCache
from spottoyt.podcasts import (date_value, duration_value, extract_podcasts,
                              match_podcasts, rank_podcasts, search_results)


@pytest.fixture
def source(monkeypatch, tmp_path):
    monkeypatch.setenv("SPOTTOYT_DATA_DIR", str(tmp_path / "private"))
    archive = Archive.create("spotify_api")
    show = {"uri": "spotify:show:synthetic", "name": "Example Podcast", "publisher": "Example Publisher"}
    episode = {"uri": "spotify:episode:synthetic", "type": "episode", "name": "Episode 12: Example Topic",
               "show": show, "release_date": "2026-01-02", "duration_ms": 1200000, "resume_point": {"resume_position_ms": 5000}}
    archive.append("saved_shows", [{"show": show}])
    archive.append("saved_episodes", [{"episode": episode}])
    archive.append("playlist_items/abc", [{"item": episode}, {"item": episode}, {"item": {"type": "track"}}])
    archive.finish("completed_with_gaps")
    archive.close()
    return archive.path


@pytest.fixture
def episode():
    return {"kind": "episode", "title": "Episode 12: Example Topic", "show_title": "Example Podcast",
            "publisher": "Example Publisher", "duration_ms": 1200000, "release_date": "2026-01-02", "episode_number": 12}


@pytest.fixture
def candidate():
    return {"kind": "episode", "target_id": "abcdefghijk", "title": "Episode 12: Example Topic", "show_title": "Example Podcast",
            "publisher": "Example Publisher", "duration_seconds": 1200, "duration_precision": "seconds", "release_date": "2026-01-02",
            "episode_number": 12, "available": True, "live": False}


def client():
    mock = Mock()
    def search(query, **kwargs):
        if kwargs["filter"] == "podcasts":
            return [{"resultType": "podcast", "browseId": "MPSPPLsynthetic123", "title": "Example Podcast"}]
        return [{"resultType": "episode", "videoId": "abcdefghijk", "title": "Episode 12: Example Topic",
                 "podcast": {"id": "MPSPPLsynthetic123", "name": "Example Podcast"}}]
    mock.search.side_effect = search
    mock.get_podcast.return_value = {"title": "Example Podcast", "author": {"name": "Example Publisher"},
                                     "episodes": [{"PRIVATE": "not retained"}]}
    mock.get_episode.return_value = {"title": "Episode 12: Example Topic", "date": "Jan 2, 2026", "duration": "20:00",
                                     "playlistId": "MPSPPLsynthetic123", "description": "not retained"}
    return mock


@pytest.mark.parametrize("value,expected", [("2026-01-02", "2026-01-02"), ("Jan 2, 2026", "2026-01-02"),
                                          ("3 months ago", None), ("2026-01", None), ("2026", None)])
def test_partial_or_relative_dates_are_not_invented(value, expected):
    assert date_value(value) == expected


@pytest.mark.parametrize("value,seconds,precision", [("20:00", 1200, "seconds"), ("1:02:03", 3723, "seconds"),
                                                   ("20 min", 1200, "rounded"), ("1 hr 20 min", 4800, "rounded"),
                                                   ("3 months ago", None, "unknown"), ("2:99", None, "unknown")])
def test_duration_parsing_records_precision(value, seconds, precision):
    assert duration_value(value) == (seconds, precision)


def test_exact_episode_metadata_is_suggestion(episode, candidate):
    assert rank_podcasts(episode, [candidate])["status"] == "strong_suggestion"


@pytest.mark.parametrize("change,reason", [
    ({"publisher": "Other Publisher"}, "publisher_missing_or_differs"),
    ({"show_title": "Another Show"}, "show_identity_missing_or_differs"),
    ({"release_date": "2026-01-03"}, "release_date_missing_or_differs"),
    ({"release_date": None}, "release_date_missing_or_differs"),
    ({"episode_number": 13}, "episode_number_missing_or_differs"),
    ({"duration_seconds": None}, "duration_missing"),
    ({"duration_precision": "rounded"}, "duration_is_rounded"),
    ({"duration_seconds": 400}, "duration_differs"),
    ({"available": False}, "unavailable"),
])
def test_repeated_titles_and_uncertain_metadata_require_review(episode, candidate, change, reason):
    result = rank_podcasts(episode, [{**candidate, **change}])
    assert result["status"] == "needs_review"
    assert reason in result["candidates"][0]["reasons"]


def test_equal_alternatives_remain_ambiguous(episode, candidate):
    assert rank_podcasts(episode, [candidate, {**candidate, "target_id": "zyxwvutsrqp"}])["status"] == "needs_review"


def test_same_named_show_wrong_publisher_not_strong():
    source = {"kind": "show", "title": "Example Podcast", "publisher": "Example Publisher"}
    candidate = {"target_id": "MPSPPLsynthetic123", "kind": "show", "title": "Example Podcast",
                 "publisher": "Unrelated Publisher", "available": True}
    assert rank_podcasts(source, [candidate])["status"] == "needs_review"


def test_extract_preserves_occurrences_not_playback_position(source):
    before = hashlib.sha256((source / "archive.sqlite3").read_bytes()).hexdigest()
    entries, skipped = extract_podcasts(source)
    assert len(entries) == 2 and not skipped
    episode = next(entry for entry in entries if entry["podcast"]["kind"] == "episode")
    assert len(episode["occurrences"]) == 3
    assert episode["podcast"]["source_resume_point_present"] is True
    assert "resume_position_ms" not in json.dumps(entries)
    assert hashlib.sha256((source / "archive.sqlite3").read_bytes()).hexdigest() == before


def test_search_strips_private_fields_and_rejects_nonpodcasts():
    results = search_results([{"resultType": "episode", "videoId": "abcdefghijk", "title": "Example",
                               "feedbackTokens": "SECRET"}, {"resultType": "song", "videoId": "zyxwvutsrqp"}], "episode")
    assert len(results) == 1
    assert "SECRET" not in str(results)


def test_live_simulation_and_offline_rerun_use_cache(source):
    mock = client()
    path = match_podcasts(source, searcher=mock, pause=lambda _: None)
    report = json.loads((path / "report.json").read_text())
    assert report["network_reads"] == 4  # two searches, one episode read, one shared show read
    assert all(result["status"] == "strong_suggestion" for result in report["results"])
    assert "not retained" not in (path / "report.json").read_text()
    second = match_podcasts(source, offline=True)
    cached = json.loads((second / "report.json").read_text())
    assert cached["results"] == report["results"]
    assert cached["network_reads"] == 0
    assert mock.search.call_count == 2
    assert mock.get_episode.call_count == 1
    assert mock.get_podcast.call_count == 1


def test_offline_cache_miss_never_constructs_client(source, monkeypatch):
    from spottoyt import podcasts
    network = Mock(side_effect=AssertionError("network forbidden"))
    monkeypatch.setattr(podcasts, "YTMusic", network)
    path = match_podcasts(source, offline=True)
    assert all(row["status"] == "not_cached" for row in json.loads((path / "report.json").read_text())["results"])
    network.assert_not_called()


def test_budget_includes_detail_reads(source):
    mock = client()
    path = match_podcasts(source, request_budget=1, searcher=mock)
    report = json.loads((path / "report.json").read_text())
    assert report["network_reads"] == 1
    assert report["state"] == "request_budget_reached"
    mock.get_episode.assert_not_called()
    mock.get_podcast.assert_not_called()


def test_no_results_report_unmatched(source):
    mock = Mock()
    mock.search.return_value = []
    path = match_podcasts(source, searcher=mock, pause=lambda _: None)
    assert all(row["status"] == "unmatched" for row in json.loads((path / "report.json").read_text())["results"])


def test_empty_specific_search_uses_title_only_fallback(source):
    mock = client()
    normal = mock.search.side_effect
    def search(query, **kwargs):
        if kwargs["filter"] == "episodes" and query.endswith("Example Podcast"):
            return []
        return normal(query, **kwargs)
    mock.search.side_effect = search
    path = match_podcasts(source, searcher=mock, pause=lambda _: None)
    report = json.loads((path / "report.json").read_text())
    assert all(row["status"] == "strong_suggestion" for row in report["results"])
    episode_queries = [call.args[0] for call in mock.search.call_args_list if call.kwargs["filter"] == "episodes"]
    assert episode_queries == ["Episode 12: Example Topic Example Podcast", "Episode 12: Example Topic"]


def test_error_stops_safely_without_response_body(source):
    mock = Mock()
    mock.search.side_effect = RuntimeError("SECRET-RESPONSE")
    with pytest.raises(SetupError) as failure:
        match_podcasts(source, searcher=mock)
    assert "SECRET" not in str(failure.value)
    reports = list((source.parent.parent / "podcast-matching").glob("*/report.json"))
    assert json.loads(reports[0].read_text())["state"] == "interrupted_or_failed"


def test_cache_namespaces_do_not_collide(tmp_path):
    first = SearchCache(tmp_path / "cache.sqlite3", namespace="songs")
    second = SearchCache(tmp_path / "cache.sqlite3", namespace="podcasts-v1")
    first.put("same query", ["song"])
    assert second.get("same query") is None
    first.db.close()
    second.db.close()
