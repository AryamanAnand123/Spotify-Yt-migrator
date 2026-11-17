# Reviewed transfers

The official adapter adds approved song likes and creates distinct private
playlist copies. It does not delete, unlike, unsubscribe or replace playlists.

## Workflow

In **Transfer plan**, prepare a plan, inspect its items/gaps, check the destination,
then authorize execution. The equivalent CLI is:

```powershell
.\.venv\Scripts\spottoyt.exe prepare-transfer
.\.venv\Scripts\spottoyt.exe preflight-transfer <plan-id>
.\.venv\Scripts\spottoyt.exe execute-transfer <plan-id>
```

Preparation is offline. Preflight performs reads. Execution requires explicit
confirmation. Music approvals use the review policy; transfers require the exact
approved video ID and a verifiable account/availability state. Title/duration
aliases are informational for new plans. Existing dislikes are not overwritten.

Partial playlists are enabled by default in the app/CLI. Use
`--no-allow-partial-playlists` for complete-only copies. Source-position
checkpoints preserve duplicates and allow later approvals to fill gaps in the
same copy. See [partial completion](partial-playlists.md).

Separate Music-library saves, podcast saved-state, album/artist mappings,
history, social follows and playback progress are unsupported by this adapter.
Legacy library-save engine paths tested with fakes are not verified capabilities
of the official adapter.

## Frozen evidence

Private files live under `%LOCALAPPDATA%\Spottoyt\transfers\<plan-id>`:

- `plan.json` / `plan.sha256`: operations and source/report/review bindings.
- `preview.md`: counts and gaps.
- `preflight.json` / `preflight.sha256`: sanitized identity, target and copy checks.
- `execution.json`: completed/paused/uncertain progress and safe diagnostics.
- Parent `journal.sqlite3`: operation states, copy mappings and occurrence checkpoints.

Changed source data, approvals, skips or deferrals invalidate a plan. Notes alone
do not change operations. Later historical verification can inspect a frozen plan
without treating current reviews as its original decisions.

The execution window lasts 30 minutes after preflight finishes. Account identity
is rechecked at execution. Recent checksum-bound target reads can be reused by
an explicit resumed preflight; cache age is tracked separately from readiness.

## Failure and retry behavior

- One OS lock permits one executor per private workspace.
- Intended writes are checkpointed before requests and read back afterward.
- Already-achieved likes are verified without another like write.
- Owned source markers recover uncertain copy creation. Missing or ambiguous
  markers block another create instead of risking duplicates.
- Bounded retries apply only to known read-visibility delays after successful
  creation/insertion. Writes are not blindly retried.
- External edits, wrong privacy/provenance, changed source order and unexpected
  sequences stop completion. There is no automatic overwrite or repair.
- A stopped run may have earlier changes or a partly filled copy. Resume the
  same plan after reconciliation; prepare a new plan after changing reviews.

Source snapshots and local hashes are not protection against a malicious process
running as the same user. Multi-service operations are not atomic, and the local
lock does not coordinate other computers or unrelated account tools.

## Official OAuth and quota

Access tokens refresh automatically using the saved Google grant. Browser cookies
are unnecessary for this workflow. Public player metadata uses a separate
unauthenticated client; OAuth credentials go only to approved Google hosts.

Each like, copy creation and playlist-item insert costs quota. Daily project
allowances may require several batches, independently of Premium. Quota stops
preserve checkpoints. Local batch budgets are not measurements of Google's
remaining quota. Inspect your project's provider quota before large runs.

References:
- https://developers.google.com/youtube/v3/determine_quota_cost
- https://developers.google.com/youtube/v3/docs/playlistItems/insert
