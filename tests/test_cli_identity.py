"""Stale Team metadata must not become a login switch or mis-key usage."""

import json
import time
from unittest.mock import Mock

import pytest

from claude_swap import cli_profile, oauth
from claude_swap.autoswitch import AutoSwitchEngine, TickOutcome
from claude_swap.json_output import USAGE_FOREIGN_CREDENTIAL
from claude_swap.models import Platform
from claude_swap.settings import AutoSwitchSettings
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import UsageEntry


def credential(number, expires=9999999999000):
    return json.dumps({"claudeAiOauth": {
        "accessToken": f"test-access-{number}",
        "refreshToken": f"test-refresh-{number}", "expiresAt": expires,
    }})


@pytest.fixture
def cli(temp_home, monkeypatch):
    monkeypatch.setattr(Platform, "detect", staticmethod(lambda: Platform.LINUX))
    directory = cli_profile.activate()
    directory.mkdir()
    s = ClaudeAccountSwitcher()
    s._setup_directories()
    data = {"sequence": list(range(1, 7)), "activeAccountNumber": 2, "accounts": {}}
    for number in map(str, range(1, 7)):
        data["accounts"][number] = {
            "email": "same@example.com", "uuid": "same-user",
            "organizationUuid": f"org-{number}", "organizationName": f"Team {number}",
        }
        s._write_account_credentials(number, "same@example.com", credential(number))
        s._write_account_config(number, "same@example.com", json.dumps({"oauthAccount": {
            "emailAddress": "same@example.com", "accountUuid": "same-user",
            "organizationUuid": f"org-{number}",
        }}))
    s._write_json(s.sequence_file, data)
    s._write_json(s._get_claude_config_path(), {"oauthAccount": {
        "emailAddress": "same@example.com", "organizationUuid": "org-6",
        "accountUuid": "same-user",
    }, "theme": "dark"})
    s._write_credentials(credential("2"))
    return s


def test_stale_team_metadata_does_not_relabel_the_live_login(cli, monkeypatch):
    probe = Mock(side_effect=AssertionError("local proof needs no network"))
    monkeypatch.setattr(oauth, "fetch_oauth_profile", probe)
    before = cli._get_claude_config_path().read_bytes()
    assert cli.current_account_number() == "2"
    rows = cli._build_accounts_info()
    assert [str(row[0]) for row in rows if row[4]] == ["2"]
    assert cli._live_identity_matches("same@example.com", "org-2")
    assert not cli._live_identity_matches("same@example.com", "org-6")
    assert cli._read_credentials() == credential("2")
    assert cli._get_claude_config_path().read_bytes() == before
    assert cli._get_sequence_data()["activeAccountNumber"] == 2


def test_stale_exhausted_team_cannot_trigger_an_automatic_switch(cli, monkeypatch):
    now = time.time()
    entries = {str(n): UsageEntry(last_good={
        "five_hour": {"pct": 100 if n == 6 else 12},
        "seven_day": {"pct": 31},
        "scoped": [{"name": "Fable", "pct": 100 if n == 6 else 46}],
    }, fetched_at=now, age_s=0) for n in range(1, 7)}
    monkeypatch.setattr(cli, "usage_entries_by_account", lambda **kw: entries)
    switch = Mock()
    monkeypatch.setattr(cli, "switch_to", switch)
    events = []
    engine = AutoSwitchEngine(cli, AutoSwitchSettings(
        threshold=95, strategy="fable-reset-first", failover_enabled=False,
    ), events.append)
    assert engine.tick() is TickOutcome.NO_ACTION
    assert events[-1].reason == "below-threshold"
    assert events[-1].detail == "31% < 95%"
    switch.assert_not_called()


@pytest.mark.parametrize("expires", [1, 9999999999000])
def test_usage_refresh_cannot_restore_a_foreign_team(cli, monkeypatch, expires):
    live = credential("2", expires)
    cli._write_credentials(live)
    refresh, fetch, write = Mock(), Mock(), Mock()
    monkeypatch.setattr(oauth, "try_refresh_oauth_credentials", refresh)
    monkeypatch.setattr(oauth, "try_fetch_usage_for_account", fetch)
    monkeypatch.setattr(cli, "_write_credentials", write)
    result = cli._fetch_active_usage("6", "same@example.com", live, "org-6")
    assert result.sentinel == USAGE_FOREIGN_CREDENTIAL
    refresh.assert_not_called()
    fetch.assert_not_called()
    write.assert_not_called()


def test_credential_switch_during_refresh_is_rechecked_under_lock(cli, monkeypatch):
    expired = credential("2", 1)
    cli._write_credentials(credential("6", 1))
    refresh, write = Mock(), Mock()
    monkeypatch.setattr(oauth, "try_refresh_oauth_credentials", refresh)
    monkeypatch.setattr(cli, "_write_credentials", write)
    cli._fetch_active_usage("2", "same@example.com", expired, "org-2")
    refresh.assert_not_called()
    write.assert_not_called()


