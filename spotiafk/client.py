import contextlib
import datetime
import glob
import logging
import os
import signal
import sys
import time
from http import HTTPStatus

import requests
import spotipy

from spotiafk import stats, statusfile
from spotiafk.config import MAX_BACKOFF_SECONDS, Config
from spotiafk.session import Session
from spotiafk.spotify import get_token_cache_path, make_client, read_retry_after_seconds
from spotiafk.units import SECONDS_PER_DAY, SECONDS_PER_HOUR, SECONDS_PER_MINUTE

# past this, status calls the run loop dead
MISSED_CHECKS_BEFORE_STALE = 3
MIN_STALE_SECONDS = 90
# spotify sometimes asks for hours, past this it just retries
MAX_RATE_LIMIT_WAIT_SECONDS = 3600
LOG_RETENTION_DAYS = 30

logger = logging.getLogger(__name__)


def _format_wait(seconds: float) -> str:
    if seconds >= SECONDS_PER_HOUR:
        return f"{round(seconds / SECONDS_PER_HOUR)} hours"
    return f"{max(1, round(seconds / SECONDS_PER_MINUTE))} minutes"


def _prune_logs(logs_dir: str) -> None:
    oldest_kept = time.time() - LOG_RETENTION_DAYS * SECONDS_PER_DAY
    for path in glob.glob(os.path.join(logs_dir, "*.log")):
        # windows cant delete a log another run still has open
        with contextlib.suppress(OSError):
            if os.path.getmtime(path) < oldest_kept:
                os.remove(path)


def setup_logging(config: Config) -> None:
    logs_dir = os.path.join(config.state_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    _prune_logs(logs_dir)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_path = os.path.join(logs_dir, f"{stamp}_{os.getpid()}.log")
    handlers = [logging.FileHandler(log_path, encoding="utf-8")]
    if sys.stdout is not None:  # None under pythonw
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%d/%m/%Y %H:%M:%S",
        handlers=handlers,
    )


class Client:
    def __init__(self, config: Config | None = None) -> None:
        self.config = config if config is not None else Config.load()

    def run(self) -> None:
        """blocks until ctrl-c or a stop signal"""
        self.config.require_valid()
        stats.import_legacy(self.config.state_dir)
        stats.upgrade_events(self.config.state_dir)
        logger.info("Started the program")
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        try:
            session = Session(self._connect(), self.config)
            # so status sees it alive before the first check
            statusfile.write(self.config.state_dir, "watching", checks=f"0/{self.config.idle_checks}")
            self._loop(session)
        finally:
            statusfile.write(self.config.state_dir, "stopped")

    def _connect(self) -> spotipy.Spotify:
        attempts = 0
        while True:
            if not os.path.isfile(get_token_cache_path(self.config)):
                # spotipy opens a login tab and waits, the watcher tells you
                statusfile.write(self.config.state_dir, "logging in")
            try:
                return make_client(self.config)
            except spotipy.oauth2.SpotifyOauthError as error:
                if getattr(error, "error", None) != "invalid_grant":
                    raise
                # the saved login is dead for good, so ask for a new one
                logger.warning("Spotify no longer accepts the saved login, asking you to log in again")
                with contextlib.suppress(FileNotFoundError):
                    os.remove(get_token_cache_path(self.config))
            except (requests.exceptions.RequestException, spotipy.SpotifyException) as error:
                attempts += 1
                logger.warning("Can't reach Spotify yet: %s", error)
                self._wait_before_retry(error, attempts)

    def _loop(self, session: Session) -> None:
        retries = 0
        while True:
            try:
                session.tick()
                retries = 0
            except (KeyboardInterrupt, SystemExit):
                session.stop()
                logger.info("Shutting down")
                raise
            except Exception as error:
                retries += 1
                logger.exception("%s: %s", type(error).__name__, error)
                self._wait_before_retry(error, retries)

    def _wait_before_retry(self, error: Exception, attempt: int) -> None:
        delay_seconds = min(self.config.retry_delay_seconds * 2 ** (attempt - 1), MAX_BACKOFF_SECONDS)
        message = str(error)
        if isinstance(error, spotipy.SpotifyException) and error.http_status == HTTPStatus.TOO_MANY_REQUESTS:
            blocked_seconds = read_retry_after_seconds(error)
            delay_seconds = max(delay_seconds, min(blocked_seconds, MAX_RATE_LIMIT_WAIT_SECONDS))
            message = f"too many requests, blocked for about {_format_wait(blocked_seconds)}"
        logger.info("Trying again in %ds", delay_seconds)
        statusfile.write(self.config.state_dir, "retrying", error=message, retry_in_seconds=round(delay_seconds))
        time.sleep(delay_seconds)

    def read_status(self) -> dict:
        status = statusfile.read(self.config.state_dir)
        if status is None:
            return {"state": "not running", "live": False}
        age = statusfile.measure_age_seconds(status)
        stale_after = max(MISSED_CHECKS_BEFORE_STALE * self.config.check_interval_seconds, MIN_STALE_SECONDS)
        # a retry can sleep longer than a check
        stale_after = max(stale_after, status.get("retry_in_seconds", 0) + MIN_STALE_SECONDS)
        is_live = status.get("state") not in ("stopped",) and age is not None and age < stale_after
        return {**status, "live": is_live, "age_seconds": None if age is None else round(age)}

    def read_stats(self, since: datetime.datetime | None = None) -> stats.Stats:
        stats.import_legacy(self.config.state_dir)
        stats.upgrade_events(self.config.state_dir)
        return stats.summarize(self.config.state_dir, since=since)
