import datetime
import json
import os
import time

import pytest
import requests
import spotipy

from spotiafk import client as client_module
from spotiafk import statusfile
from spotiafk.client import MAX_RATE_LIMIT_WAIT_SECONDS, Client, _prune_logs
from spotiafk.config import Config
from spotiafk.spotify import get_token_cache_path

DAY_SECONDS = 86_400


@pytest.fixture
def client(tmp_path, monkeypatch):
    slept = []
    monkeypatch.setattr(client_module.time, "sleep", slept.append)
    instance = Client(Config(state_dir=str(tmp_path), retry_delay_seconds=10))
    instance.slept = slept
    return instance


def rate_limited(retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after is not None else None
    return spotipy.SpotifyException(http_status=429, code=-1, msg="slow down", headers=headers)


def test_errors_back_off_doubling(client):
    for attempt in (1, 2, 3):
        client._wait_before_retry(RuntimeError("boom"), attempt)
    assert client.slept == [10, 20, 40]


def test_a_rate_limit_waits_as_long_as_spotify_asks(client):
    client._wait_before_retry(rate_limited("120"), 1)
    assert client.slept == [120]


def test_a_rate_limit_wait_is_capped(client):
    client._wait_before_retry(rate_limited("999999"), 1)
    assert client.slept == [MAX_RATE_LIMIT_WAIT_SECONDS]


def test_a_rate_limit_without_a_wait_backs_off(client):
    client._wait_before_retry(rate_limited(), 2)
    assert client.slept == [20]


def test_the_retry_wait_lands_in_the_status(client):
    client._wait_before_retry(rate_limited("600"), 1)
    status = statusfile.read(client.config.state_dir)
    assert status["state"] == "retrying"
    assert status["retry_in_seconds"] == 600


def write_status(state_dir, seconds_ago, **fields):
    updated = datetime.datetime.now() - datetime.timedelta(seconds=seconds_ago)
    payload = {"state": "retrying", "pid": 1, "updated": updated.isoformat(timespec="seconds"), **fields}
    with open(os.path.join(state_dir, statusfile.STATUS_FILE), "w", encoding="utf-8") as file:
        json.dump(payload, file)


def test_a_long_retry_still_reads_as_running(client):
    write_status(client.config.state_dir, 300, retry_in_seconds=600)
    assert client.read_status()["live"] is True


def test_a_retry_long_past_its_wait_reads_as_stopped(client):
    write_status(client.config.state_dir, 3000, retry_in_seconds=600)
    assert client.read_status()["live"] is False


def test_connect_waits_for_the_network(client, monkeypatch):
    answers = [requests.exceptions.ConnectionError("no network"), "spotify"]

    def make_client(config):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(client_module, "make_client", make_client)
    assert client._connect() == "spotify"
    assert client.slept == [10]


def test_a_revoked_login_is_dropped_so_you_get_asked_again(client, monkeypatch):
    token_path = get_token_cache_path(client.config)
    with open(token_path, "w", encoding="utf-8") as file:
        file.write("{}")
    answers = [spotipy.oauth2.SpotifyOauthError("revoked", error="invalid_grant"), "spotify"]

    def make_client(config):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(client_module, "make_client", make_client)
    assert client._connect() == "spotify"
    assert not os.path.exists(token_path)
    assert statusfile.read(client.config.state_dir)["state"] == "logging in"


def test_other_login_errors_still_stop_it(client, monkeypatch):
    def make_client(config):
        raise spotipy.oauth2.SpotifyOauthError("bad client", error="invalid_client")

    monkeypatch.setattr(client_module, "make_client", make_client)
    with pytest.raises(spotipy.oauth2.SpotifyOauthError):
        client._connect()


def test_logs_older_than_a_month_are_removed(tmp_path):
    old_log, new_log = tmp_path / "old.log", tmp_path / "new.log"
    old_log.write_text("old")
    new_log.write_text("new")
    long_ago = time.time() - 40 * DAY_SECONDS
    os.utime(old_log, (long_ago, long_ago))
    _prune_logs(str(tmp_path))
    assert not old_log.exists()
    assert new_log.exists()
