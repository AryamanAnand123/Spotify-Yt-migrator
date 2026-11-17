"""Pagination, privacy, partial failures and account-download ingestion."""

import json
import zipfile
from unittest.mock import Mock

import pytest
from spotipy.exceptions import SpotifyException

from spottoyt.archive import Archive, private_root
from spottoyt.auth import SetupError
from spottoyt.exporter import capture, export_spotify, next_params
from spottoyt.importer import import_account, input_files


@pytest.fixture
def root(monkeypatch, tmp_path):
    path = tmp_path / "private"
    monkeypatch.setenv("SPOTTOYT_DATA_DIR", str(path))
    return path


def page(items, following=None, total=None):
    return {"items": items, "next": following, "total": len(items) if total is None else total}


def test_pagination_preserves_order_duplicates_and_null_tracks(root):
    archive = Archive.create("test")
    client = Mock()
    repeated = {"item": {"uri": "spotify:track:example"}}
    missing = {"item": None, "is_local": True}
    client._get.side_effect = [page([repeated], "https://api.spotify.com/v1/playlists/abc/items?offset=1&limit=50", 3),
                               page([repeated, missing], total=3)]
    capture(client, archive, "playlist_items/abc", "playlists/abc/items")
    archive.finish("test")
    assert archive.items("playlist_items/abc") == [repeated, repeated, missing]
    inventory = json.loads((archive.path / "inventory.json").read_text())
    assert inventory["coverage"][0]["status"] == "complete"
    assert inventory["coverage"][0]["captured"] == 3
    archive.close()


@pytest.mark.parametrize("url", [
    "https://evil.example/v1/me/tracks?offset=50",
    "http://api.spotify.com/v1/me/tracks?offset=50",
    "https://api.spotify.com/v1/me/albums?offset=50",
    "https://api.spotify.com/v1/me/tracks?token=SECRET",
])
def test_untrusted_pagination_rejected(url):
    with pytest.raises(SetupError):
        next_params(url, "me/tracks")


def test_followed_artists_cursor_pages(root):
    archive = Archive.create("test")
    client = Mock()
    client._get.side_effect = [{"artists": page([{"id": "a"}], "https://api.spotify.com/v1/me/following?after=a&type=artist&limit=50", 2)},
                               {"artists": page([{"id": "b"}], total=2)}]
    capture(client, archive, "followed_artists", "me/following", {"type": "artist", "limit": 50})
    assert archive.items("followed_artists") == [{"id": "a"}, {"id": "b"}]
    archive.close()


def test_rate_limit_checkpoints_partial_archive_and_stops(root):
    client = Mock()
    client._get.side_effect = [page([{"id": "p"}], total=1), SpotifyException(429, -1, "PRIVATE")]
    with pytest.raises(SetupError) as error:
        export_spotify(client=client)
    assert "PRIVATE" not in str(error.value)
    paths = list((root / "archives").iterdir())
    report = json.loads((paths[0] / "snapshot.json").read_text())
    assert report["metadata"]["state"] == "interrupted_or_partial"
    assert report["collections"]["playlists"] == [{"id": "p"}]
    assert client._get.call_count == 2


def test_inaccessible_playlist_is_reported_without_losing_reference(root):
    client = Mock()
    def get(path, **kwargs):
        if path == "me/playlists":
            return page([{"id": "abc", "name": "PRIVATE-NAME"}])
        if path == "me/following":
            return {"artists": page([])}
        if path == "playlists/abc/items":
            raise SpotifyException(403, -1, "PRIVATE-ERROR")
        return page([])
    client._get.side_effect = get
    path = export_spotify(client=client)
    report = json.loads((path / "inventory.json").read_text())
    item = next(row for row in report["coverage"] if row["collection"] == "playlist_items/abc")
    assert item["status"] == "unavailable"
    assert "PRIVATE-NAME" not in (path / "inventory.json").read_text()
    assert "PRIVATE-ERROR" not in (path / "snapshot.json").read_text()


def test_incorrect_total_is_not_marked_complete(root):
    archive = Archive.create("test")
    client = Mock()
    client._get.return_value = page([{"id": "x"}], total=3)
    capture(client, archive, "liked_songs", "me/tracks")
    assert archive.db.execute("SELECT status FROM coverage").fetchone()[0] == "count_mismatch"
    archive.close()


