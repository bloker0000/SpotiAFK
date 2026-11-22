import datetime

from spotiafk import stats


def test_add_and_summary(tmp_path):
    state = str(tmp_path)
    stats.add(state, 30, track="a")
    stats.add(state, 40, track="b")
    result = stats.summarize(state)
    assert result.total_seconds == 70
    assert result.tracks == 2
    assert sum(result.by_day.values()) == 70


def test_zero_and_negative_seconds_are_ignored(tmp_path):
    stats.add(str(tmp_path), 0)
    stats.add(str(tmp_path), -5)
    assert stats.summarize(str(tmp_path)).total_seconds == 0


def test_corrupt_lines_are_skipped(tmp_path):
    stats.add(str(tmp_path), 10, track="a")
    with open(tmp_path / stats.EVENTS_FILE, "a") as file:
        file.write("not json\n")
    assert stats.summarize(str(tmp_path)).total_seconds == 10


def test_since_filters_out_old_events(tmp_path):
    stats.add(str(tmp_path), 10, track="a")
    future = datetime.datetime.now() + datetime.timedelta(hours=1)
    assert stats.summarize(str(tmp_path), since=future).total_seconds == 0


def test_legacy_time_txt_is_imported_once(tmp_path, monkeypatch):
    legacy_dir = tmp_path / "repo"
    legacy_dir.mkdir()
    (legacy_dir / "time.txt").write_text("120.5")
    monkeypatch.setattr(stats, "BASE_DIR", str(legacy_dir))
    state = str(tmp_path / "state")
    stats.import_legacy(state)
    stats.import_legacy(state)  # second call must not double the baseline
    result = stats.summarize(state)
    assert result.total_seconds == 120.5
    assert result.tracks == 0
    assert result.by_day == {}  # baseline is excluded from daily breakdown


def test_format_duration():
    assert stats.format_duration(3725) == "1h 02m"


OLD_EVENTS = (
    '{"t": "2026-11-20T10:00:00", "s": 61.5, "track": "a"}\n'
    "torn {\n"
    '{"t": "2026-11-21T09:00:00", "s": 120.5, "legacy": true}\n'
    '{"time": "2026-11-22T08:00:00", "seconds": 3.0, "track": "ü"}\n'
)
NEW_EVENTS = (
    '{"time": "2026-11-20T10:00:00", "seconds": 61.5, "track": "a"}\n'
    "torn {\n"
    '{"time": "2026-11-21T09:00:00", "seconds": 120.5, "legacy": true}\n'
    '{"time": "2026-11-22T08:00:00", "seconds": 3.0, "track": "ü"}\n'
)


def test_old_one_letter_keys_are_upgraded_in_place(tmp_path):
    (tmp_path / stats.EVENTS_FILE).write_text(OLD_EVENTS, encoding="utf-8")
    stats.upgrade_events(str(tmp_path))
    assert (tmp_path / stats.EVENTS_FILE).read_text(encoding="utf-8") == NEW_EVENTS
    result = stats.summarize(str(tmp_path))
    assert result.total_seconds == 185.0
    assert result.tracks == 2


def test_upgrading_twice_changes_nothing(tmp_path):
    (tmp_path / stats.EVENTS_FILE).write_text(NEW_EVENTS, encoding="utf-8")
    before = (tmp_path / stats.EVENTS_FILE).stat().st_mtime_ns
    stats.upgrade_events(str(tmp_path))
    assert (tmp_path / stats.EVENTS_FILE).stat().st_mtime_ns == before


def test_upgrading_without_history_is_fine(tmp_path):
    stats.upgrade_events(str(tmp_path))
    assert not (tmp_path / stats.EVENTS_FILE).exists()
