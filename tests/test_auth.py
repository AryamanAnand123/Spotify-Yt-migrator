"""Offline security and failure checks: no accounts or credential store touched."""

from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from spotipy.exceptions import SpotifyException
from typer.testing import CliRunner

from spottoyt import auth
from spottoyt.cli import app


def test_pkce_url_has_only_read_scopes():
    manager = auth.spotify_manager("example", Mock(), "state")
    query = parse_qs(urlsplit(manager.get_authorize_url()).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == ["state"]
    assert all("read" in scope for scope in query["scope"][0].split())
    assert "client_secret" not in query


@pytest.mark.parametrize("suffix", [
    "?code=secret&state=wrong", "?code=secret", "?state=ok",
    "?code=secret&state=ok&state=ok", "?error=access_denied&state=ok",
])
def test_invalid_callback_is_rejected_without_echoing_secret(suffix):
    with pytest.raises(auth.SetupError) as error:
        auth.callback_code(auth.REDIRECT + suffix, "ok")
    assert "secret" not in str(error.value)


def test_wrong_callback_origin_rejected():
    with pytest.raises(auth.SetupError):
        auth.callback_code("https://evil.example/callback?state=ok&code=secret", "ok")
    assert auth.callback_code(auth.REDIRECT + "?state=ok&code=valid", "ok") == "valid"


def test_failed_exchange_does_not_overwrite_vault(monkeypatch):
    vault = Mock()
    manager = Mock()
    manager.get_access_token.side_effect = RuntimeError("sensitive provider body")
    monkeypatch.setattr(auth, "spotify_manager", lambda *args: manager)
    monkeypatch.setattr(auth.secrets, "token_urlsafe", lambda _: "state")
    with pytest.raises(RuntimeError):
        auth.connect_spotify("client", lambda *a, **k: auth.REDIRECT + "?state=state&code=c", lambda _: None, vault)
    vault.put.assert_not_called()


def test_rate_limit_stops_probes(monkeypatch):
    vault = Mock()
    vault.get.return_value = {"client_id": "client", "token": {"access_token": "secret"}}
    manager = Mock()
    manager.validate_token.return_value = True
    monkeypatch.setattr(auth, "spotify_manager", lambda *args: manager)
    client = Mock()
    client._get.side_effect = SpotifyException(429, -1, "private error")
    monkeypatch.setattr(auth, "Spotify", lambda **kwargs: client)
    results = auth.spotify_probes(vault)
    assert len(results) == 1
    assert "429" in results["playlists"]
    assert "private" not in str(results)
    assert client._get.call_count == 1


def test_cli_hides_unexpected_provider_errors(monkeypatch):
    monkeypatch.setattr(auth, "Vault", Mock)
    monkeypatch.setattr(auth, "spotify_probes", Mock(side_effect=RuntimeError("SECRET-TOKEN")))
    result = CliRunner().invoke(app, ["check-spotify"])
    assert result.exit_code == 1
    assert "SECRET-TOKEN" not in result.output


def test_youtube_flow_never_writes_token_file(monkeypatch):
    vault = Mock()
    token = Mock()
    token.as_dict.return_value = {"access_token": "secret"}
    setup = Mock(return_value=token)
    monkeypatch.setattr(auth, "setup_oauth", setup)
    auth.connect_youtube("client", "secret", vault)
    assert setup.call_args.kwargs["filepath"] is None
    assert setup.call_args.kwargs["open_browser"] is False
    vault.put.assert_called_once()


def test_youtube_probe_only_reads_and_saves_refresh(monkeypatch):
    config = {"client_id": "c", "client_secret": "s", "token": {"access_token": "old"}}
    vault = Mock()
    vault.get.return_value = config
    yt = Mock()
    yt.get_library_playlists.return_value = []
    yt._token.as_dict.return_value = {"access_token": "new"}
    monkeypatch.setattr(auth, "YTMusic", Mock(return_value=yt))
    assert "readable" in auth.youtube_probe(vault)
    yt.get_library_playlists.assert_called_once_with(limit=1)
    assert vault.put.call_args.args[1]["token"]["access_token"] == "new"


@pytest.mark.parametrize("error,expected", [
    ("invalid_client", "matching secret"),
    ("authorization_pending", "approval is not complete"),
    ("access_denied", "Test users"),
    ("expired_token", "expired"),
    ("SECRET-UNKNOWN", "HTTP 400"),
    ({"unexpected": "SECRET"}, "HTTP 400"),
])
def test_oauth_errors_are_actionable_without_leaking_body(monkeypatch, error, expected):
    response = Mock(status_code=400)
    response.json.return_value = {"error": error, "error_description": "PRIVATE-CREDENTIAL"}
    monkeypatch.setattr(auth.requests.Session, "request", Mock(return_value=response))
    with pytest.raises(auth.SetupError) as failure:
        auth.TimedSession().post("https://oauth2.googleapis.com/token")
    assert expected in str(failure.value)
    assert "PRIVATE-CREDENTIAL" not in str(failure.value)
    assert "SECRET" not in str(failure.value)


def test_vault_write_error_has_no_secret_or_plaintext_fallback():
    vault = object.__new__(auth.Vault)
    vault.backend = Mock()
    vault.backend.set_password.side_effect = RuntimeError("SECRET")
    with pytest.raises(auth.SetupError) as failure:
        vault.put("youtube", {"token": "SECRET"})
    assert "Credential Manager" in str(failure.value)
    assert "SECRET" not in str(failure.value)


@pytest.mark.parametrize("failure,expected", [
    (auth.requests.exceptions.Timeout("SECRET"), "timed out"),
    (auth.requests.exceptions.ConnectionError("SECRET"), "Could not connect"),
    (auth.requests.exceptions.SSLError("SECRET"), "TLS certificate"),
])
def test_network_diagnostics_do_not_leak_secrets(failure, expected):
    message = auth.safe_failure(failure)
    assert expected in message
    assert "SECRET" not in message


def test_music_http_error_hides_body(monkeypatch):
    response = Mock(status_code=400, text="PRIVATE-RESPONSE")
    monkeypatch.setattr(auth.requests.Session, "request", Mock(return_value=response))
    with pytest.raises(auth.SetupError, match="YouTube Music HTTP 400") as failure:
        auth.TimedSession().post("https://music.youtube.com/youtubei/v1/browse")
    assert "PRIVATE" not in str(failure.value)


def test_browser_cookie_preserves_required_session_context():
    headers = auth.browser_headers("unrelated=private; __Secure-3PAPISID=example", "1")
    assert headers["cookie"] == "unrelated=private; __Secure-3PAPISID=example"
    assert headers["x-goog-authuser"] == "1"


def test_actual_library_recognizes_generated_browser_headers_without_network(monkeypatch):
    from ytmusicapi.auth.types import AuthType

    session = Mock()
    headers = auth.browser_headers("__Secure-3PAPISID=example", "1")
    yt = auth.YTMusic(auth=headers, requests_session=session)
    assert yt.auth_type == AuthType.BROWSER
    assert yt.headers["authorization"].startswith("SAPISIDHASH ")
    assert yt.headers["x-goog-authuser"] == "1"
    assert "example" not in yt.headers["authorization"]
    session.post.assert_not_called()
    session.get.assert_not_called()


@pytest.mark.parametrize("cookie,index", [("other=secret", "0"), ("__Secure-3PAPISID=secret", "bad")])
def test_invalid_browser_credentials_are_not_echoed(cookie, index):
    with pytest.raises(auth.SetupError) as failure:
        auth.browser_headers(cookie, index)
    assert "secret" not in str(failure.value)


def test_browser_connection_verifies_before_saving(monkeypatch):
    vault = Mock()
    yt = Mock()
    yt.get_account_info.return_value = {"accountName": "Synthetic", "channelHandle": "@synthetic"}
    yt.get_library_playlists.side_effect = auth.SetupError("HTTP 400")
    monkeypatch.setattr(auth, "YTMusic", Mock(return_value=yt))
    with pytest.raises(auth.SetupError):
        auth.connect_youtube_browser("__Secure-3PAPISID=example", "0", vault)
    vault.put.assert_not_called()
    yt.get_library_playlists.side_effect = None
    yt.get_library_playlists.return_value = []
    auth.connect_youtube_browser("__Secure-3PAPISID=example", "0", vault)
    vault.put.assert_called_once()


def test_browser_connection_rejects_anonymous_empty_library(monkeypatch):
    vault = Mock()
    yt = Mock()
    yt.get_account_info.side_effect = KeyError("PRIVATE-RESPONSE")
    yt.get_library_playlists.return_value = []
    monkeypatch.setattr(auth, "YTMusic", Mock(return_value=yt))
    with pytest.raises(auth.SetupError, match="identity") as failure:
        auth.connect_youtube_browser("__Secure-3PAPISID=example", "0", vault)
    assert "PRIVATE-RESPONSE" not in str(failure.value)
    vault.put.assert_not_called()


class MemoryCredentials:
    def __init__(self):
        self.values = {}
        self.remaining = None

    def get_password(self, service, name):
        return self.values.get((service, name))

    def set_password(self, service, name, value):
        if self.remaining is not None:
            if self.remaining == 0:
                raise RuntimeError("SECRET")
            self.remaining -= 1
        self.values[(service, name)] = value

    def delete_password(self, service, name):
        self.values.pop((service, name), None)


def test_large_credentials_are_chunked_in_vault_not_files():
    vault = object.__new__(auth.Vault)
    vault.backend = MemoryCredentials()
    config = {"headers": {"cookie": "SID=" + "x" * 20000}}
    vault.put("youtube-browser", config)
    assert vault.get("youtube-browser") == config
    assert len(vault.backend.values) > 10
    assert all(len(value) <= 800 for value in vault.backend.values.values())


def test_failed_chunk_update_keeps_previous_credentials():
    vault = object.__new__(auth.Vault)
    vault.backend = MemoryCredentials()
    previous = {"token": "previous"}
    vault.put("spotify", previous)
    before = dict(vault.backend.values)
    vault.backend.remaining = 1
    with pytest.raises(auth.SetupError):
        vault.put("spotify", {"token": "x" * 4000})
    assert vault.get("spotify") == previous
    assert vault.backend.values == before


def test_legacy_single_entry_credentials_still_readable():
    import json
    vault = object.__new__(auth.Vault)
    vault.backend = MemoryCredentials()
    vault.backend.set_password("Spottoyt", "spotify", json.dumps({"token": "legacy"}))
    assert vault.get("spotify") == {"token": "legacy"}


@pytest.mark.parametrize("url", [
    "http://music.youtube.com/", "https://evil.example/",
    "https://music.youtube.com.evil.example/", "https://music.youtube.com:444/",
    "https://user@music.youtube.com/",
])
def test_session_blocks_untrusted_destinations_before_network(monkeypatch, url):
    request = Mock()
    monkeypatch.setattr(auth.requests.Session, "request", request)
    with pytest.raises(auth.SetupError):
        auth.TimedSession().post(url, headers={"cookie": "SECRET"})
    request.assert_not_called()


def test_redirect_is_blocked_without_exposing_location(monkeypatch):
    response = Mock(status_code=302, headers={"Location": "https://evil.example/SECRET"})
    request = Mock(return_value=response)
    monkeypatch.setattr(auth.requests.Session, "request", request)
    with pytest.raises(auth.SetupError, match="redirect") as failure:
        auth.TimedSession().post("https://music.youtube.com/youtubei/v1/browse")
    assert request.call_args.kwargs["allow_redirects"] is False
    assert "SECRET" not in str(failure.value)


def test_tls_cannot_be_disabled(monkeypatch):
    request = Mock()
    monkeypatch.setattr(auth.requests.Session, "request", request)
    with pytest.raises(auth.SetupError, match="TLS"):
        auth.TimedSession().post("https://music.youtube.com/", verify=False)
    request.assert_not_called()
