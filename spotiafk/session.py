import dataclasses
import logging
import math
import platform
import time
from http import HTTPStatus

import spotipy

from spotiafk import stats, statusfile
from spotiafk.config import Config
from spotiafk.spotify import fetch_playback, load_playlist, parse_context_uri

# spotify cut the app off after about 25k requests in a day, every 5 seconds stays under that playing all day
PLAYING_CHECK_INTERVAL_SECONDS = 5
# one paused poll can just be the device loading the next track
PAUSED_POLLS_FOR_TAKEOVER = 2
PAUSE_CONFIRM_SECONDS = 1
# spotify can report the old state right after a start
LOAD_GRACE_SECONDS = 5.0
# keep under MIN_STALE_SECONDS in client.py
HEARTBEAT_INTERVAL_SECONDS = 15.0
PAUSE_ATTEMPTS = 10
PAUSE_RETRY_DELAY_SECONDS = 0.3
LANDING_CHECKS = 6
LANDING_CHECK_DELAY_SECONDS = 0.5
# on disk so a restart still remembers you
YOUR_PLAYBACK_FILE = "your_playback.json"

MOVED = "moved"
SWITCHED = "switched"
PAUSED = "paused"
STOPPED = "stopped"  # nothing plays anywhere, the device probably went offline

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Takeover:
    reason: str
    playback: dict | None


def _get_uri_id(uri: str | None) -> str | None:
    # spotify sometimes reports a playlist as spotify:user:<name>:playlist:<id>
    return uri.rsplit(":", 1)[-1] if uri else None


def _trim_playback(current: dict) -> dict:
    context = current.get("context")
    return {
        "device": {key: current["device"].get(key) for key in ("id", "name")},
        "item": {key: current["item"].get(key) for key in ("uri", "name")},
        "context": {"uri": context.get("uri")} if context else None,
        "is_playing": current.get("is_playing"),
        "progress_ms": current.get("progress_ms"),
    }


