"""Terminal-injection tests (#125): untrusted text is escaped where it is printed.

`safe_display()` itself, then every human-readable output path that prints a
value from the scanned repo, from git or from the GitHub API. The shared
assertion is `assert_inert()`: nothing in the output may be a control or
invisible formatting character, apart from the newlines and colour codes the
tool writes itself.
"""

import json
import subprocess
import unicodedata
from unittest.mock import MagicMock, patch

import pytest

from gh_safe_repo.cli import main
from gh_safe_repo.commands import _common, create
from gh_safe_repo.commands._common import (
    _print_findings,
    _scan_findings_prompt,
    error,
    format_findings_json,
    print_plan,
    print_success,
    print_verification,
    warn,
)
from gh_safe_repo.config_manager import ConfigManager
from gh_safe_repo.diff import Change, ChangeCategory, ChangeType, Plan, PlanWarning
from gh_safe_repo.display import safe_display
from gh_safe_repo.errors import APIError
from gh_safe_repo.git_transport import GitTransport
from gh_safe_repo.github_client import LocalWiring
from gh_safe_repo.plugins.branch_protection import BranchProtectionPlugin
from gh_safe_repo.security_scanner import (
    Finding,
    FindingCategory,
    SecurityScanner,
    Severity,
    format_findings,
)
from gh_safe_repo.verify import Check, Verification

ESC = "\x1b"
CSI_ERASE = f"{ESC}[2K"                                   # erase the line
OSC8 = f"{ESC}]8;;https://example.com/evil{ESC}\\click{ESC}]8;;{ESC}\\"  # hyperlink, ST-terminated
OSC52 = f"{ESC}]52;c;ZXZpbA==\x07"                        # clipboard write, BEL-terminated
C1_CSI = "\x9b31m"                                        # 8-bit CSI
BIDI = "\u202egnp.exe"                                    # right-to-left override
FORGED = "\r  No issues found."                           # overwrite the line

# One of everything, for values that end up in a single output field.
HOSTILE = (
    f"a{CSI_ERASE}b{OSC8}c{OSC52}d\re\x08f\x00g\x7fh{C1_CSI}i{BIDI}"
    "j\u2066k\u200bl\u2028m\u2029n\ufeffo"
)

# The tool's own formatting, switched on by the `colour` fixture.
TOOL_CODES = {
    "_RESET": f"{ESC}[0m", "_BOLD": f"{ESC}[1m", "_DIM": f"{ESC}[2m", "_RED": f"{ESC}[31m",
    "_GREEN": f"{ESC}[32m", "_YELLOW": f"{ESC}[33m", "_CYAN": f"{ESC}[36m",
}

_UNSAFE = {"Cc", "Cf", "Cs", "Zl", "Zp"}


def assert_inert(text, *, allow=("\n",), tool_codes=()):
    """No raw control/formatting characters except newlines and `tool_codes`."""
    for code in sorted(tool_codes, key=len, reverse=True):
        text = text.replace(code, "")
    bad = sorted({ch for ch in text if ch not in allow and unicodedata.category(ch) in _UNSAFE})
    assert not bad, f"raw control characters in output: {bad!r}\n{text!r}"


@pytest.fixture
def colour(monkeypatch):
    """Turn the tool's own colour codes on, as on a terminal."""
    for name, code in TOOL_CODES.items():
        monkeypatch.setattr(_common, name, code)
    return tuple(TOOL_CODES.values())


def make_config(tmp_path=None):
    return ConfigManager(config_path="/tmp/nonexistent-gh-safe-repo.ini")


def finding(severity, **kw):
    base = dict(
        severity=severity, category=FindingCategory.EMAIL, file_path="a.py",
        line_number=3, rule="Email address", match="example@example.com",
    )
    base.update(kw)
    return Finding(**base)


def hostile_findings():
    return [
        finding(sev, file_path=f"dir/{HOSTILE}.py", rule=f"rule {HOSTILE}",
                match=f"match {HOSTILE}\nsecond {FORGED}",
                commit=f"abc{CSI_ERASE}", timestamp=f"2026-01-01{OSC52}")
        for sev in (Severity.CRITICAL, Severity.WARNING, Severity.INFO)
    ]


