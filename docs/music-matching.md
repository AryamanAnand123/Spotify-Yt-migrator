# Music matching

```powershell
.\.venv\Scripts\spottoyt.exe match-music --limit 20 --request-budget 20
```

The default source is the latest finished Spotify API archive. Select one with
`--archive "C:\path\to\private\archives\<archive-id>"`. Interrupted snapshots
are refused. Matching reads the source database without modifying it.

Liked songs and playlist tracks are deduplicated for searching while retaining
every collection/position reference. Local files, unavailable records and
episodes are reported separately. This command does not match saved album/artist
entities or infer current library membership from account-download history.

Public searches send song title and artists, not credentials, playlist names,
Spotify identifiers or full archives. Queries use the song filter, retain up to
ten candidates and do not automatically retry failures. Request budgets count
high-level searches; underlying client HTTP calls can differ.

## Scores and approval

Title, artists, duration and album contribute 45/30/15/10 points. Recording-version
and explicit-status differences reduce the score. Strict metadata labels also
consider missing fields, duration differences, availability and close alternatives.
Unicode normalization retains non-English scripts and combining marks.

Music bulk approval accepts the top available candidate at **85/100 or higher**.
Album/artist warnings and close alternatives do not impose extra approval gates.
Missing fields still affect the weighted score. Existing manual choices, skips
and deferrals are preserved. Scores are heuristics, not accuracy percentages or
proof of identical audio; no audio fingerprinting or LLM is used.

Approvals are separate from matching reports. Use the [review UI](review-interface.md),
not edited CSV files, to make decisions. Transfers still validate the exact
approved video ID, account, availability and destination state.

## Cache and reports

Results live under `%LOCALAPPDATA%\Spottoyt\matching`:

- A per-run `report.json`, `review.csv` and `summary.json`.
- `search-cache.sqlite3` for sanitized successful results.

Nonempty searches expire after seven days; empty searches after one day. Reruns
re-score cached candidates and create new reports. Spreadsheet-formula-leading
CSV cells are escaped. No raw feedback tokens or signed audio URLs are retained.

`--limit 0` selects all songs, but the request budget still applies. `--offline`
uses cache only; missing/expired entries remain `not_cached`. Budget-limited
items stay pending rather than being marked missing. Interrupted results are
saved, and a later explicit run can reuse successful searches. Safe failure
categories/status codes omit provider response bodies.

An unmatched result means no usable candidate was found in the attempted search,
not proof that the recording is absent from the entire destination catalog.

Reference: https://ytmusicapi.readthedocs.io/en/stable/reference/search.html
