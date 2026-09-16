"""Opt-in CLI-only launchers; never inherit the Desktop/default login.

Use ``claude-cli`` for Claude Code and ``cswap-cli`` for its account manager.
Both select the same isolated config, active Keychain item and account roster.
Credentials are deliberately not imported: a fresh CLI login avoids sharing a
rotating OAuth token family with a still-running Desktop instance.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys

from claude_swap.exceptions import ConfigError


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
    """Share customizations/history, but create no login or session state."""
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
    activate()
    executable = shutil.which("claude")
    if executable is None:
        raise ConfigError("Claude Code is not installed or not on PATH")
    os.execv(executable, [executable, *sys.argv[1:]])
