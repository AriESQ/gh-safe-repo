"""Tests for shell completion (#60): `gh-safe-repo --completion {bash,zsh,fish}`.

The scripts are generated from the live parser, so the content tests walk
`build_parser()` independently and check every command and flag appears.
The bash tests source the script in a real `bash` and drive the completion
function through COMP_WORDS / COMP_CWORD; fish is driven with `complete -C`,
and zsh through its real completion system in an interactive `zsh` on a
pseudo-terminal. The zsh and fish tests skip when the shell is missing,
except under CI (`CI` set), where they fail: CI installs both (#141).
"""

import argparse
import os
import pty
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from gh_safe_repo.cli import build_parser, main
from gh_safe_repo.completion import SHELLS, describe, generate

COMPLETIONS = Path(__file__).resolve().parent.parent / "completions"
CHECKED_IN = {"bash": "gh-safe-repo.bash", "zsh": "_gh-safe-repo", "fish": "gh-safe-repo.fish"}


def _in_ci():
    return os.environ.get("CI", "").lower() not in ("", "0", "false")


def _require_shell(shell):
    """Skip when `shell` is not installed; under CI, fail instead.

    CI installs zsh and fish (.github/workflows/ci.yml), so a missing shell
    there means that step broke, and a skip would let a broken script merge
    with green CI (#141).
    """
    if shutil.which(shell) is not None:
        return
    if _in_ci():
        pytest.fail(
            f"{shell} is not installed, and CI is set: CI must run every shell "
            f"completion test. Install {shell} (see the 'Install zsh and fish' step "
            "in .github/workflows/ci.yml), or unset CI to skip these tests locally.",
            pytrace=False,
        )
    pytest.skip(f"{shell} not installed")


@pytest.fixture
def bash_installed():
    _require_shell("bash")


@pytest.fixture
def zsh_installed():
    _require_shell("zsh")


@pytest.fixture
def fish_installed():
    _require_shell("fish")


needs_bash = pytest.mark.usefixtures("bash_installed")
needs_zsh = pytest.mark.usefixtures("zsh_installed")
needs_fish = pytest.mark.usefixtures("fish_installed")


def _subparsers(parser):
    return next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))


def _all_commands():
    return list(_subparsers(build_parser()).choices)


def _flags(parser):
    return [f for a in parser._actions for f in a.option_strings]


def _script(shell):
    return generate(build_parser(), shell)


def _choices_parser():
    """The real parser plus a choices-valued option on `scan`."""
    parser = build_parser()
    _subparsers(parser).choices["scan"].add_argument(
        "--format", choices=["table", "sarif"], help="Output format")
    return parser


# --- the hidden --completion flag ---------------------------------------------

class TestFlag:
    @pytest.mark.parametrize("shell", SHELLS)
    @pytest.mark.parametrize("form", ["separate", "equals"])
    def test_prints_script_and_exits_0(self, shell, form, capsys):
        argv = ["--completion", shell] if form == "separate" else [f"--completion={shell}"]
        with patch.object(sys, "argv", ["gh-safe-repo", *argv]):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 0
        assert capsys.readouterr().out == _script(shell)

    def test_unknown_shell_is_usage_error(self, capsys):
        with patch.object(sys, "argv", ["gh-safe-repo", "--completion", "tcsh"]):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 2
        assert "invalid choice" in capsys.readouterr().err

    def test_hidden_from_every_help(self):
        parser = build_parser()
        assert "completion" not in parser.format_help()
        assert "completion" not in parser.format_usage()
        for name, sub in _subparsers(parser).choices.items():
            assert "completion" not in sub.format_help(), name

    @pytest.mark.parametrize("shell", SHELLS)
    def test_not_offered_by_the_scripts(self, shell):
        spec = describe(build_parser())
        assert not any("--completion" in o.flags for o in spec.options)
        body = "\n".join(l for l in _script(shell).splitlines() if not l.startswith("#"))
        assert "completion" not in body

    def test_run_as_a_program(self):
        """The launcher prints the script (what Homebrew's generator runs)."""
        launcher = Path(__file__).resolve().parent.parent / "gh-safe-repo"
        out = subprocess.run([sys.executable, str(launcher), "--completion=fish"],
                             capture_output=True, text=True, check=True).stdout
        assert out == _script("fish")