class TestSafeDisplay:
    @pytest.mark.parametrize("raw, shown", [
        (f"{ESC}[31mred{ESC}[0m", "\\x1b[31mred\\x1b[0m"),
        (CSI_ERASE, "\\x1b[2K"),
        (f"{ESC}]0;title\x07", "\\x1b]0;title\\x07"),
        (OSC8, "\\x1b]8;;https://example.com/evil\\x1b\\click\\x1b]8;;\\x1b\\"),
        ("a\rb", "a\\x0db"),
        ("a\x08b", "a\\x08b"),
        ("a\x00b", "a\\x00b"),
        ("a\x7fb", "a\\x7fb"),
        ("a\x9b31mb", "a\\x9b31mb"),
        ("a\x85b", "a\\x85b"),
        ("a\u202eb", "a\\u202eb"),
        ("a\u2066b\u2069", "a\\u2066b\\u2069"),
        ("a\u200bb", "a\\u200bb"),
        ("a\u2028b\u2029c", "a\\u2028b\\u2029c"),
        ("\ufeffa", "\\ufeffa"),
        ("a\U000e0001b", "a\\U000e0001b"),
    ])
    def test_escapes_visibly(self, raw, shown):
        assert safe_display(raw) == shown
        assert_inert(safe_display(raw), allow=())

    def test_newline_escaped_unless_multiline(self):
        assert safe_display("a\nb") == "a\\x0ab"
        assert safe_display("a\nb", multiline=True) == "a\nb"
        # Only "\n" is kept: CR and the Unicode line breaks still escape.
        assert safe_display("a\r\nb\u2028c", multiline=True) == "a\\x0d\nb\\u2028c"

    def test_tab_and_printable_text_unchanged(self):
        for text in ("plain.py", "a\tb", "naïve/日本語/🎉 file.py", "back\\slash", ""):
            assert safe_display(text) == text

    def test_undecodable_file_name_byte_shown_as_the_byte(self):
        # os.walk / `git ls-files -z` decode with surrogateescape; print()
        # would raise UnicodeEncodeError on the raw surrogate.
        name = b"a\x9b31m\xffb".decode("utf-8", errors="surrogateescape")
        shown = safe_display(name)
        assert shown == "a\\x9b31m\\xffb"
        shown.encode("utf-8")

    def test_other_lone_surrogate(self):
        assert safe_display("a\ud800b") == "a\\ud800b"

    def test_non_string_values(self):
        assert safe_display(True) == "True"
        assert safe_display(["a\x1b"]) == "['a\\x1b']"

    def test_everything_at_once(self):
        shown = safe_display(HOSTILE)
        assert_inert(shown, allow=())
        assert "\\x1b[2K" in shown and "\\u202e" in shown and "\\x9b" in shown


class TestFindingsReport:
    def test_print_findings_escapes_every_field(self, capsys):
        assert _print_findings(hostile_findings(), make_config()) is True
        out = capsys.readouterr().out
        assert_inert(out)
        assert "\\x1b[2K" in out            # visible, not dropped
        assert "\\x1b]8;;" in out
        assert "\\x9b31m" in out
        assert "\\u202egnp.exe" in out
        assert out.count("[CRITICAL]") == 1 and out.count("[WARNING]") == 1

    def test_forged_clean_line_cannot_start_a_line(self, capsys):
        f = finding(Severity.CRITICAL, file_path=f"x{FORGED}\n  No issues found.",
                    match=f"hint\rNo issues found.\u2028No issues found.")
        _print_findings([f], make_config())
        out = capsys.readouterr().out
        assert_inert(out)
        assert not any(line.strip().startswith("No issues found") for line in out.splitlines())

    def test_tool_colour_still_emitted(self, capsys, colour):
        _print_findings(hostile_findings(), make_config())
        out = capsys.readouterr().out
        assert f"{TOOL_CODES['_RED']}[CRITICAL]{TOOL_CODES['_RESET']}" in out
        assert TOOL_CODES["_DIM"] in out
        assert_inert(out, tool_codes=colour)

    def test_view_hint_is_escaped(self, capsys):
        f = finding(Severity.WARNING, file_path=f"a{CSI_ERASE}.py", line_number=7)
        _print_findings([f], make_config())
        out = capsys.readouterr().out
        assert "View with: sed -n '3,11p' 'a\\x1b[2K.py'" in out
        assert_inert(out)

    def test_format_findings(self):
        out = format_findings(hostile_findings())
        assert_inert(out)
        assert "\\x1b[2K" in out

    def test_skipped_directories_are_escaped(self, capsys):
        scanner = MagicMock()
        scanner.skipped_committed_dirs = [f"evil{CSI_ERASE}{FORGED}/node_modules"]
        _scan_findings_prompt(scanner, [], make_config(), warn_skipped_committed_dirs=True)
        out = capsys.readouterr().out
        assert_inert(out)
        assert "evil\\x1b[2K\\x0d" in out

    def test_json_keeps_raw_values_and_is_ascii(self):
        findings = hostile_findings()
        doc = format_findings_json(findings, [f"d{CSI_ERASE}"])
        # json.dumps (ensure_ascii default) escapes every control and
        # non-ASCII character, so the document cannot drive a terminal...
        assert doc.isascii()
        assert_inert(doc)
        # ...and the consumer still gets the exact original text back.
        payload = json.loads(doc)
        assert payload["findings"][0]["file_path"] == findings[0].file_path
        assert payload["findings"][0]["match"] == findings[0].match
        assert payload["skipped_committed_dirs"] == [f"d{CSI_ERASE}"]

    def test_json_survives_undecodable_file_name(self):
        name = b"a\x9bb".decode("utf-8", errors="surrogateescape")
        doc = format_findings_json([finding(Severity.INFO, file_path=name)])
        assert doc.isascii()
        assert json.loads(doc)["findings"][0]["file_path"] == name


