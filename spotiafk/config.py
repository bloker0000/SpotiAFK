import dataclasses
import importlib.util
import math
import os
import stat
import tomllib

from spotiafk.units import SECONDS_PER_MINUTE

BASE_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
SCOPE = (
    "user-modify-playback-state playlist-read-private user-read-playback-state"
    " user-read-private"  # to see if the account has premium
)
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8888/callback/"
MAX_BACKOFF_SECONDS = 300
# config and token hold secrets, owner only (0o600)
PRIVATE_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR
# spotify counts a play as a stream after 30 seconds
MIN_SKIP_AFTER_SECONDS = 31
DEFAULT_IDLE_MINUTES = 15.0
DEFAULT_CHECK_INTERVAL_SECONDS = 30.0
DEFAULT_RETRY_DELAY_SECONDS = 10.0
# what spotiafk 1 did when its options.py didnt say
LEGACY_SKIP_AFTER_SECONDS = 35.0
LEGACY_IDLE_CHECKS = 5
# so float noise like 3.0000000000000004 doesnt add a check
IDLE_CHECKS_DECIMALS = 6


class ConfigError(Exception):
    """missing or invalid config, the message says how to fix it"""


def get_default_config_path() -> str:
    config_home = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(config_home, "spotiafk", "config.toml")


def get_default_state_dir() -> str:
    data_home = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    return os.path.join(data_home, "spotiafk")


def _count_minutes(checks: float, check_interval_seconds: float) -> float:
    return checks * check_interval_seconds / SECONDS_PER_MINUTE


