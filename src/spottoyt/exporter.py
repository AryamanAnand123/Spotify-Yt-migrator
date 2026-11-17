"""Read-only Spotify export with bounded pagination and explicit coverage gaps."""

import re
from urllib.parse import parse_qs, urlsplit

from spotipy.exceptions import SpotifyException

from spottoyt.archive import Archive
from spottoyt.auth import SetupError, Vault, spotify_reader

COLLECTIONS = {
    "playlists": "me/playlists", "liked_songs": "me/tracks", "saved_albums": "me/albums",
    "followed_artists": "me/following", "saved_shows": "me/shows", "saved_episodes": "me/episodes",
    "saved_audiobooks": "me/audiobooks",
}


class ExportStopped(Exception):
    pass


def next_params(url, path):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "api.spotify.com" or
            parsed.path != "/v1/" + path or parsed.fragment):
        raise SetupError("Blocked an unexpected Spotify pagination URL.")
    query = parse_qs(parsed.query)
    if any(key not in {"offset", "limit", "after", "type", "additional_types"} for key in query):
        raise SetupError("Unexpected Spotify pagination parameters.")
    return {key: values[0] for key, values in query.items()}


def capture(client, archive, collection, path, params=None, progress=lambda _: None):
    archive.coverage(collection, "in_progress")
    expected = None
    count = 0
    seen = set()
    params = params or {"limit": 50}
    try:
        for _ in range(10000):
            signature = tuple(sorted((key, str(value)) for key, value in params.items()))
            if signature in seen:
                raise SetupError("Repeated Spotify page detected.")
            seen.add(signature)
            data = client._get(path, **params)
            page = data.get("artists", data) if collection == "followed_artists" else data
            items = page.get("items")
            if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
                raise SetupError("Spotify returned an unexpected page structure.")
            total = page.get("total")
            if isinstance(total, int):
                expected = total
            archive.append(collection, items)
            count += len(items)
            archive.coverage(collection, "in_progress", expected)
            progress(f"{collection}: {count} records captured")
            following = page.get("next")
            if not following:
                status = "complete" if expected is None or count == expected else "count_mismatch"
                archive.coverage(collection, status, expected,
                                 "Snapshot may change during pagination; compare reported and captured counts.")
                return
            if not items:
                raise SetupError("Spotify returned an empty page with a continuation.")
            params = next_params(following, path)
        raise SetupError("Pagination safety limit reached.")
    except SpotifyException as exc:
        archive.coverage(collection, "unavailable" if count == 0 else "partial", expected,
                         f"HTTP {exc.http_status}; response body omitted.")
        if exc.http_status in (401, 429):
            raise ExportStopped from exc
    except Exception:
        archive.coverage(collection, "partial", expected, "Read failed; checkpoint retained; provider details omitted.")
        raise


def export_spotify(progress=lambda _: None, client=None):
    client = client or spotify_reader(Vault())
    archive = Archive.create("spotify_api")
    progress(f"Private archive: {archive.path}")
    try:
        for name in COLLECTIONS:
            archive.coverage(name, "not_attempted")
        archive.coverage("history", "requires_account_download")
        archive.coverage("social_follow_data", "requires_account_download")
        archive.coverage("podcast_playback_progress", "not_verified",
                         detail="Episode resume_point fields are preserved if supplied; no completeness claim.")
        for name, path in COLLECTIONS.items():
            params = {"limit": 50, "type": "artist"} if name == "followed_artists" else None
            capture(client, archive, name, path, params, progress)
        for playlist in archive.items("playlists"):
            identifier = playlist.get("id", "")
            if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9]+", identifier):
                archive.coverage("playlist_items/invalid_id", "unavailable", detail="Invalid playlist identifier.")
                continue
            # Preserve followed playlist references even when contents are inaccessible.
            capture(client, archive, "playlist_items/" + identifier,
                    "playlists/" + identifier + "/items", {"limit": 50, "additional_types": "track,episode"}, progress)
        gaps = archive.db.execute("SELECT COUNT(*) FROM coverage WHERE status != 'complete'").fetchone()[0]
        archive.finish("completed_with_gaps" if gaps else "complete")
    except (KeyboardInterrupt, Exception):
        archive.finish("interrupted_or_partial")
        raise SetupError(f"Export stopped safely; saved pages remain at {archive.path}. "
                         "Inspect inventory.json. A new run creates a separate snapshot; no automatic retry.") from None
    finally:
        archive.close()
    return archive.path
