"""CLI account operations must leave the Desktop/default stores untouched."""

import json
import os
import plistlib
from pathlib import Path
from unittest.mock import Mock

import pytest

from claude_swap import cli_profile, macos_keychain, paths
from claude_swap.credentials import (
    CLAUDE_CODE_KEYCHAIN_SERVICE,
    CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE,
    SECURITY_SERVICE,
    active_keychain_service,
    backup_keychain_service,
)
from claude_swap.exceptions import ConfigError
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher


def _credential(label):
    return json.dumps({"claudeAiOauth": {
        "accessToken": f"test-{label}", "refreshToken": f"test-refresh-{label}",
        "expiresAt": 9999999999000,
    }})


@pytest.fixture
def isolated_cli(temp_home, monkeypatch):
    monkeypatch.setattr(Platform, "detect", staticmethod(lambda: Platform.MACOS))
    directory = cli_profile.activate()
    directory.mkdir()
    return directory


def test_launchers_override_inherited_desktop_profile(temp_home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(temp_home / ".claude"))
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "")
    directory = cli_profile.activate()
    assert paths.get_claude_config_home() == directory == temp_home / ".claude-cli"
    assert paths.get_global_config_path() == directory / ".claude.json"
    assert paths.get_backup_root() == directory / "swap"
    assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" not in os.environ
    assert active_keychain_service() != CLAUDE_CODE_KEYCHAIN_SERVICE
    assert backup_keychain_service() != SECURITY_SERVICE


@pytest.mark.parametrize("target", [".claude", ".claude/nested", "."])
def test_default_profile_cannot_be_selected_as_cli_only(temp_home, monkeypatch, target):
    monkeypatch.setenv("CLAUDE_SWAP_CLI_DIR", str(temp_home / target))
    with pytest.raises(ConfigError):
        cli_profile.activate()


def test_initialization_shares_history_but_never_login_or_session_state(temp_home):
    default = temp_home / ".claude"
    for name in ("settings.json", "history.jsonl", ".credentials.json", ".config.json"):
        (default / name).write_text("{}")
    (default / "sessions").mkdir()
    (default / "projects").mkdir()
    (temp_home / ".claude.json").write_text('{"oauthAccount": {"emailAddress": "desktop@example.com"}}')
    directory = cli_profile.initialize()
    assert (directory / "settings.json").resolve() == default / "settings.json"
    assert (directory / "projects").resolve() == default / "projects"
    for name in (".credentials.json", ".config.json", ".claude.json", "sessions"):
        assert not (directory / name).exists()
    assert cli_profile.initialize() == directory  # Idempotent; no clobbering.


