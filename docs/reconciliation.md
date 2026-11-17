# Destination verification and gaps

Verification reads destination state without repairing or changing an account.
Opening saved reports uses no credentials or network access.

## Run verification

In **Verification**, select a frozen plan and explicitly choose
**Read destination and create verification report**. Optionally select imported
account-download archives for checksum/history coverage. The CLI equivalent is:

```powershell
.\.venv\Scripts\spottoyt.exe verify-transfer <plan-id> --account-archive <download-archive-id>
```

Repeat `--account-archive` for multiple downloads. Use archive IDs from import
output rather than original ZIP paths. Synthetic imports remain separate choices.

## Checks and states

- Source, plan and matching-report hashes must agree.
- A checksum-verified preflight identifies the expected account. Fresh account
  identity is checked before destination items are inspected.
- Planned likes are read back by exact target ID.
- Copies must retain private visibility, title and marker. Their full sequence,
  including duplicates, is compared with the plan or occurrence checkpoints.
- Journal state is reported separately; verification does not modify it.
- An execution lock prevents overlap with this workspace's executor.

`verified_plan_with_gaps` means all planned operations were observed as intended.
`verified_progress_with_gaps` includes valid partial-copy checkpoints with entries
still queued. `completed_with_differences` and `blocked_or_partial` require
inspection. Read failures save earlier observations and unchecked counts without
claiming success. Verification is not proof of full account migration or future
availability, and is not an atomic account snapshot.

## Imported coverage

Selected downloads receive SQLite integrity and original-file SHA-256 checks.
Reports retain provenance, recognition statuses and counts. History summaries
count event kinds, distinct supported URIs, timezone-aware date bounds and overlap
with the API library. Unknown event shapes/timestamps remain visible in coverage.

Past plays are not current saved items. History, progress, social follows,
album/artist mappings and Music-specific saved podcast states are not recreated
by the official adapter. Extended history does not replace Account data or prove
complete provider coverage. Partial copies contain only approved occurrences.

## Private reports

Each run creates `%LOCALAPPDATA%\Spottoyt\verification\<run-id>` with
`report.json`, `report.sha256` and `report.md`. Keep these private: they can
contain account/library identifiers. Hashes detect accidental changes, not a
malicious same-user process able to edit both files and hashes.
