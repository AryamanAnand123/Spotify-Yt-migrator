"""Additive transfer behavior with fake destination state; never touches accounts."""

import json
from unittest.mock import Mock

import pytest

from spottoyt.archive import Archive
from spottoyt.auth import SetupError
from spottoyt.matching import extract_songs
from spottoyt.review import ReviewStore, load_report
from spottoyt.transfers import (Destination, create_plan, execute, execution_lock,
                               load_plan, operation_counts, plan_folder, preflight)

IDS = ["abcdefghijk", "zyxwvutsrqp"]


@pytest.fixture
def reviewed(monkeypatch, tmp_path):
    root = tmp_path / "private"
    monkeypatch.setenv("SPOTTOYT_DATA_DIR", str(root))
    archive = Archive.create("spotify_api")
    songs = [{"uri": "spotify:track:" + str(index), "type": "track", "name": title,
              "artists": [{"name": "Example Artist"}], "album": {"name": "Example Album"},
              "duration_ms": 180000, "explicit": False} for index, title in enumerate(("First song", "Second song"))]
    archive.append("liked_songs", [{"track": song} for song in songs])
    archive.append("playlists", [{"id": "source1", "name": "Example playlist"}])
    archive.append("playlist_items/source1", [{"item": songs[index]} for index in (0, 1, 0)])
    archive.coverage("playlist_items/source1", "complete", 3)
    archive.finish("completed_with_gaps")
    archive.close()
    rows, _ = extract_songs(archive.path)
    results = [{**row, "status": "strong_suggestion", "candidates": [{"video_id": IDS[index], "title": row["song"]["title"],
                "duration_seconds": 180, "available": True, "score": 100, "reasons": [], "strong_metadata_match": True}]}
               for index, row in enumerate(rows)]
    folder = root / "matching" / "20260101T000000Z-aaaaaaaa"
    folder.mkdir(parents=True)
    (folder / "report.json").write_text(json.dumps({"schema_version": 1, "source_archive": archive.path.name,
        "state": "completed_selected_sample", "unselected_songs": 0, "results": results}))
    report = load_report("music", folder.name)
    store = ReviewStore()
    for index, row in enumerate(results):
        store.save(report, row["source_key"], "approved", candidate_id=IDS[index], confirmed=True)
    store.close()
    return report


class FakeYouTube:
    def __init__(self):
        self.likes, self.library = set(), set()
        self.playlists, self.writes = {}, []
        self.create_count = 0
        self.like_timeout = self.create_timeout = self.append_timeout = False
        self.partial_append = False
        self.before_create_timeout = False
        self.handle = "@SyntheticUser"
        self.shows, self.episodes = set(), set()

    def get_account_info(self):
        return {"channelHandle": self.handle, "accountName": "Synthetic User", "accountPhotoUrl": "PRIVATE-PHOTO"}

    def get_song(self, identifier):
        return {"playabilityStatus": {"status": "OK"}, "videoDetails": {"videoId": identifier,
                "title": "Episode 1" if identifier == "lmnopqrstuv" else "First song" if identifier == IDS[0] else "Second song",
                "lengthSeconds": "1200" if identifier == "lmnopqrstuv" else "180"},
                "streamingData": {"url": "SECRET-SIGNED-STREAM"}}

    def get_watch_playlist(self, videoId, limit):
        return {"tracks": [{"videoId": videoId, "likeStatus": "LIKE" if videoId in self.likes else "INDIFFERENT",
                            "inLibrary": videoId in self.library, "feedbackTokens": {"add": "SECRET-FEEDBACK:" + videoId}}]}

    def rate_song(self, identifier, rating):
        assert rating == "LIKE"
        self.writes.append(("like", identifier))
        self.likes.add(identifier)
        if self.like_timeout:
            self.like_timeout = False
            raise TimeoutError("PRIVATE-RESPONSE")
        return {}

    def edit_song_library_status(self, tokens):
        self.library.add(tokens[0].split(":")[1])
        self.writes.append(("library", tokens[0].split(":")[1]))
        return {}

    def get_library_playlists(self, limit=None):
        return [{"playlistId": key, "title": value["title"], "owned": True} for key, value in self.playlists.items()]

    def get_playlist(self, identifier, limit=None):
        state = self.playlists[identifier]
        return {**state, "tracks": [{"videoId": item} for item in state["video_ids"]], "trackCount": len(state["video_ids"])}

    def create_playlist(self, title, description, privacy_status):
        assert privacy_status == "PRIVATE"
        if self.before_create_timeout:
            self.before_create_timeout = False
            raise TimeoutError("PRIVATE-RESPONSE")
        self.create_count += 1
        identifier = f"PLsynthetic{self.create_count:03}"
        self.playlists[identifier] = {"title": title, "description": description, "privacy": privacy_status, "video_ids": []}
        self.writes.append(("create", identifier))
        if self.create_timeout:
            self.create_timeout = False
            raise TimeoutError("PRIVATE-RESPONSE")
        return identifier

    def add_playlist_items(self, identifier, video_ids, duplicates=False):
        if identifier == "SE":
            assert duplicates is False
            self.episodes.update(video_ids)
            self.writes.append(("episode", tuple(video_ids)))
            return {"status": "STATUS_SUCCEEDED"}
        assert duplicates is True
        self.playlists[identifier]["video_ids"].extend(video_ids[:1] if self.partial_append else video_ids)
        self.writes.append(("append", tuple(video_ids)))
        if self.append_timeout or self.partial_append:
            self.append_timeout = self.partial_append = False
            raise TimeoutError("PRIVATE-RESPONSE")
        return {"status": "STATUS_SUCCEEDED"}

    def get_podcast(self, identifier, limit=1):
        return {"title": "Example Show", "saved": identifier in self.shows}

    def get_episode(self, identifier):
        return {"title": "Episode 1", "saved": identifier in self.episodes}

    def rate_playlist(self, identifier, rating):
        assert rating == "LIKE"
        self.shows.add(identifier)
        self.writes.append(("show", identifier))
        return {}