# --- the checked-in copies in completions/ ------------------------------------

@pytest.mark.parametrize("shell", SHELLS)
def test_checked_in_scripts_are_current(shell):
    path = COMPLETIONS / CHECKED_IN[shell]
    assert path.exists() and path.read_text() == _script(shell), (
        f"completions/{CHECKED_IN[shell]} is out of date with the CLI's parser; "
        "regenerate it with tools/gen-completions.sh and commit the result"
    )


# --- what the parser describes ------------------------------------------------

class TestDescribe:
    def test_every_command(self):
        spec = describe(build_parser())
        assert [c.name for c in spec.commands] == _all_commands()
        assert all(c.help for c in spec.commands)

    def test_value_kinds(self):
        spec = describe(build_parser())
        cmds = {c.name: c for c in spec.commands}
        config = next(o for o in spec.options if "--config" in o.flags)
        assert config.takes_value and config.kind == "file"
        assert [p.kind for p in cmds["push"].positionals] == ["dir", ""]
        assert [(p.kind, p.optional) for p in cmds["scan"].positionals] == [("dir", True)]
        assert [p.kind for p in cmds["new"].positionals] == [""]
        fmt = next(o for o in {c.name: c for c in describe(_choices_parser()).commands}["scan"].options
                   if "--format" in o.flags)
        assert fmt.kind == "choice" and fmt.choices == ("table", "sarif")
        yes = next(o for o in cmds["fix"].options if "--yes" in o.flags)
        assert not yes.takes_value and set(yes.flags) == {"--yes", "-y"}


# --- every command and flag reaches every script ------------------------------

@pytest.mark.parametrize("shell", SHELLS)
class TestScriptContent:
    def test_every_command(self, shell):
        script = _script(shell)
        for name in _all_commands():
            assert name in script, f"{shell}: {name} missing"

    def test_every_flag_of_every_command(self, shell):
        parser = build_parser()
        script = _script(shell)
        sub = _subparsers(parser)
        for name, subparser in [("", parser), *sub.choices.items()]:
            for flag in _flags(subparser):
                if flag == "--completion":
                    continue  # hidden; see TestFlag
                if shell == "fish":
                    # fish writes `-l yes` / `-s y`
                    needle = f"-l {flag[2:]}" if flag.startswith("--") else f"-s {flag[1:]}"
                else:
                    needle = flag
                assert needle in script, f"{shell}: {name or 'top level'} {flag} missing"

    def test_choices_follow_the_parser(self, shell):
        """A choices-valued option added to a command shows up with its choices."""
        script = generate(_choices_parser(), shell)
        assert "--format" in script or "-l format" in script
        assert "table sarif" in script


# --- syntax -------------------------------------------------------------------

@needs_bash
def test_bash_syntax():
    subprocess.run(["bash", "-n"], input=_script("bash"), text=True, check=True)


@needs_zsh
def test_zsh_syntax():
    subprocess.run(["zsh", "-n"], input=_script("zsh"), text=True, check=True)


@needs_fish
def test_fish_syntax(tmp_path):
    path = tmp_path / "gh-safe-repo.fish"
    path.write_text(_script("fish"))
    subprocess.run(["fish", "--no-execute", str(path)], check=True)


# --- bash, driven through COMP_WORDS ------------------------------------------

def bash_complete(line, cwd=None, script=None):
    """Complete `line` (the cursor at its end) in bash; return COMPREPLY."""
    script = script if script is not None else _script("bash")
    words = line.split(" ")
    driver = (
        script + "\n"
        + "COMP_WORDS=(" + " ".join(_bash_quote(w) for w in words) + ")\n"
        + f"COMP_CWORD={len(words) - 1}\n"
        + "_gh_safe_repo\n"
        + 'printf "%s\\n" "${COMPREPLY[@]}"\n'
    )
    out = subprocess.run(["bash", "--norc", "--noprofile"], input=driver, text=True,
                         capture_output=True, cwd=cwd, check=True).stdout
    return sorted(w for w in out.split("\n") if w)


