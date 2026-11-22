import argparse
import json

import pytest

from spotiafk import statusfile
from spotiafk.cli import ExitCode, _parse_since, main


def write_config(tmp_path):
    path = tmp_path / "spotiafk.toml"
    path.write_text(
        'playlist = "AFK"\nplay_on = ["pi"]\n'
        f"state_dir = {json.dumps(str(tmp_path / 'state'))}\n"
        '[spotify]\nclient_id = "id"\nclient_secret = "sec"\n'
    )
    return str(path)


def test_parse_since_accepts_days_hours_minutes():
    assert _parse_since("7d") < _parse_since("24h") < _parse_since("30m")


def test_parse_since_rejects_garbage():
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_since("soon")


def test_status_not_running_exits_5(tmp_path, capsys):
    code = main(["--config", write_config(tmp_path), "status"])
    assert code == ExitCode.NOT_RUNNING
    assert "Not running" in capsys.readouterr().out


def test_status_live_json(tmp_path, capsys):
    config = write_config(tmp_path)
    statusfile.write(str(tmp_path / "state"), "watching", checks="1/5")
    code = main(["--config", config, "status", "--json"])
    assert code == ExitCode.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["live"] is True
    assert payload["checks"] == "1/5"


def test_stats_json(tmp_path, capsys):
    from spotiafk import stats

    config = write_config(tmp_path)
    stats.add(str(tmp_path / "state"), 90, track="a")
    code = main(["--config", config, "stats", "--json"])
    assert code == ExitCode.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_seconds"] == 90
    assert payload["tracks"] == 1


def test_missing_config_exits_3(tmp_path, capsys):
    code = main(["--config", str(tmp_path / "absent.toml"), "status"])
    assert code == ExitCode.CONFIG
    assert "spotiafk setup" in capsys.readouterr().err


def test_status_text_has_no_long_dash(tmp_path, capsys):
    config = write_config(tmp_path)
    statusfile.write(str(tmp_path / "state"), "watching", checks="1/60")
    main(["--config", config, "status"])
    assert capsys.readouterr().out.startswith("Running: watching, idle checks: 1/60")


def test_a_broken_config_leaves_no_log_behind(tmp_path, capsys):
    path = tmp_path / "spotiafk.toml"
    path.write_text(f"state_dir = {json.dumps(str(tmp_path / 'state'))}\n")
    assert main(["--config", str(path), "run"]) == ExitCode.CONFIG
    assert not (tmp_path / "state" / "logs").exists()


class FakeLibrary:
    # your playlists: one you made, one you follow, and the 403 spotify gives for the followed one
    def __init__(self):
        self.read = []

    def current_user(self):
        return {"id": "you"}

    def current_user_playlists(self):
        return {
            "items": [
                {"name": "mine", "id": "pl-mine", "uri": "spotify:playlist:pl-mine", "owner": {"id": "you"}},
                {"name": "lofi", "id": "pl-lofi", "uri": "spotify:playlist:pl-lofi", "owner": {"id": "them"}},
            ],
            "next": None,
        }

    def playlist_items(self, playlist_id, limit=None):
        import spotipy

        self.read.append(playlist_id)
        if playlist_id == "pl-lofi":
            raise spotipy.SpotifyException(http_status=403, code=-1, msg="Forbidden")
        return {"items": [], "next": None}


@pytest.mark.parametrize(
    ("playlist", "works", "said"),
    [
        ("spotify:playlist:37i9dQZF1EYkqdzj48dyYq", True, "plays Spotify DJ"),
        ("https://open.spotify.com/album/abc?si=1", True, "plays spotify:album:abc as a whole"),
        ("mine", True, "playlist 'mine' found"),
        ("lofi", False, "only lets this app read your own"),
        ("gone", False, "not found"),
    ],
)
def test_doctor_checks_the_playlist(capsys, playlist, works, said):
    from spotiafk.cli import _check_playlist

    assert _check_playlist(FakeLibrary(), playlist) is works
    assert said in capsys.readouterr().out


@pytest.mark.parametrize(
    ("answer", "chosen"),
    [("1", "spotify:playlist:37i9dQZF1EYkqdzj48dyYq"), ("2", "mine"), ("3", "spotify:playlist:pl-lofi")],
)
def test_setup_offers_dj_and_saves_followed_playlists_as_links(monkeypatch, capsys, answer, chosen):
    from spotiafk.cli import _pick_what_to_play

    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    assert _pick_what_to_play(FakeLibrary()) == chosen
    assert "lofi (followed)" in capsys.readouterr().out
