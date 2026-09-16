"""Opt-in CLI-only launchers; never inherit the Desktop/default login.

Use ``claude-cli`` for Claude Code and ``cswap-cli`` for its account manager.
Both select the same isolated config, active Keychain item and account roster.
Credentials are deliberately not imported: a fresh CLI login avoids sharing a
rotating OAuth token family with a still-running Desktop instance.
"""

from __future__ import annotations

import os
import json
from pathlib import Path
import shutil
import sys

from claude_swap.exceptions import ConfigError


_PREFERENCES_VERSION_KEY = "claudeSwapCliPreferencesVersion"
_GLOBAL_PREFERENCES = (
    "hasCompletedOnboarding", "lastOnboardingVersion", "theme",
    "preferredNotifChannel", "shiftEnterKeyBindingInstalled",
    "optionAsMetaKeyInstalled", "hasUsedBackslashReturn", "autoCompactEnabled",
    "verbose", "editorMode", "diffSidebarOpen", "hasSeenTasksHint",
    "lastReleaseNotesSeen",
)
_PROJECT_PREFERENCES = (
    "allowedTools", "enabledMcpjsonServers", "disabledMcpjsonServers",
    "hasTrustDialogAccepted", "hasCompletedProjectOnboarding",
    "hasClaudeMdExternalIncludesApproved", "hasClaudeMdExternalIncludesWarningShown",
    "projectOnboardingSeenCount", "ignorePatterns", "localSettingsSeenGitTracked",
)


def _read_config(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise ConfigError(f"Cannot read preferences from {path}; file left unchanged") from error
    if not isinstance(data, dict):
        raise ConfigError(f"Expected a JSON object in {path}; file left unchanged")
    return data


def _initialize_preferences() -> None:
    """Once, fill missing UI/project preferences without importing auth state.

    Explicit allowlists exclude OAuth, API keys, MCP credentials, account
    caches and instance/session state. Existing CLI choices always win.
    """
    from claude_swap.claude_locks import claude_config_lock
    from claude_swap.paths import get_default_global_config_path, get_global_config_path
    from claude_swap.settings import atomic_write_json

    target = get_global_config_path()
    if _read_config(target).get(_PREFERENCES_VERSION_KEY) == 1:
        return
    source = _read_config(get_default_global_config_path())
    with claude_config_lock():
        current = _read_config(target)
        if current.get(_PREFERENCES_VERSION_KEY) == 1:
            return
        for key in _GLOBAL_PREFERENCES:
            if key in source and isinstance(source[key], (str, bool, int, float)):
                current.setdefault(key, source[key])
        source_projects = source.get("projects")
        if isinstance(source_projects, dict):
            projects = current.setdefault("projects", {})
            if not isinstance(projects, dict):
                raise ConfigError(f"Invalid projects in {target}; file left unchanged")
            for path, values in source_projects.items():
                if not isinstance(values, dict):
                    continue
                preferences = {key: values[key] for key in _PROJECT_PREFERENCES if key in values}
                if not preferences:
                    continue
                project = projects.setdefault(path, {})
                if isinstance(project, dict):
                    for key, value in preferences.items():
                        project.setdefault(key, value)
        current[_PREFERENCES_VERSION_KEY] = 1
        atomic_write_json(target, current)


def enabled() -> bool:
    return os.environ.get("CLAUDE_SWAP_CLI_ONLY") == "1"


def profile_dir() -> Path:
    raw = os.environ.get("CLAUDE_SWAP_CLI_DIR")
    directory = Path(raw).expanduser() if raw else Path.home() / ".claude-cli"
    if not directory.is_absolute():
        raise ConfigError("CLAUDE_SWAP_CLI_DIR must be an absolute path")
    directory = directory.resolve()
    default = (Path.home() / ".claude").resolve()
    if directory == Path.home().resolve() or directory == default or default in directory.parents:
        raise ConfigError("The CLI-only profile must be outside ~/.claude and distinct from HOME")
    for name in (".claude.json", ".config.json", ".credentials.json", "swap"):
        if (directory / name).is_symlink():
            raise ConfigError(f"CLI login storage must not be a symlink: {directory / name}")
    return directory


def activate() -> Path:
    directory = profile_dir()
    # Per-process only: no global export that a Desktop-launched child inherits.
    os.environ["CLAUDE_SWAP_CLI_ONLY"] = "1"
    os.environ["CLAUDE_SWAP_CLI_DIR"] = str(directory)
    os.environ["CLAUDE_CONFIG_DIR"] = str(directory)
    os.environ.pop("CLAUDE_SECURESTORAGE_CONFIG_DIR", None)
    return directory


def initialize() -> Path:
    """Share customizations/history and seed preferences, but never logins."""
    directory = activate()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        for name in (
            "settings.json", "keybindings.json", "CLAUDE.md", "skills",
            "commands", "agents", "plugins", "projects", "history.jsonl",
            "file-history", "plans",
        ):
            source = Path.home() / ".claude" / name
            target = directory / name
            if source.exists() and not target.exists() and not target.is_symlink():
                target.symlink_to(source, target_is_directory=source.is_dir())
    _initialize_preferences()
    return directory


def swap_main() -> None:
    activate()
    if sys.argv[1:] == ["init"]:
        directory = initialize()
        print(f"CLI profile: {directory}")
        print("Log in with: claude-cli auth login")
        print("Save each CLI account/Team with: cswap-cli add")
        return
    if sys.argv[1:2] in (["upgrade"], ["update"], ["--upgrade"]):
        raise ConfigError("Update this fork from its Git repository; upstream self-upgrade would remove CLI isolation")
    from claude_swap.cli import main
    main()


def claude_main() -> None:
    initialize()
    executable = shutil.which("claude")
    if executable is None:
        raise ConfigError("Claude Code is not installed or not on PATH")
    os.execv(executable, [executable, *sys.argv[1:]])