def _bash_quote(word):
    return "'" + word.replace("'", "'\\''") + "'"


@needs_bash
class TestBashCompletion:
    def test_commands(self):
        assert bash_complete("gh-safe-repo ") == sorted(_all_commands())

    def test_command_prefix(self):
        assert bash_complete("gh-safe-repo fi") == ["fix"]
        assert bash_complete("gh-safe-repo c") == ["copy"]

    def test_global_options_before_command(self):
        assert bash_complete("gh-safe-repo --") == sorted(
            f for f in _flags(build_parser()) if f.startswith("--") and f != "--completion")

    def test_command_options(self):
        fix = _subparsers(build_parser()).choices["fix"]
        assert bash_complete("gh-safe-repo fix -") == sorted(_flags(fix))
        assert bash_complete("gh-safe-repo fix --mi") == ["--migrate-branch-protection"]
        # audit has no --yes / --dry-run
        assert bash_complete("gh-safe-repo audit --d") == ["--debug"]

    def test_options_after_positional(self):
        assert "--public" in bash_complete("gh-safe-repo new alice/app ")

    def test_command_after_global_option_value(self):
        assert bash_complete("gh-safe-repo --config my.ini ") == sorted(_all_commands())
        assert bash_complete("gh-safe-repo --debug --config my.ini n") == ["new"]

    def test_choices(self):
        script = generate(_choices_parser(), "bash")
        assert bash_complete("gh-safe-repo scan --format ", script=script) == ["sarif", "table"]
        assert bash_complete("gh-safe-repo scan --format = s", script=script) == ["sarif"]

    def test_config_completes_files(self, tmp_path):
        (tmp_path / "alt.ini").write_text("")
        (tmp_path / "sub").mkdir()
        assert bash_complete("gh-safe-repo --config ", cwd=tmp_path) == ["alt.ini", "sub"]
        assert bash_complete("gh-safe-repo fix --config a", cwd=tmp_path) == ["alt.ini"]
        assert bash_complete("gh-safe-repo fix --config = a", cwd=tmp_path) == ["alt.ini"]

    def test_path_positionals_complete_directories(self, tmp_path):
        (tmp_path / "file.txt").write_text("")
        (tmp_path / "proj").mkdir()
        assert bash_complete("gh-safe-repo push ", cwd=tmp_path) == ["proj"]
        assert bash_complete("gh-safe-repo scan --json ", cwd=tmp_path) == ["proj"]
        # A value option between the command and the positional is skipped.
        assert bash_complete("gh-safe-repo push --config x.ini ", cwd=tmp_path) == ["proj"]
        # The second positional of push is OWNER/REPO: no directories.
        assert "proj" not in bash_complete("gh-safe-repo push proj ", cwd=tmp_path)

    def test_unknown_command_completes_nothing(self):
        assert bash_complete("gh-safe-repo frobnicate ") == []


# --- fish, driven through `complete -C` ---------------------------------------

def fish_complete(line, tmp_path, script=None):
    path = tmp_path / "gh-safe-repo.fish"
    path.write_text(script if script is not None else _script("fish"))
    out = subprocess.run(
        ["fish", "--no-config", "-c", f"source {path}; complete -C{_bash_quote(line)}"],
        text=True, capture_output=True, check=True, cwd=tmp_path,
    ).stdout
    return sorted(line.split("\t")[0] for line in out.splitlines() if line)


@needs_fish
class TestFishCompletion:
    def test_commands(self, tmp_path):
        assert fish_complete("gh-safe-repo ", tmp_path) == sorted(_all_commands())

    def test_command_options(self, tmp_path):
        assert fish_complete("gh-safe-repo fix --mi", tmp_path) == ["--migrate-branch-protection"]
        assert fish_complete("gh-safe-repo audit --d", tmp_path) == ["--debug"]

    def test_choices(self, tmp_path):
        script = generate(_choices_parser(), "fish")
        assert fish_complete("gh-safe-repo scan --format ", tmp_path, script) == ["sarif", "table"]

    def test_no_completion_flag(self, tmp_path):
        assert "--completion" not in fish_complete("gh-safe-repo --", tmp_path)

    def test_command_after_global_option_value(self, tmp_path):
        assert fish_complete("gh-safe-repo --config x.ini n", tmp_path) == ["new"]


