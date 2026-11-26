"""Tests for tools/release_notes.py (#129), the release workflow's checks.

Runs on the real CHANGELOG.md for two past releases, plus small fixtures for
each failure. No network, no git.
"""

import importlib.util
import os
import textwrap

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "tools", "release_notes.py")

_spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
release_notes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_notes)

ReleaseError = release_notes.ReleaseError


def read(name):
    with open(os.path.join(REPO_ROOT, name), encoding="utf-8") as f:
        return f.read()


def pyproject(version, name="gh-safe-repo"):
    return textwrap.dedent(f"""\
        [build-system]
        requires = ["hatchling"]

        [project]
        name = "{name}"
        version = "{version}"
        """)


CHANGELOG = textwrap.dedent("""\
    # Changelog

    ## [Unreleased]

    ### Added
    - Something new.

    ## [1.2.0] - 2026-11-20

    ### Fixed
    - A bug.

    ## [1.1.0]

    ### Added
    - Undated.

    ## [1.0.1] - 2026-11-01

    ## [1.0.0] - 2026-10-01

    Initial.
    """)


class TestRealChangelog:
    """The extraction against CHANGELOG.md as it stands (v0.14.0, v0.13.0)."""

    def test_v0_14_0(self):
        notes = release_notes.build_notes(
            "v0.14.0", changelog=read("CHANGELOG.md"),
            pyproject=pyproject("0.14.0"), repo="AriESQ/gh-safe-repo",
        )
        assert notes.startswith("### Changed\n- `scan --json`: the `engine` object")
        assert "## [" not in notes
        assert "## [0.13.0]" not in notes and "`-v` / `--version`" not in notes
        assert notes.endswith(
            "**Full changelog**: https://github.com/AriESQ/gh-safe-repo/"
            "compare/v0.13.0...v0.14.0\n"
        )

    def test_v0_13_0(self):
        notes = release_notes.build_notes(
            "v0.13.0", changelog=read("CHANGELOG.md"),
            pyproject=pyproject("0.13.0"), repo="AriESQ/gh-safe-repo",
        )
        body, link = notes.rsplit("\n\n", 1)
        assert body.startswith("### Added\n- `-v` / `--version` print")
        assert body.endswith("offer the new flags.")
        assert link == (
            "**Full changelog**: https://github.com/AriESQ/gh-safe-repo/"
            "compare/v0.12.0...v0.13.0\n"
        )

    def test_current_pyproject_and_lock_agree(self):
        # The checked-in files must pass for the version they declare.
        version = release_notes.pyproject_version(read("pyproject.toml"))
        assert version
        assert release_notes.uv_lock_version(read("uv.lock"), "gh-safe-repo") == version