def destination(client):
    return Destination(client=client, credential_hash="synthetic-session", pause=lambda _: None)


def prepared(reviewed, client=None, **options):
    client = client or FakeYouTube()
    folder = create_plan([reviewed], **options)
    checked = preflight(folder.name, destination(client))
    assert checked["state"] == "ready"
    return folder, client


def test_plan_is_offline_preserves_duplicates_and_separates_library_saves(reviewed):
    folder = create_plan([reviewed])
    plan = load_plan(folder.name)
    assert operation_counts(plan) == {"like_song": 2, "save_song": 0, "save_show": 0, "save_episode": 0, "playlist": 1}
    playlist = next(op for op in plan["operations"] if op["action"] == "playlist")
    assert playlist["video_ids"] == [IDS[0], IDS[1], IDS[0]]
    assert playlist["privacy"] == "PRIVATE"


def test_preflight_is_read_only_and_excludes_sensitive_response_fields(reviewed):
    folder, client = prepared(reviewed)
    assert not client.writes
    text = (folder / "preflight.json").read_text()
    assert "SECRET" not in text and "PRIVATE-PHOTO" not in text


def test_execute_and_rerun_do_not_duplicate_likes_or_playlist_entries(reviewed):
    folder, client = prepared(reviewed)
    result = execute(folder.name, confirmed=True, destination=destination(client))
    assert result["state"] == "verified"
    before = list(client.writes)
    execute(folder.name, confirmed=True, destination=destination(client))
    assert client.writes == before
    assert client.create_count == 1
    assert next(iter(client.playlists.values()))["video_ids"] == [IDS[0], IDS[1], IDS[0]]


def test_optional_library_saves_use_ephemeral_tokens_not_journal(reviewed):
    folder, client = prepared(reviewed, also_save_to_library=True)
    execute(folder.name, confirmed=True, destination=destination(client))
    assert client.library == set(IDS)
    assert "SECRET" not in (folder.parent / "journal.sqlite3").read_bytes().decode(errors="ignore")


def test_timeout_after_like_is_reconciled_without_repeating_write(reviewed):
    client = FakeYouTube()
    client.like_timeout = True
    folder, client = prepared(reviewed, client)
    with pytest.raises(SetupError, match="stopped"):
        execute(folder.name, confirmed=True, destination=destination(client))
    assert IDS[0] in client.likes
    execute(folder.name, confirmed=True, destination=destination(client))
    assert client.writes.count(("like", IDS[0])) == 1


@pytest.mark.parametrize("failure", ["create_timeout", "append_timeout", "partial_append"])
def test_playlist_uncertain_writes_recover_by_marker_and_sequence(reviewed, failure):
    client = FakeYouTube()
    setattr(client, failure, True)
    folder, client = prepared(reviewed, client)
    with pytest.raises(SetupError):
        execute(folder.name, confirmed=True, destination=destination(client))
    assert preflight(folder.name, destination(client))["state"] == "ready"
    assert execute(folder.name, confirmed=True, destination=destination(client))["state"] == "verified"
    assert client.create_count == 1
    assert next(iter(client.playlists.values()))["video_ids"] == [IDS[0], IDS[1], IDS[0]]


