# Account setup

Use a private local terminal. Never share credentials, cookies, OAuth links,
callback URLs, device codes or raw responses in chat, screenshots or logs.
The offline [demo](demo.md) needs no account setup.

Credentials use Windows Credential Manager under service `Spottoyt`, with entries
`spotify` and `youtube`. Storage errors fail without a plaintext fallback.

## Spotify

1. Create or reuse a Spotify developer application with Web API access. Check
   the provider's current account and Development Mode requirements.
2. Register `http://127.0.0.1:8765/callback` as the redirect URI.
3. Run `.\.venv\Scripts\spottoyt.exe connect-spotify` and enter the client ID
   at the hidden prompt. PKCE does not require a Spotify client secret.
4. Open the displayed authorization URL, review the read-only permissions and
   authorize the intended account.
5. The callback address has no listening server, so a browser connection error
   is expected. Copy the full address-bar URL into the hidden terminal prompt.
   The app validates its origin, state and authorization code.
6. Run `.\.venv\Scripts\spottoyt.exe check-spotify` for category-access probes.

The probe is a bounded sample, not proof of complete export coverage. A 401/429
stops remaining probes; a 403 can reflect permissions or service restrictions.

## YouTube OAuth

Likes and private playlist copies use the official YouTube Data API.

1. Select a Google Cloud project and enable **YouTube Data API v3**.
2. Configure OAuth consent for your use. Add the intended Google account as a
   test user where required. No billing activation is needed for this setup.
3. Create an OAuth client of type **TVs and Limited Input devices**, matching
   the device flow used by this application. Keep its ID and secret private.
4. Run `.\.venv\Scripts\spottoyt.exe connect-youtube`.
5. Enter the client ID and secret at the hidden prompts. Follow the displayed
   Google device-authorization link and review the app identity and consent.
6. Return to the terminal after approval, then run
   `.\.venv\Scripts\spottoyt.exe check-youtube`.

The YouTube grant is broader than read-only. Connection checks read account and
playlist data; account changes require a reviewed plan and explicit execution.
Access tokens refresh automatically. Revocation or refresh-grant expiry can
require reconnecting. External apps in Google's Testing state may receive
refresh tokens that expire after seven days. Premium does not remove API quota.

OAuth against unofficial Music endpoints can behave differently from official
YouTube endpoints. The normal likes/playlist workflow does not require browser
cookies. Music-specific library and podcast saves remain unsupported.

## Optional legacy browser connection

Browser cookies grant broad session access. Use this only for a workflow that
explicitly requires it; official likes/playlist transfers use OAuth instead.

1. Sign in to `https://music.youtube.com` with the intended account.
2. Open browser Developer Tools, choose **Network**, filter by `browse`, then
   refresh or open Library. Select a successful Music browse request.
3. Copy the complete **Cookie request-header value** and note `X-Goog-AuthUser`
   from the same request. Do not copy these into files or messages.
4. Run `.\.venv\Scripts\spottoyt.exe connect-youtube-browser` and enter both
   values locally. Setup verifies signed-in identity before saving the cookie.
5. Clear the clipboard and clipboard history. Verify with
   `.\.venv\Scripts\spottoyt.exe check-youtube-browser`.

Large credentials use bounded Credential Manager chunks with an atomic manifest.
There is no cookie JSON fallback. Browser sign-out/revocation and OAuth revocation
are separate actions; deleting a local entry does not revoke remote access.

## Recovery and revocation

- Provider/network errors use fixed diagnostics rather than printing response bodies.
- Requests have per-request timeouts and reject redirects/disabled TLS checks.
- Do not repeatedly retry authentication or rate-limit failures.
- Revoke remote grants through Spotify apps or Google third-party connections.
- Remove only Spottoyt's local entries through Windows Credential Manager if
  clearing local credentials. Chunked entries include a base manifest and parts.
- Same-user processes may access credentials; local storage is not protection
  against a compromised Windows account.

References:
- https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow
- https://developers.google.com/youtube/v3/getting-started
- https://developers.google.com/identity/protocols/oauth2
- https://ytmusicapi.readthedocs.io/en/stable/setup/oauth.html
- https://ytmusicapi.readthedocs.io/en/stable/setup/browser.html
