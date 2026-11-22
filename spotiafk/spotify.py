import glob
import logging
import os
import random
import re
import shutil
from http import HTTPStatus

import requests
import spotipy
import urllib3
from requests.adapters import HTTPAdapter
from spotipy.oauth2 import SpotifyOAuth

from spotiafk.config import BASE_DIR, PRIVATE_FILE_MODE, SCOPE, Config
from spotiafk.units import MS_PER_SECOND

# without "episode" a playing podcast comes back with no item
PLAYBACK_TYPES = "track,episode"
SPOTIFY_LINK = re.compile(r"open\.spotify\.com/(?:intl-[\w-]+/)?(\w+)/(\w+)")
DJ_URI = "spotify:playlist:37i9dQZF1EYkqdzj48dyYq"
REQUEST_TIMEOUT_SECONDS = 15
REQUEST_RETRIES = 5
RETRIED_STATUSES = (
    HTTPStatus.INTERNAL_SERVER_ERROR,
    HTTPStatus.BAD_GATEWAY,
    HTTPStatus.SERVICE_UNAVAILABLE,
    HTTPStatus.GATEWAY_TIMEOUT,
)

logger = logging.getLogger(__name__)


def parse_context_uri(text: str) -> str | None:
    # None when the config names one of your playlists
    if text.startswith("spotify:"):
        return text
    match = SPOTIFY_LINK.search(text)
    return f"spotify:{match[1]}:{match[2]}" if match else None


def get_token_cache_path(config: Config) -> str:
    return os.path.join(config.state_dir, "token.dat")


def _migrate_legacy_token(config: Config) -> None:
    cache = get_token_cache_path(config)
    if os.path.isfile(cache):
        return
    legacy_tokens = sorted(glob.glob(os.path.join(BASE_DIR, "token-*.dat")))
    if legacy_tokens:
        os.makedirs(config.state_dir, exist_ok=True)
        shutil.copy(legacy_tokens[0], cache)
        logger.info("Migrated legacy token cache into %s", config.state_dir)


def _build_session() -> requests.Session:
    retry = urllib3.Retry(
        total=REQUEST_RETRIES,
        connect=None,
        read=False,
        status=REQUEST_RETRIES,
        allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE"]),
        backoff_factor=1,
        status_forcelist=RETRIED_STATUSES,
        # a 429 can ask for hours, the run loop waits instead
        respect_retry_after_header=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def make_client(config: Config) -> spotipy.Spotify:
    _migrate_legacy_token(config)
    os.makedirs(config.state_dir, exist_ok=True)
    auth_manager = SpotifyOAuth(
        client_id=config.client_id,
        client_secret=config.client_secret,
        redirect_uri=config.redirect_uri,
        scope=SCOPE,
        cache_path=get_token_cache_path(config),
        # with a browser, spotipy catches the login redirect on 127.0.0.1 itself
        open_browser=True,
    )
    client = spotipy.Spotify(
        auth_manager=auth_manager,
        requests_session=_build_session(),
        requests_timeout=REQUEST_TIMEOUT_SECONDS,
    )
    client.current_user()
    if os.path.isfile(get_token_cache_path(config)):
        os.chmod(get_token_cache_path(config), PRIVATE_FILE_MODE)
    logger.info("Authenticated with Spotify")
    return client


def read_retry_after_seconds(error: spotipy.SpotifyException) -> float:
    try:
        return float((error.headers or {}).get("Retry-After", 0))
    except (TypeError, ValueError):
        return 0.0


def fetch_playback(client: spotipy.Spotify) -> dict | None:
    # None when nothing plays anywhere on the account
    return client.current_playback(additional_types=PLAYBACK_TYPES)


def list_playlists(client: spotipy.Spotify, limit: int) -> list:
    # [name, uri, whether you made it]
    user_id = client.current_user()["id"]
    playlists = []
    page = client.current_user_playlists()
    while page and len(playlists) < limit:
        for playlist in page["items"]:
            if playlist:
                owner_id = (playlist.get("owner") or {}).get("id")
                playlists.append([playlist["name"], playlist["uri"], owner_id == user_id])
        page = client.next(page) if page["next"] else None
    return playlists[:limit]


def find_playlist_id(client: spotipy.Spotify, name: str) -> str | None:
    playlists = client.current_user_playlists()
    while playlists:
        for playlist in playlists["items"]:
            if playlist and playlist["name"] == name:
                return playlist["id"]
        playlists = client.next(playlists) if playlists["next"] else None
    return None


def can_read_playlist(client: spotipy.Spotify, playlist_id: str) -> bool:
    # dev mode apps only get your own playlists, the rest 403
    try:
        client.playlist_items(playlist_id, limit=1)
    except spotipy.SpotifyException as error:
        if error.http_status == HTTPStatus.FORBIDDEN:
            return False
        raise
    return True


def load_playlist(client: spotipy.Spotify, config: Config) -> tuple[str, list]:
    # [uri, duration in seconds, name] per playable track
    playlist_id = find_playlist_id(client, config.playlist)
    if playlist_id is None:
        raise RuntimeError(f"Playlist {config.playlist!r} was not found on this account")

    tracks_to_play = []
    page = client.playlist_items(playlist_id)
    while page:
        for entry in page["items"]:
            # the api renamed track to item, old answers still say track
            track = entry.get("item") or entry.get("track")
            if not track or not track.get("uri") or track.get("duration_ms") is None:
                continue  # removed or unavailable
            if track.get("is_local"):
                continue  # local files cant be started remotely
            tracks_to_play.append([track["uri"], track["duration_ms"] / MS_PER_SECOND, track["name"]])
        page = client.next(page) if page["next"] else None

    if config.shuffle:
        random.shuffle(tracks_to_play)
    logger.info("Updated playlist (%d tracks)", len(tracks_to_play))
    return playlist_id, tracks_to_play