class Session:
    def __init__(self, client: spotipy.Spotify, config: Config) -> None:
        self.client = client
        self.config = config
        self.free_checks = 0
        # so a restarted farm knows its own leftover playback
        self.context_uri = parse_context_uri(config.playlist)
        self.your_playback = statusfile.load(config.state_dir, YOUR_PLAYBACK_FILE)
        self.device_ids_before = set()
        self.started_at = 0.0

    def is_on_farm_device(self, current: dict) -> bool:
        return (current.get("device") or {}).get("name") in self.config.play_on

    def is_in_farm_context(self, current: dict) -> bool:
        playing_in = _get_uri_id((current.get("context") or {}).get("uri"))
        return self.context_uri is not None and playing_in == _get_uri_id(self.context_uri)

    def is_farm_playback(self, current: dict | None) -> bool:
        return bool(current) and self.is_on_farm_device(current) and self.is_in_farm_context(current)

    def remember(self, current: dict | None) -> None:
        if current and current.get("item") and not self.is_on_farm_device(current):
            self.your_playback = _trim_playback(current)
            statusfile.save(self.config.state_dir, YOUR_PLAYBACK_FILE, self.your_playback)

    def tick(self) -> None:
        time.sleep(self.config.check_interval_seconds)
        current = fetch_playback(self.client)
        if current and current.get("is_playing") and not self.is_farm_playback(current):
            self.free_checks = 0
        else:
            self.free_checks += 1
        self.remember(current)
        logger.info("Idle check %d/%d", self.free_checks, self.config.idle_checks)
        statusfile.write(self.config.state_dir, "watching", checks=f"{self.free_checks}/{self.config.idle_checks}")
        if self.free_checks >= self.config.idle_checks:
            self.play()

    def play(self) -> None:
        devices = self.client.devices()["devices"]
        device_ids = [device["id"] for device in devices if device["name"] in self.config.play_on]
        if not device_ids:
            logger.info("None of %s are online, waiting", list(self.config.play_on))
            statusfile.write(self.config.state_dir, "waiting for device")
            return
        logger.info("Started playing tracks")
        self.device_ids_before = {device["id"] for device in devices}
        uri = parse_context_uri(self.config.playlist)
        takeover = self.play_context(device_ids, uri) if uri else self.play_playlist(device_ids)
        self.free_checks = 0
        self.hand_back(takeover)

    def play_playlist(self, device_ids: list) -> Takeover:
        while True:
            playlist_id, tracks = load_playlist(self.client, self.config)
            self.context_uri = f"spotify:playlist:{playlist_id}"
            if not tracks:
                raise RuntimeError(f"Playlist {self.config.playlist!r} has no playable tracks")
            for uri, duration, name in tracks:
                self.start(device_ids, offset={"uri": uri})
                skip_after_seconds = self.config.skip_after_seconds
                wait_seconds = skip_after_seconds if skip_after_seconds is not None else duration
                takeover = self.wait(name, deadline=time.time() + wait_seconds)
                if takeover:
                    return takeover
                logger.info("Played %s", name)

    def play_context(self, device_ids: list, uri: str) -> Takeover:
        # for what the app cant list, like DJ
        self.context_uri = uri
        self.start(device_ids)
        return self.wait(None)

    def start(self, device_ids: list, offset: dict | None = None) -> None:
        # playing inside the context leaves your queue alone, unlike add_to_queue
        for device_id in device_ids:
            try:
                self.client.start_playback(device_id=device_id, context_uri=self.context_uri, offset=offset)
                self.started_at = time.time()
                return
            except spotipy.SpotifyException as error:
                if error.http_status != HTTPStatus.NOT_FOUND:
                    raise
                logger.info("Device %s is gone, trying the next one", device_id)
        raise RuntimeError("No configured device accepted playback")

    def wait(self, name: str | None, deadline: float | None = None) -> Takeover | None:
        track_started_at = time.time()
        paused_polls = 0
        heartbeat = (None, 0.0)  # track and time of the last status write
        try:
            while True:
                remaining = math.inf if deadline is None else deadline - time.time()
                if remaining <= 0 and not paused_polls:
                    return None
                # a pause gets its second look right away, also when the track is already over
                time.sleep(PAUSE_CONFIRM_SECONDS if paused_polls else min(PLAYING_CHECK_INTERVAL_SECONDS, remaining))
                current = fetch_playback(self.client)
                reason = self.find_takeover_reason(current)
                if time.time() - self.started_at < LOAD_GRACE_SECONDS:
                    reason = None
                paused_polls = paused_polls + 1 if reason == PAUSED else 0
                if reason and (reason != PAUSED or paused_polls >= PAUSED_POLLS_FOR_TAKEOVER):
                    return Takeover(reason, current)
                if deadline is None and self.is_farm_playback(current):
                    now_playing = (current.get("item") or {}).get("name")
                    if now_playing != name:
                        stats.add(self.config.state_dir, time.time() - track_started_at, track=name)
                        name, track_started_at = now_playing, time.time()
                if heartbeat[0] != name or time.time() - heartbeat[1] >= HEARTBEAT_INTERVAL_SECONDS:
                    statusfile.write(self.config.state_dir, "playing", track=name)
                    heartbeat = (name, time.time())
        finally:
            stats.add(self.config.state_dir, time.time() - track_started_at, track=name)

    def find_takeover_reason(self, current: dict | None) -> str | None:
        if current is None:
            return STOPPED
        if not self.is_on_farm_device(current):
            return MOVED
        if not self.is_in_farm_context(current):
            return SWITCHED
        if not current.get("is_playing"):
            return PAUSED
        return None

    def hand_back(self, takeover: Takeover) -> None:
        logger.info("You took over (%s), stopped playing tracks", takeover.reason)
        if takeover.reason == STOPPED or not self.config.hand_back:
            return
        current = takeover.playback
        try:
            if takeover.reason == MOVED:
                # you picked your device already, the farm track just came along
                self.restore(current["device"], play=bool(current.get("is_playing")))
                return
            device = self.guess_your_device()
            if device is None:
                logger.info("Can't tell which device you're on, leaving playback where it is")
            elif takeover.reason == SWITCHED:
                self.client.transfer_playback(device["id"], force_play=bool(current.get("is_playing")))
                logger.info("Moved your pick to %s", device["name"])
            else:
                self.restore(device, play=False)
        except spotipy.SpotifyException as error:
            logger.warning("Couldn't hand playback back: %s", error)

    def guess_your_device(self) -> dict | None:
        devices = self.client.devices()["devices"]
        others = [candidate for candidate in devices if candidate["name"] not in self.config.play_on]
        # opening spotify somewhere makes that device show up
        appeared = [candidate for candidate in others if candidate["id"] not in self.device_ids_before]
        if len(appeared) == 1:
            return appeared[0]
        last = (self.your_playback or {}).get("device") or {}
        for device in others:
            if device["id"] == last.get("id") or device["name"] == last.get("name"):
                return device
        # the desktop app is named after the computer
        for device in others:
            if device["name"].lower() == platform.node().lower():
                return device
        return others[0] if len(others) == 1 else None

    def restore(self, device: dict, play: bool) -> None:
        device_id = device["id"]
        yours = self.your_playback
        if yours is None:
            self.client.transfer_playback(device_id, force_play=play)
            logger.info("Moved playback to %s", device["name"])
            return
        item_uri = yours["item"]["uri"]
        attempts = [{"uris": [item_uri]}]
        your_context_uri = (yours.get("context") or {}).get("uri")
        if your_context_uri:
            # keeps your album going after the track, some refuse offsets
            attempts.insert(0, {"context_uri": your_context_uri, "offset": {"uri": item_uri}})
        for attempt in attempts:
            try:
                self.client.start_playback(device_id=device_id, position_ms=yours.get("progress_ms") or 0, **attempt)
            except spotipy.SpotifyException as error:
                can_fall_back = error.http_status in (HTTPStatus.BAD_REQUEST, HTTPStatus.NOT_FOUND)
                if not can_fall_back or attempt is attempts[-1]:
                    raise
                continue
            if not play:
                self.pause(device_id)
            # some contexts, like DJ, accept the offset and then start fresh anyway
            if attempt is attempts[-1] or self.has_landed_on(item_uri):
                break
            logger.info("Your context started fresh, putting back just the track")
        logger.info("Put %s back on %s where you left it", yours["item"].get("name"), device["name"])

    def has_landed_on(self, item_uri: str) -> bool:
        for _ in range(LANDING_CHECKS):
            time.sleep(LANDING_CHECK_DELAY_SECONDS)
            current = fetch_playback(self.client)
            if ((current or {}).get("item") or {}).get("uri") == item_uri:
                return True
        return False

    def pause(self, device_id: str) -> None:
        # right after a start the device can still refuse a pause
        for attempt in range(PAUSE_ATTEMPTS):
            try:
                self.client.pause_playback(device_id)
                return
            except spotipy.SpotifyException:
                if attempt == PAUSE_ATTEMPTS - 1:
                    raise
                time.sleep(PAUSE_RETRY_DELAY_SECONDS)

    def stop(self) -> None:
        # only the farm's own playback, never yours
        try:
            current = fetch_playback(self.client)
            if current and current.get("is_playing") and self.is_farm_playback(current):
                self.client.pause_playback(current["device"]["id"])
                logger.info("Paused playback on %s", current["device"]["name"])
        except Exception as error:
            logger.warning("Couldn't pause on shutdown: %s", error)
