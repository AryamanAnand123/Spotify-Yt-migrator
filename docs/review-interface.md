# Local review interface

```powershell
.\.venv\Scripts\spottoyt.exe review
```

Open **http://127.0.0.1:8501** and keep the terminal running. Choose another local
port with `--port` if needed. Use [the isolated demo](demo.md) for invented data.

## Review

Select the intended reports in the sidebar. **Music** and **Podcasts** show
source/candidate metadata, reported differences and saved decisions. Filter by
matching status and review choice, then select an item/candidate.

- Music **Approve scores 85+ and skip missing** approves eligible top candidates,
  skips searched items with no result, and leaves other items undecided.
- Bulk actions preserve every existing choice and commit atomically. Pending
  or uncached items are not treated as missing.
- Individual approval requires selecting a candidate and checking confirmation
  before **Save decision**. Confirmation resets after saving/changing targets.
- **Skip**, **Defer** and **Clear** never approve an item. Notes are optional;
  do not put credentials in them.
- Manually entered links must use supported HTTPS hosts/ID shapes. They remain
  marked as manually verified metadata rather than becoming a scored match.

Decisions bind to exact report/row fingerprints in the private review database.
A new matching report does not silently inherit old choices. Editing a CSV is
not an approval-import mechanism. **Approved preview** is informational only.

## Transfer

On **Transfer plan**, prepare a plan, inspect exact counts/items, check the
destination account, then explicitly authorize execution. Partial copies are
enabled by default and use source-occurrence checkpoints for later gap filling.
Viewing a screen or saving a review never starts a transfer.

After interruption, refresh checks and resume the same plan. After changing
reviews, prepare a new plan from the same source snapshot. Unsupported podcast
and Music-library save states remain excluded from the official adapter.
See [transfers](transfers.md) and [partial completion](partial-playlists.md).

## Verification and local operation

**Verification** reads a selected plan only when its button is clicked. Saved
reports open offline and distinguish complete operations from verified partial
checkpoints. Select real imported downloads explicitly if adding coverage evidence.

The server binds loopback, enables CORS/XSRF protection, disables telemetry/static
serving and hides detailed client exceptions. It is not an authenticated public
service. Browser links open only after explicit clicks; demo listening links are
replaced with disabled controls. See [security](safety-review.md).

Browser regression checks:

```powershell
.\.venv\Scripts\python.exe scripts/check_review_browser.py
```

The script uses an isolated headless Edge profile and invented data. Diagnostic
screenshots go to the ignored `.pytest_cache/ui-qa` folder. To regenerate the
documentation's synthetic screenshots, use `scripts/capture_demo.py`.