def test_actual_team_can_refresh_even_when_config_names_another_team(cli, monkeypatch):
    expired = credential("2", 1)
    cli._write_credentials(expired)
    refreshed = credential("2-rotated")
    refresh = Mock(return_value=oauth.RefreshOutcome(refreshed, None))
    monkeypatch.setattr(oauth, "try_refresh_oauth_credentials", refresh)
    monkeypatch.setattr(oauth, "try_fetch_usage_for_account", Mock(
        return_value=oauth.UsageOutcome({"five_hour": {"pct": 12}}),
    ))
    result = cli._fetch_active_usage("2", "same@example.com", expired, "org-2")
    assert result.usage == {"five_hour": {"pct": 12}}
    refresh.assert_called_once_with(expired, timeout_s=6.0)
    assert cli._read_credentials() == refreshed
    assert cli._read_account_credentials("2", "same@example.com") == refreshed
    assert cli._read_account_credentials("6", "same@example.com") == credential("6")


@pytest.mark.parametrize("live", [None, "", "{}", credential("unmanaged")])
def test_unknown_or_missing_credential_never_falls_back_to_config(cli, monkeypatch, live):
    monkeypatch.setattr(cli, "_read_credentials", lambda: live)
    monkeypatch.setattr(oauth, "fetch_oauth_profile", lambda token: None)
    assert cli.current_account_number() is None
    assert not cli._live_identity_matches("same@example.com", "org-6")


def test_rotated_credential_is_resolved_by_org_and_memoized(cli, monkeypatch):
    cli._write_credentials(credential("rotated"))
    probe = Mock(return_value={"uuid": "same-user", "email": "same@example.com",
                               "organizationUuid": "org-2"})
    monkeypatch.setattr(oauth, "fetch_oauth_profile", probe)
    assert cli.current_account_number() == "2"
    assert cli.current_account_number() == "2"
    assert cli._live_identity_matches("same@example.com", "org-2")
    probe.assert_called_once()


@pytest.mark.parametrize("profile", [
    {"uuid": "same-user", "email": "same@example.com", "organizationUuid": None},
    {"uuid": "different-user", "email": "same@example.com", "organizationUuid": "org-2"},
    {"uuid": "same-user", "email": "same@example.com", "organizationUuid": "unmanaged"},
])
def test_partial_or_foreign_profile_does_not_authorize_rotation(cli, monkeypatch, profile):
    cli._write_credentials(credential("unmanaged"))
    monkeypatch.setattr(oauth, "fetch_oauth_profile", lambda token: profile)
    assert cli.current_account_number() is None


def test_profile_probe_race_does_not_cache_a_stale_owner(cli, monkeypatch):
    cli._write_credentials(credential("rotated"))
    def probe(token):
        cli._write_credentials(credential("3"))
        return {"uuid": "same-user", "email": "same@example.com", "organizationUuid": "org-2"}
    monkeypatch.setattr(oauth, "fetch_oauth_profile", probe)
    assert cli.current_account_number() is None
    assert not cli._probe_verdicts
    assert cli.current_account_number() == "3"


def test_duplicate_backup_identity_is_not_guessed(cli):
    cli._write_account_credentials("6", "same@example.com", credential("2"))
    assert cli.current_account_number() is None
    assert not cli._live_identity_matches("same@example.com", "org-2")


def test_empty_oauth_blob_cannot_establish_ownership(cli):
    cli._write_credentials("{}")
    cli._write_account_credentials("6", "same@example.com", "{}")
    assert cli.current_account_number() is None
    assert not cli._live_identity_matches("same@example.com", "org-6")


def test_switch_from_stale_config_preserves_all_team_backups_offline(cli, monkeypatch):
    monkeypatch.setattr(oauth, "fetch_oauth_profile", lambda token: None)
    before = {str(n): cli._read_account_credentials(str(n), "same@example.com")
              for n in range(1, 7)}
    result = cli.switch_to("3", json_output=True)
    assert result["switched"]
    assert result["from"]["number"] == 2
    assert result["to"]["number"] == 3
    assert cli.current_account_number() == "3"
    assert {str(n): cli._read_account_credentials(str(n), "same@example.com")
            for n in range(1, 7)} == before


def test_verified_rotation_is_not_mistaken_for_an_already_saved_backup(cli, monkeypatch):
    live = credential("rotated")
    cli._write_credentials(live)
    monkeypatch.setattr(oauth, "fetch_oauth_profile", lambda token: {
        "uuid": "same-user", "email": "same@example.com", "organizationUuid": "org-2",
    })
    assert cli.current_account_number() == "2"
    assert cli._classify_outgoing_credential(
        "6", "same@example.com", live, {"resolved": None}, cli._get_sequence_data(),
    ) == ("foreign", "2")  # Preserve the unsaved rotation before switching.
