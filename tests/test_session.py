import dataclasses
import time

import pytest
import spotipy

from spotiafk import session as session_module
from spotiafk import stats
from spotiafk.config import Config
from spotiafk.session import MOVED, PAUSED, STOPPED, SWITCHED, Session, Takeover

FARM = {"id": "afk-id", "name": "AFK"}
PHONE = {"id": "phone-id", "name": "Phone"}
DESKTOP = {"id": "desktop-id", "name": "Desktop"}
PLAYLIST_ID = "farm-pl"
FARM_CONTEXT = f"spotify:playlist:{PLAYLIST_ID}"
YOUR_ALBUM = "spotify:album:yours"
YOUR_TRACK = "spotify:track:yours"


def make_playback(device, context=FARM_CONTEXT, item="spotify:track:farm", is_playing=True):
    return {
        "device": dict(device),
        "context": {"uri": context} if context else None,
        "item": {"uri": item, "name": item.rsplit(":", 1)[-1]},
        "is_playing": is_playing,
        "progress_ms": 61_000,
    }


class FakeSpotify:
    # one account: the devices online and what it plays

    def __init__(self, devices=(FARM, PHONE)):
        self.online = [dict(d) for d in devices]
        self.current = None
        self.queued = []  # playbacks served by the next polls, before self.current
        self.calls = []
        self.refuses_offset = False
        self.follows_starts = True  # a start becomes what's playing
        self.fresh_contexts = set()  # contexts that ignore the offset, like DJ

    def devices(self):
        return {"devices": self.online}

    def current_playback(self, additional_types=None):
        return self.queued.pop(0) if self.queued else self.current

    def start_playback(self, device_id=None, context_uri=None, uris=None, offset=None, position_ms=None):
        if device_id not in {d["id"] for d in self.online}:
            raise spotipy.SpotifyException(http_status=404, code=-1, msg="Device not found")
        if offset and self.refuses_offset:
            raise spotipy.SpotifyException(http_status=400, code=-1, msg="Can't offset")
        self.calls.append(("start", device_id, context_uri, uris, offset, position_ms))
        if self.follows_starts:
            asked_uri = (offset or {}).get("uri") or (uris or [None])[0]
            starts_fresh = context_uri in self.fresh_contexts or asked_uri is None
            device = next(d for d in self.online if d["id"] == device_id)
            item = "spotify:track:fresh" if starts_fresh else asked_uri
            self.current = make_playback(device, context=context_uri, item=item)

    def pause_playback(self, device_id=None):
        self.calls.append(("pause", device_id))
        if self.current:
            self.current = {**self.current, "is_playing": False}

    def transfer_playback(self, device_id, force_play=True):
        self.calls.append(("transfer", device_id, force_play))


@pytest.fixture
def spotify():
    return FakeSpotify()


