# Completing partial playlists

Music bulk approval accepts the top available score at **85/100 or higher**,
without extra album/artist, metadata-warning or runner-up-margin gates. Missing
fields still affect the weighted score. Existing choices, skips, deferrals and
notes are preserved. Unsearched/unavailable candidates are not bulk approved.

New plans trust the exact approved video ID; title/duration aliases are recorded
as informational warnings. Playback availability, account identity and existing
dislikes remain checked. Scores are not probabilities or audio fingerprints.

## Using partial copies

1. Save review decisions or apply the music bulk policy.
2. On **Transfer plan**, leave **Transfer partial playlists and fill reviewed gaps later** enabled.
3. Prepare a plan, inspect approved/unresolved counts, check the account and execute.
4. After more review choices, prepare a new plan from the same source snapshot.
   Source-occurrence checkpoints link it to the existing private copies.

The CLI also enables partial copies by default. Use
`--no-allow-partial-playlists` for complete-only behavior. Names remain stable;
reports expose incomplete source coverage instead of changing a title suffix.

## Occurrence checkpoints

Each copied entry records its original source position and approved video ID.
Intentional duplicates remain separate occurrences. An initial `[A, A]` at source
positions 0 and 2 becomes `[A, B, A]` when position 1 is approved later. Only B
is inserted; the existing occurrences retain their relative order and copy ID.

The intended sequence is saved before insertion. Afterward, the full contents,
private visibility, title and marker are checked. An explicit resume recognizes
saved before/after states and avoids repeating an already-applied occurrence.
Bounded read waits handle delayed visibility after successful writes. Unfinished
copies are processed in alternating rounds so a large copy does not monopolize
a batch.

Changed source order, external edits, or changed/removed reviews for already
copied occurrences stop automatic completion. The app does not delete, replace
or move existing entries to resolve those conflicts. A complete legacy copy can
be adopted only when its sequence matches; arbitrary old partial alignments are
not guessed from duplicate IDs.

## Pause and verify

Quota-limited or interrupted work can leave copies partly filled. A known pause
records its reason and preserves progress. Refresh checks and resume the **same
plan** when reviews are unchanged. Prepare a new plan after additional reviews.

Verification distinguishes complete operations from `partial_verified` copy
checkpoints and reports observed/remaining counts. `verified_progress_with_gaps`
does not claim every approved entry was copied. Changed or unverified contents
are not silently treated as successful progress.

References:
- https://developers.google.com/youtube/v3/docs/playlistItems/insert
- https://developers.google.com/youtube/v3/determine_quota_cost
