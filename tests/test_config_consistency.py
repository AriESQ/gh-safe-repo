"""Tests that SAFE_DEFAULTS, gh-safe-repo.ini.example, and config loading stay in sync."""

import configparser
import re
from pathlib import Path

import pytest

from gh_safe_repo.config_manager import SAFE_DEFAULTS, ConfigManager

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_INI = ROOT / "gh-safe-repo.ini.example"
SOURCE_DIR = ROOT / "gh_safe_repo"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_example_ini():
    """Parse gh-safe-repo.ini.example and return {section: {key: value}}."""
    assert EXAMPLE_INI.exists(), f"Example config not found: {EXAMPLE_INI}"
    parser = configparser.ConfigParser()
    parser.read(EXAMPLE_INI)
    return {section: dict(parser[section]) for section in parser.sections()}


def _flip_value(value: str) -> str:
    """Return a clearly different value for testing config overrides."""
    low = value.strip().lower()
    if low == "true":
        return "false"
    if low == "false":
        return "true"
    if low.isdigit():
        return str(int(low) + 1)
    if low == "":
        return "test-sentinel"
    return value + "-changed"


def _all_config_reads() -> list[tuple[str, str, bool]]:
    """Static analysis: find all config.get/getbool calls under gh_safe_repo/.

    Returns list of (section, key, has_fallback) tuples.
    """
    pattern = re.compile(
        r"""config\.(?:get|getbool)\(\s*["']([^"']+)["']\s*,\s*["']([^"']+)["']"""
        r"""(?:\s*,\s*fallback\s*=)?"""
    )
    results = []
    for py_file in SOURCE_DIR.rglob("*.py"):
        text = py_file.read_text()
        for m in pattern.finditer(text):
            section, key = m.group(1), m.group(2)
            # Check if this specific call has a fallback by finding the full call
            call_start = m.start()
            # Find the closing paren
            depth = 0
            call_text = ""
            for i, ch in enumerate(text[call_start:], start=call_start):
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        call_text = text[call_start:i + 1]
                        break
            has_fallback = "fallback=" in call_text
            results.append((section, key, has_fallback))
    return results


# ---------------------------------------------------------------------------
# Group 1: SAFE_DEFAULTS ↔ gh-safe-repo.ini.example parity
# ---------------------------------------------------------------------------

