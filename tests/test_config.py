import pytest

from spotiafk.config import Config, ConfigError


def write(tmp_path, text):
    path = tmp_path / "spotiafk.toml"
    path.write_text(text)
    return path


MINIMAL = """
playlist = "AFK"
play_on = ["pi"]
[spotify]
client_id = "id"
client_secret = "secret"
"""


def test_minimal_toml_with_defaults(tmp_path):
    config = Config.load(write(tmp_path, MINIMAL))
    assert config.playlist == "AFK"
    assert config.play_on == ("pi",)
    assert config.skip_after_seconds is None
    assert config.shuffle is True
    assert config.idle_minutes == 15
    assert config.idle_checks == 30
    assert config.hand_back is True
    assert config.find_issues() == []


def test_skip_after_zero_means_full_tracks(tmp_path):
    config = Config.load(write(tmp_path, MINIMAL + "[playback]\nskip_after = 0\n"))
    assert config.skip_after_seconds is None


def test_skip_after_can_still_be_set(tmp_path):
    config = Config.load(write(tmp_path, MINIMAL + "[playback]\nskip_after = 35\n"))
    assert config.skip_after_seconds == 35.0


def test_hand_back_can_be_turned_off(tmp_path):
    config = Config.load(write(tmp_path, MINIMAL + "[playback]\nhand_back = false\n"))
    assert config.hand_back is False


def test_idle_minutes_turn_into_whole_checks(tmp_path):
    config = Config.load(write(tmp_path, MINIMAL + "[checks]\nidle_minutes = 2.5\ninterval = 20\n"))
    assert config.idle_checks == 8  # 150 seconds over 20 second checks, rounded up


def test_float_noise_doesnt_add_a_check():
    assert Config(idle_minutes=3 * 12.5 / 60, check_interval_seconds=12.5).idle_checks == 3


def test_old_idle_checks_still_work(tmp_path):
    config = Config.load(write(tmp_path, MINIMAL + "[checks]\nidle_checks = 60\n"))
    assert config.idle_minutes == 30
    assert config.idle_checks == 60


def test_idle_minutes_wins_over_old_idle_checks(tmp_path):
    config = Config.load(write(tmp_path, MINIMAL + "[checks]\nidle_checks = 5\nidle_minutes = 10\n"))
    assert config.idle_minutes == 10


def test_missing_config_raises_with_fix_hint(tmp_path):
    with pytest.raises(ConfigError, match="spotiafk setup"):
        Config.load(tmp_path / "nope.toml")


def test_invalid_toml_raises(tmp_path):
    with pytest.raises(ConfigError, match="not valid TOML"):
        Config.load(write(tmp_path, "playlist = ["))


def test_issues_name_each_problem():
    problems = Config().find_issues()
    assert any("client_id" in problem for problem in problems)
    assert any("playlist" in problem for problem in problems)
    assert any("play_on" in problem for problem in problems)


def test_issues_have_no_long_dashes():
    assert not any("—" in problem for problem in Config(skip_after_seconds=1, idle_minutes=0).find_issues())


def test_skip_after_below_stream_threshold_is_an_issue(tmp_path):
    config = Config.load(write(tmp_path, MINIMAL + "[playback]\nskip_after = 20\n"))
    assert any("31" in problem for problem in config.find_issues())


@pytest.mark.parametrize("line", ["idle_minutes = 0", "interval = 0", "retry = -1"])
def test_times_that_cant_work_are_issues(tmp_path, line):
    config = Config.load(write(tmp_path, MINIMAL + f"[checks]\n{line}\n"))
    assert config.find_issues()


def test_legacy_options_py_is_mapped(tmp_path, monkeypatch):
    legacy = tmp_path / "options.py"
    legacy.write_text(
        'CLIENT_ID = "id"\nCLIENT_SECRET = "sec"\nPLAYLIST_NAME = "Old"\n'
        'SERVER_NAMES = ["srv"]\nSKIP_SONGS = False\nCHEAKS_BEFORE_PLAYING = 7\n'
        "NOTIFICATION_ENABLED = False\n"
    )
    monkeypatch.setattr("spotiafk.config.BASE_DIR", str(tmp_path))
    monkeypatch.setattr("spotiafk.config.get_default_config_path", lambda: str(tmp_path / "absent"))
    monkeypatch.chdir(tmp_path)
    config = Config.load()
    assert config.playlist == "Old"
    assert config.play_on == ("srv",)
    assert config.skip_after_seconds is None  # SKIP_SONGS = False
    assert config.idle_checks == 7  # old typo name accepted


def test_legacy_options_py_keeps_its_old_skip_default(tmp_path, monkeypatch):
    (tmp_path / "options.py").write_text('CLIENT_ID = "id"\nCLIENT_SECRET = "sec"\n')
    monkeypatch.setattr("spotiafk.config.BASE_DIR", str(tmp_path))
    monkeypatch.setattr("spotiafk.config.get_default_config_path", lambda: str(tmp_path / "absent"))
    monkeypatch.chdir(tmp_path)
    config = Config.load()
    assert config.skip_after_seconds == 35.0
    assert config.idle_checks == 5
