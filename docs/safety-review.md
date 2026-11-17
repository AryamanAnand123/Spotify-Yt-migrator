# Security and operating limits

## Credentials and network

- Spotify uses PKCE, random state, exact callback validation and read scopes.
- OAuth grants and optional browser cookies use Windows Credential Manager with
  no plaintext fallback. Large records use bounded chunks and an atomic manifest.
- Provider sessions restrict HTTPS hosts, require TLS verification, reject
  redirects and apply per-request timeouts.
- Normal CLI diagnostics omit provider bodies and local-variable dumps.
- Public matching sends title/artist or podcast search metadata without account
  credentials. Official writes use the saved Google grant.

Credential Manager and local hashes do not defend against a compromised Windows
account or malicious same-user process. Refresh grants can expire or be revoked.
Browser sessions are powerful and separate from OAuth; prefer official OAuth
for the supported transfer path. See [account setup](authentication.md).

## Local data and UI

Private archives, caches, decisions and journals default outside the repository
and OneDrive. Project/OneDrive archive overrides are refused. Archive data is
not encrypted by this application. Git exclusions cannot untrack existing files
or prevent cloud syncing data placed in the wrong directory.

The UI binds loopback with CORS/XSRF protection, disabled telemetry/static serving
and hidden detailed client errors. It has no public authentication layer. Do not
expose the local server through tunnels or public interfaces. User metadata is
rendered through text/table controls; listening URLs use validated IDs/hosts.

## Account changes and recovery

Spotify access is read-only. Destination writes require reviewed choices, a
hash-bound plan, account/target preflight and explicit execution. Likes/private
copies are additive; existing dislikes are protected. The adapter has no delete,
unlike or playlist-replacement operation.

An OS lock prevents duplicate local executors. Copy markers, source signatures
and per-occurrence before/after checkpoints support explicit recovery. Unknown
write outcomes are reconciled before another write. Bounded retries apply only
to known read-visibility delays. Source changes and external edits stop automatic
completion. Local locking cannot coordinate unrelated tools or other computers.

Independent verification reports confirmed, partial, different and unchecked
states. Metadata scores are heuristics, and the 85-point policy can accept
recordings with differing metadata. Exact audio identity is not guaranteed.
Quotas and provider compatibility may limit completion.

## Input handling and demonstration

ZIPs are read without extraction, with path/file/count/byte limits. Imported
credential-like fields and linked folder entries are refused. Original bytes and
checksums retain provenance. CSV values are escaped against spreadsheet formulas.

The demo uses a fresh marked temporary root and local simulated destination.
Credentials and application service HTTP are blocked, including access through
previously created Vault instances. Demo listening controls contain no URLs.
It refuses existing/unmarked workspaces and occupied ports; temporary data is
discarded after stopping its own server. Screenshots use invented data only.

## Checks

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\spottoyt.exe demo --walkthrough
.\.venv\Scripts\python.exe scripts/capture_demo.py
```

The suite exercises guardrails and failure paths with synthetic data/mocked
services. It does not prove absence of vulnerabilities, validate current remote
permissions or exhaustively cover provider behavior. Review candidate files for
credentials/private data before sharing; never commit environments, downloads,
real reports, cookies, request captures, databases, private keys or logs.
