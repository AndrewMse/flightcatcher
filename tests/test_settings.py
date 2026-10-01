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


def test_local_mode_defaults_run_scheduler_and_one_worker(tmp_path) -> None:
    settings = Settings.load(tmp_path / "missing.toml")
    assert settings.backend.mode == "local"
    assert settings.workers.in_process == 1
    assert settings.workers.scheduler is True
    assert settings.queue.visibility_s == 120
    assert settings.queue.max_receives == 5
    assert settings.logging.format == "text"


def test_aws_mode_defaults_disable_scheduler_and_workers(tmp_path) -> None:
    settings = Settings.load(write_config(tmp_path, '[backend]\nmode = "aws"\n'))
    assert settings.workers.in_process == 0
    assert settings.workers.scheduler is False
    assert settings.aws.region == "eu-central-1"
    assert settings.aws.table_prefix == "hopwatch"


def test_toml_overrides_mode_defaults(tmp_path) -> None:
    settings = Settings.load(write_config(tmp_path, """
[backend]
mode = "aws"
[workers]
in_process = 2
scheduler = true
[aws]
queue_url = "https://sqs.example/q"
"""))
    assert settings.workers.in_process == 2
    assert settings.workers.scheduler is True
    assert settings.aws.queue_url == "https://sqs.example/q"


def test_env_configures_the_backend(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOPWATCH_BACKEND", "aws")
    monkeypatch.setenv("HOPWATCH_QUEUE_URL", "https://sqs.example/q")
    monkeypatch.setenv("HOPWATCH_DLQ_URL", "https://sqs.example/dlq")
    monkeypatch.setenv("HOPWATCH_TABLE_PREFIX", "hw-test")
    monkeypatch.setenv("HOPWATCH_AWS_REGION", "us-east-1")
    monkeypatch.setenv("HOPWATCH_AWS_ENDPOINT", "http://localhost:5000")
    monkeypatch.setenv("HOPWATCH_LOG_FORMAT", "json")
    monkeypatch.setenv("HOPWATCH_WIZZ_BACKEND", "http://fake")
    settings = Settings.from_env()
    assert settings.backend.mode == "aws"
    assert settings.aws.queue_url == "https://sqs.example/q"
    assert settings.aws.dlq_url == "https://sqs.example/dlq"
    assert settings.aws.table_prefix == "hw-test"
    assert settings.aws.region == "us-east-1"
    assert settings.aws.endpoint_url == "http://localhost:5000"
    assert settings.logging.format == "json"
    assert settings.wizz.backend_url == "http://fake"
    assert settings.workers.scheduler is False


def test_env_configures_queue_timing_as_numbers(monkeypatch) -> None:
    monkeypatch.setenv("HOPWATCH_QUEUE_VISIBILITY_S", "900")
    monkeypatch.setenv("HOPWATCH_QUEUE_MAX_RECEIVES", "3")
    monkeypatch.setenv("HOPWATCH_SEARCH_INTERVAL_MIN", "30")
    settings = Settings.from_env()
    assert settings.queue.visibility_s == 900
    assert settings.queue.max_receives == 3
    assert settings.watcher.search_interval_min == 30
