# Spotify archives and account-download import

These commands read Spotify or local input files. They do not change either
library. Every run creates a separate archive and preserves source provenance.

## Export the API library

```powershell
.\.venv\Scripts\spottoyt.exe export-spotify
```

The exporter reads playlists and their ordered contents, liked tracks, saved
albums, followed artists, saved shows/episodes and audiobooks where accessible.
Duplicates, local/unavailable items and returned source fields are retained.
It does not enumerate every saved album's track catalog or a show's episodes.

Pagination and returned totals are checked. The export is not an atomic account
snapshot: Spotify can change during reads. `completed_with_gaps` does not claim
every account category is available. A 401/429 stops the run; interrupted pages
remain committed. Export reruns create new snapshots rather than resuming one.

## Import additional downloads

Request Account data and Extended streaming history through Spotify's privacy
page, then keep the original download in a private location:

```powershell
.\.venv\Scripts\spottoyt.exe import-account "C:\path\to\spotify-download.zip"
```

ZIPs are read without extraction. JSON files and extracted folders are also
accepted. Known playlist, library, follow and history shapes are normalized;
other valid JSON remains `raw_only`. Original bytes and SHA-256 digests are
retained. Malformed/unsupported input is reported rather than silently discarded.
Exact duplicate files are imported once; repeated plays/playlist occurrences
remain separate records.

Limits are 100 MiB per JSON file, 512 MiB total JSON and 2,000 members/files.
Unsafe ZIP paths, linked folder entries and credential-like fields are refused.
Select account-download files, not a general downloads folder.

Imports stay separate from API snapshots. Downloads reflect their own request
date, and past plays do not imply current likes. Extended history does not replace
the separate Account data download. Cross-source merging is not automatic.

## Storage and coverage

Private storage defaults to `%LOCALAPPDATA%\Spottoyt\archives\<archive-id>`:

- `archive.sqlite3`: source records, coverage and imported original bytes.
- `snapshot.json`: structured portable data with provenance.
- `inventory.json` / `inventory.md`: captured counts, statuses and gaps.

Project/OneDrive archive locations are refused. `SPOTTOYT_DATA_DIR` can select
another absolute private root. Archives are not encrypted by the application;
do not commit or sync them publicly. A private-root Git exclusion supplements
the repository's ignore rules.

Try the importer with invented input using
`spottoyt import-account examples/account-data`, or use the isolated [demo](demo.md).
See [verification](reconciliation.md) for checksum and history-coverage reporting.

References:
- https://www.spotify.com/account/privacy/
- https://support.spotify.com/article/understanding-your-data/
