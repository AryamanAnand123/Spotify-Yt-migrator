"""Interactive account setup. Secrets stay in Windows Credential Manager."""

import json
import logging
import os
import secrets
from urllib.parse import parse_qs, urlsplit

import requests
from spotipy import Spotify
from spotipy.cache_handler import CacheHandler, MemoryCacheHandler
from spotipy.oauth2 import SpotifyPKCE
from ytmusicapi import OAuthCredentials, YTMusic, setup_oauth
from ytmusicapi.helpers import get_authorization

REDIRECT = "http://127.0.0.1:8765/callback"
SCOPES = "playlist-read-private playlist-read-collaborative user-library-read user-follow-read"


class SetupError(Exception):
    """Safe, user-facing configuration error."""


class TransferPaused(SetupError):
    """A known quota/batch stop; preserve progress for explicit same-plan resume."""


class PlaylistNotReady(SetupError):
    """A known newly-created playlist may not yet be visible to read endpoints."""


class Vault:
    """Explicit Windows backend: never fall back to plaintext storage."""

    def __init__(self):
        if os.environ.get('SPOTTOYT_DEMO') == '1':
            raise SetupError('Credential access is disabled in the synthetic demo.')
        from keyring.backends.Windows import WinVaultKeyring

        self.backend = WinVaultKeyring()

    def get(self, name):
        if os.environ.get('SPOTTOYT_DEMO') == '1':
            raise SetupError('Credential access is disabled in the synthetic demo.')
        raw = self.backend.get_password("Spottoyt", name)
        if not raw:
            raise SetupError(f"No {name} credentials. Run connect-{name} first.")
        try:
            value = json.loads(raw)
            if isinstance(value, dict) and value.get("spottoyt_vault_format") == 2:
                generation, count = value["generation"], value["parts"]
                if not isinstance(generation, str) or len(generation) != 32 or not all(char in "0123456789abcdef" for char in generation) or type(count) is not int or not 1 <= count <= 128:
                    raise ValueError
                parts = [self.backend.get_password("Spottoyt", f"{name}:{generation}:{index}") for index in range(count)]
                if any(not isinstance(part, str) for part in parts):
                    raise ValueError
                value = json.loads("".join(parts))
            return value
        except (ValueError, TypeError, KeyError):
            raise SetupError("Stored credentials are incomplete or damaged. Reconnect locally; no secret details were printed.") from None

    def put(self, name, value):
        if os.environ.get('SPOTTOYT_DEMO') == '1':
            raise SetupError('Credential access is disabled in the synthetic demo.')
        generation = secrets.token_hex(16)
        written = []
        try:
            raw = json.dumps(value)
            if len(raw) > 102400:
                raise ValueError
            previous = self.backend.get_password("Spottoyt", name)
            parts = [raw[index:index + 800] for index in range(0, len(raw), 800)]
            for index, part in enumerate(parts):
                key = f"{name}:{generation}:{index}"
                self.backend.set_password("Spottoyt", key, part)
                written.append(key)
            manifest = {"spottoyt_vault_format": 2, "generation": generation, "parts": len(parts)}
            # Commit the manifest only after all parts exist. Older credentials
            # remain readable if any chunk write fails; never use plaintext files.
            self.backend.set_password("Spottoyt", name, json.dumps(manifest))
        except Exception as exc:
            for key in written:
                try:
                    self.backend.delete_password("Spottoyt", key)
                except Exception:
                    pass
            raise SetupError("Windows Credential Manager could not save credentials. "
                             "No plaintext fallback was written. Check Windows credential-store access.") from exc
        try:
            old = json.loads(previous) if isinstance(previous, str) else {}
            if old.get("spottoyt_vault_format") == 2 and old.get("generation") != generation:
                for index in range(min(int(old["parts"]), 128)):
                    self.backend.delete_password("Spottoyt", f"{name}:{old['generation']}:{index}")
        except Exception:
            pass  # Stale credential chunks are private; cleanup must not invalidate a saved connection.