def _scan_config(tmp_path):
    cfg = tmp_path.parent / f"{tmp_path.name}-off.ini"
    cfg.write_text("[pre_flight_scan]\ntrufflehog_mode = off\n")
    return str(cfg)


def _run_scan(tmp_path, *extra):
    argv = ["gh-safe-repo", "scan", str(tmp_path), *extra, "--config", _scan_config(tmp_path)]
    with patch("sys.argv", argv):
        with pytest.raises(SystemExit) as exc:
            main()
    return exc.value.code


class TestScanCommand:
    """Real files with hostile names and contents, through `scan`."""

    NAME = f"evil{CSI_ERASE}\r{ESC}]0;owned\x07\x08\u202e.txt"
    DIRNAME = f"dir{ESC}[1A\n  No issues found."

    def _populate(self, root):
        (root / self.NAME).write_text(
            f"contact example@example.com {CSI_ERASE}\n"
            f"TODO {OSC8} {OSC52} \x9b31m \u202e \x08\x08\x08 done\n",
            encoding="utf-8",
        )
        nested = root / self.DIRNAME
        nested.mkdir()
        (nested / "CLAUDE.md").write_text("notes\n")
        skipped = nested / "node_modules"
        skipped.mkdir()
        (skipped / "index.js").write_text("x\n")

    def test_human_report_is_inert(self, tmp_path, capsys):
        self._populate(tmp_path)
        assert _run_scan(tmp_path) == 1       # the AI context file is CRITICAL
        captured = capsys.readouterr()
        assert_inert(captured.out)
        assert_inert(captured.err)
        out = captured.out
        assert "evil\\x1b[2K\\x0d\\x1b]0;owned\\x07\\x08\\u202e.txt:1" in out   # email
        assert "TODO/FIXME/HACK/XXX comment in evil\\x1b[2K" in out
        assert "dir\\x1b[1A\\x0a  No issues found./CLAUDE.md" in out
        assert "scrub-ai-context.sh dir\\x1b[1A\\x0a" in out               # remediation hint
        assert "dir\\x1b[1A\\x0a  No issues found./node_modules/" in out   # skipped dir
        assert not any(l.strip() == "No issues found." for l in out.splitlines())

    def test_git_tracked_names_are_escaped(self, tmp_path, capsys):
        self._populate(tmp_path)
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True, capture_output=True)
        assert _run_scan(tmp_path) == 1
        captured = capsys.readouterr()
        assert_inert(captured.out)
        assert_inert(captured.err)
        assert "evil\\x1b[2K\\x0d" in captured.out

    def test_json_report_round_trips_raw_names(self, tmp_path, capsys):
        self._populate(tmp_path)
        assert _run_scan(tmp_path, "--json") == 1
        captured = capsys.readouterr()
        assert captured.out.isascii()
        assert_inert(captured.out)
        assert_inert(captured.err)
        payload = json.loads(captured.out)
        paths = {f["file_path"] for f in payload["findings"]}
        assert self.NAME in paths
        assert f"{self.DIRNAME}/CLAUDE.md" in paths
        assert payload["skipped_committed_dirs"] == [f"{self.DIRNAME}/node_modules"]

    def test_scan_path_itself_is_escaped(self, tmp_path, capsys):
        root = tmp_path / f"repo{CSI_ERASE}"
        root.mkdir()
        (root / "a.py").write_text("x = 1\n")
        assert _run_scan(root) == 0
        captured = capsys.readouterr()
        assert_inert(captured.out)
        assert "repo\\x1b[2K..." in captured.out


