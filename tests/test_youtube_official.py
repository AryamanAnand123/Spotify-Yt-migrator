"""Official OAuth adapter tests; fake HTTP only, no accounts or quotas consumed."""

import copy
import json
import time
from unittest.mock import Mock

import pytest

from spottoyt.auth import SetupError
from spottoyt.youtube_official import YouTubeOfficial


class Vault:
    def __init__(self, expires=0):
        self.data = {"client_id": "synthetic-client", "client_secret": "SECRET", "token": {
            "refresh_token": "REFRESH-SECRET", "access_token": "OLD-SECRET", "official_expires_at": expires}}
        self.saved = 0

    def get(self, name):
        assert name == "youtube"
        return copy.deepcopy(self.data)

    def put(self, name, value):
        self.data = copy.deepcopy(value)
        self.saved += 1


def response(data=None, status=200):
    return Mock(status_code=status, content=b"" if status == 204 else json.dumps(data).encode(), json=Mock(return_value=data))


def test_access_token_refresh_is_automatic_and_credential_binding_stable():
    vault, session = Vault(), Mock()
    session.post.return_value = response({"access_token": "NEW-SECRET", "expires_in": 3600})
    session.request.return_value = response({"items": [{"id": "UCsynthetic", "snippet": {"title": "Synthetic", "customUrl": "@synthetic"}}]})
    client = YouTubeOfficial(vault=vault, session=session)
    before = client.credential_hash
    assert client.get_account_info()["channelHandle"] == "@synthetic"
    client.get_account_info()
    assert session.post.call_count == 1
    assert vault.saved == 1
    assert vault.data["token"]["refresh_token"] == "REFRESH-SECRET"
    assert client.credential_hash == before
    assert session.request.call_args.kwargs["headers"] == {"Authorization": "Bearer NEW-SECRET"}


def test_likes_use_official_endpoint_and_never_retry_mutations():
    session = Mock()
    session.request.return_value = response(status=204)
    client = YouTubeOfficial(vault=Vault(time.time() + 3600), session=session)
    client.rate_song("abcdefghijk", "LIKE")
    call = session.request.call_args
    assert call.args == ("POST", "https://www.googleapis.com/youtube/v3/videos/rate")
    assert call.kwargs["params"] == {"id": "abcdefghijk", "rating": "like"}
    assert session.request.call_count == 1
    with pytest.raises(SetupError):
        client.rate_song("abcdefghijk", "DISLIKE")


def test_quota_failure_is_safe_and_not_retried():
    session = Mock()
    session.request.return_value = response({"error": {"message": "SECRET", "errors": [{"reason": "quotaExceeded"}]}}, 403)
    client = YouTubeOfficial(vault=Vault(time.time() + 3600), session=session)
    with pytest.raises(SetupError, match="quota") as error:
        client.rate_song("abcdefghijk", "LIKE")
    assert "SECRET" not in str(error.value)
    assert session.request.call_count == 1


def test_playlist_inserts_keep_duplicate_occurrences_in_input_order():
    session = Mock()
    session.request.return_value = response({"id": "synthetic"})
    client = YouTubeOfficial(vault=Vault(time.time() + 3600), session=session)
    client.add_playlist_items("PLsynthetic123", ["abcdefghijk", "zyxwvutsrqp", "abcdefghijk"], duplicates=True)
    assert [call.kwargs["json"]["snippet"]["resourceId"]["videoId"] for call in session.request.call_args_list] == ["abcdefghijk", "zyxwvutsrqp", "abcdefghijk"]


def test_public_player_never_receives_oauth_and_sensitive_fields_are_removed():
    public = Mock()
    public.get_song.return_value = {"videoDetails": {"videoId": "abcdefghijk", "title": "Example", "lengthSeconds": "180"},
                                   "playabilityStatus": {"status": "OK"}, "streamingData": {"url": "SECRET"}}
    session = Mock()
    client = YouTubeOfficial(vault=Vault(time.time() + 3600), session=session, public_client=public)
    assert "SECRET" not in str(client.get_song("abcdefghijk"))
    public.get_song.assert_called_once_with("abcdefghijk")
    session.request.assert_not_called()


def test_pagination_and_music_specific_states_are_explicit():
    session = Mock()
    session.request.side_effect = [response({"items": [{"id": "one", "snippet": {"title": "First"}}], "nextPageToken": "next"}),
                                   response({"items": [{"id": "two", "snippet": {"title": "Second"}}]})]
    client = YouTubeOfficial(vault=Vault(time.time() + 3600), session=session)
    assert [row["playlistId"] for row in client.get_library_playlists()] == ["one", "two"]
    with pytest.raises(SetupError, match="not supported"):
        client.get_podcast("MPSPPLsynthetic")


def test_positioned_insertion_sends_index_and_budget_pauses_without_sending_another_write():
    from spottoyt.auth import TransferPaused
    session = Mock()
    session.request.return_value = response({'id': 'synthetic-item'})
    client = YouTubeOfficial(vault=Vault(time.time() + 3600), session=session)
    client.write_limit = 1
    client.insert_playlist_item('PLsynthetic', 'abcdefghijk', 2)
    assert session.request.call_args.kwargs['json']['snippet']['position'] == 2
    with pytest.raises(TransferPaused):
        client.rate_song('abcdefghijk', 'LIKE')
    assert session.request.call_count == 1
    client.request('GET', 'channels', {'mine': 'true'})
    assert session.request.call_count == 2