class TokenCache(CacheHandler):
    def __init__(self, vault):
        self.vault = vault

    def get_cached_token(self):
        return self.vault.get("spotify").get("token")

    def save_token_to_cache(self, token_info):
        config = self.vault.get("spotify")
        config["token"] = token_info
        self.vault.put("spotify", config)


class TimedSession(requests.Session):
    allowed_hosts = {"music.youtube.com", "oauth2.googleapis.com"}

    def request(self, method, url, **kwargs):
        if os.environ.get('SPOTTOYT_DEMO') == '1':
            raise SetupError('External service requests are disabled in the synthetic demo.')
        target = urlsplit(url)
        if (target.scheme != "https" or target.hostname not in
                self.allowed_hosts or
                target.username is not None or target.password is not None or
                target.port not in (None, 443)):
            raise SetupError("Blocked a request outside the approved HTTPS authentication/service hosts.")
        if kwargs.get("verify") is False:
            raise SetupError("Disabling TLS certificate checks is not allowed.")
        # Browser cookies are powerful credentials: never follow redirects with them.
        kwargs["allow_redirects"] = False
        kwargs.setdefault("timeout", 20)
        response = super().request(method, url, **kwargs)
        if 300 <= response.status_code < 400:
            raise SetupError("The service returned a redirect; it was blocked to protect account credentials.")
        if urlsplit(url).hostname == "music.youtube.com" and response.status_code >= 400:
            hints = {
                400: "YouTube Music rejected the library request. This may be an unofficial-client compatibility issue.",
                401: "YouTube Music rejected authorization. The saved grant may need reconnection.",
                403: "YouTube Music refused library access. Check account permissions and service restrictions.",
                429: "YouTube Music rate-limited the request. Wait before retrying.",
            }
            raise SetupError(f"YouTube Music HTTP {response.status_code}. " +
                             hints.get(response.status_code, "YouTube Music returned a service error."))
        if urlsplit(url).hostname == "oauth2.googleapis.com" and response.status_code >= 400:
            try:
                error = response.json().get("error")
            except (ValueError, AttributeError):
                error = None
            hints = {
                "invalid_client": "Google rejected the OAuth client. Check the client ID and matching secret, "
                                  "and confirm the client type is TVs and Limited Input devices.",
                "unauthorized_client": "Google does not allow this OAuth client to use this flow. "
                                       "Confirm TVs and Limited Input devices and your project's consent settings.",
                "authorization_pending": "Google approval is not complete. Restart connect-youtube, finish "
                                         "approval in the browser, then press Enter in the terminal.",
                "access_denied": "Google denied authorization. Check your consent decision and that your "
                                 "YouTube account is listed under Audience > Test users.",
                "expired_token": "The Google device code expired. Restart connect-youtube for a new code.",
                "invalid_grant": "Google rejected the authorization grant. Reconnect YouTube with the "
                                 "same client used to authorize it.",
                "invalid_scope": "Google rejected the requested YouTube scope. Check consent Data access settings.",
                "slow_down": "Google requested a delay. Wait before starting a new authorization attempt.",
            }
            hint = hints.get(error) if isinstance(error, str) else None
            raise SetupError(hint or f"Google OAuth request failed (HTTP {response.status_code}). "
                             "Check client type, consent settings and service availability.")
        return response


class SpotifySession(TimedSession):
    allowed_hosts = {"api.spotify.com", "accounts.spotify.com"}