# --- zsh, driven through compsys on a pseudo-terminal -------------------------

# The .zshrc of the zsh under test. compsys only runs inside ZLE, so the shell
# is interactive on a pty, and the test types the line and a Tab. compadd is
# wrapped to append what each call offers (after compsys's own prefix
# matching, via `compadd -O`) to $GSR_OUT; calls that only collect matches
# (-O / -A / -D) are compsys internals and pass straight through. Files mark
# the hand-offs, so nothing depends on parsing the terminal or on timing:
# zle-line-init writes $GSR_READY once ZLE is reading keys, and comppostfuncs
# writes $GSR_DONE when completion has finished, with or without matches.
_ZSHRC = r"""
fpath=("$GSR_FPATH" $fpath)
autoload -Uz compinit && compinit -u -D
[[ -n $GSR_SOURCE ]] && source "$GSR_FPATH/_gh-safe-repo"
PS1='> ' RPS1=''
unsetopt beep
bindkey '^I' complete-word
compadd() {
    if [[ ${@[1,(i)(-|--)]} == *-(O|A|D)\ * ]]; then
        builtin compadd "$@"; return
    fi
    local -a hits
    builtin compadd -O hits "$@"
    (( $#hits )) && print -rl -- $hits >> "$GSR_OUT"
    builtin compadd "$@"
}
_gsr_done() { print -r -- done > "$GSR_DONE" }
comppostfuncs=(_gsr_done)
_gsr_ready() { print -r -- ready > "$GSR_READY" }
zle -N zle-line-init _gsr_ready
"""


def zsh_complete(line, tmp_path, cwd=None, script=None, source=False, timeout=15):
    """Press Tab after `line` in an interactive zsh; return what compsys offered.

    The script is installed the way completions/_gh-safe-repo is meant to
    be, as `_gh-safe-repo` on $fpath before compinit (autoloaded on first
    use), or, with `source=True`, sourced after compinit, as
    `source <(gh-safe-repo --completion zsh)` does.
    """
    home = tmp_path / "zsh-home"
    fpath = home / "fpath"
    fpath.mkdir(parents=True, exist_ok=True)
    (fpath / "_gh-safe-repo").write_text(script if script is not None else _script("zsh"))
    (home / ".zshrc").write_text(_ZSHRC)
    out, ready, done = home / "out", home / "ready", home / "done"
    for f in (out, ready, done):
        f.unlink(missing_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
        "ZDOTDIR": str(home), "TERM": "dumb", "GSR_FPATH": str(fpath),
        "GSR_OUT": str(out), "GSR_READY": str(ready), "GSR_DONE": str(done),
        **({"GSR_SOURCE": "1"} if source else {}),
    }
    master, slave = pty.openpty()
    # -d: no global rc files beyond /etc/zshenv; ZDOTDIR supplies the .zshrc.
    proc = subprocess.Popen(["zsh", "-d", "-i"], stdin=slave, stdout=slave, stderr=slave,
                            cwd=cwd or tmp_path, env=env, start_new_session=True)
    os.close(slave)
    screen = b""

    def wait_for(marker):
        # Drain the pty while waiting, so zsh never blocks on a full buffer.
        nonlocal screen
        deadline = time.monotonic() + timeout
        while not marker.exists():
            if time.monotonic() > deadline or proc.poll() is not None:
                pytest.fail(f"zsh wrote no {marker.name} marker completing {line!r}; "
                            f"terminal:\n{screen.decode(errors='replace')}", pytrace=False)
            if select.select([master], [], [], 0.05)[0]:
                try:
                    screen += os.read(master, 4096)
                except OSError:
                    pass

    try:
        wait_for(ready)
        os.write(master, line.encode() + b"\t")
        wait_for(done)
    finally:
        proc.kill()
        proc.wait()
        os.close(master)
    return sorted({w for w in out.read_text().splitlines() if w}) if out.exists() else []