def test_claude_launcher_passes_arguments_and_isolated_environment(temp_home, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/native/claude")
    monkeypatch.setattr("sys.argv", ["claude-cli", "--resume", "session-1"])
    execute = Mock()
    monkeypatch.setattr(os, "execv", execute)
    cli_profile.claude_main()
    execute.assert_called_once_with("/native/claude", ["/native/claude", "--resume", "session-1"])
    assert os.environ["CLAUDE_CONFIG_DIR"] == str(temp_home / ".claude-cli")


def test_active_and_backup_writes_and_deletes_never_touch_default_keychain(
    isolated_cli, block_real_keychain, monkeypatch,
):
    user = macos_keychain.keychain_account_name()
    default = {
        (CLAUDE_CODE_KEYCHAIN_SERVICE, user): _credential("desktop"),
        (CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, user): "test-desktop-key",
        (SECURITY_SERVICE, "account-1-same@example.com"): _credential("default-backup"),
    }
    block_real_keychain.data.update(default)
    switcher = ClaudeAccountSwitcher()
    switcher._setup_directories()
    store = switcher._store
    store._write_credentials(_credential("cli-one"))
    assert store._read_active_credentials().value == _credential("cli-one")
    store._write_account_credentials("1", "same@example.com", _credential("cli-one"))
    store._write_account_credentials("1", "same@example.com", _credential("cli-two"))
    assert store._read_previous_backup("1", "same@example.com") == _credential("cli-one")
    assert store._read_account_credentials("1", "same@example.com") == _credential("cli-two")
    store._clear_oauth_credential()
    store._kc_delete_backup("1", "same@example.com")
    store._kc_delete_backup_prev("1", "same@example.com")
    assert {k: block_real_keychain.data[k] for k in default} == default


def test_keychain_failure_fallback_stays_in_cli_profile(
    isolated_cli, temp_home, block_real_keychain, monkeypatch,
):
    desktop_file = temp_home / ".claude" / ".credentials.json"
    desktop_file.write_text("desktop-must-stay")
    switcher = ClaudeAccountSwitcher()
    monkeypatch.setattr(switcher._store, "_use_keychain", lambda: False)
    switcher._store._write_oauth_credentials(_credential("cli"))
    assert (isolated_cli / ".credentials.json").read_text() == _credential("cli")
    assert desktop_file.read_text() == "desktop-must-stay"


def test_cli_roster_never_migrates_or_purges_default_roster(
    isolated_cli, temp_home, monkeypatch,
):
    legacy = paths.get_legacy_backup_root()
    legacy.mkdir()
    marker = legacy / "sequence.json"
    marker.write_text('{"desktop": true}')
    assert paths.migrate_legacy_backup_dir(paths.get_backup_root()) is False
    switcher = ClaudeAccountSwitcher()
    switcher._setup_directories()
    monkeypatch.setattr("builtins.input", lambda _: "y")
    switcher.purge()
    assert marker.read_text() == '{"desktop": true}'
    assert not (isolated_cli / "swap").exists()


def test_custom_cli_menu_service_keeps_launcher_and_profile(isolated_cli, monkeypatch):
    from claude_swap import launch_agent
    launcher = isolated_cli / "cswap-cli"
    launcher.write_text("test launcher")
    monkeypatch.setattr("sys.argv", [str(launcher), "menubar", "--install-service"])
    parsed = plistlib.loads(launch_agent.build_plist())
    assert parsed["ProgramArguments"] == [str(launcher), "menubar"]
    assert parsed["EnvironmentVariables"]["CLAUDE_SWAP_CLI_DIR"] == str(isolated_cli)


def test_upstream_self_upgrade_cannot_remove_cli_isolation(temp_home, monkeypatch):
    monkeypatch.setattr("sys.argv", ["cswap-cli", "upgrade"])
    with pytest.raises(ConfigError, match="fork"):
        cli_profile.swap_main()


def test_desktop_team_changes_do_not_trigger_cli_switch_but_95_percent_does(
    isolated_cli, temp_home, block_real_keychain, monkeypatch,
):
    import time
    from claude_swap.autoswitch import AutoSwitchEngine, TickOutcome
    from claude_swap.settings import AutoSwitchSettings
    from claude_swap.usage_store import UsageEntry

    user = macos_keychain.keychain_account_name()
    desktop_key = (CLAUDE_CODE_KEYCHAIN_SERVICE, user)
    block_real_keychain.data[desktop_key] = _credential("desktop")
    switcher = ClaudeAccountSwitcher()
    switcher._setup_directories()
    data = {"accounts": {}, "sequence": [], "activeAccountNumber": None}
    configs = {}
    for n in (1, 2):
        num, email = str(n), "same@example.com"
        identity = {"emailAddress": email, "accountUuid": "same-user", "organizationUuid": f"team-{n}"}
        configs[num] = {"oauthAccount": identity}
        switcher._write_account_credentials(num, email, _credential(num))
        switcher._write_account_config(num, email, json.dumps(configs[num]))
        data["accounts"][num] = {"email": email, "uuid": "same-user", "organizationUuid": f"team-{n}", "organizationName": f"CLI Team {n}"}
    data["sequence"] = [1, 2]
    data["activeAccountNumber"] = 1
    switcher._write_json(switcher.sequence_file, data)
    paths.get_global_config_path().write_text(json.dumps(configs["1"]))
    switcher._write_credentials(_credential("1"))
    now = time.time()
    usages = {
        "1": UsageEntry(last_good={"five_hour": {"pct": 60.0}, "seven_day": {"pct": 43.0}}, age_s=0, fetched_at=now),
        "2": UsageEntry(last_good={"five_hour": {"pct": 10.0}, "seven_day": {"pct": 20.0}}, age_s=0, fetched_at=now),
    }
    monkeypatch.setattr(switcher, "usage_entries_by_account", lambda **kwargs: usages)
    events = []
    engine = AutoSwitchEngine(switcher, AutoSwitchSettings(threshold=95), events.append)
    desktop_config = temp_home / ".claude.json"
    for n in range(4):
        desktop_config.write_text(json.dumps({"oauthAccount": {"emailAddress": f"desktop-{n}@example.com"}}))
        assert engine.tick() == TickOutcome.NO_ACTION
        assert switcher.current_account_number() == "1"
    desktop_before = desktop_config.read_bytes()
    usages["1"] = UsageEntry(last_good={"five_hour": {"pct": 95.0}, "seven_day": {"pct": 43.0}}, age_s=0, fetched_at=now)
    assert engine.tick() == TickOutcome.SWITCHED
    assert switcher.current_account_number() == "2"
    assert switcher._read_credentials() == _credential("2")
    assert desktop_config.read_bytes() == desktop_before
    assert block_real_keychain.data[desktop_key] == _credential("desktop")
    assert any(e.kind == "switch" and e.trigger == "proactive" for e in events)
