import argparse
import datetime
import enum
import getpass
import json
import os
import re
import sys

import spotipy

from spotiafk import __version__
from spotiafk import stats as stats_module
from spotiafk.client import Client, setup_logging
from spotiafk.config import DEFAULT_REDIRECT_URI, PRIVATE_FILE_MODE, Config, ConfigError, get_default_config_path
from spotiafk.spotify import (
    DJ_URI,
    can_read_playlist,
    find_playlist_id,
    list_playlists,
    make_client,
    parse_context_uri,
)

SETUP_PLAYLIST_LIMIT = 50
STATS_DAYS_SHOWN = 7


class ExitCode(enum.IntEnum):
    OK = 0
    ERROR = 1
    USAGE = 2  # argparse uses this one itself
    CONFIG = 3
    AUTH = 4
    NOT_RUNNING = 5


def _parse_since(text: str) -> datetime.datetime:
    match = re.fullmatch(r"(\d+)([dhm])", text)
    if not match:
        raise argparse.ArgumentTypeError("use a number plus d/h/m, e.g. 7d, 24h, 30m")
    amount, unit = int(match[1]), match[2]
    delta = {"d": "days", "h": "hours", "m": "minutes"}[unit]
    return datetime.datetime.now() - datetime.timedelta(**{delta: amount})


def _load_config(arguments) -> Config:
    return Config.load(arguments.config)


def run_farm(arguments) -> int:
    config = _load_config(arguments)
    # before logging, or every failed restart leaves a log
    config.require_valid()
    setup_logging(config)
    Client(config).run()
    return ExitCode.OK


def print_status(arguments) -> int:
    status = Client(_load_config(arguments)).read_status()
    if arguments.json:
        print(json.dumps(status))
    elif not status["live"]:
        print("Not running.")
        if status.get("state") not in (None, "not running", "stopped"):
            print(f"(last seen {status.get('age_seconds')}s ago in state '{status['state']}')")
    else:
        line = f"Running: {status['state']}"
        if status.get("track"):
            line += f", track: {status['track']}"
        if status.get("checks"):
            line += f", idle checks: {status['checks']}"
        print(line + f" (heartbeat {status['age_seconds']}s ago, pid {status.get('pid')})")
    return ExitCode.OK if status["live"] else ExitCode.NOT_RUNNING


def print_stats(arguments) -> int:
    result = Client(_load_config(arguments)).read_stats(since=arguments.since)
    if arguments.json:
        print(json.dumps(result.as_dict()))
        return ExitCode.OK
    print(f"Total playtime: {stats_module.format_duration(result.total_seconds)} ({int(result.total_seconds)}s)")
    if result.tracks:
        print(f"Tracks credited: {result.tracks}")
    for day in sorted(result.by_day)[-STATS_DAYS_SHOWN:]:
        print(f"  {day}  {stats_module.format_duration(result.by_day[day])}")
    return ExitCode.OK


def report_pass(message: str) -> None:
    print(f"  ✓ {message}")


def report_problem(message: str, fix: str) -> None:
    print(f"  ✗ {message}\n    fix: {fix}")


def _check_playlist(client: spotipy.Spotify, playlist: str) -> bool:
    uri = parse_context_uri(playlist)
    if uri == DJ_URI:
        report_pass("plays Spotify DJ")
        return True
    if uri:
        report_pass(f"plays {uri} as a whole, like starting it in the app")
        return True
    playlist_id = find_playlist_id(client, playlist)
    if playlist_id is None:
        report_problem(f"playlist {playlist!r} not found", "check the name, or put its link in the config instead")
        return False
    if not can_read_playlist(client, playlist_id):
        report_problem(
            f"playlist {playlist!r} is someone else's, and Spotify only lets this app read your own",
            "put its link in the config instead (Share, Copy link to playlist), then it plays as a whole",
        )
        return False
    report_pass(f"playlist {playlist!r} found")
    return True


def run_doctor(arguments) -> int:
    try:
        config = _load_config(arguments)
    except ConfigError as error:
        report_problem("no usable configuration", str(error))
        return ExitCode.CONFIG
    report_pass(f"config loaded from {config.source}")

    problems = config.find_issues()
    for problem in problems:
        report_problem(problem, "edit the config or re-run 'spotiafk setup'")
    if problems:
        return ExitCode.CONFIG

    try:
        client = make_client(config)
    except Exception as error:
        report_problem(f"Spotify auth failed: {error}", "check client_id/client_secret and redirect_uri")
        return ExitCode.AUTH
    user = client.current_user()
    report_pass(f"authenticated as {user.get('display_name') or user['id']}")
    # the playback endpoints refuse anything but premium
    if user.get("product", "premium") != "premium":
        report_problem(f"account is on Spotify {user['product']}", "remote playback needs Premium")
        return ExitCode.ERROR

    if not _check_playlist(client, config.playlist):
        return ExitCode.ERROR

    online = [device["name"] for device in client.devices()["devices"]]
    matched = [name for name in config.play_on if name in online]
    if matched:
        report_pass(f"devices online: {', '.join(matched)}")
    else:
        report_problem(
            f"none of {list(config.play_on)} are online (online now: {online or 'none'})",
            "open Spotify on the device, or fix play_on in the config",
        )
        return ExitCode.ERROR
    report_pass("all checks passed")
    return ExitCode.OK