@needs_zsh
class TestZshCompletion:
    def test_commands(self, tmp_path):
        assert zsh_complete("gh-safe-repo ", tmp_path) == sorted(_all_commands())

    def test_commands_when_sourced(self, tmp_path):
        assert zsh_complete("gh-safe-repo ", tmp_path, source=True) == sorted(_all_commands())

    def test_command_prefix(self, tmp_path):
        assert zsh_complete("gh-safe-repo fi", tmp_path) == ["fix"]

    def test_global_options_before_command(self, tmp_path):
        assert zsh_complete("gh-safe-repo --", tmp_path) == sorted(
            f for f in _flags(build_parser()) if f.startswith("--") and f != "--completion")

    def test_command_options(self, tmp_path):
        assert zsh_complete("gh-safe-repo fix --mi", tmp_path) == ["--migrate-branch-protection"]
        # audit has no --yes / --dry-run
        assert zsh_complete("gh-safe-repo audit --d", tmp_path) == ["--debug"]

    def test_options_after_positional(self, tmp_path):
        assert zsh_complete("gh-safe-repo new alice/app --p", tmp_path) == ["--public"]

    def test_command_after_global_option_value(self, tmp_path):
        assert zsh_complete("gh-safe-repo --config x.ini n", tmp_path) == ["new"]

    def test_choices(self, tmp_path):
        script = generate(_choices_parser(), "zsh")
        assert zsh_complete("gh-safe-repo scan --format ", tmp_path, script=script) == [
            "sarif", "table"]

    def test_config_completes_files(self, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        (work / "alt.ini").write_text("")
        (work / "sub").mkdir()
        assert zsh_complete("gh-safe-repo --config ", tmp_path, cwd=work) == ["alt.ini", "sub"]
        assert zsh_complete("gh-safe-repo fix --config a", tmp_path, cwd=work) == ["alt.ini"]

    def test_config_equals_completes_files(self, tmp_path):
        # argparse accepts --config=x.ini; complete the value after `=`, at
        # the top level and per command, and still reach the command after it.
        work = tmp_path / "work"
        work.mkdir()
        (work / "alt.ini").write_text("")
        (work / "sub").mkdir()
        assert zsh_complete("gh-safe-repo --config=", tmp_path, cwd=work) == ["alt.ini", "sub"]
        assert zsh_complete("gh-safe-repo fix --config=a", tmp_path, cwd=work) == ["alt.ini"]
        assert zsh_complete("gh-safe-repo --config=x.ini n", tmp_path) == ["new"]

    def test_choices_after_equals(self, tmp_path):
        script = generate(_choices_parser(), "zsh")
        assert zsh_complete("gh-safe-repo scan --format=t", tmp_path, script=script) == [
            "table"]

    def test_path_positionals_complete_directories(self, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        (work / "file.txt").write_text("")
        (work / "proj").mkdir()
        assert zsh_complete("gh-safe-repo push ", tmp_path, cwd=work) == ["proj"]
        assert zsh_complete("gh-safe-repo push --config=x.ini ", tmp_path, cwd=work) == ["proj"]
        assert zsh_complete("gh-safe-repo scan --json ", tmp_path, cwd=work) == ["proj"]
        # Options a command shares with the top level (--debug, --config)
        # belong to the command, and its positionals still complete.
        assert zsh_complete("gh-safe-repo push --debug ", tmp_path, cwd=work) == ["proj"]
        assert zsh_complete("gh-safe-repo push --config x.ini ", tmp_path, cwd=work) == ["proj"]
        # The second positional of push is OWNER/REPO: no directories.
        assert zsh_complete("gh-safe-repo push proj ", tmp_path, cwd=work) == []

    def test_unknown_command_completes_nothing(self, tmp_path):
        assert zsh_complete("gh-safe-repo frobnicate ", tmp_path) == []