@pytest.fixture
def session(monkeypatch, tmp_path, spotify):
    monkeypatch.setattr(session_module, "PLAYING_CHECK_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(session_module, "PAUSE_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(session_module, "LANDING_CHECK_DELAY_SECONDS", 0)
    monkeypatch.setattr(session_module, "LOAD_GRACE_SECONDS", 0)
    monkeypatch.setattr(session_module.platform, "node", lambda: "test-host")
    config = Config(
        playlist="AFK list",
        play_on=("AFK",),
        client_id="id",
        client_secret="sec",
        check_interval_seconds=0.001,
        idle_minutes=0.0001,  # six checks of a millisecond
        skip_after_seconds=None,
        state_dir=str(tmp_path),
    )
    session = Session(spotify, config)
    session.context_uri = FARM_CONTEXT
    return session


def wait_for(session, seconds, name="song"):
    return session.wait(name, deadline=time.time() + seconds)


def test_tick_plays_after_enough_free_checks(monkeypatch, session):
    rounds = []
    monkeypatch.setattr(Session, "play", lambda self: rounds.append(1))
    for _ in range(session.config.idle_checks):
        session.tick()
    assert rounds == [1]


def test_tick_resets_when_you_play_anywhere(session, spotify):
    session.free_checks = 3
    spotify.current = make_playback(FARM, context=YOUR_ALBUM)  # even on the farm device
    session.tick()
    assert session.free_checks == 0


def test_tick_counts_the_farms_leftover_as_free(session, spotify):
    spotify.current = make_playback(FARM)
    session.tick()
    assert session.free_checks == 1


def test_tick_remembers_your_playback_not_the_farms(session, spotify):
    yours = make_playback(PHONE, context=YOUR_ALBUM, item=YOUR_TRACK, is_playing=False)
    spotify.current = yours
    session.tick()
    spotify.current = make_playback(FARM)
    session.tick()
    assert session.your_playback == yours


@pytest.mark.parametrize(
    ("current", "reason"),
    [
        (None, STOPPED),
        (make_playback(PHONE), MOVED),
        (make_playback(FARM, context=YOUR_ALBUM), SWITCHED),
        (make_playback(FARM, context=None), SWITCHED),
        (make_playback(FARM, is_playing=False), PAUSED),
        (make_playback(FARM), None),
    ],
)
def test_takeover_reason(session, current, reason):
    assert session.find_takeover_reason(current) == reason


def test_wait_credits_playtime(session, spotify):
    spotify.current = make_playback(FARM)
    assert wait_for(session, 0.03) is None
    result = stats.summarize(session.config.state_dir)
    assert result.total_seconds > 0
    assert result.tracks == 1


def test_one_paused_poll_is_not_a_takeover(session, spotify):
    spotify.queued = [make_playback(FARM, is_playing=False)]
    spotify.current = make_playback(FARM)
    assert wait_for(session, 0.05) is None


def test_two_paused_polls_are_a_takeover(session, spotify):
    spotify.current = make_playback(FARM, is_playing=False)
    assert wait_for(session, 5).reason == PAUSED


def test_pause_at_the_very_end_still_gets_its_second_look(session, spotify):
    spotify.current = make_playback(FARM, is_playing=False)
    assert wait_for(session, 0.005).reason == PAUSED


def test_paused_takeover_puts_your_track_back_paused(session, spotify):
    session.your_playback = make_playback(PHONE, context=YOUR_ALBUM, item=YOUR_TRACK)
    session.hand_back(Takeover(PAUSED, make_playback(FARM, is_playing=False)))
    assert spotify.calls == [
        ("start", "phone-id", YOUR_ALBUM, None, {"uri": YOUR_TRACK}, 61_000),
        ("pause", "phone-id"),
    ]


def test_restore_drops_the_context_when_it_refuses_an_offset(session, spotify):
    spotify.refuses_offset = True
    session.your_playback = make_playback(PHONE, context="spotify:artist:x", item=YOUR_TRACK)
    session.restore(PHONE, play=True)
    assert spotify.calls == [("start", "phone-id", None, [YOUR_TRACK], None, 61_000)]


def test_switched_takeover_moves_your_pick_to_your_device(session, spotify):
    session.your_playback = make_playback(PHONE, context=YOUR_ALBUM, item=YOUR_TRACK)
    session.hand_back(Takeover(SWITCHED, make_playback(FARM, context=YOUR_ALBUM)))
    assert spotify.calls == [("transfer", "phone-id", True)]


def test_moved_takeover_puts_your_track_on_the_device_you_moved_to(session, spotify):
    session.your_playback = make_playback(PHONE, context=YOUR_ALBUM, item=YOUR_TRACK)
    session.hand_back(Takeover(MOVED, make_playback(PHONE)))
    assert spotify.calls == [
        ("start", "phone-id", YOUR_ALBUM, None, {"uri": YOUR_TRACK}, 61_000),
    ]


def test_without_your_playback_a_pause_just_moves_the_session(session, spotify):
    session.hand_back(Takeover(PAUSED, make_playback(FARM, is_playing=False)))
    assert spotify.calls == [("transfer", "phone-id", False)]


def test_hand_back_leaves_it_when_your_device_is_unclear(session, spotify):
    spotify.online.append(dict(DESKTOP))
    session.hand_back(Takeover(PAUSED, make_playback(FARM, is_playing=False)))
    assert spotify.calls == []


def test_hand_back_prefers_the_device_you_were_on(session, spotify):
    spotify.online.append(dict(DESKTOP))
    session.your_playback = make_playback(DESKTOP, context=YOUR_ALBUM, item=YOUR_TRACK)
    session.hand_back(Takeover(SWITCHED, make_playback(FARM, context=YOUR_ALBUM)))
    assert spotify.calls == [("transfer", "desktop-id", True)]


def test_hand_back_off_or_stopped_does_nothing(session, spotify):
    session.hand_back(Takeover(STOPPED, None))
    session.config = dataclasses.replace(session.config, hand_back=False)
    session.hand_back(Takeover(PAUSED, make_playback(FARM, is_playing=False)))
    assert spotify.calls == []


def test_start_falls_back_to_the_next_device(session, spotify):
    session.start(["gone-id", "afk-id"])
    assert spotify.calls[0][:2] == ("start", "afk-id")


def test_start_raises_when_no_device_accepts(session):
    with pytest.raises(RuntimeError):
        session.start(["gone-id"])


def test_play_waits_when_no_farm_device_is_online(session, spotify):
    spotify.online = [dict(PHONE)]
    session.play()
    assert spotify.calls == []


def test_stop_pauses_only_the_farm(session, spotify):
    spotify.current = make_playback(PHONE, context=YOUR_ALBUM)
    session.stop()
    spotify.current = make_playback(FARM)
    session.stop()
    assert spotify.calls == [("pause", "afk-id")]


def test_old_style_playlist_uri_still_counts_as_the_farm(session):
    current = make_playback(FARM, context=f"spotify:user:someone:playlist:{PLAYLIST_ID}")
    assert session.find_takeover_reason(current) is None


def test_a_uri_in_the_config_plays_as_a_whole_context(monkeypatch, session, spotify):
    dj = "spotify:playlist:37i9dQZF1EYkqdzj48dyYq"
    session.config = dataclasses.replace(session.config, playlist=dj)
    spotify.follows_starts = False
    spotify.queued = [make_playback(FARM, context=dj), make_playback(FARM, context=dj)]
    spotify.current = make_playback(PHONE)
    session.your_playback = make_playback(PHONE, context=YOUR_ALBUM, item=YOUR_TRACK)
    session.play()
    assert spotify.calls[0] == ("start", "afk-id", dj, None, None, None)
    assert session.free_checks == 0


def test_without_a_deadline_each_track_is_credited_by_name(session, spotify):
    spotify.queued = [
        make_playback(FARM, item="spotify:track:one"),
        make_playback(FARM, item="spotify:track:one"),
        make_playback(FARM, item="spotify:track:two"),
    ]
    spotify.current = make_playback(PHONE)
    assert session.wait(None).reason == MOVED
    result = stats.summarize(session.config.state_dir)
    assert result.tracks == 2  # "one" when "two" started, "two" when you took over


def test_hand_back_prefers_a_device_that_showed_up_while_the_farm_played(session, spotify):
    session.device_ids_before = {"afk-id", "desktop-id"}
    spotify.online.append(dict(DESKTOP))
    session.your_playback = make_playback(DESKTOP, context=YOUR_ALBUM, item=YOUR_TRACK)
    session.hand_back(Takeover(SWITCHED, make_playback(FARM, context=YOUR_ALBUM)))
    assert spotify.calls == [("transfer", "phone-id", True)]


def test_restore_puts_back_just_the_track_when_the_context_starts_fresh(session, spotify):
    dj = "spotify:playlist:37i9dQZF1EYkqdzj48dyYq"
    spotify.fresh_contexts = {dj}
    session.your_playback = make_playback(PHONE, context=dj, item=YOUR_TRACK)
    session.restore(PHONE, play=False)
    assert spotify.calls == [
        ("start", "phone-id", dj, None, {"uri": YOUR_TRACK}, 61_000),
        ("pause", "phone-id"),
        ("start", "phone-id", None, [YOUR_TRACK], None, 61_000),
        ("pause", "phone-id"),
    ]


def test_the_old_state_right_after_the_farms_start_is_not_a_takeover(monkeypatch, session, spotify):
    monkeypatch.setattr(session_module, "LOAD_GRACE_SECONDS", 10)
    session.started_at = time.time()
    spotify.current = make_playback(PHONE, is_playing=False)  # spotify still shows where you were
    assert wait_for(session, 0.05) is None


def test_the_heartbeat_is_written_once_per_track_not_every_poll(monkeypatch, session, spotify):
    writes = []
    monkeypatch.setattr(session_module.statusfile, "write", lambda *args, **kw: writes.append(kw))
    spotify.current = make_playback(FARM)
    wait_for(session, 0.05)
    assert writes == [{"track": "song"}]


def test_a_restarted_farm_knows_its_leftover_from_a_uri_in_the_config(tmp_path, spotify):
    dj = "spotify:playlist:37i9dQZF1EYkqdzj48dyYq"
    config = Config(playlist=dj, play_on=("AFK",), check_interval_seconds=0.001, state_dir=str(tmp_path))
    spotify.current = make_playback(FARM, context=dj)
    starts_fresh = Session(spotify, config)
    starts_fresh.tick()
    assert starts_fresh.free_checks == 1


def test_what_you_played_survives_a_restart(session, spotify):
    yours = make_playback(PHONE, context=YOUR_ALBUM, item=YOUR_TRACK, is_playing=False)
    spotify.current = yours
    session.tick()
    assert Session(spotify, session.config).your_playback == yours


def test_when_unclear_hand_back_goes_to_this_computers_own_app(monkeypatch, session, spotify):
    monkeypatch.setattr(session_module.platform, "node", lambda: "DESKTOP")
    spotify.online.append(dict(DESKTOP))
    session.device_ids_before = {"afk-id", "phone-id", "desktop-id"}
    session.hand_back(Takeover(PAUSED, make_playback(FARM, is_playing=False)))
    assert spotify.calls == [("transfer", "desktop-id", False)]


def test_hand_back_says_where_playback_went(caplog, session, spotify):
    caplog.set_level("INFO")
    session.hand_back(Takeover(SWITCHED, make_playback(FARM, context=YOUR_ALBUM)))
    session.your_playback = None
    session.hand_back(Takeover(PAUSED, make_playback(FARM, is_playing=False)))
    assert "Moved your pick to Phone" in caplog.text
    assert "Moved playback to Phone" in caplog.text
