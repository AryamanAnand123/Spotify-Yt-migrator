"""Official YouTube Data API for durable OAuth likes and playlist copies."""

import hashlib
import json
import time

from ytmusicapi import YTMusic

from spottoyt.auth import PlaylistNotReady, SetupError, TimedSession, TransferPaused, Vault, quiet_library_logs


class OfficialSession(TimedSession):
    allowed_hosts = {"www.googleapis.com", "oauth2.googleapis.com"}


class YouTubeOfficial:
    def __init__(self, vault=None, session=None, public_client=None):
        quiet_library_logs()
        self.vault = vault or Vault()
        self.config = self.vault.get("youtube")
        self.session = session or OfficialSession()
        self.public_client = public_client
        self.write_limit = None
        self.writes_attempted = 0
        self.write_counts = {}
        # Access/refresh token rotation does not change the app binding; channel
        # identity is checked separately before every execution.
        self.credential_hash = hashlib.sha256(json.dumps({"provider": "youtube-data-api",
            "client_id": self.config["client_id"]}, sort_keys=True).encode()).hexdigest()

    def refresh(self):
        token = self.config["token"]
        response = self.session.post("https://oauth2.googleapis.com/token", data={
            "client_id": self.config["client_id"], "client_secret": self.config["client_secret"],
            "refresh_token": token["refresh_token"], "grant_type": "refresh_token"})
        if response.status_code != 200:
            raise SetupError("Google OAuth refresh failed. Reauthorize the Google OAuth grant locally; no browser cookie is needed.")
        update = response.json()
        if not isinstance(update.get("access_token"), str) or not isinstance(update.get("expires_in"), (int, float)):
            raise SetupError("Google returned an incomplete OAuth token response.")
        token.update({key: update[key] for key in ("access_token", "refresh_token", "scope", "token_type") if key in update})
        token["official_expires_at"] = time.time() + update["expires_in"]
        self.vault.put("youtube", self.config)

    def request(self, method, endpoint, params=None, body=None):
        if method != 'GET':
            if self.write_limit is not None and self.writes_attempted >= self.write_limit:
                raise TransferPaused("This batch reached its write budget; no further write was sent. Resume the same plan in the next quota window.")
            self.writes_attempted += 1
            self.write_counts[endpoint] = self.write_counts.get(endpoint, 0) + 1
        token = self.config["token"]
        if token.get("official_expires_at", 0) <= time.time() + 60:
            self.refresh()
        response = self.session.request(method, "https://www.googleapis.com/youtube/v3/" + endpoint,
                                        params=params, json=body,
                                        headers={"Authorization": "Bearer " + token["access_token"]})
        if response.status_code >= 400:
            if method == 'GET' and endpoint in ('playlists', 'playlistItems') and response.status_code == 404:
                raise PlaylistNotReady('The playlist is not visible to the read endpoint yet, or is inaccessible.')
            reasons = []
            try:
                reasons = [row.get("reason") for row in response.json().get("error", {}).get("errors", [])]
            except (ValueError, AttributeError, TypeError):
                pass
            if any(reason in {"quotaExceeded", "dailyLimitExceeded"} for reason in reasons):
                raise TransferPaused("YouTube daily API quota reached. Resume the same plan after the quota resets; no write was retried.")
            if response.status_code == 401:
                # A fresh token is obtained on the next explicit attempt. Do not
                # automatically repeat an ambiguous failed mutation.
                token["official_expires_at"] = 0
                self.vault.put("youtube", self.config)
            raise SetupError(f"Official YouTube API request failed (HTTP {response.status_code}). "
                             "Check API enablement, OAuth permissions or quota. Response details were omitted.")
        return {} if response.status_code == 204 or not response.content else response.json()

    def pages(self, endpoint, params):
        result, seen = [], set()
        while True:
            data = self.request("GET", endpoint, params=params)
            items = data.get("items")
            if not isinstance(items, list):
                raise SetupError("Official YouTube API returned an unexpected list response.")
            result.extend(items)
            next_page = data.get("nextPageToken")
            if not next_page:
                return result
            if next_page in seen or len(seen) >= 2000:
                raise SetupError("Official YouTube pagination limit/repetition detected.")
            seen.add(next_page)
            params = {**params, "pageToken": next_page}

    def get_account_info(self):
        data = self.request("GET", "channels", {"part": "id,snippet", "mine": "true"})
        items = data.get("items", [])
        if len(items) != 1:
            raise SetupError("Exactly one authorized YouTube channel is required. Check the Google account/channel used for OAuth.")
        snippet = items[0].get("snippet", {})
        if not isinstance(snippet.get("customUrl"), str) or not snippet["customUrl"].startswith("@"):
            raise SetupError("Official OAuth returned no verifiable channel handle. Check the YouTube channel selected for Google authorization.")
        return {"accountName": snippet.get("title"), "channelHandle": snippet.get("customUrl"), "channelId": items[0].get("id")}

    def get_song(self, videoId):
        # Public player metadata supplies the existing availability check without
        # any saved browser cookie. No streaming URL is fetched or returned here.
        if self.public_client is None:
            self.public_client = YTMusic(requests_session=TimedSession())
        data = self.public_client.get_song(videoId)
        return {"playabilityStatus": {"status": data.get("playabilityStatus", {}).get("status")},
                "videoDetails": {key: data.get("videoDetails", {}).get(key) for key in ("videoId", "title", "lengthSeconds")}}

    def get_watch_playlist(self, videoId, limit=1):
        data = self.request("GET", "videos/getRating", {"id": videoId})
        row = next((item for item in data.get("items", []) if item.get("videoId") == videoId), None)
        ratings = {"like": "LIKE", "dislike": "DISLIKE", "none": "INDIFFERENT"}
        return {"tracks": [{"videoId": videoId, "likeStatus": ratings.get(row.get("rating")) if row else None,
                            "inLibrary": None, "feedbackTokens": None}]}

    def rate_song(self, videoId, rating):
        if rating != "LIKE":
            raise SetupError("This adapter only adds likes; dislikes and rating removal are not supported.")
        return self.request("POST", "videos/rate", {"id": videoId, "rating": "like"})

    def get_library_playlists(self, limit=None):
        params = {"part": "id,snippet,status", "mine": "true", "maxResults": min(limit, 50) if limit else 50}
        items = self.pages("playlists", params) if limit is None else self.request("GET", "playlists", params).get("items", [])
        return [{"playlistId": item["id"], "title": item["snippet"]["title"], "owned": True} for item in items]

    def get_playlist(self, playlistId, limit=None):
        data = self.request("GET", "playlists", {"part": "id,snippet,status,contentDetails", "id": playlistId})
        items = data.get("items", [])
        if len(items) != 1:
            raise PlaylistNotReady("The mapped destination playlist is missing or not visible yet.")
        playlist = items[0]
        entries = self.pages("playlistItems", {"part": "snippet,contentDetails", "playlistId": playlistId, "maxResults": 50})
        entries.sort(key=lambda item: item["snippet"]["position"])
        return {"id": playlist["id"], "title": playlist["snippet"]["title"],
                "description": playlist["snippet"].get("description", ""), "privacy": playlist["status"]["privacyStatus"].upper(),
                "trackCount": playlist["contentDetails"]["itemCount"],
                "tracks": [{"videoId": item.get("contentDetails", {}).get("videoId")} for item in entries]}

    def create_playlist(self, title, description, privacy_status="PRIVATE"):
        if privacy_status != "PRIVATE":
            raise SetupError("Only private playlist copies are supported.")
        data = self.request("POST", "playlists", {"part": "snippet,status"},
                            {"snippet": {"title": title, "description": description}, "status": {"privacyStatus": "private"}})
        return data.get("id")

    def add_playlist_items(self, playlistId, videoIds, duplicates=False):
        if playlistId == "SE":
            raise SetupError("Official YouTube API cannot save YouTube Music-specific episodes. That state remains unsupported in this adapter.")
        for identifier in videoIds:
            self.request("POST", "playlistItems", {"part": "snippet"}, {"snippet": {"playlistId": playlistId,
                         "resourceId": {"kind": "youtube#video", "videoId": identifier}}})
        return {"status": "STATUS_SUCCEEDED"}

    def get_podcast(self, *args, **kwargs):
        raise SetupError("YouTube Music-specific show-library saves are not supported by the official adapter.")

    def insert_playlist_item(self, playlistId, videoId, position):
        if type(position) is not int or position < 0:
            raise SetupError("Invalid playlist insertion position.")
        data = self.request('POST', 'playlistItems', {'part': 'snippet'}, {'snippet': {
            'playlistId': playlistId, 'position': position,
            'resourceId': {'kind': 'youtube#video', 'videoId': videoId}}})
        if not isinstance(data.get('id'), str):
            raise SetupError("Playlist insertion returned no item ID. Reconcile the same plan before resuming.")
        return {'status': 'STATUS_SUCCEEDED'}

    def get_episode(self, *args, **kwargs):
        raise SetupError("YouTube Music-specific saved-episode state is not supported by the official adapter.")