@dataclasses.dataclass(frozen=True)
class Config:
    playlist: str = ""
    play_on: tuple = ()
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = DEFAULT_REDIRECT_URI
    skip_after_seconds: float | None = None  # None plays every track in full
    shuffle: bool = True
    hand_back: bool = True
    idle_minutes: float = DEFAULT_IDLE_MINUTES
    check_interval_seconds: float = DEFAULT_CHECK_INTERVAL_SECONDS
    retry_delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS
    state_dir: str = dataclasses.field(default_factory=get_default_state_dir)
    source: str = "<defaults>"

    @property
    def idle_checks(self) -> int:
        checks = self.idle_minutes * SECONDS_PER_MINUTE / self.check_interval_seconds
        return max(1, math.ceil(round(checks, IDLE_CHECKS_DECIMALS)))

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        candidates = (
            [os.fspath(path)]
            if path is not None
            else [
                os.environ.get("SPOTIAFK_CONFIG", ""),
                os.path.join(os.getcwd(), "spotiafk.toml"),
                get_default_config_path(),
            ]
        )
        for candidate in candidates:
            if candidate and os.path.isfile(candidate):
                return cls._load_toml(candidate)
        legacy = os.path.join(BASE_DIR, "options.py")
        if path is None and os.path.isfile(legacy):
            return cls._load_options_py(legacy)
        raise ConfigError(
            "No configuration found. Run 'spotiafk setup' to create one "
            f"(looked for spotiafk.toml here and at {get_default_config_path()})."
        )

    @classmethod
    def _load_toml(cls, path: str) -> "Config":
        try:
            with open(path, "rb") as file:
                data = tomllib.load(file)
        except tomllib.TOMLDecodeError as error:
            raise ConfigError(f"{path} is not valid TOML: {error}") from error
        spotify = data.get("spotify", {})
        playback = data.get("playback", {})
        checks = data.get("checks", {})
        skip_after_seconds = playback.get("skip_after", 0)
        check_interval_seconds = float(checks.get("interval", DEFAULT_CHECK_INTERVAL_SECONDS))
        # configs from before idle_minutes counted checks instead
        if "idle_minutes" not in checks and "idle_checks" in checks:
            idle_minutes = _count_minutes(float(checks["idle_checks"]), check_interval_seconds)
        else:
            idle_minutes = float(checks.get("idle_minutes", DEFAULT_IDLE_MINUTES))
        return cls(
            playlist=data.get("playlist", ""),
            play_on=tuple(data.get("play_on", [])),
            client_id=spotify.get("client_id", ""),
            client_secret=spotify.get("client_secret", ""),
            redirect_uri=spotify.get("redirect_uri", DEFAULT_REDIRECT_URI),
            skip_after_seconds=None if skip_after_seconds in (False, 0) else float(skip_after_seconds),
            shuffle=playback.get("shuffle", True),
            hand_back=playback.get("hand_back", True),
            idle_minutes=idle_minutes,
            check_interval_seconds=check_interval_seconds,
            retry_delay_seconds=float(checks.get("retry", DEFAULT_RETRY_DELAY_SECONDS)),
            state_dir=os.path.expanduser(data.get("state_dir", get_default_state_dir())),
            source=path,
        )

    @classmethod
    def _load_options_py(cls, path: str) -> "Config":
        spec = importlib.util.spec_from_file_location("spotiafk_legacy_options", path)
        options = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(options)

        def read_option(*names, default=None):
            for name in names:
                if hasattr(options, name):
                    return getattr(options, name)
            return default

        skips_songs = read_option("SKIP_SONGS", default=True)
        check_interval_seconds = float(
            read_option("TIME_BETWEEN_CHECKS", "TIME_BETWEEN_CHEAKS", default=DEFAULT_CHECK_INTERVAL_SECONDS)
        )
        # the old options file spelled checks as cheaks
        idle_checks = int(read_option("CHECKS_BEFORE_PLAYING", "CHEAKS_BEFORE_PLAYING", default=LEGACY_IDLE_CHECKS))
        skip_after_seconds = float(read_option("SKIP_DELAY", default=LEGACY_SKIP_AFTER_SECONDS))
        return cls(
            playlist=read_option("PLAYLIST_NAME", default=""),
            play_on=tuple(read_option("SERVER_NAMES", default=[])),
            client_id=read_option("CLIENT_ID", default=""),
            client_secret=read_option("CLIENT_SECRET", default=""),
            redirect_uri=read_option("REDIRECT_URI", default=DEFAULT_REDIRECT_URI),
            skip_after_seconds=skip_after_seconds if skips_songs else None,
            shuffle=read_option("RANDOM_ORDER_TRACKS", default=True),
            idle_minutes=_count_minutes(idle_checks, check_interval_seconds),
            check_interval_seconds=check_interval_seconds,
            retry_delay_seconds=float(read_option("RETRY_TIME", default=DEFAULT_RETRY_DELAY_SECONDS)),
            source=path,
        )

    def find_issues(self) -> list:
        problems = []
        # an untouched options.example.py held XXXX placeholders
        if not self.client_id or set(self.client_id) == {"X"}:
            problems.append("spotify.client_id is not set, copy it from your Spotify app")
        if not self.client_secret or set(self.client_secret) == {"X"}:
            problems.append("spotify.client_secret is not set, copy it from your Spotify app")
        if not self.playlist:
            problems.append("playlist is not set, give the name of one of your playlists or a Spotify link")
        if not self.play_on:
            problems.append("play_on is empty, list at least one Spotify device name")
        if self.skip_after_seconds is not None and self.skip_after_seconds < MIN_SKIP_AFTER_SECONDS:
            problems.append(
                f"playback.skip_after must be at least {MIN_SKIP_AFTER_SECONDS}s for a play to count as a stream"
            )
        if self.idle_minutes <= 0:
            problems.append("checks.idle_minutes must be more than 0")
        if self.check_interval_seconds <= 0:
            problems.append("checks.interval must be more than 0 seconds")
        if self.retry_delay_seconds <= 0:
            problems.append("checks.retry must be more than 0 seconds")
        return problems

    def require_valid(self) -> None:
        problems = self.find_issues()
        if problems:
            raise ConfigError(f"Fix {self.source} before running:\n  - " + "\n  - ".join(problems))
