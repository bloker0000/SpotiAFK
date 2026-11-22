from spotiafk.config import Config
from spotiafk.spotify import load_playlist


def get_tracks(client, config):
    return load_playlist(client, config)[1]


def make_track(name, uri="spotify:track:x", duration=1000):
    return {"name": name, "uri": uri, "duration_ms": duration}


class FakeClient:
    # one playlist page, in the old or the new api shape

    def __init__(self, entries):
        self.entries = entries

    def current_user_playlists(self):
        return {"items": [{"name": "AFK", "id": "pl-1"}], "next": None}

    def playlist_items(self, playlist_id):
        return {"items": self.entries, "next": None}


CONFIG = Config(playlist="AFK", shuffle=False)


def test_new_api_shape_uses_item_key():
    client = FakeClient([{"item": make_track("a")}, {"item": make_track("b")}])
    assert [name for _, _, name in get_tracks(client, CONFIG)] == ["a", "b"]


def test_old_api_shape_uses_track_key():
    client = FakeClient([{"track": make_track("a")}])
    assert [name for _, _, name in get_tracks(client, CONFIG)] == ["a"]


def test_unplayable_entries_are_skipped():
    client = FakeClient(
        [
            {"item": None},  # removed track
            {"item": {"name": "no-duration", "uri": "spotify:track:y", "duration_ms": None}},
            {"item": {**make_track("local"), "is_local": True}},
            {"item": make_track("keep")},
        ]
    )
    assert [name for _, _, name in get_tracks(client, CONFIG)] == ["keep"]


def test_load_playlist_returns_the_playlist_id():
    client = FakeClient([{"item": make_track("a")}])
    assert load_playlist(client, CONFIG)[0] == "pl-1"


def test_context_uri_takes_uris_and_links_but_not_names():
    from spotiafk.spotify import parse_context_uri

    dj = "spotify:playlist:37i9dQZF1EYkqdzj48dyYq"
    assert parse_context_uri(dj) == dj
    assert parse_context_uri("https://open.spotify.com/playlist/37i9dQZF1EYkqdzj48dyYq?si=abc") == dj
    assert parse_context_uri("https://open.spotify.com/intl-nl/album/1a2B3c") == "spotify:album:1a2B3c"
    assert parse_context_uri("004") is None


def test_read_retry_after_seconds():
    import spotipy

    from spotiafk.spotify import read_retry_after_seconds

    def error(headers):
        return spotipy.SpotifyException(http_status=429, code=-1, msg="slow down", headers=headers)

    assert read_retry_after_seconds(error({"Retry-After": "42"})) == 42
    assert read_retry_after_seconds(error(None)) == 0
    assert read_retry_after_seconds(error({"Retry-After": "soon"})) == 0


def test_requests_never_sleep_out_a_rate_limit_themselves():
    from spotiafk.spotify import _build_session

    retry = _build_session().get_adapter("https://api.spotify.com").max_retries
    assert retry.respect_retry_after_header is False
    assert 429 not in retry.status_forcelist
    assert 503 in retry.status_forcelist
