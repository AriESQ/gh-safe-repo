"""
INI config loading with safe defaults baked in.

Config lookup order (first match wins):
  1. --config PATH           (explicit override)
  2. ./gh-safe-repo.ini      (current working directory)
  3. $XDG_CONFIG_HOME/gh-safe-repo/gh-safe-repo.ini  (defaults to ~/.config)
"""

import configparser
import os
from pathlib import Path

from .errors import ConfigError

CONFIG_FILENAME = "gh-safe-repo.ini"

# Safe defaults that differ from GitHub's own defaults
SAFE_DEFAULTS = {
    "repo": {
        "private": "true",
        "delete_branch_on_merge": "false",
        "allow_squash_merge": "true",
        "allow_merge_commit": "true",
        "allow_rebase_merge": "true",
        "auto_init": "false",
    },
    "actions": {
        "enabled": "true",
        "allowed_actions": "selected",
        "github_owned_allowed": "true",
        "verified_allowed": "true",
        "patterns_allowed": "",
        "sha_pinning_required": "true",
        "default_workflow_permissions": "read",
        "can_approve_pull_request_reviews": "false",
        "fork_pr_approval_policy": "all_external_contributors",
    },
    "branch_protection": {
        "protected_branch": "master, main",
        "require_pull_request": "true",
        "required_approving_reviews": "1",
        "dismiss_stale_reviews": "true",
        "require_conversation_resolution": "true",
        "enforce_admins": "false",
        "allow_force_pushes": "false",
        "allow_deletions": "false",
        # Rulesets API is the default; set false for the legacy classic
        # branch-protection path (kept for one release cycle).
        "use_rulesets": "true",
    },
    "tag_protection": {
        "protected_tags": "*",
        "prevent_tag_deletion": "true",
        "prevent_tag_update": "true",
    },
    "security": {
        "enable_dependabot_alerts": "true",
        "enable_dependabot_security_updates": "true",
        "enable_private_vulnerability_reporting": "true",
        "enable_secret_scanning_push_protection": "true",
    },
    "pre_flight_scan": {
        "scan_for_secrets":  "true",
        "scan_for_emails":   "true",
        "scan_for_todos":    "true",
        "max_file_size_mb":  "100",
        "trufflehog_mode":   "auto",
        "banned_strings":    "",
        "scan_email_history":    "true",
        "exclude_emails":        "",
        "warn_ai_context_files": "true",
        "scan_exclude_paths":    "",
    },
    # Tool-run behavior, never repo state — must stay out of any future
    # fix-enforcement / settings-tier logic (same category as pre_flight_scan).
    "git_transport": {
        "mode": "auto",
    },
}


def _default_config_path():
    """Return the first existing config file, or the XDG path as fallback."""
    # Check current working directory first
    cwd_path = Path.cwd() / CONFIG_FILENAME
    if cwd_path.is_file():
        return cwd_path

    # XDG_CONFIG_HOME (defaults to ~/.config)
    xdg_home = os.environ.get("XDG_CONFIG_HOME", "")
    if xdg_home:
        xdg_path = Path(xdg_home) / "gh-safe-repo" / CONFIG_FILENAME
    else:
        xdg_path = Path.home() / ".config" / "gh-safe-repo" / CONFIG_FILENAME
    return xdg_path


class ConfigManager:
    def __init__(self, config_path=None, *, require_exists=False):
        self._require_exists = require_exists
        self._path = Path(config_path) if config_path else _default_config_path()
        self._config = configparser.ConfigParser()
        self._load()

    @property
    def config_source(self) -> str:
        """Human-readable description of which config was loaded."""
        return self._config_source

    def _load(self):
        # Seed with safe defaults
        for section, values in SAFE_DEFAULTS.items():
            self._config[section] = values

        # Override with user config if it exists
        if self._path.exists():
            try:
                self._config.read(self._path)
                self._config_source = str(self._path)
            except configparser.Error as e:
                raise ConfigError(f"Failed to parse config at {self._path}: {e}")
        elif self._require_exists:
            raise ConfigError(f"Config file not found: {self._path}")
        else:
            self._config_source = "built-in defaults"

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
        settings = dict(self._config[section])

        merge_keys = ("allow_squash_merge", "allow_merge_commit", "allow_rebase_merge")
        if all(settings.get(k, "true").lower() == "false" for k in merge_keys):
            raise ConfigError(
                "Config disables all merge strategies "
                "(allow_squash_merge, allow_merge_commit, allow_rebase_merge). "
                "GitHub requires at least one to be enabled."
            )

        return settings

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