class TestProgressLine:
    def test_file_name_cannot_move_the_cursor(self, capsys):
        scanner = SecurityScanner(make_config(), progress=False)
        scanner._draw_progress(1, 2, f"a{FORGED}{CSI_ERASE}\u202e.py")
        err = capsys.readouterr().err
        # The line's own "\r" + erase-line prefix is the tool's formatting.
        assert err.startswith("\r\033[K")
        rest = err[len("\r\033[K"):]
        assert_inert(rest, allow=())
        assert "a\\x0d  No issues found.\\x1b[2K\\u202e.py" in rest

    def test_escaped_name_still_fits_the_width(self, capsys):
        scanner = SecurityScanner(make_config(), progress=False)
        with patch("shutil.get_terminal_size", return_value=MagicMock(columns=40)):
            scanner._draw_progress(1, 2, "\x1b" * 200)
        err = capsys.readouterr().err
        assert len(err) - len("\r\033[K") <= 39


class TestTruffleHogFields:
    def test_reported_fields_are_escaped(self, capsys):
        record = {
            "DetectorName": f"AWS{CSI_ERASE}{FORGED}",
            "SourceMetadata": {"Data": {"Git": {
                "file": f"src/{HOSTILE}.env", "line": 4,
                "commit": f"{ESC}[1A" + "0" * 7, "timestamp": f"2026-01-01 {OSC52}",
            }}},
        }
        scanner = SecurityScanner(make_config(), progress=False)
        scanner._trufflehog_mode = "native"
        scanner._discovery = {"method": "native", "version": "3.99.0"}
        proc = MagicMock()
        proc.communicate.return_value = (json.dumps(record) + "\n", None)
        proc.returncode = 1
        with patch("gh_safe_repo.security_scanner.subprocess.Popen", return_value=proc):
            findings = scanner._try_trufflehog("/nonexistent-gh-safe-repo-scan")
        assert len(findings) == 1 and ESC in findings[0].rule   # stored raw
        _print_findings(findings, make_config())
        out = capsys.readouterr().out
        assert_inert(out)
        assert "Secret detected by truffleHog (AWS\\x1b[2K\\x0d" in out
        assert "commit \\x1b[1A" in out


class TestMessages:
    def test_error_and_warn_escape_remote_text(self, capsys):
        stderr = f"remote: denied{CSI_ERASE}\r{OSC8}\nfatal: {BIDI}\x9b0m"
        error(f"git push failed: {stderr}")
        warn(f"Security settings failed: {stderr}")
        err = capsys.readouterr().err
        assert_inert(err)
        assert err.count("remote: denied\\x1b[2K\\x0d") == 2
        assert "\nfatal: \\u202egnp.exe\\x9b0m" in err      # line breaks kept

    def test_push_failure_recovery_block(self, capsys):
        e = APIError(f"git push failed to x: remote: {HOSTILE}")
        with pytest.raises(SystemExit) as exc:
            create._abort_on_push_failure(
                e, "me", "repo", [f"cd /tmp/{CSI_ERASE}", f"git push -u origin b{BIDI}:b{BIDI}"]
            )
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert_inert(err)
        assert "cd /tmp/\\x1b[2K" in err
        assert "Re-running `create` will fail" in err

    def test_git_debug_line(self, capsys):
        transport = GitTransport(protocol="https", source_dir="/tmp", debug=True)
        with patch("gh_safe_repo.git_transport.subprocess.run"):
            transport.run(["git", "-C", f"/tmp/{HOSTILE}", "status"])
        err = capsys.readouterr().err
        assert_inert(err)
        assert "[debug] git -C /tmp/a\\x1b[2K" in err

    def test_branch_protection_skip_line(self, capsys):
        client = MagicMock()
        client.repo_path.return_value = "/repos/me/repo/branches/x/protection"
        client.call_json.side_effect = APIError("nope", status_code=404)
        plugin = BranchProtectionPlugin.__new__(BranchProtectionPlugin)
        plugin.client, plugin.owner, plugin.repo = client, "me", "repo"
        plugin.branches = [f"feat{BIDI}\x9b0m"]
        plugin._apply_classic({
            "required_approving_reviews": 1, "dismiss_stale_reviews": True,
            "require_code_owner_reviews": False, "required_status_checks": None,
            "enforce_admins": False, "allow_force_pushes": False,
            "allow_deletions": False, "require_conversation_resolution": True,
        })
        err = capsys.readouterr().err
        assert_inert(err)
        assert "Branch 'feat\\u202egnp.exe\\x9b0m' not found" in err


