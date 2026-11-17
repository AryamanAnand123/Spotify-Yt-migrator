# Podcast matching

```powershell
.\.venv\Scripts\spottoyt.exe match-podcasts --limit 20 --request-budget 20
```

The command reads a finished API archive and suggests show/episode candidates.
It makes public catalog queries without account credentials and never subscribes,
saves, plays or changes progress. Use `--archive` for a specific private API
snapshot, `--limit 0` to select all items and `--offline` for cached evidence only.

## Evidence

Show ranking compares title and publisher/channel metadata. Episode ranking also
compares show identity, duration, release date and episode numbering where
available. Missing/relative dates are not invented as exact release days.
Same-name shows, publisher differences, rounded durations and close alternatives
remain reviewable. Scores do not establish identical audio or RSS-feed identity.

Search/detail budgets bound public requests. Successful sanitized evidence is
cached under `%LOCALAPPDATA%\Spottoyt\podcast-matching`; reports include JSON,
CSV and a capability summary. Raw feedback tokens, thumbnails and signed playback
URLs are excluded. Errors save partial evidence without automatic retries.
Review decisions are stored separately through the [local UI](review-interface.md).

## Transfer limits

The official destination adapter does not support Music-specific show-library
or saved-episode state. Suggestions and approvals do not claim those states were
transferred. Playback history, played/unplayed flags, download state and resume
position have no implemented destination mapping. Original returned fields stay
in the private archive. A Spotify show is not silently mapped to a YouTube
channel subscription.

References:
- https://ytmusicapi.readthedocs.io/en/stable/reference/podcasts.html
- https://ytmusicapi.readthedocs.io/en/stable/reference/search.html