def safe_failure(exc):
    """Describe known failure categories without exposing provider response bodies."""
    from ytmusicapi.auth.oauth.exceptions import BadOAuthClient, UnauthorizedOAuthClient

    if isinstance(exc, (BadOAuthClient, UnauthorizedOAuthClient)):
        return ("Google rejected the OAuth client. Check that the ID and secret belong to the same "
                "TVs and Limited Input devices client and YouTube Data API v3 is enabled in that project.")
    if isinstance(exc, requests.exceptions.SSLError):
        return "TLS certificate validation failed. Check system time and network certificates; do not disable TLS checks."
    if isinstance(exc, requests.exceptions.Timeout):
        return "The service request timed out. Check connectivity and try again later."
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "Could not connect to the service. Check internet access, DNS, firewall or proxy settings."
    return ("Account operation failed due to an unexpected local or service error. "
            "Sensitive details were suppressed; report which command and sign-in stage failed.")


def quiet_library_logs():
    # OAuth libraries can include request bodies or URLs in diagnostic logs.
    for name in ("spotipy", "spotipy.oauth2", "spotipy.client", "ytmusicapi", "urllib3.connectionpool"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        logger.setLevel(logging.CRITICAL + 1)


def callback_code(url, expected_state):
    parsed = urlsplit(url.strip())
    expected = urlsplit(REDIRECT)
    if (parsed.scheme, parsed.netloc, parsed.path) != (expected.scheme, expected.netloc, expected.path) or parsed.fragment:
        raise SetupError("Callback must use the configured local redirect URL.")
    query = parse_qs(parsed.query)
    state = query.get("state", [])
    if len(state) != 1 or not secrets.compare_digest(state[0], expected_state):
        raise SetupError("Authorization state mismatch. Restart connection.")
    if "error" in query:
        raise SetupError("Spotify authorization was declined. Restart when ready.")
    code = query.get("code", [])
    if len(code) != 1 or not code[0]:
        raise SetupError("Callback has no valid authorization code.")
    return code[0]


def spotify_manager(client_id, vault, state=None):
    return SpotifyPKCE(client_id=client_id, redirect_uri=REDIRECT, scope=SCOPES,
                       state=state, cache_handler=TokenCache(vault),
                       requests_timeout=20, requests_session=SpotifySession(), open_browser=False)


def spotify_reader(vault):
    quiet_library_logs()
    config = vault.get("spotify")
    manager = spotify_manager(config["client_id"], vault)
    if not manager.validate_token(config.get("token")):
        raise SetupError("Spotify token missing or permissions changed. Reconnect Spotify.")
    return Spotify(auth_manager=manager, requests_session=SpotifySession(),
                   requests_timeout=20, retries=0, status_retries=0)


def connect_spotify(client_id, prompt, echo, vault):
    quiet_library_logs()
    state = secrets.token_urlsafe(32)
    manager = spotify_manager(client_id, vault, state)
    cache = MemoryCacheHandler()
    manager.cache_handler = cache
    echo("Open this Spotify authorization URL locally (do not share it):")
    echo(manager.get_authorize_url())
    echo("After consent the browser may say it cannot connect. Copy its full address below.")
    code = callback_code(prompt("Callback URL", hide_input=True), state)
    # No token file is ever created; the cache handler writes only to the vault.
    manager.get_access_token(code=code, check_cache=False)
    vault.put("spotify", {"client_id": client_id, "token": cache.get_cached_token()})


def spotify_probes(vault):
    quiet_library_logs()
    config = vault.get("spotify")
    manager = spotify_manager(config["client_id"], vault)
    if not manager.validate_token(config.get("token")):
        raise SetupError("Spotify token missing or permissions changed. Reconnect Spotify.")
    client = Spotify(auth_manager=manager, requests_timeout=20, retries=0, status_retries=0)
    probes = {
        "playlists": ("me/playlists", {"limit": 1}),
        "liked songs": ("me/tracks", {"limit": 1}),
        "saved albums": ("me/albums", {"limit": 1}),
        "followed artists": ("me/following", {"type": "artist", "limit": 1}),
        "saved shows": ("me/shows", {"limit": 1}),
        "saved episodes": ("me/episodes", {"limit": 1}),
    }
    results = {}
    from spotipy.exceptions import SpotifyException

    for label, (path, params) in probes.items():
        try:
            data = client._get(path, **params)
            total = data.get("artists", data).get("total", "unknown")
            results[label] = f"readable; reported total: {total}"
        except SpotifyException as exc:
            results[label] = f"unavailable (HTTP {exc.http_status}); no automatic retries"
            if exc.http_status in (401, 429):
                break
    return results


def connect_youtube(client_id, client_secret, vault):
    quiet_library_logs()
    # filepath=None prevents ytmusicapi from writing OAuth JSON to disk.
    token = setup_oauth(client_id, client_secret, filepath=None,
                        session=TimedSession(), open_browser=False)
    vault.put("youtube", {"client_id": client_id, "client_secret": client_secret,
                          "token": token.as_dict()})


def youtube_probe(vault):
    quiet_library_logs()
    config = vault.get("youtube")
    session = TimedSession()
    credentials = OAuthCredentials(config["client_id"], config["client_secret"], session=session)
    yt = YTMusic(auth=config["token"], oauth_credentials=credentials, requests_session=session)
    # Small authenticated read; the service may return more than the requested limit.
    playlists = yt.get_library_playlists(limit=1)
    config["token"] = yt._token.as_dict()
    vault.put("youtube", config)
    return f"Library readable; sample returned {len(playlists)} playlist entries (not a total)."


def browser_headers(cookie, authuser="0"):
    """Preserve the supplied browser Cookie header; some sessions need more than SAPISID."""
    if not authuser.isdigit() or len(authuser) > 3:
        raise SetupError("AuthUser must be a numeric Google account index, such as 0.")
    parts = {}
    for part in cookie.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep:
            parts[key] = value
    token = parts.get("__Secure-3PAPISID", "")
    if not token or len(token) > 1000 or any(char.isspace() for char in token) or len(cookie) > 65536 or any(ord(char) < 32 for char in cookie):
        raise SetupError("Cookie header must contain a valid __Secure-3PAPISID value. "
                         "Copy the Cookie request header from your signed-in YouTube Music tab.")
    # Browser detection requires SAPISIDHASH in the initial authorization header.
    # YTMusic then regenerates its timestamp/hash for each request.
    return {"cookie": cookie, "x-goog-authuser": authuser,
            "authorization": get_authorization(token + " https://music.youtube.com"),
            "x-origin": "https://music.youtube.com", "content-type": "application/json"}


def connect_youtube_browser(cookie, authuser, vault):
    quiet_library_logs()
    headers = browser_headers(cookie, authuser)
    yt = YTMusic(auth=headers, requests_session=TimedSession())
    verify_browser_account(yt)
    playlists = yt.get_library_playlists(limit=1)
    # Do not replace existing entries until the read succeeds.
    vault.put("youtube-browser", {"headers": headers})
    return f"Browser library readable; sample returned {len(playlists)} playlist entries."


def youtube_browser_probe(vault):
    quiet_library_logs()
    config = vault.get("youtube-browser")
    yt = YTMusic(auth=config["headers"], requests_session=TimedSession())
    verify_browser_account(yt)
    playlists = yt.get_library_playlists(limit=1)
    return f"Browser library readable; sample returned {len(playlists)} playlist entries (not a total)."


def verify_browser_account(yt):
    try:
        account = yt.get_account_info()
        if not isinstance(account, dict) or not isinstance(account.get("accountName"), str) or not account["accountName"]:
            raise ValueError
        if not isinstance(account.get("channelHandle"), str) or not account["channelHandle"].startswith("@"):
            raise ValueError
        return account
    except (KeyError, TypeError, ValueError):
        raise SetupError("Signed-in YouTube account identity could not be verified. Refresh your signed-in Music tab, "
                         "copy the complete current Cookie request-header value and account index, and run connect-youtube-browser again. "
                         "An empty library response alone is not proof of sign-in.") from None