def test_unknown_creation_without_marker_is_not_blindly_retried(reviewed):
    client = FakeYouTube()
    client.before_create_timeout = True
    folder, client = prepared(reviewed, client)
    with pytest.raises(SetupError):
        execute(folder.name, confirmed=True, destination=destination(client))
    checked = preflight(folder.name, destination(client))
    assert checked["state"] == "blocked"
    with pytest.raises(SetupError):
        execute(folder.name, confirmed=True, destination=destination(client))
    assert client.create_count == 0


def test_playlist_external_edits_are_not_overwritten(reviewed):
    folder, client = prepared(reviewed)
    execute(folder.name, confirmed=True, destination=destination(client))
    next(iter(client.playlists.values()))["video_ids"].reverse()
    # Palindromic deliberate duplicates need a real alteration, not reversal alone.
    next(iter(client.playlists.values()))["video_ids"].append("lmnopqrstuv")
    before = list(client.writes)
    assert preflight(folder.name, destination(client))["state"] == "blocked"
    assert client.writes == before


def test_approval_change_invalidates_frozen_plan(reviewed):
    folder = create_plan([reviewed])
    store = ReviewStore()
    store.save(reviewed, "spotify:track:0", "clear")
    store.close()
    with pytest.raises(SetupError, match="choices"):
        load_plan(folder.name)


def test_unresolved_playlist_is_held_unless_partial_explicit(reviewed):
    store = ReviewStore()
    store.save(reviewed, "spotify:track:1", "clear")
    store.close()
    folder = create_plan([reviewed])
    plan = load_plan(folder.name)
    assert operation_counts(plan)["playlist"] == 0
    assert len(plan["held_playlists"]) == 1
    partial = load_plan(create_plan([reviewed], allow_partial_playlists=True).name)
    operation = next(op for op in partial["operations"] if op["action"] == "playlist")
    assert operation["video_ids"] == [IDS[0], IDS[0]] and operation["unresolved_entries"] == 1
    assert operation['title'] == 'Example playlist'
    assert [entry['source_position'] for entry in operation['occurrences']] == [0, 2]


def test_execution_requires_explicit_confirmation_and_preflight(reviewed):
    folder = create_plan([reviewed])
    with pytest.raises(SetupError, match="authorize"):
        execute(folder.name)
    with pytest.raises(SetupError, match="preflight"):
        execute(folder.name, confirmed=True, destination=destination(FakeYouTube()))


def test_expired_preflight_and_account_switch_block_writes(reviewed, monkeypatch):
    from spottoyt import transfers
    folder, client = prepared(reviewed)
    client.handle = "@DifferentAccount"
    with pytest.raises(SetupError, match="account/session"):
        execute(folder.name, confirmed=True, destination=destination(client))
    assert not client.writes
    assert json.loads((folder / "preflight.json").read_text())["state"] == "blocked"
    now = transfers.time.time()
    monkeypatch.setattr(transfers.time, "time", lambda: now + 1900)
    with pytest.raises(SetupError, match="stale"):
        execute(folder.name, confirmed=True, destination=destination(client))
    assert not client.writes


def test_plan_and_preflight_edits_are_refused(reviewed):
    folder, client = prepared(reviewed)
    (folder / "preflight.json").write_text("{}")
    with pytest.raises(SetupError, match="modified"):
        execute(folder.name, confirmed=True, destination=destination(client))
    with (folder / "plan.json").open("a") as output:
        output.write(" ")
    with pytest.raises(SetupError, match="changed"):
        load_plan(folder.name)
    assert not client.writes


def test_concurrent_execution_lock_is_exclusive(reviewed):
    with execution_lock():
        with pytest.raises(SetupError, match="Another transfer"):
            with execution_lock():
                pass


