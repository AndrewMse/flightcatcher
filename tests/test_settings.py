from __future__ import annotations

from hopwatch.settings import EXAMPLE_CONFIG, Settings


def write_config(tmp_path, body: str):
    path = tmp_path / "config.toml"
    path.write_text(body)
    return path


def test_example_config_is_loadable(tmp_path) -> None:
    """The file `init-config` writes must actually parse."""
    settings = Settings.load(write_config(tmp_path, EXAMPLE_CONFIG))
    assert settings.web.port == 8765
    assert settings.discord.enabled is False
    assert settings.watcher.approval_hold_min == 12


def test_nested_sections_load(tmp_path) -> None:
    settings = Settings.load(write_config(tmp_path, """
[passenger]
first_name = "Ada"
last_name = "Lovelace"

[watcher]
max_checks_per_hour = 5
max_bookings_per_day = 1

[discord]
enabled = true
channel_id = 42
approver_ids = [123, 456]
guild_id = 7
"""))
    assert settings.passenger.first_name == "Ada"
    assert settings.passenger.is_complete
    assert settings.watcher.max_checks_per_hour == 5
    assert settings.discord.approver_ids == [123, 456]
    assert settings.discord.guild_id == 7


def test_unknown_keys_are_ignored(tmp_path) -> None:
    """A stale key from an older config should not crash startup."""
    settings = Settings.load(write_config(tmp_path, """
nonsense = true
[watcher]
some_removed_option = 3
recheck_interval_min = 11
"""))
    assert settings.watcher.recheck_interval_min == 11


def test_missing_config_falls_back_to_defaults(tmp_path) -> None:
    settings = Settings.load(tmp_path / "nope.toml")
    assert settings.web.port == 8765
    assert settings.discord.approver_ids == []


def test_env_overrides_the_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOPWATCH_DISCORD_TOKEN", "from-env")
    monkeypatch.setenv("HOPWATCH_DISCORD_CHANNEL", "999")
    settings = Settings.load(write_config(tmp_path, '[discord]\nbot_token = "from-file"\n'))
    assert settings.discord.bot_token == "from-env"
    assert settings.discord.channel_id == 999
    assert settings.discord.enabled is True


# --- the approver gate ------------------------------------------------------


def test_only_listed_users_may_approve(tmp_path) -> None:
    settings = Settings.load(write_config(tmp_path, """
[discord]
enabled = true
channel_id = 1
approver_ids = [111]
"""))
    assert settings.discord.may_approve(111)
    assert not settings.discord.may_approve(222)


def test_an_empty_approver_list_fails_closed(tmp_path) -> None:
    """Nobody listed must mean nobody can spend, not everybody."""
    settings = Settings.load(write_config(tmp_path, "[discord]\nenabled = true\n"))
    assert not settings.discord.may_approve(111)
    assert not settings.discord.may_approve(0)


def test_empty_approver_list_is_flagged_as_a_problem(tmp_path) -> None:
    settings = Settings.load(write_config(tmp_path, """
[discord]
enabled = true
channel_id = 5
bot_token = "x"
"""))
    assert any("approver_ids" in p for p in settings.describe_problems())


def test_a_complete_discord_config_has_no_complaints(tmp_path) -> None:
    settings = Settings.load(write_config(tmp_path, """
[passenger]
first_name = "Ada"
last_name = "Lovelace"

[discord]
enabled = true
channel_id = 5
bot_token = "x"
approver_ids = [111]
"""))
    assert settings.describe_problems() == []


def test_incomplete_passenger_is_flagged(tmp_path) -> None:
    settings = Settings.load(write_config(tmp_path, '[passenger]\nfirst_name = "Ada"\n'))
    assert any("passenger" in p for p in settings.describe_problems())


def test_load_migrates_legacy_config_file(tmp_path) -> None:
    legacy = tmp_path / "flightcatcher" / "config.toml"
    legacy.parent.mkdir()
    legacy.write_text("[watcher]\nmax_checks_per_hour = 7\n")

    settings = Settings.load(tmp_path / "hopwatch" / "config.toml")
    assert settings.watcher.max_checks_per_hour == 7
    assert (tmp_path / "hopwatch" / "config.toml").exists()


def test_load_migrates_legacy_database_at_the_default_path(isolated_home) -> None:
    legacy = isolated_home / ".local/share/flightcatcher/flightcatcher.db"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("db")

    settings = Settings.load(isolated_home / "missing.toml")
    assert settings.database == isolated_home / ".local/share/hopwatch/hopwatch.db"
    assert settings.database.read_text() == "db"


def test_explicit_legacy_paths_are_left_where_they_are(tmp_path, isolated_home) -> None:
    db = isolated_home / ".local/share/flightcatcher/flightcatcher.db"
    db.parent.mkdir(parents=True)
    db.write_text("db")

    settings = Settings.load(write_config(
        tmp_path, 'database = "~/.local/share/flightcatcher/flightcatcher.db"\n'
    ))
    assert settings.database == db
    assert db.read_text() == "db"
