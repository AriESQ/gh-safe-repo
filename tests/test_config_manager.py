"""Tests for config_manager.py."""

import tempfile
import textwrap
from pathlib import Path

import pytest
from lib.config_manager import ConfigManager
from lib.errors import ConfigError


class TestConfigManagerDefaults:
    def setup_method(self):
        # Use a non-existent path so only defaults are loaded
        self.config = ConfigManager(config_path="/tmp/nonexistent-gh-safe-repo.ini")

    def test_has_wiki_default_false(self):
        assert self.config.getbool("repo", "has_wiki") is False

    def test_has_projects_default_false(self):
        assert self.config.getbool("repo", "has_projects") is False

    def test_delete_branch_on_merge_default_true(self):
        assert self.config.getbool("repo", "delete_branch_on_merge") is True

    def test_workflow_permissions_default_read(self):
        assert self.config.get("actions", "default_workflow_permissions") == "read"

    def test_can_approve_prs_default_false(self):
        assert self.config.getbool("actions", "can_approve_pull_request_reviews") is False

    def test_private_default_true(self):
        assert self.config.getbool("repo", "private") is True


class TestConfigManagerOverrides:
    def test_apply_overrides_bool(self):
        config = ConfigManager(config_path="/tmp/nonexistent-gh-safe-repo.ini")
        assert config.getbool("repo", "has_wiki") is False
        config.apply_overrides({("repo", "has_wiki"): "true"})
        assert config.getbool("repo", "has_wiki") is True

    def test_apply_overrides_string(self):
        config = ConfigManager(config_path="/tmp/nonexistent-gh-safe-repo.ini")
        config.apply_overrides({("actions", "default_workflow_permissions"): "write"})
        assert config.get("actions", "default_workflow_permissions") == "write"


class TestConfigManagerFileLoading:
    def test_user_config_overrides_defaults(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ini", delete=False) as f:
            f.write(textwrap.dedent("""\
                [repo]
                has_wiki = true
                delete_branch_on_merge = false
            """))
            path = f.name

        config = ConfigManager(config_path=path)
        assert config.getbool("repo", "has_wiki") is True
        assert config.getbool("repo", "delete_branch_on_merge") is False
        # Other defaults remain
        assert config.getbool("repo", "has_projects") is False

    def test_invalid_config_raises(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ini", delete=False) as f:
            f.write("[invalid\n")
            path = f.name

        with pytest.raises(ConfigError):
            ConfigManager(config_path=path)


class TestConfigManagerSettings:
    def setup_method(self):
        self.config = ConfigManager(config_path="/tmp/nonexistent-gh-safe-repo.ini")

    def test_repo_settings_returns_dict(self):
        settings = self.config.repo_settings()
        assert isinstance(settings, dict)
        assert "has_wiki" in settings

    def test_actions_settings_returns_dict(self):
        settings = self.config.actions_settings()
        assert isinstance(settings, dict)
        assert "default_workflow_permissions" in settings