def test_show_and_episode_saves_are_additive_and_idempotent(reviewed):
    from spottoyt.archive import private_root
    from spottoyt.podcasts import extract_podcasts
    source = private_root() / "archives" / reviewed["data"]["source_archive"]
    archive = Archive(source)
    show = {"uri": "spotify:show:demo", "name": "Example Show", "publisher": "Example Publisher"}
    episode = {"uri": "spotify:episode:demo", "type": "episode", "name": "Episode 1", "show": show,
               "duration_ms": 1200000, "release_date": "2026-01-01"}
    archive.append("saved_shows", [{"show": show}])
    archive.append("saved_episodes", [{"episode": episode}])
    archive.finish("completed_with_gaps")
    archive.close()
    items, _ = extract_podcasts(source)
    results = []
    for item in items:
        kind = item["podcast"]["kind"]
        identifier = "MPSPPLshowdemo123" if kind == "show" else "lmnopqrstuv"
        results.append({**item, "status": "needs_review", "candidates": [{"target_id": identifier, "title": item["podcast"]["title"],
                         "duration_seconds": 1200 if kind == "episode" else None, "available": True}]})
    folder = private_root() / "podcast-matching" / "20260101T000000Z-bbbbbbbb"
    folder.mkdir(parents=True)
    (folder / "report.json").write_text(json.dumps({"schema_version": 1, "source_archive": source.name, "results": results}))
    report = load_report("podcasts", folder.name)
    store = ReviewStore()
    for row in results:
        store.save(report, row["source_key"], "approved", candidate_id=row["candidates"][0]["target_id"], confirmed=True)
    store.close()
    plan = create_plan([reviewed, report])
    client = FakeYouTube()
    assert preflight(plan.name, destination(client))["state"] == "ready"
    execute(plan.name, confirmed=True, destination=destination(client))
    before = list(client.writes)
    execute(plan.name, confirmed=True, destination=destination(client))
    assert client.writes == before
    assert client.shows == {"MPSPPLshowdemo123"} and client.episodes == {"lmnopqrstuv"}


def test_target_metadata_change_after_preflight_blocks_like(reviewed):
    folder, client = prepared(reviewed, strict_target_metadata=True)
    normal = client.get_song
    def changed(identifier):
        result = normal(identifier)
        result["videoDetails"]["title"] = "Completely different recording"
        return result
    client.get_song = changed
    with pytest.raises(SetupError):
        execute(folder.name, confirmed=True, destination=destination(client))
    assert not client.writes


def test_ui_transfer_requires_checked_plan_and_confirmation(reviewed, monkeypatch):
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    from spottoyt import transfers
    folder, client = prepared(reviewed)
    monkeypatch.setattr(transfers, "Destination", lambda: destination(client))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "src" / "spottoyt" / "ui.py"), default_timeout=30).run()
    next(radio for radio in app.radio if radio.label == "Workspace").set_value("Transfer plan").run()
    assert not app.exception and not app.error
    next(button for button in app.button if button.label == "Execute this transfer").click().run()
    assert not client.writes
    next(checkbox for checkbox in app.checkbox if checkbox.label == "I authorize the exact planned likes, saves and private playlist copies shown above").check()
    next(button for button in app.button if button.label == "Execute this transfer").click().run()
    assert not app.exception and not app.error
    assert client.likes == set(IDS) and client.create_count == 1


def test_skip_to_defer_also_invalidates_playlist_plan(reviewed):
    store = ReviewStore()
    store.save(reviewed, "spotify:track:1", "skipped")
    folder = create_plan([reviewed])
    store.save(reviewed, "spotify:track:1", "deferred")
    with pytest.raises(SetupError, match="choices"):
        load_plan(folder.name)
    store.close()


def test_large_playlist_uses_bounded_batches_and_preserves_repeats(reviewed):
    from spottoyt.archive import private_root
    source = private_root() / "archives" / reviewed["data"]["source_archive"]
    archive = Archive(source)
    songs = [row["track"] for row in archive.items("liked_songs")]
    archive.append("playlist_items/source1", [{"item": songs[index]} for index in (0, 1, 0) * 39])
    archive.coverage("playlist_items/source1", "complete", 120)
    archive.finish("completed_with_gaps")
    archive.close()
    entries = {row["source_key"]: row for row in extract_songs(source)[0]}
    for row in reviewed["data"]["results"]:
        row["occurrences"] = entries[row["source_key"]]["occurrences"]
    report_path = private_root() / "matching" / reviewed["run_id"] / "report.json"
    report_path.write_text(json.dumps(reviewed["data"]))
    updated = load_report("music", reviewed["run_id"])
    store = ReviewStore()
    for index, row in enumerate(updated["data"]["results"]):
        store.save(updated, row["source_key"], "approved", candidate_id=IDS[index], confirmed=True)
    store.close()
    folder, client = prepared(updated)
    execute(folder.name, confirmed=True, destination=destination(client))
    assert [len(write[1]) for write in client.writes if write[0] == "append"] == [50, 50, 20]
    assert next(iter(client.playlists.values()))["video_ids"] == [IDS[0], IDS[1], IDS[0]] * 40