def _pick(prompt: str, names: list) -> str:
    for index, name in enumerate(names, 1):
        print(f"  {index}. {name}")
    while True:
        answer = input(f"{prompt} [number or name]: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(names):
            return names[int(answer) - 1]
        if answer in names:
            return answer
        print("Not one of the options, try again.")


def _pick_what_to_play(client: spotipy.Spotify) -> str:
    choices = {"Spotify DJ": DJ_URI}
    for name, uri, is_yours in list_playlists(client, SETUP_PLAYLIST_LIMIT):
        # spotify only lets this app read your own playlists
        label = name if is_yours else f"{name} (followed)"
        choices.setdefault(label, name if is_yours else uri)
    return choices[_pick("Play", list(choices))]


def run_setup(arguments) -> int:
    print("SpotiAFK setup, takes about 3 minutes.\n")
    print("1/4 Spotify app credentials")
    print("    Create an app at https://developer.spotify.com/dashboard (tick Web API),")
    print(f"    add this redirect URI in its settings: {DEFAULT_REDIRECT_URI}")
    client_id = input("    Client ID: ").strip()
    client_secret = getpass.getpass("    Client secret (hidden): ").strip()

    config = Config(client_id=client_id, client_secret=client_secret)
    print("\n2/4 Logging in to Spotify (a browser window opens, approve it there)...")
    try:
        client = make_client(config)
    except Exception as error:
        print(f"Authorization failed: {error}", file=sys.stderr)
        return ExitCode.AUTH
    user = client.current_user()
    print(f"    Hi {user.get('display_name') or user['id']}!")

    print("\n3/4 What should SpotiAFK play?")
    playlist = _pick_what_to_play(client)

    print("\n4/4 Play on which device? (must be online now to appear)")
    devices = [device["name"] for device in client.devices()["devices"]]
    play_on = [_pick("Device", devices)] if devices else []
    if not devices:
        print("    No devices online. On Windows, windows\\start.ps1 runs a hidden web player for this.")
        print("    Set play_on in the config once it shows up.")

    path = arguments.config or get_default_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        file.write(
            f"playlist = {json.dumps(playlist)}\n"
            f"play_on = {json.dumps(play_on)}\n\n"
            f"[spotify]\n"
            f"client_id = {json.dumps(client_id)}\n"
            f"client_secret = {json.dumps(client_secret)}\n"
        )
    os.chmod(path, PRIVATE_FILE_MODE)
    print(f"\nWrote {path} (kept private with chmod 600).")
    print("All set! Start with: spotiafk run")
    return ExitCode.OK


def main(command_line=None) -> int:
    parser = argparse.ArgumentParser(
        prog="spotiafk",
        description="Plays Spotify on a spare device while you're away, and hands it back when you're not.",
    )
    parser.add_argument("--version", action="version", version=f"spotiafk {__version__}")
    parser.add_argument("--config", help="path to spotiafk.toml (default: standard locations)")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("setup", help="guided first-time setup").set_defaults(handler=run_setup)
    commands.add_parser("run", help="start it (blocks, Ctrl-C stops)").set_defaults(handler=run_farm)

    status_parser = commands.add_parser("status", help="is it running, and what is it doing")
    status_parser.add_argument("--json", action="store_true")
    status_parser.set_defaults(handler=print_status)

    stats_parser = commands.add_parser("stats", help="total playtime and per-day breakdown")
    stats_parser.add_argument("--since", type=_parse_since, help="window like 7d, 24h, 30m")
    stats_parser.add_argument("--json", action="store_true")
    stats_parser.set_defaults(handler=print_stats)

    commands.add_parser("doctor", help="check config, auth, playlist, and devices").set_defaults(handler=run_doctor)

    # windows pipes are cp1252, which cant print ✓
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    arguments = parser.parse_args(command_line)
    try:
        return arguments.handler(arguments)
    except ConfigError as error:
        print(f"spotiafk: {error}", file=sys.stderr)
        return ExitCode.CONFIG
    except spotipy.oauth2.SpotifyOauthError as error:
        print(f"spotiafk: authentication failed: {error}", file=sys.stderr)
        return ExitCode.AUTH
    except KeyboardInterrupt:
        return ExitCode.OK


if __name__ == "__main__":
    sys.exit(main())