class TestChecks:
    def build(self, tag="v1.2.0", version="1.2.0", changelog=CHANGELOG, **kw):
        return release_notes.build_notes(
            tag, changelog=changelog, pyproject=pyproject(version), **kw,
        )

    def test_section_body_without_heading(self):
        assert self.build() == "### Fixed\n- A bug.\n"

    def test_compare_link_skips_unreleased_and_uses_previous_section(self):
        notes = self.build("v1.0.1", "1.0.1", changelog=CHANGELOG.replace(
            "## [1.0.1] - 2026-11-01\n", "## [1.0.1] - 2026-11-01\n\n- Patch.\n"),
            repo="o/r")
        assert notes.endswith("compare/v1.0.0...v1.0.1\n")

    def test_oldest_release_has_no_compare_link(self):
        assert "Full changelog" not in self.build("v1.0.0", "1.0.0", repo="o/r")

    @pytest.mark.parametrize("tag", ["1.2.0", "v1.2", "v1.2.0-rc1", "v1.2.0.1", "release-1"])
    def test_bad_tag(self, tag):
        with pytest.raises(ReleaseError, match="not a release tag"):
            self.build(tag)

    def test_tag_does_not_match_pyproject(self):
        with pytest.raises(ReleaseError, match="does not match pyproject.toml version '1.1.9'"):
            self.build(version="1.1.9")

    def test_pyproject_without_version(self):
        with pytest.raises(ReleaseError, match="no \\[project\\] version"):
            release_notes.build_notes("v1.2.0", changelog=CHANGELOG, pyproject="[project]\n")

    def test_version_outside_project_table_is_ignored(self):
        text = '[tool.x]\nversion = "1.2.0"\n[project]\nversion = "1.0.0"\n'
        assert release_notes.pyproject_version(text) == "1.0.0"

    def test_uv_lock_mismatch(self):
        lock = '[[package]]\nname = "other"\nversion = "1.2.0"\n\n' \
               '[[package]]\nname = "gh-safe-repo"\nversion = "1.1.0"\n'
        with pytest.raises(ReleaseError, match="uv.lock records gh-safe-repo '1.1.0'"):
            self.build(uv_lock=lock)

    def test_uv_lock_match(self):
        lock = '[[package]]\nname = "gh-safe-repo"\nversion = "1.2.0"\n'
        assert self.build(uv_lock=lock)

    def test_missing_section_hints_at_unreleased(self):
        with pytest.raises(ReleaseError, match=r"no section for \[1\.3\.0\].*Unreleased"):
            self.build("v1.3.0", "1.3.0")

    def test_missing_section_without_unreleased(self):
        with pytest.raises(ReleaseError) as e:
            self.build("v1.3.0", "1.3.0", changelog="## [1.2.0] - 2026-11-20\n\n- x\n")
        assert "Unreleased" not in str(e.value)

    def test_undated_section(self):
        with pytest.raises(ReleaseError, match="has no date"):
            self.build("v1.1.0", "1.1.0")

    def test_invalid_date(self):
        with pytest.raises(ReleaseError, match="invalid date"):
            self.build(changelog=CHANGELOG.replace("2026-11-20", "2026-13-45"))

    def test_empty_section(self):
        with pytest.raises(ReleaseError, match="is empty"):
            self.build("v1.0.1", "1.0.1")


class TestMain:
    def files(self, tmp_path, version="1.2.0", lock=True):
        (tmp_path / "CHANGELOG.md").write_text(CHANGELOG)
        (tmp_path / "pyproject.toml").write_text(pyproject(version))
        args = ["--changelog", str(tmp_path / "CHANGELOG.md"),
                "--pyproject", str(tmp_path / "pyproject.toml"),
                "--uv-lock", str(tmp_path / "uv.lock")]
        if lock:
            (tmp_path / "uv.lock").write_text(
                f'[[package]]\nname = "gh-safe-repo"\nversion = "{version}"\n')
        return args

    def test_writes_output_file(self, tmp_path, capsys):
        out = tmp_path / "notes.md"
        assert release_notes.main(["v1.2.0", *self.files(tmp_path), "-o", str(out)]) == 0
        assert out.read_text() == "### Fixed\n- A bug.\n"
        assert capsys.readouterr().out == ""

    def test_stdout_and_missing_lock_is_fine(self, tmp_path, capsys):
        assert release_notes.main(["v1.2.0", *self.files(tmp_path, lock=False)]) == 0
        assert capsys.readouterr().out == "### Fixed\n- A bug.\n"

    def test_failure_exits_1_with_reason_and_no_output(self, tmp_path, capsys):
        out = tmp_path / "notes.md"
        rc = release_notes.main(["v9.9.9", *self.files(tmp_path), "-o", str(out)])
        assert rc == 1
        captured = capsys.readouterr()
        assert captured.err.startswith("release_notes: tag v9.9.9 does not match")
        assert not out.exists()

    def test_unreadable_changelog_exits_1(self, tmp_path, capsys):
        args = self.files(tmp_path)
        args[1] = str(tmp_path / "missing.md")
        assert release_notes.main(["v1.2.0", *args]) == 1
        assert "release_notes:" in capsys.readouterr().err
