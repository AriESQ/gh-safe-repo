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


# ---------------------------------------------------------------------------
# Three-way merge metadata for `fix` / `audit` (docs/2026-03-28-user-stories.md)
#
# Intrinsic properties of the settings themselves, not of any repo or run.
# The policy that uses them lives in commands/fix.py:apply_merge_policy().
# ---------------------------------------------------------------------------

# Config sections that describe repo state and take part in the merge.
# [pre_flight_scan] and [git_transport] are tool-run behaviour and never do.
MERGE_SECTIONS = ("repo", "actions", "branch_protection", "tag_protection", "security")

SECURITY = "security"
PREFERENCE = "preference"

# Default tier for a section; SETTING_TIERS overrides per key.
_SECTION_TIERS = {
    "repo": PREFERENCE,
    "actions": SECURITY,
    "branch_protection": SECURITY,
    "tag_protection": SECURITY,
    "security": SECURITY,
}

# (section, key) → tier. Keyed by the *plan row* a plugin emits, i.e. the
# Change.category value and Change.key; for the security plugin those are its
# plan keys (dependabot_alerts, ...) rather than the enable_* config keys, so
# both spellings are listed. Keys a plugin emits that are absent here fall
# back to the section tier (`setting_tier()`).
SETTING_TIERS = {
    # [repo] — all preference: visibility, wiki, merge strategy are choices.
    ("repo", "private"): PREFERENCE,
    ("repo", "delete_branch_on_merge"): PREFERENCE,
    ("repo", "allow_squash_merge"): PREFERENCE,
    ("repo", "allow_merge_commit"): PREFERENCE,
    ("repo", "allow_rebase_merge"): PREFERENCE,
    ("repo", "auto_init"): PREFERENCE,
    ("repo", "has_wiki"): PREFERENCE,
    ("repo", "has_issues"): PREFERENCE,
    ("repo", "has_projects"): PREFERENCE,
    # [actions] — enforcement is security; whether Actions runs at all is not.
    ("actions", "enabled"): PREFERENCE,
    ("actions", "allowed_actions"): SECURITY,
    ("actions", "github_owned_allowed"): SECURITY,
    ("actions", "verified_allowed"): SECURITY,
    ("actions", "patterns_allowed"): SECURITY,
    ("actions", "sha_pinning_required"): SECURITY,
    ("actions", "default_workflow_permissions"): SECURITY,
    ("actions", "can_approve_pull_request_reviews"): SECURITY,
    ("actions", "fork_pr_approval_policy"): SECURITY,
    # [branch_protection] — structural protection is security; policy knobs
    # (who it applies to, which API) are preference.
    ("branch_protection", "protected_branch"): PREFERENCE,
    ("branch_protection", "require_pull_request"): SECURITY,
    ("branch_protection", "required_approving_reviews"): SECURITY,
    ("branch_protection", "dismiss_stale_reviews"): SECURITY,
    ("branch_protection", "require_conversation_resolution"): PREFERENCE,
    ("branch_protection", "enforce_admins"): PREFERENCE,
    ("branch_protection", "allow_force_pushes"): SECURITY,
    ("branch_protection", "allow_deletions"): SECURITY,
    ("branch_protection", "use_rulesets"): PREFERENCE,
    # [tag_protection] — all security.
    ("tag_protection", "protected_tags"): SECURITY,
    ("tag_protection", "prevent_tag_deletion"): SECURITY,
    ("tag_protection", "prevent_tag_update"): SECURITY,
    # [security] — all security (config keys, then the plugin's plan keys).
    ("security", "enable_dependabot_alerts"): SECURITY,
    ("security", "enable_dependabot_security_updates"): SECURITY,
    ("security", "enable_private_vulnerability_reporting"): SECURITY,
    ("security", "enable_secret_scanning_push_protection"): SECURITY,
    ("security", "dependabot_alerts"): SECURITY,
    ("security", "dependabot_security_updates"): SECURITY,
    ("security", "private_vulnerability_reporting"): SECURITY,
    ("security", "secret_scanning"): SECURITY,
    ("security", "enable_dependency_graph"): SECURITY,
}

