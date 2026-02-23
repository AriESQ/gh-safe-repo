"""
INI config loading with safe defaults baked in.
Config lives at ~/.config/gh-safe-repo/config.ini (XDG).
"""

import configparser
import os
from pathlib import Path

from .errors import ConfigError

# Safe defaults that differ from GitHub's own defaults
SAFE_DEFAULTS = {
    "repo": {
        "private": "true",
        "has_wiki": "false",
        "has_projects": "false",
        "has_issues": "true",
        "delete_branch_on_merge": "true",
        "allow_squash_merge": "true",
        "allow_merge_commit": "false",
        "allow_rebase_merge": "true",
        "auto_init": "true",
    },
    "actions": {
        "enabled": "true",
        "allowed_actions": "selected",
        "default_workflow_permissions": "read",
        "can_approve_pull_request_reviews": "false",
    },
    "branch_protection": {
        "protected_branch": "main",
        "require_pull_request": "true",
        "required_approving_reviews": "1",
        "dismiss_stale_reviews": "true",
        "require_conversation_resolution": "true",
        "enforce_admins": "false",
        "allow_force_pushes": "false",
        "allow_deletions": "false",
    },
    "security": {
        "enable_dependabot_alerts": "true",
    },
}

CONFIG_PATH = Path.home() / ".config" / "gh-safe-repo" / "config.ini"


class ConfigManager:
    def __init__(self, config_path=None):
        self._path = Path(config_path) if config_path else CONFIG_PATH
        self._config = configparser.ConfigParser()
        self._load()

    def _load(self):
        # Seed with safe defaults
        for section, values in SAFE_DEFAULTS.items():
            self._config[section] = values

        # Override with user config if it exists
        if self._path.exists():
            try:
                self._config.read(self._path)
            except configparser.Error as e:
                raise ConfigError(f"Failed to parse config at {self._path}: {e}")

    def get(self, section, key, fallback=None):
        return self._config.get(section, key, fallback=fallback)

    def getbool(self, section, key, fallback=False):
        try:
            return self._config.getboolean(section, key, fallback=fallback)
        except ValueError as e:
            raise ConfigError(f"[{section}] {key}: {e}")

    def apply_overrides(self, overrides: dict):
        """Apply CLI flag overrides. overrides = {(section, key): value}."""
        for (section, key), value in overrides.items():
            if not self._config.has_section(section):
                self._config.add_section(section)
            self._config.set(section, key, str(value))

    def repo_settings(self):
        """Return the full repo settings dict."""
        section = "repo"
        if not self._config.has_section(section):
            return {}
        return dict(self._config[section])

    def actions_settings(self):
        """Return the full actions settings dict."""
        section = "actions"
        if not self._config.has_section(section):
            return {}
        return dict(self._config[section])

    def branch_protection_settings(self):
        """Return the full branch protection settings dict."""
        section = "branch_protection"
        if not self._config.has_section(section):
            return {}
        return dict(self._config[section])
