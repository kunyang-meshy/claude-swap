"""Read the isolated CLI's active Team from credentials, not UI metadata.

Concurrent Claude Code sessions can write an old oauthAccount back to config.
That must neither relabel another Team's usage nor restore the old Team's
credential during an otherwise routine usage refresh. No store is written here.
"""

from __future__ import annotations

from claude_swap import oauth
from claude_swap.credentials import looks_like_api_key


def matching_slots(switcher, data: dict, live: str | None) -> list[str]:
    """Local evidence only; safe while the credential locks are held."""
    if not live or not (oauth.extract_access_token(live) or looks_like_api_key(live)):
        return []
    fingerprint = oauth.credential_fingerprint(live)
    matches = []
    for number, account in data.get("accounts", {}).items():
        email = account.get("email", "")
        backup = switcher._read_account_credentials(number, email)
        if backup and not (oauth.extract_access_token(backup) or looks_like_api_key(backup)):
            backup = ""
        if (backup and backup == live) or (fingerprint and (
            fingerprint == oauth.credential_fingerprint(backup)
            or switcher._probe_verdicts.get(
                switcher._lineage_key(number, email, fingerprint)
            ) is True
        )):
            matches.append(number)
    return matches


def current_slot(switcher, data: dict) -> str | None:
    """Resolve a unique credential owner; unknown identity means hold position."""
    live = switcher._read_credentials()
    if not live:
        return None
    matches = matching_slots(switcher, data, live)
    if len(matches) > 1:
        return None
    number = matches[0] if matches else None
    if number is None:
        token = oauth.extract_access_token(live)
        if not token:
            return None
        resolved = oauth.fetch_oauth_profile(token)
        if not resolved or not isinstance(resolved.get("organizationUuid"), str):
            return None
        matches = [
            n for n, account in data.get("accounts", {}).items()
            if (account.get("organizationUuid") or "") == resolved["organizationUuid"]
            and (
                account.get("uuid") == resolved.get("uuid")
                if account.get("uuid") else
                bool(account.get("email")) and account["email"] == resolved.get("email")
            )
        ]
        if (len(matches) != 1 or switcher._read_credentials() != live
                or (switcher._get_sequence_data() or {}).get("accounts") != data.get("accounts")):
            return None
        number = matches[0]
        fingerprint = oauth.credential_fingerprint(live)
        if fingerprint:
            email = data["accounts"][number].get("email", "")
            switcher._probe_verdicts[
                switcher._lineage_key(number, email, fingerprint)
            ] = True

    config_identity = switcher._get_current_account()
    config_slot = (switcher._find_account_slot(data, *config_identity)
                   if config_identity else None)
    drift = (config_slot, number) if config_slot != number else None
    if drift and drift != getattr(switcher, "_cli_identity_drift", None):
        switcher._logger.warning(
            "CLI Team metadata drift: config=%s, credential=%s; using the "
            "credential owner for display and automatic decisions. Login unchanged.",
            config_slot, number,
        )
    switcher._cli_identity_drift = drift
    return number