class TestPlanAndBanners:
    def _plan(self):
        plan = Plan()
        plan.add(Change(type=ChangeType.ADD, category=ChangeCategory.REPO,
                        key="description", new=f"desc {HOSTILE}"))
        plan.add(Change(type=ChangeType.UPDATE, category=ChangeCategory.REPO,
                        key=f"topic{BIDI}", old=f"old{CSI_ERASE}", new="new", weakened=True))
        plan.add(Change(type=ChangeType.DELETE, category=ChangeCategory.REPO,
                        key="homepage", old=f"gone{OSC52}"))
        plan.add(Change(type=ChangeType.SKIP, category=ChangeCategory.REPO,
                        key="x", reason=f"why{FORGED}"))
        plan.add(Change(type=ChangeType.PRESERVE, category=ChangeCategory.REPO,
                        key="y", old=f"kept{CSI_ERASE}", reason=f"because{C1_CSI}"))
        plan.warnings.append(PlanWarning(
            kind="actions_blocked", message=f"1 action would be blocked{CSI_ERASE}",
            details={"branch": f"main{BIDI}", "actions": [{
                "uses": f"evil/action@v1{OSC8}", "workflow": f".github/workflows/ci{CSI_ERASE}.yml",
                "file": f".github/actions/x{FORGED}/action.yml", "line": 3,
                "reasons": [f"not whitelisted{C1_CSI}"], "pin": f"evil/action@abc{ESC}[0m",
            }]},
        ))
        return plan

    def test_plan_table_and_warnings(self, capsys):
        print_plan(self._plan())
        out = capsys.readouterr().out
        assert_inert(out)
        assert "desc a\\x1b[2Kb" in out
        assert "evil/action@v1\\x1b]8;;" in out
        assert "(branch main\\u202egnp.exe)" in out

    def test_plan_table_keeps_tool_colour(self, capsys, colour):
        print_plan(self._plan())
        out = capsys.readouterr().out
        assert f"{TOOL_CODES['_YELLOW']}⚠ weakened from default{TOOL_CODES['_RESET']}" in out
        assert_inert(out, tool_codes=colour)

    def test_verification_rows(self, capsys):
        verification = Verification([
            Check("local", f"tracking{BIDI}", "ok"),
            Check("code", f"branch main{CSI_ERASE}", "mismatch", expected=f"a{ESC}", actual=None,
                  remediation=f"git push origin main{FORGED}"),
            Check("repo", f"x{C1_CSI}", "unknown"),
        ])
        print_verification(verification)
        out = capsys.readouterr().out
        assert_inert(out)
        assert "code/branch main\\x1b[2K: expected 'a\\x1b'" in out

    def test_success_banner_with_hostile_branch(self, capsys):
        print_success("me", "repo", local_push=True,
                      wiring=LocalWiring(branch=f"main{BIDI}\x9b0m"))
        out = capsys.readouterr().out
        assert_inert(out)
        assert "git push -u origin main\\u202egnp.exe\\x9b0m:main\\u202egnp.exe\\x9b0m" in out
        # The box is sized from the escaped text, so its right edge lines up.
        widths = {len(line) for line in out.splitlines() if line.startswith("│")}
        assert len(widths) == 1