# Which way is "stricter", for every security-tier setting that can appear
# as a plan row. Three shapes:
#   True / False  — a boolean whose *strict* value is this one
#   "higher"      — a number; larger is stricter
#   "subset"      — a list; fewer entries is stricter (strict subset)
#   (a, b, c)     — an enum ordered weakest → strictest
# `protected_tags` is scope (which tags), not strictness, and has no direction.
SAFE_DIRECTIONS = {
    # local_only (only actions defined in this repo) admits less third-party
    # code than selected (GitHub-owned + verified + patterns).
    ("actions", "allowed_actions"): ("all", "selected", "local_only"),
    ("actions", "github_owned_allowed"): False,
    ("actions", "verified_allowed"): False,
    ("actions", "patterns_allowed"): "subset",
    ("actions", "sha_pinning_required"): True,
    ("actions", "default_workflow_permissions"): ("write", "read"),
    ("actions", "can_approve_pull_request_reviews"): False,
    ("actions", "fork_pr_approval_policy"): (
        "first_time_contributors_new_to_github",
        "first_time_contributors",
        "all_external_contributors",
    ),
    ("branch_protection", "require_pull_request"): True,
    ("branch_protection", "required_approving_reviews"): "higher",
    ("branch_protection", "dismiss_stale_reviews"): True,
    ("branch_protection", "allow_force_pushes"): False,
    ("branch_protection", "allow_deletions"): False,
    ("tag_protection", "prevent_tag_deletion"): True,
    ("tag_protection", "prevent_tag_update"): True,
    ("security", "enable_dependabot_alerts"): True,
    ("security", "enable_dependabot_security_updates"): True,
    ("security", "enable_private_vulnerability_reporting"): True,
    ("security", "enable_secret_scanning_push_protection"): True,
    ("security", "dependabot_alerts"): True,
    ("security", "dependabot_security_updates"): True,
    ("security", "private_vulnerability_reporting"): True,
    ("security", "secret_scanning"): True,
    ("security", "enable_dependency_graph"): True,
}


def setting_tier(section, key):
    """Tier of a plan row: "security", "preference", or None outside MERGE_SECTIONS."""
    if section not in MERGE_SECTIONS:
        return None
    return SETTING_TIERS.get((section, key), _SECTION_TIERS[section])


def _as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes")


def is_stricter(section, key, value, than):
    """True if ``value`` is a stricter posture than ``than`` for this setting.

    False when they are equal, when ``value`` is weaker, or when the setting
    has no known direction (the policy then treats the human's value as
    not-stricter and applies ours, without the ⚠ weakened marker — see
    ``has_direction()``).
    """
    direction = SAFE_DIRECTIONS.get((section, key))
    if direction is None:
        return False
    if direction == "higher":
        try:
            return int(value) > int(than)
        except (TypeError, ValueError):
            return False
    if direction == "subset":
        a, b = set(value or ()), set(than or ())
        return a < b
    if isinstance(direction, tuple):
        try:
            return direction.index(value) > direction.index(than)
        except ValueError:
            return False
    # boolean: the strict value is `direction`
    return _as_bool(value) == direction and _as_bool(than) != direction


def has_direction(section, key):
    return (section, key) in SAFE_DIRECTIONS


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

        # Sections the user's own file names: explicit intent, as opposed to
        # values that fell through from SAFE_DEFAULTS. The merge policy in
        # fix/audit enforces a declared section even where a human changed
        # the repo away from GitHub's default.
        self._declared_sections = frozenset()

        # Override with user config if it exists
        if self._path.exists():
            try:
                user = configparser.ConfigParser()
                user.read(self._path)
                self._declared_sections = frozenset(user.sections())
                self._config.read(self._path)
                self._config_source = str(self._path)
            except configparser.Error as e:
                raise ConfigError(f"Failed to parse config at {self._path}: {e}")
        elif self._require_exists:
            raise ConfigError(f"Config file not found: {self._path}")
        else:
            self._config_source = "built-in defaults"

    @property
    def declared_sections(self) -> frozenset:
        """Sections present in the user's INI file (empty with no file)."""
        return self._declared_sections

    def is_declared(self, section) -> bool:
        return section in self._declared_sections

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
