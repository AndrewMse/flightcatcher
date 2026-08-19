"""Runtime settings for the service, loaded from TOML with env overrides.

Lives separately from ``config.py``: that holds constants about how Wizz Air
behaves, this holds choices about how *your* deployment behaves. Secrets come
from the environment by preference so the config file can stay readable.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from .migrate import env, migrate_path



def default_config_path() -> Path:
    return Path(env("CONFIG") or "~/.config/hopwatch/config.toml").expanduser()


@dataclass
class PassengerSettings:
    """Details typed into the booking form.

    Only ever the passenger identity fields. Payment credentials are never
    stored, read, or entered by this program -- see ``booking.py``.
    """

    first_name: str = ""
    last_name: str = ""
    date_of_birth: str = ""  # YYYY-MM-DD
    gender: str = ""  # "male" | "female", as the form words it
    email: str = ""
    phone: str = ""

    @property
    def is_complete(self) -> bool:
        return bool(self.first_name and self.last_name)


@dataclass
class BrowserSettings:
    profile_dir: Path = Path("~/.local/share/hopwatch/profile")
    headless: bool = True
    slow_mo_ms: int = 250  # deliberate pacing; this drives a real airline site
    nav_timeout_ms: int = 45_000
    locale: str = "en-GB"
    timezone_id: str = "Europe/Bucharest"
    screenshot_dir: Path = Path("~/.local/share/hopwatch/screenshots")
    # Wizz expires idle sessions. The watcher can go many hours between
    # authenticated checks, so without a periodic touch the session is reliably
    # dead at the exact moment a booking window opens. 0 disables.
    keepalive_min: int = 20
    # Cookies are only flushed to the profile on a clean browser close, so a
    # hard kill loses the session. This snapshot is the belt to that braces.
    cookie_backup: Path = Path("~/.local/share/hopwatch/session-cookies.json")


@dataclass
class WatcherSettings:
    # Cheap unauthenticated re-scan of every active want.
    search_interval_min: int = 15
    # Authenticated checks are the scarce resource -- they touch a logged-in
    # session and are the part Wizz could plausibly object to.
    max_checks_per_hour: int = 30
    min_seconds_between_checks: int = 45
    # How often to re-check a candidate whose window is already open.
    recheck_interval_min: int = 20
    # Near the unlock moment, look more often: this is the race.
    hot_window_min: int = 30
    hot_recheck_interval_min: int = 3
    # Guard rails on spending.
    max_open_bookings: int = 2
    max_bookings_per_day: int = 4
    # How long a prefilled booking waits for a human before giving up.
    approval_hold_min: int = 12


@dataclass
class DiscordSettings:
    enabled: bool = False
    bot_token: str = ""
    channel_id: int = 0
    mention: str = ""  # e.g. "<@123456789>" to ping yourself
    # Whose taps count. The Approve button spends real money and real trip
    # credits, so anyone who can see the channel could otherwise book for you.
    # Empty means "nobody" -- deliberately fail closed rather than open.
    approver_ids: list[int] = field(default_factory=list)
    # Sync slash commands to this guild for instant availability. Global sync
    # can take up to an hour to propagate.
    guild_id: int = 0

    def may_approve(self, user_id: int) -> bool:
        return user_id in self.approver_ids


@dataclass
class WebSettings:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765


@dataclass
class Settings:
    database: Path = Path("~/.local/share/hopwatch/hopwatch.db")
    passenger: PassengerSettings = field(default_factory=PassengerSettings)
    browser: BrowserSettings = field(default_factory=BrowserSettings)
    watcher: WatcherSettings = field(default_factory=WatcherSettings)
    discord: DiscordSettings = field(default_factory=DiscordSettings)
    web: WebSettings = field(default_factory=WebSettings)

    @classmethod
    def load(cls, path: Path | None = None) -> Settings:
        path = path or default_config_path()
        migrate_path(path)
        raw: dict[str, Any] = {}
        if path.exists():
            raw = tomllib.loads(path.read_text())
        settings = _from_dict(cls, raw)
        settings._apply_env()
        settings._expand_paths()
        settings._migrate_legacy_paths()
        return settings

    def _apply_env(self) -> None:
        token = env("DISCORD_TOKEN")
        if token:
            self.discord.bot_token = token
            self.discord.enabled = True
        channel = env("DISCORD_CHANNEL")
        if channel:
            self.discord.channel_id = int(channel)
        db = env("DB")
        if db:
            self.database = Path(db)

    def _expand_paths(self) -> None:
        self.database = Path(self.database).expanduser()
        self.browser.profile_dir = Path(self.browser.profile_dir).expanduser()
        self.browser.screenshot_dir = Path(self.browser.screenshot_dir).expanduser()
        self.browser.cookie_backup = Path(self.browser.cookie_backup).expanduser()

    def _migrate_legacy_paths(self) -> None:
        """Pick up FlightCatcher-era state before anything opens these paths."""
        migrate_path(self.database, companions=("-wal", "-shm"))
        migrate_path(self.browser.profile_dir)
        migrate_path(self.browser.screenshot_dir)
        migrate_path(self.browser.cookie_backup)

    def describe_problems(self) -> list[str]:
        """Configuration gaps that will bite at runtime, worth saying early."""
        problems: list[str] = []
        if not self.passenger.is_complete:
            problems.append(
                "passenger.first_name / last_name are unset — booking prefill "
                "cannot run, availability checks still will"
            )
        if self.discord.enabled and not self.discord.bot_token:
            problems.append("discord.enabled is true but no bot token is set")
        if self.discord.enabled and not self.discord.channel_id:
            problems.append("discord.enabled is true but no channel_id is set")
        if self.discord.enabled and not self.discord.approver_ids:
            problems.append(
                "discord.approver_ids is empty — nobody can approve from Discord. "
                "Add your Discord user ID, or approvals will only work in the web UI"
            )
        return problems


def _from_dict(cls: type, data: dict[str, Any]) -> Any:
    """Build a nested dataclass from parsed TOML, ignoring unknown keys."""
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        if is_dataclass(f.type) if isinstance(f.type, type) else False:
            kwargs[f.name] = _from_dict(f.type, value)
        elif isinstance(value, dict) and f.name in {
            "passenger", "browser", "watcher", "discord", "web"
        }:
            nested = {
                "passenger": PassengerSettings,
                "browser": BrowserSettings,
                "watcher": WatcherSettings,
                "discord": DiscordSettings,
                "web": WebSettings,
            }[f.name]
            kwargs[f.name] = _from_dict(nested, value)
        else:
            kwargs[f.name] = value
    return cls(**kwargs)


EXAMPLE_CONFIG = """\
# Hopwatch configuration.
# Keep this file private: chmod 600. It holds passenger identity details.
# It must never hold payment details — the bot does not enter them.

database = "~/.local/share/hopwatch/hopwatch.db"

[passenger]
first_name = ""
last_name = ""
date_of_birth = ""   # YYYY-MM-DD
gender = ""          # "male" or "female", matching the booking form
email = ""
phone = ""

[browser]
headless = true
slow_mo_ms = 250
timezone_id = "Europe/Bucharest"

[watcher]
search_interval_min = 15
max_checks_per_hour = 30
recheck_interval_min = 20
hot_window_min = 30
hot_recheck_interval_min = 3
max_open_bookings = 2
max_bookings_per_day = 4
approval_hold_min = 12

[discord]
enabled = false
# Prefer the env var HOPWATCH_DISCORD_TOKEN over putting the token here.
bot_token = ""
channel_id = 0
# Your Discord user ID. The Approve button spends money, so only the people
# listed here may press it. Leave empty and Discord approval is disabled
# entirely (the web UI still works).
approver_ids = []
# Optional: your server's ID, so slash commands appear immediately rather
# than after Discord's global propagation delay.
guild_id = 0
mention = ""

[web]
enabled = true
host = "127.0.0.1"
port = 8765
"""