class TestExampleMatchesDefaults:
    """Ensure gh-safe-repo.ini.example stays in sync with SAFE_DEFAULTS."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.example = _parse_example_ini()

    def test_example_sections_match_defaults(self):
        assert set(self.example.keys()) == set(SAFE_DEFAULTS.keys()), (
            f"Section mismatch.\n"
            f"  In example only: {set(self.example) - set(SAFE_DEFAULTS)}\n"
            f"  In defaults only: {set(SAFE_DEFAULTS) - set(self.example)}"
        )

    @pytest.mark.parametrize("section", list(SAFE_DEFAULTS.keys()))
    def test_example_keys_match_defaults(self, section):
        example_keys = set(self.example.get(section, {}).keys())
        default_keys = set(SAFE_DEFAULTS[section].keys())
        assert example_keys == default_keys, (
            f"[{section}] key mismatch.\n"
            f"  In example only: {example_keys - default_keys}\n"
            f"  In defaults only: {default_keys - example_keys}"
        )

    @pytest.mark.parametrize(
        "section,key",
        [
            (section, key)
            for section, values in SAFE_DEFAULTS.items()
            for key in values
        ],
    )
    def test_example_values_match_defaults(self, section, key):
        example_val = self.example.get(section, {}).get(key)
        default_val = SAFE_DEFAULTS[section][key]
        assert example_val == default_val, (
            f"[{section}] {key}: example has {example_val!r}, "
            f"SAFE_DEFAULTS has {default_val!r}"
        )


# ---------------------------------------------------------------------------
# Group 2: Config file values are picked up
# ---------------------------------------------------------------------------

_ALL_KEYS = [
    (section, key)
    for section, values in SAFE_DEFAULTS.items()
    for key in values
]


class TestConfigFilePickup:
    """Every key in SAFE_DEFAULTS can be overridden via a config file."""

    @pytest.mark.parametrize("section,key", _ALL_KEYS)
    def test_each_key_overridable_via_config_file(self, section, key, tmp_path):
        default_val = SAFE_DEFAULTS[section][key]
        new_val = _flip_value(default_val)

        ini = tmp_path / "test.ini"
        ini.write_text(f"[{section}]\n{key} = {new_val}\n")

        config = ConfigManager(config_path=str(ini))
        actual = config.get(section, key)
        assert actual == new_val, (
            f"[{section}] {key}: expected {new_val!r} from config file, "
            f"got {actual!r}"
        )

    def test_unset_keys_retain_defaults(self, tmp_path):
        """A config file that sets one key should not disturb others."""
        ini = tmp_path / "test.ini"
        ini.write_text("[repo]\nhas_wiki = true\n")

        config = ConfigManager(config_path=str(ini))
        # The overridden key
        assert config.get("repo", "has_wiki") == "true"
        # An untouched key in the same section
        assert config.get("repo", "private") == SAFE_DEFAULTS["repo"]["private"]
        # A key in a different section
        assert (
            config.get("actions", "allowed_actions")
            == SAFE_DEFAULTS["actions"]["allowed_actions"]
        )


# ---------------------------------------------------------------------------
# Group 3: CLI overrides take precedence
# ---------------------------------------------------------------------------

class TestCLIOverrides:
    """apply_overrides() wins over both defaults and config file values."""

    def test_cli_override_trumps_default(self):
        config = ConfigManager(config_path="/tmp/nonexistent-gh-safe-repo.ini")
        assert config.getbool("repo", "private") is True  # default
        config.apply_overrides({("repo", "private"): "false"})
        assert config.getbool("repo", "private") is False

    def test_cli_override_trumps_config_file(self, tmp_path):
        ini = tmp_path / "test.ini"
        ini.write_text("[repo]\nprivate = true\n")

        config = ConfigManager(config_path=str(ini))
        assert config.getbool("repo", "private") is True
        config.apply_overrides({("repo", "private"): "false"})
        assert config.getbool("repo", "private") is False

    def test_override_creates_section_if_missing(self):
        config = ConfigManager(config_path="/tmp/nonexistent-gh-safe-repo.ini")
        config.apply_overrides({("custom_section", "custom_key"): "value"})
        assert config.get("custom_section", "custom_key") == "value"

    @pytest.mark.parametrize("section,key", _ALL_KEYS)
    def test_every_key_overridable_via_apply_overrides(self, section, key):
        default_val = SAFE_DEFAULTS[section][key]
        new_val = _flip_value(default_val)

        config = ConfigManager(config_path="/tmp/nonexistent-gh-safe-repo.ini")
        config.apply_overrides({(section, key): new_val})
        assert config.get(section, key) == new_val


# ---------------------------------------------------------------------------
# Group 4: All consumed config keys have a default
# ---------------------------------------------------------------------------

class TestAllConsumedKeysHaveDefaults:
    """Every config.get()/getbool() call in the source either reads a key
    present in SAFE_DEFAULTS or specifies an explicit fallback=."""

    def test_every_consumed_key_has_default_or_fallback(self):
        reads = _all_config_reads()
        assert len(reads) > 0, "Static analysis found no config reads — check the regex"

        missing = []
        for section, key, has_fallback in reads:
            in_defaults = (
                section in SAFE_DEFAULTS and key in SAFE_DEFAULTS[section]
            )
            if not in_defaults and not has_fallback:
                missing.append(f"  [{section}] {key}")

        assert not missing, (
            "Config keys read without SAFE_DEFAULTS entry or fallback=:\n"
            + "\n".join(missing)
        )
