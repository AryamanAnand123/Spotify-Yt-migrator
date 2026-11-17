"""Offline account-download importer; ZIP files are read without extraction."""

import json
import zipfile
from pathlib import Path, PurePosixPath

from spottoyt.archive import Archive
from spottoyt.auth import SetupError

MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_FILES = 2000
LIBRARY_KEYS = {"tracks": "liked_songs", "albums": "saved_albums", "artists": "followed_artists",
                "shows": "saved_shows", "episodes": "saved_episodes"}


def safe_member(name):
    name = name.replace("\\", "/")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or ":" in name:
        raise SetupError("Unsafe archive member path rejected. No ZIP files were extracted.")
    return path.name


def input_files(source):
    source = Path(source).resolve()
    if not source.exists():
        raise SetupError("Account-data input does not exist.")
    total = 0
    count = 0

    def bounded(size):
        nonlocal total, count
        total += size
        count += 1
        if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES or count > MAX_FILES:
            raise SetupError("Account download exceeds import safety limits (100 MiB/file, 512 MiB total, 2000 files).")

    if source.is_file() and source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as zipped:
            if len(zipped.infolist()) > MAX_FILES:
                raise SetupError("ZIP has too many members.")
            for info in zipped.infolist():
                name = safe_member(info.filename)
                if info.is_dir() or not name.lower().endswith(".json"):
                    continue
                bounded(info.file_size)
                with zipped.open(info) as stream:
                    raw = stream.read(MAX_FILE_BYTES + 1)
                if len(raw) > MAX_FILE_BYTES:
                    raise SetupError("Account JSON exceeds the per-file size limit.")
                yield name, raw
    elif source.is_file() and source.suffix.lower() == ".json":
        bounded(source.stat().st_size)
        yield source.name, source.read_bytes()
    elif source.is_dir():
        for path in sorted(source.rglob("*.json")):
            if path.is_symlink() or source not in path.resolve().parents:
                raise SetupError("Linked account-data paths are not imported.")
            bounded(path.stat().st_size)
            yield path.name, path.read_bytes()
    else:
        raise SetupError("Choose a Spotify ZIP, JSON file, or extracted data folder.")


def reject_credentials(value):
    if isinstance(value, dict):
        if any(key.lower() in {"access_token", "refresh_token", "client_secret", "authorization", "cookie"}
               for key in value):
            raise SetupError("Credential-like fields detected in input. Import only Spotify account-download files.")
        for item in value.values():
            reject_credentials(item)
    elif isinstance(value, list):
        for item in value:
            reject_credentials(item)


def normalize(archive, name, payload):
    filename = name.lower()
    if filename.startswith("playlist") and isinstance(payload, dict) and isinstance(payload.get("playlists"), list):
        for index, playlist in enumerate(payload["playlists"]):
            if not isinstance(playlist, dict):
                return "raw_only"
            archive.append("playlists", [{"source_file": name, "source_index": index, "playlist": playlist}])
            items = playlist.get("items")
            if isinstance(items, list):
                # Filename/index preserve separate playlists and intentional duplicate tracks.
                collection = f"account_playlist_items/{name}/{index}"
                archive.append(collection, items)
        return "recognized"
    if filename.startswith("yourlibrary") and isinstance(payload, dict):
        recognized = False
        for key, collection in LIBRARY_KEYS.items():
            if isinstance(payload.get(key), list):
                archive.append(collection, [{"source_file": name, "item": item} for item in payload[key]])
                recognized = True
        return "recognized" if recognized else "raw_only"
    if (filename.startswith(("streaminghistory", "streaming_history", "endsong")) and isinstance(payload, list)):
        archive.append("history", [{"source_file": name, "event": item} for item in payload])
        return "recognized"
    if filename.startswith("follow") and isinstance(payload, dict):
        archive.append("social_follow_data", [{"source_file": name, "data": payload}])
        return "recognized"
    return "raw_only"


def import_account(source, progress=lambda _: None):
    if not Path(source).exists():
        raise SetupError("Account-data input does not exist.")
    archive = Archive.create("spotify_account_download")
    progress(f"Private archive: {archive.path}")
    accepted = 0
    issues = 0
    seen = set()
    try:
        for name, raw in input_files(source):
            import hashlib
            digest = hashlib.sha256(raw).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            try:
                payload = json.loads(raw.decode("utf-8-sig"))
                if not isinstance(payload, (dict, list)):
                    raise ValueError
            except (ValueError, UnicodeError):
                issues += 1
                archive.coverage(f"rejected_file/{accepted + issues}", "rejected", detail="Invalid JSON; source file unchanged.")
                continue
            reject_credentials(payload)
            status = normalize(archive, name, payload)
            archive.import_blob(name, raw, payload, status)
            accepted += 1
            if status == "raw_only":
                issues += 1
            progress(f"Account files archived: {accepted}; contents omitted from terminal")
        if accepted == 0:
            raise SetupError("No usable account JSON files were found.")
        for row in archive.db.execute("SELECT DISTINCT collection FROM records").fetchall():
            archive.coverage(row[0], "imported_not_reconciled", detail="Download snapshot; not merged with API snapshots.")
        archive.coverage("download_scope", "not_verified", detail="Only selected files imported; no claim of complete account coverage.")
        archive.finish("imported_with_unparsed_or_rejected_files" if issues else "imported_not_reconciled")
    except (KeyboardInterrupt, Exception) as exc:
        archive.finish("interrupted_or_partial")
        hint = str(exc) if isinstance(exc, SetupError) else "Check input structure and safety limits."
        raise SetupError(f"Import stopped; any accepted files remain at {archive.path}. "
                         f"{hint} Source files were not modified.") from None
    finally:
        archive.close()
    return archive.path
