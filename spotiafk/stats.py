"""play history, one appended json line per track so a crash never loses the totals"""

import dataclasses
import datetime
import json
import os

from spotiafk.config import BASE_DIR
from spotiafk.units import MINUTES_PER_HOUR, SECONDS_PER_MINUTE

EVENTS_FILE = "events.jsonl"
# milliseconds are plenty
SECONDS_DECIMALS = 3
# spotiafk 2 wrote one letter keys
OLD_EVENT_KEYS = {"t": "time", "s": "seconds"}


def _get_events_path(state_dir: str) -> str:
    return os.path.join(state_dir, EVENTS_FILE)


def import_legacy(state_dir: str) -> None:
    # spotiafk 1 kept one total in time.txt, it comes along once
    if os.path.isfile(_get_events_path(state_dir)):
        return
    legacy_path = os.path.join(BASE_DIR, "time.txt")
    try:
        with open(legacy_path, encoding="utf-8") as file:
            total = float(file.readline() or 0.0)
    except (OSError, ValueError):
        return
    if total > 0:
        add(state_dir, total, track=None, is_legacy=True)


def _upgrade_line(line: str) -> str:
    try:
        event = json.loads(line)
    except ValueError:
        return line  # a torn line stays as it is, summarize skips it
    if not isinstance(event, dict) or not OLD_EVENT_KEYS.keys() & event.keys():
        return line
    upgraded = {OLD_EVENT_KEYS.get(key, key): value for key, value in event.items()}
    return json.dumps(upgraded, ensure_ascii=False) + "\n"


def upgrade_events(state_dir: str) -> None:
    path = _get_events_path(state_dir)
    try:
        with open(path, encoding="utf-8") as file:
            lines = file.readlines()
    except OSError:
        return
    upgraded = [_upgrade_line(line) for line in lines]
    if upgraded == lines:
        return
    # a crash halfway leaves the old file whole
    temporary_path = path + ".tmp"
    with open(temporary_path, "w", encoding="utf-8") as file:
        file.writelines(upgraded)
    os.replace(temporary_path, path)


def add(state_dir: str, seconds: float, track: str | None = None, is_legacy: bool = False) -> None:
    if seconds <= 0:
        return
    os.makedirs(state_dir, exist_ok=True)
    event = {
        "time": datetime.datetime.now().isoformat(timespec="seconds"),
        "seconds": round(seconds, SECONDS_DECIMALS),
    }
    if track:
        event["track"] = track
    if is_legacy:
        event["legacy"] = True
    with open(_get_events_path(state_dir), "a", encoding="utf-8") as file:
        file.write(json.dumps(event, ensure_ascii=False) + "\n")


@dataclasses.dataclass(frozen=True)
class Stats:
    total_seconds: float
    tracks: int  # the legacy baseline has no track, so it doesnt count
    by_day: dict  # iso date to seconds, inside the asked window

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def summarize(state_dir: str, since: datetime.datetime | None = None) -> Stats:
    total = 0.0
    tracks = 0
    by_day: dict = {}
    try:
        with open(_get_events_path(state_dir), encoding="utf-8") as file:
            lines = file.readlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            event = json.loads(line)
            when = datetime.datetime.fromisoformat(event["time"])
            seconds = float(event["seconds"])
        except (ValueError, KeyError, TypeError):
            continue  # a torn or corrupt line
        if since and when < since:
            continue
        total += seconds
        if "track" in event:
            tracks += 1
        if not event.get("legacy"):
            day = when.date().isoformat()
            by_day[day] = by_day.get(day, 0.0) + seconds
    return Stats(total_seconds=total, tracks=tracks, by_day=by_day)


def format_duration(seconds: float) -> str:
    minutes, _ = divmod(int(seconds), SECONDS_PER_MINUTE)
    hours, minutes = divmod(minutes, MINUTES_PER_HOUR)
    return f"{hours}h {minutes:02d}m"