def test_repeated_page_cannot_loop(root):
    archive = Archive.create("test")
    client = Mock()
    client._get.return_value = page([{"id": "x"}], "https://api.spotify.com/v1/me/tracks?limit=50", 2)
    with pytest.raises(SetupError, match="Repeated"):
        capture(client, archive, "liked_songs", "me/tracks")
    assert client._get.call_count == 1
    archive.close()


def test_archive_paths_never_overwrite_and_sqlite_reopens(root):
    first, second = Archive.create("test"), Archive.create("test")
    assert first.path != second.path
    assert "*" in (root / ".gitignore").read_text().splitlines()
    first.append("liked_songs", [{"id": "a"}])
    first.close()
    reopened = Archive(first.path)
    assert reopened.items("liked_songs") == [{"id": "a"}]
    reopened.close()
    second.close()


def test_reject_onedrive_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("SPOTTOYT_DATA_DIR", str(tmp_path / "OneDrive" / "private"))
    with pytest.raises(SetupError, match="OneDrive"):
        private_root()


def test_zip_import_preserves_raw_unknown_and_deduplicates_exact_files(root, tmp_path):
    source = tmp_path / "spotify.zip"
    playlist = {"playlists": [{"name": "Synthetic", "items": [{"track": {"trackUri": "spotify:track:a"}}] * 2}]}
    library = {"tracks": [{"uri": "spotify:track:a"}], "shows": [{"uri": "spotify:show:a"}]}
    with zipfile.ZipFile(source, "w") as zipped:
        zipped.writestr("Spotify/Playlist1.json", json.dumps(playlist))
        zipped.writestr("Spotify/YourLibrary.json", json.dumps(library))
        zipped.writestr("Spotify/YourLibrary-copy.json", json.dumps(library))
        zipped.writestr("Spotify/Streaming_History_Audio_0.json", json.dumps([{"ts": "example", "ms_played": 1}]))
        zipped.writestr("Spotify/Follow.json", json.dumps({"followingUsers": ["synthetic"]}))
        zipped.writestr("Spotify/Unknown.json", json.dumps({"new_field": "preserved"}))
        zipped.writestr("Spotify/Broken.json", "not JSON")
    original = source.read_bytes()
    path = import_account(source)
    report = json.loads((path / "snapshot.json").read_text())
    assert len(report["account_files"]) == 5
    assert len(report["collections"]["account_playlist_items/Playlist1.json/0"]) == 2
    assert len(report["collections"]["liked_songs"]) == 1
    assert len(report["collections"]["history"]) == 1
    assert any(item["status"] == "raw_only" for item in report["account_files"])
    assert source.read_bytes() == original
    database = Archive(path)
    stored = database.db.execute("SELECT sha256, content FROM original_files").fetchall()
    import hashlib
    assert len(stored) == 5
    assert all(hashlib.sha256(content).hexdigest() == digest for digest, content in stored)
    database.close()


@pytest.mark.parametrize("member", ["../escape.json", "/absolute.json", "C:/escape.json", "..\\escape.json"])
def test_zip_traversal_rejected_without_extraction(tmp_path, member):
    source = tmp_path / "bad.zip"
    with zipfile.ZipFile(source, "w") as zipped:
        zipped.writestr(member, "{}")
    with pytest.raises(SetupError, match="Unsafe"):
        list(input_files(source))
    assert not (tmp_path / "escape.json").exists()


def test_credentials_are_refused_before_archiving(root, tmp_path):
    source = tmp_path / "wrong.json"
    source.write_text(json.dumps({"access_token": "DO-NOT-ARCHIVE"}))
    with pytest.raises(SetupError, match="Credential-like"):
        import_account(source)
    snapshots = list((root / "archives").glob("*/snapshot.json"))
    assert all("DO-NOT-ARCHIVE" not in path.read_text() for path in snapshots)


def test_input_limit_rejected(monkeypatch, tmp_path):
    from spottoyt import importer
    source = tmp_path / "large.json"
    source.write_text("{}")
    monkeypatch.setattr(importer, "MAX_FILE_BYTES", 1)
    with pytest.raises(SetupError, match="safety limits"):
        list(input_files(source))
