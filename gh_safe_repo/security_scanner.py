"""
Pre-flight security scanner for the push and copy workflows.

Detects hardcoded secrets, emails, large files, and TODOs before
code is mirrored or pushed to a GitHub repository.

truffleHog is used if installed; regex fallback otherwise.
Always runs locally — never in GitHub Actions.
"""

import codecs
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Set, Tuple

from .display import safe_display
from .local_repo import external_git_dir, is_git_checkout


# --- Module-level constants ---

# Warnings go to stderr; colour them only when that is a terminal and the user
# has not opted out via NO_COLOR (commands/_common.py does the same for stdout).
_WARN = (
    "\033[33mWarning:\033[0m"
    if not os.environ.get("NO_COLOR") and sys.stderr.isatty()
    else "Warning:"
)

BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".tiff", ".webp",
    ".mp3", ".mp4", ".avi", ".mov", ".mkv", ".wav", ".flac", ".ogg",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".rar", ".7z",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".class", ".pyc",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".sqlite", ".db",
}

# Content sniff for binaries the extension list misses (e.g. TouchDesigner
# .toe): NUL bytes in the first block mean the file is not regex-scanned,
# unless the block reads as UTF-16 text or as UTF-8 text with a few stray
# NULs (#157).
_BINARY_SNIFF_BYTES = 8192

# UTF-16/32 byte order marks with their code unit size, longest first: the
# UTF-32 LE mark begins with the UTF-16 LE one.
_WIDE_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32", 4),
    (codecs.BOM_UTF32_BE, "utf-32", 4),
    (codecs.BOM_UTF16_LE, "utf-16", 2),
    (codecs.BOM_UTF16_BE, "utf-16", 2),
)

# C0 controls that may appear in text a terminal renders. Any other C0
# control next to a NUL marks the block as binary.
_TEXT_CONTROLS_NO_NUL = frozenset(b"\t\n\r\x0b\x0c\x08\x1b")
_TEXT_CONTROLS = _TEXT_CONTROLS_NO_NUL | {0}


def _utf16_without_bom(block: bytes) -> Optional[str]:
    """"utf-16-le"/"utf-16-be" when block looks like mostly-ASCII UTF-16:
    a NUL in (nearly) every high byte and (almost) none in the low bytes."""
    pairs = len(block) // 2
    if pairs < 2:
        return None
    even_nuls = block[0:pairs * 2:2].count(0)
    odd_nuls = block[1:pairs * 2:2].count(0)
    if odd_nuls >= 0.9 * pairs and even_nuls <= 0.1 * pairs:
        return "utf-16-le"
    if even_nuls >= 0.9 * pairs and odd_nuls <= 0.1 * pairs:
        return "utf-16-be"
    return None


def _mostly_text(sample: str) -> bool:
    """True when at most 5% of a decoded sample are control characters a
    text file would not hold (NUL counts; whitespace, BS and ESC do not)."""
    if not sample:
        return True
    bad = sum(1 for c in sample
              if unicodedata.category(c) == "Cc" and ord(c) not in _TEXT_CONTROLS_NO_NUL)
    return bad <= 0.05 * len(sample)


def _text_encoding(block: bytes) -> Optional[str]:
    """The codec to read a file whose first block is `block`, or None if the
    file is binary and must not be content-scanned."""
    if block.startswith(codecs.BOM_UTF8) and b"\0" not in block:
        return "utf-8-sig"
    for bom, codec, unit in _WIDE_BOMS:
        if block.startswith(bom):
            # Whole code units only: the block may end mid-character.
            sample = block[: len(block) // unit * unit].decode(codec, errors="replace")
            return codec if _mostly_text(sample) else None
    if b"\0" not in block:
        return "utf-8"
    codec = _utf16_without_bom(block)
    if codec:
        sample = block[: len(block) // 2 * 2].decode(codec, errors="replace")
        return codec if _mostly_text(sample) else None
    # UTF-8 text with a few stray NULs (an evasion trick, #157): valid
    # UTF-8, no other non-text control byte, and NULs rare. Real binaries
    # fail the first two tests almost always.
    if any(b < 0x20 and b not in _TEXT_CONTROLS for b in block):
        return None
    if block.count(0) > max(4, len(block) // 256):
        return None
    try:
        codecs.getincrementaldecoder("utf-8")().decode(block, final=False)
    except UnicodeDecodeError:
        return None
    return "utf-8"


def _read_text(path: str) -> Optional[str]:
    """Return the decoded content of a text file, or None when the file is
    binary or unreadable. Undecodable bytes become U+FFFD."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    codec = _text_encoding(data[:_BINARY_SNIFF_BYTES])
    if codec is None:
        return None
    return data.decode(codec, errors="replace")


def _split_lines(text: str) -> List[str]:
    """Split on "\\n" only, keeping it, so line numbers match `sed -n` and
    git. A lone CR stays inside its line, where normalization handles it."""
    lines = text.split("\n")
    out = [line + "\n" for line in lines[:-1]]
    if lines[-1]:
        out.append(lines[-1])
    return out


# --- Normalization of hidden characters (#157) ---
#
# A terminal shows text differently from its bytes: Cf characters (zero-width,
# BOM, soft hyphen, bidi controls, the tag block) take no space, ANSI escape
# sequences and most C0 controls are not shown, a backspace moves the cursor
# left so the next character overwrites the previous one, and a CR returns to
# column 0. A key split by any of these reads normally on screen but is not
# matched by a regex over the raw line. Each line that contains such a
# character is matched again in two normalized forms:
#   stripped  every hidden character removed (keeps text a later overwrite
#             would cover, so a CR or backspace cannot destroy a key)
#   rendered  what a terminal shows: CR and backspace move the cursor and
#             later characters overwrite earlier ones
# A secret pattern that matches only a normalized form is reported with the
# hidden characters named in its rule.

_ANSI_ESCAPE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"            # CSI (colours, cursor movement)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC, ended by BEL or ST
    r"|\x1b[PX^_][^\x1b]*(?:\x1b\\)?"     # DCS / SOS / PM / APC strings
    r"|\x1b[ -/]*[0-~]"                   # two-character escapes, ESC ( B
    r"|\x9b[0-?]*[ -/]*[@-~]"             # C1 CSI
)

_CONTROL_NAMES = {
    "\x00": "NUL",
    "\x08": "BACKSPACE",
    "\x0d": "CARRIAGE RETURN",
    "\x1b": "ESCAPE",
    "\x7f": "DELETE",
}


def _hidden_name(ch: str) -> str:
    cp = ord(ch)
    name = _CONTROL_NAMES.get(ch) or unicodedata.name(ch, "")
    return f"U+{cp:04X} {name}".rstrip() if name else f"U+{cp:04X}"


def _normalize_line(line: str) -> Optional[Tuple[Tuple[str, str], str]]:
    """Return ((stripped, rendered), hidden) for a line with hidden
    characters, or None when the line has none.

    `hidden` names what was hidden, for the finding's rule. The line ending
    ("\\n" or "\\r\\n") is removed first, so CRLF files take the fast path.
    """
    body = line[:-1] if line.endswith("\n") else line
    if body.endswith("\r"):
        body = body[:-1]
    if body.isprintable():
        return None
    names: List[str] = []

    def note(name: str) -> None:
        if name not in names:
            names.append(name)

    def drop_escape(m: "re.Match") -> str:
        note("ANSI escape sequence")
        return ""

    body = _ANSI_ESCAPE.sub(drop_escape, body)
    stripped = []
    for ch in body:
        if _is_hidden(ch):
            note(_hidden_name(ch))
        else:
            stripped.append(ch)
    if not names:
        return None  # e.g. NBSP or U+2028: not printable, but nothing hidden
    shown = names[:3] + (["..."] if len(names) > 3 else [])
    return ("".join(stripped), _render(body)), ", ".join(shown)


# RIGHT-TO-LEFT OVERRIDE up to POP DIRECTIONAL FORMATTING or the end of the
# line: a bidi-aware viewer (GitHub's file view, many editors) shows the
# characters in it in reverse order.
_RLO_SPAN = re.compile(r"\u202e([^\u202c]*)")


def _is_hidden(ch: str) -> bool:
    return ch != "\t" and unicodedata.category(ch) in ("Cc", "Cf")


def _render(body: str) -> str:
    """What a viewer shows for body (ANSI escapes already removed): RLO
    spans reversed, then CR and backspace move the cursor so later
    characters overwrite earlier ones, and other hidden characters take no
    space."""
    body = _RLO_SPAN.sub(lambda m: m.group(1)[::-1], body)
    screen: List[str] = []
    cursor = 0
    for ch in body:
        if not _is_hidden(ch):
            if cursor < len(screen):
                screen[cursor] = ch
            else:
                screen.append(ch)
            cursor += 1
        elif ch == "\r":
            cursor = 0
        elif ch == "\b":
            cursor = max(0, cursor - 1)
    return "".join(screen)


def _pattern_hit(pattern, line: str, normalized) -> Optional[str]:
    """None: no match. "": the raw line matches. Otherwise the match needed
    the hidden characters removed, and this names them."""
    if pattern.search(line):
        return ""
    if normalized and any(pattern.search(form) for form in normalized[0]):
        return normalized[1]
    return None


def _obfuscated_rule(rule: str, hidden: str) -> str:
    """The rule name, marked as obfuscated when hidden characters split it."""
    return f"{rule} (obfuscated with {hidden})" if hidden else rule

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
}

_AI_CONTEXT_FILES = {
    "CLAUDE.md",
    "AGENTS.md",
    ".cursorrules",
    "copilot-instructions.md",
    ".github/copilot-instructions.md",
    ".cursor",          # directory — flag if it exists
}

# Pre-computed lowercase sets for O(1) case-insensitive matching
_AI_CONTEXT_BASENAMES = frozenset(
    p.lower() for p in _AI_CONTEXT_FILES
    if "/" not in p and p.lower() != ".cursor"
)
_AI_CONTEXT_REL_PATHS = frozenset(
    p.lower() for p in _AI_CONTEXT_FILES
    if "/" in p
)

# Candidates for git history check: (display_path, git_log_path)
# display_path is used in Finding.file_path; git_log_path is passed to `git log --`.
_AI_CONTEXT_HISTORY_CANDIDATES = (
    ("CLAUDE.md",                       "CLAUDE.md"),
    ("AGENTS.md",                       "AGENTS.md"),
    (".cursorrules",                    ".cursorrules"),
    ("copilot-instructions.md",         "copilot-instructions.md"),
    (".github/copilot-instructions.md", ".github/copilot-instructions.md"),
    (".cursor",                         ".cursor"),
)


# --- Enums ---

class Severity(Enum):
    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    INFO = "INFO"


# Engine names reported by SecurityScanner.engine_used after scan().
ENGINE_NATIVE = "trufflehog-native"
ENGINE_CONTAINER = "trufflehog-container"
ENGINE_REGEX = "regex"

TRUFFLEHOG_MODES = ("auto", "native", "docker", "off")


class FindingCategory(Enum):
    SECRET = "secret"
    EMAIL = "email"
    LARGE_FILE = "large_file"
    TODO = "todo"
    BANNED_STRING = "banned_string"
    AI_CONTEXT_FILE = "ai_context_file"


# --- Dataclass ---

@dataclass
class Finding:
    severity: Severity
    category: FindingCategory
    file_path: str      # relative to scanned root
    line_number: int    # 0 = file-level (large file, etc.)
    rule: str           # human-readable rule name
    match: str          # "[redacted]" for secrets, literal for emails/todos
    commit: str = ""    # short commit hash (trufflehog git mode only)
    timestamp: str = "" # commit timestamp (trufflehog git mode only)
    hidden: str = ""    # hidden characters the match needed removed (#157)


# --- Compiled regex patterns ---

# Critical secret patterns
_CRITICAL_PATTERNS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"),
     "AWS Access Key ID"),
    (re.compile(r"(ghp|gho|ghu|ghr|ghs)_[A-Za-z0-9]{36,}"),
     "GitHub token"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{82,}"),
     "GitHub fine-grained PAT"),
    (re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY"),
     "Private key header"),
    (re.compile(r"(?:postgres|mysql|mongodb|redis)://[^:]+:[^@\s]+@"),
     "Database URL with credentials"),
]

# Warning patterns (higher false-positive rate)
_WARNING_PATTERNS = [
    (re.compile(r"api[_-]?key\s*[=:]\s*[\"'][A-Za-z0-9_-]{16,}[\"']", re.IGNORECASE),
     "Generic API key"),
    (re.compile(r"(?:password|secret|token)\s*[=:]\s*[\"'][^\"']{8,}[\"']", re.IGNORECASE),
     "Generic password/secret/token"),
    (re.compile(r"bearer\s+[A-Za-z0-9_\-.]{20,}", re.IGNORECASE),
     "Bearer token"),
]

EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
# Stricter alternative: only match common comment prefixes (#, //, /*, --)
# TODO_PATTERN = re.compile(r"(?i)(?:#|//|/\*|--)\s*(?:TODO|FIXME|HACK|XXX)\b")
TODO_PATTERN = re.compile(r"(?i)\b(?:TODO|FIXME|HACK|XXX)\b")


# --- Helpers ---

def _ai_context_hint(rel_path: str) -> str:
    """Build the remediation message for an AI context file finding.

    The message is multi-line prose that embeds the path, so the path is
    sanitized here: at print time a newline inside a directory name could no
    longer be told apart from the message's own line breaks (#125).
    """
    rel_path = safe_display(rel_path)
    return (
        f"This file may contain internal development notes. Its git history may hold\n"
        f"more sensitive content than the current version.\n"
        f"To strip history and re-add as a clean file (run in your local source repo):\n"
        f"  scrub-ai-context.sh {rel_path}          # from gh-safe-repo tools/\n"
        f"  git push --force-with-lease --all\n"
        f"Then re-run gh-safe-repo. Or continue to mirror as-is."
    )


def _ai_context_history_hint(rel_path: str) -> str:
    """Remediation message for an AI context file found only in git history."""
    rel_path = safe_display(rel_path)
    return (
        f"This file was present in git history but has since been deleted.\n"
        f"Its historical commits may contain sensitive development notes.\n"
        f"To permanently remove from history (run in your local source repo):\n"
        f"  scrub-ai-context.sh {rel_path}          # from gh-safe-repo tools/\n"
        f"  git push --force-with-lease --all\n"
        f"Then re-run gh-safe-repo. Or continue to mirror as-is."
    )


# --- Scanner class ---

class SecurityScanner:
    def __init__(self, config, debug=False, progress=True):
        self.debug = debug
        # Single-line progress on stderr; only ever drawn when stderr is a TTY.
        # Callers pass progress=False in --json mode.
        self._progress = progress and sys.stderr.isatty()
        self._scan_secrets = config.getbool("pre_flight_scan", "scan_for_secrets", fallback=True)
        self._scan_emails  = config.getbool("pre_flight_scan", "scan_for_emails", fallback=True)
        self._scan_todos   = config.getbool("pre_flight_scan", "scan_for_todos", fallback=True)
        # trufflehog_mode: auto / native / docker / off
        mode = (config.get("pre_flight_scan", "trufflehog_mode", fallback="auto") or "auto").strip().lower()
        # Backwards-compat: use_trufflehog = false in old user configs → off
        if mode == "auto":
            old_flag = config.get("pre_flight_scan", "use_trufflehog", fallback=None)
            if old_flag is not None and old_flag.strip().lower() in ("false", "0", "no"):
                mode = "off"
        self._trufflehog_mode = mode
        # Discovery result cache: None = not yet run; dict after first _run_discovery() call
        self._discovery: Optional[dict] = None
        # Why truffleHog could not be used (discovery or invocation); "" if it could.
        self._trufflehog_failure = ""
        # Populated by scan(): the engine that actually produced secret findings
        # (ENGINE_* constant), whether that was a fallback from truffleHog to
        # regex, and why. Plan-time scanner_description says what *should* run;
        # these say what *did*.
        self.engine_used: Optional[str] = None
        self.fell_back = False
        self.fallback_reason = ""
        # float() then int() to allow decimal config values like "0.001" for tests
        self._max_file_size_bytes = int(
            float(config.get("pre_flight_scan", "max_file_size_mb", fallback="100")) * 1024 * 1024
        )
        # Banned strings: split on newlines and commas, strip whitespace, drop empties
        raw = config.get("pre_flight_scan", "banned_strings", fallback="")
        self._banned_strings = [s.strip() for s in re.split(r"[\n,]", raw) if s.strip()]
        self._warn_ai_context_files = config.getbool(
            "pre_flight_scan", "warn_ai_context_files", fallback=True
        )
        # scan_exclude_paths: regex patterns — files/dirs matching any are skipped
        raw_exclude = config.get("pre_flight_scan", "scan_exclude_paths", fallback="")
        _exclude_strings = [s.strip() for s in re.split(r"[\n,]", raw_exclude) if s.strip()]
        self._exclude_path_patterns = [re.compile(p) for p in _exclude_strings]
        self._exclude_path_strings = _exclude_strings   # raw strings for truffleHog temp file
        # exclude_emails: unified exclusion — "@domain" for domain, else exact address
        raw_exclude_emails = config.get("pre_flight_scan", "exclude_emails", fallback="")
        entries = [e.strip().lower() for e in re.split(r"[\n,]", raw_exclude_emails) if e.strip()]
        self._exclude_emails_domains: Set[str] = {e.lstrip("@") for e in entries if e.startswith("@")}
        self._exclude_emails_addresses: Set[str] = {e for e in entries if not e.startswith("@")}
        self._scan_email_history = config.getbool("pre_flight_scan", "scan_email_history", fallback=True)
        # Populated during scan(); readable by callers afterward to show coverage warnings
        self.skipped_committed_dirs: List[str] = []

    # --- Helpers ---

    def _is_email_excluded(self, email: str) -> bool:
        """Return True if email matches any exclude_emails entry."""
        email_lower = email.lower()
        if email_lower in self._exclude_emails_addresses:
            return True
        domain = email_lower.split("@", 1)[1] if "@" in email_lower else ""
        return domain in self._exclude_emails_domains

    def _is_excluded(self, rel_path: str) -> bool:
        """Return True if rel_path matches any scan_exclude_paths pattern."""
        return any(p.search(rel_path) for p in self._exclude_path_patterns)

    def _is_committed(self, root_path: str, rel_dir: str) -> bool:
        """Return True if any files under rel_dir are tracked in the git index."""
        try:
            result = subprocess.run(
                ["git", "-C", root_path, "ls-files", "--", rel_dir],
                capture_output=True, text=True,
            )
            return bool(result.stdout.strip())
        except FileNotFoundError:
            return False

    def _is_tracked(self, root_path: str, rel_path: str) -> bool:
        """Return True if rel_path is tracked in git (in the index).

        Untracked / gitignored files are not pushed by `git push`, so flagging
        them as AI-context leaks would be a false positive for the publish
        workflow. On any git error, return True (be conservative — flag).
        """
        try:
            result = subprocess.run(
                ["git", "-C", root_path, "ls-files", "--error-unmatch", "--", rel_path],
                capture_output=True, text=True,
            )
            return result.returncode == 0
        except FileNotFoundError:
            return True

    def _tracked_files(self, root_path: str) -> Optional[Set[str]]:
        """Return the set of git-tracked paths (relative, '/'-separated), or
        None if root_path is not a usable git repo. Only tracked files are
        pushed, so the content walk is restricted to them (issue #84)."""
        try:
            result = subprocess.run(
                ["git", "-C", root_path, "ls-files", "-z"],
                capture_output=True,
            )
        except FileNotFoundError:
            return None
        if result.returncode != 0:
            return None
        out = result.stdout.decode("utf-8", errors="surrogateescape")
        return {p for p in out.split("\0") if p}

    def _draw_progress(self, done: int, total: Optional[int], rel_path: str) -> None:
        if total:
            head = f"Scanning files: {done:,}/{total:,} ({done * 100 // total}%)  "
        else:
            head = f"Scanning files: {done:,}  "
        width = shutil.get_terminal_size((80, 20)).columns - 1
        # The file name is untrusted: escape it before truncating to the
        # width, so the line still fits and cannot move the cursor (#125).
        line = (head + safe_display(rel_path))[:width]
        sys.stderr.write("\r\033[K" + line)
        sys.stderr.flush()

    def _clear_progress(self) -> None:
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()

    # --- Discovery ---

    def _detect_native(self) -> Optional[str]:
        """Try to run trufflehog --version. Returns v3 version string, or None.

        Prints a user-visible warning if truffleHog is found but is not v3.
        Returns None silently if truffleHog is not on PATH at all.
        """
        try:
            result = subprocess.run(
                ["trufflehog", "--version"],
                capture_output=True, text=True, timeout=10,
            )
        except FileNotFoundError:
            return None
        except Exception:
            return None
        output = (result.stdout + result.stderr).strip()
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", output)
        if not m:
            self._trufflehog_failure = "unrecognised truffleHog version output"
            self._warn_auto_fallback(
                self._trufflehog_failure + " — falling back to container or regex scanner"
            )
            return None
        major = int(m.group(1))
        if major != 3:
            self._trufflehog_failure = f"truffleHog v{m.group(0)} found on PATH (v3 required)"
            self._warn_auto_fallback(
                f"truffleHog v{m.group(0)} detected "
                f"(v3 required) — falling back to container or regex scanner"
            )
            return None
        return m.group(0)   # e.g. "3.93.4"

    def _warn_auto_fallback(self, msg: str) -> None:
        """Warn about a fallback, but only in auto mode.

        In an explicit mode (native/docker) a fallback is a failure: the reason
        is kept in _trufflehog_failure and reported once by engine_error(),
        rather than as a "falling back" warning that would contradict it.
        """
        if self._trufflehog_mode == "auto":
            print(_WARN + " " + msg, file=sys.stderr)

    def _detect_container_runtime(self) -> Optional[Tuple[str, str]]:
        """Return (name, full_path) for podman or docker, or None.

        Respects the CONTAINER_RUNTIME env var (same precedence as the shell wrapper).
        """
        env_runtime = os.environ.get("CONTAINER_RUNTIME", "").strip()
        if env_runtime:
            path = shutil.which(env_runtime)
            if path:
                return (env_runtime, path)
        for name in ("podman", "docker"):
            path = shutil.which(name)
            if path:
                return (name, path)
        return None

    def _run_discovery(self) -> dict:
        """Run the truffleHog discovery chain once and cache the result.

        Returns a dict with key "method" (one of "native", "container", "none")
        plus "version" (native) or "runtime"/"runtime_path" (container).
        """
        if self._discovery is not None:
            return self._discovery

        mode = self._trufflehog_mode

        if mode == "off":
            self._discovery = {"method": "none"}
            return self._discovery

        if mode not in TRUFFLEHOG_MODES:
            self._trufflehog_failure = (
                f"unrecognised trufflehog_mode {mode!r} "
                f"(expected one of: {', '.join(TRUFFLEHOG_MODES)})"
            )
            self._discovery = {"method": "none"}
            return self._discovery

        # Step 1: try native truffleHog (auto or native mode)
        if mode in ("auto", "native"):
            version = self._detect_native()
            if version:
                self._discovery = {"method": "native", "version": version}
                return self._discovery
            if mode == "native":
                if not self._trufflehog_failure:
                    self._trufflehog_failure = "truffleHog not found on PATH"
                self._discovery = {"method": "none"}
                return self._discovery
            # mode == "auto": fall through to container detection

        # Step 2: try container runtime (auto or docker mode)
        if mode in ("auto", "docker"):
            runtime = self._detect_container_runtime()
            if runtime:
                self._discovery = {
                    "method": "container",
                    "runtime": runtime[0],
                    "runtime_path": runtime[1],
                }
                return self._discovery
            if mode == "docker":
                self._trufflehog_failure = "no container runtime (podman or docker) found"
                self._discovery = {"method": "none"}
                return self._discovery

        # Step 3: nothing available (auto mode exhausted all options)
        self._trufflehog_failure = "truffleHog not found and no container runtime available"
        print(
            _WARN + " truffleHog not found and no container runtime available "
            "— using regex scanner\n"
            "         (install truffleHog v3 or podman/docker for better secret detection)",
            file=sys.stderr,
        )
        self._discovery = {"method": "none"}
        return self._discovery

    @property
    def scanner_description(self) -> str:
        """Human-readable description of which scanner will run. Triggers discovery (cached)."""
        disc = self._run_discovery()
        if disc["method"] == "native":
            return f"truffleHog v{disc['version']}"
        if disc["method"] == "container":
            return f"truffleHog via {disc['runtime']}"
        if self._trufflehog_mode == "off":
            return "regex only"
        if self.is_strict:
            return (
                f"truffleHog unavailable: {self._trufflehog_failure} — "
                f"trufflehog_mode = {self._trufflehog_mode}, so the scan will fail"
            )
        return "regex only — see warning above"

    @property
    def trufflehog_mode(self) -> str:
        """Effective mode, after the legacy use_trufflehog mapping."""
        return self._trufflehog_mode

    @property
    def is_strict(self) -> bool:
        """True when the configured mode forbids a silent fallback to regex.

        `native` and `docker` name a specific engine, so falling back to regex
        would quietly give weaker secret detection than the user asked for.
        An unrecognised value is treated the same way rather than as `auto`.
        """
        return self._trufflehog_mode not in ("auto", "off")

    def engine_error(self) -> Optional[str]:
        """After scan(): an error message if an explicit mode fell back, else None."""
        if not self.strict_failure:
            return None
        # The regex patterns run on every scan, so their results are real and
        # are still reported; what the strict mode refuses is to count a
        # regex-only scan as a pass (#153).
        return (
            f"trufflehog_mode = {self._trufflehog_mode} but truffleHog could not run "
            f"({self.fallback_reason}). Only the regex scanner ran, and it misses "
            f"most credential types, so this scan does not count as clean. "
            f"Fix the truffleHog install, or set trufflehog_mode = auto to accept "
            f"a regex-only scan"
        )

    @property
    def strict_failure(self) -> bool:
        """After scan(): True when a strict mode could not run truffleHog."""
        return self.fell_back and self.is_strict

    def engine_report(self) -> dict:
        """The `engine` object for `scan --json`."""
        return {
            "configured": self._trufflehog_mode,
            "used": self.engine_used,
            "fell_back": self.fell_back,
            "error": self.engine_error(),
        }

    @property
    def engine_summary(self) -> str:
        """One-line human description of the engine scan() actually used."""
        disc = self._discovery or {}
        if self.engine_used == ENGINE_NATIVE:
            return f"truffleHog v{disc.get('version', '?')}"
        if self.engine_used == ENGINE_CONTAINER:
            return f"truffleHog via {disc.get('runtime', 'container')}"
        if self.strict_failure:
            # Not "fell back": the strict mode does not accept this result.
            return f"regex only (truffleHog failed: {self.fallback_reason})"
        if self.fell_back:
            return f"regex (fell back from truffleHog: {self.fallback_reason})"
        if not self._scan_secrets:
            return "regex (secret scanning disabled)"
        return "regex"

    # --- Scanning ---

    def scan(self, root_path: str) -> List[Finding]:
        self.skipped_committed_dirs = []

        if self.debug:
            print(f"[debug] SKIP_DIRS: {sorted(SKIP_DIRS)}", file=sys.stderr)

        is_git_repo = is_git_checkout(root_path)

        # truffleHog handles secrets (including full git history) when available
        secrets_via_trufflehog = False
        findings: List[Finding] = []
        self.engine_used = ENGINE_REGEX
        self.fell_back = False
        self.fallback_reason = ""
        if self._scan_secrets and self._trufflehog_mode != "off":
            trufflehog_results = self._try_trufflehog(root_path)
            if trufflehog_results is not None:
                findings.extend(trufflehog_results)
                secrets_via_trufflehog = True
                method = (self._discovery or {}).get("method")
                self.engine_used = ENGINE_CONTAINER if method == "container" else ENGINE_NATIVE
            else:
                self.fell_back = True
                self.fallback_reason = self._trufflehog_failure or "truffleHog failed"
                if self.debug:
                    print(
                        f"[debug] secret scan engine: regex (fell back: {self.fallback_reason})",
                        file=sys.stderr,
                    )

        # Single walk: large files, AI context files, text content.
        # Always run regex secret patterns even when trufflehog succeeds —
        # trufflehog is a credential verifier that needs paired keys (e.g.
        # both AKIA + secret access key); our regex catches standalone patterns.
        # See: https://github.com/trufflesecurity/trufflehog/issues/2940
        walk_findings, skipped = self._unified_walk(
            root_path, scan_secrets=self._scan_secrets, is_git_repo=is_git_repo
        )
        if secrets_via_trufflehog:
            # Deduplicate: drop regex secret findings already covered by trufflehog
            th_locations = {(f.file_path, f.line_number) for f in findings
                           if f.category == FindingCategory.SECRET}
            walk_findings = [
                f for f in walk_findings
                if f.category != FindingCategory.SECRET
                or (f.file_path, f.line_number) not in th_locations
            ]
        findings.extend(walk_findings)
        self.skipped_committed_dirs = sorted(skipped)

        findings.extend(self._check_ai_context_history(root_path, is_git_repo=is_git_repo))
        findings.extend(self._check_email_history(root_path, is_git_repo=is_git_repo))

        return findings

    def _unified_walk(
        self, root_path: str, scan_secrets: bool = True, is_git_repo: bool = False
    ) -> Tuple[List[Finding], Set[str]]:
        """Single os.walk() pass covering large files, AI context files, and text content.

        Returns (findings, skipped_dirs) where skipped_dirs is the set of SKIP_DIRS
        subdirectory paths (relative to root_path) actually encountered during the walk.
        .git is excluded from skipped_dirs — its presence is always expected.

        When is_git_repo is True, SKIP_DIRS that contain tracked files are scanned
        instead of skipped.
        """
        # In a git repo only tracked files get pushed, so scan just those —
        # walking ignored build output / binaries can take minutes (#84).
        tracked = self._tracked_files(root_path) if is_git_repo else None
        tracked_dirs: Set[str] = set()
        if tracked is not None:
            for p in tracked:
                parts = p.split("/")[:-1]
                for i in range(1, len(parts) + 1):
                    tracked_dirs.add("/".join(parts[:i]))
        total = len(tracked) if tracked is not None else None
        try:
            return self._walk(root_path, scan_secrets, is_git_repo,
                              tracked, tracked_dirs, total)
        finally:
            if self._progress:
                self._clear_progress()

    def _walk(self, root_path, scan_secrets, is_git_repo, tracked, tracked_dirs, total):
        findings: List[Finding] = []
        skipped_dirs: Set[str] = set()
        done = 0

        for dirpath, dirs, files in os.walk(root_path, followlinks=False):
            # Track and prune SKIP_DIRS; in git repos, scan dirs with tracked files
            skip_set: Set[str] = set()
            for d in list(dirs):
                if d not in SKIP_DIRS:
                    continue
                if d == ".git":
                    skip_set.add(d)
                    continue
                rel = os.path.relpath(os.path.join(dirpath, d), root_path)
                if is_git_repo and self._is_committed(root_path, rel):
                    pass  # committed — scan it, don't add to skipped_dirs
                else:
                    skip_set.add(d)
                    skipped_dirs.add(rel)
            dirs[:] = [d for d in dirs if d not in skip_set]
            if tracked is not None:
                dirs[:] = [
                    d for d in dirs
                    if os.path.relpath(os.path.join(dirpath, d), root_path)
                    .replace(os.sep, "/") in tracked_dirs
                ]

            # AI context directory check (.cursor)
            if self._warn_ai_context_files:
                for d in list(dirs):
                    if d.lower() == ".cursor":
                        full_path = os.path.join(dirpath, d)
                        rel_path = os.path.relpath(full_path, root_path).replace(os.sep, "/")
                        dirs.remove(d)
                        if self._is_excluded(rel_path):
                            continue
                        # In a git repo, only flag if something under the dir
                        # is tracked — untracked/ignored files won't be pushed.
                        if is_git_repo and not self._is_committed(root_path, rel_path):
                            continue
                        findings.append(Finding(
                            severity=Severity.CRITICAL,
                            category=FindingCategory.AI_CONTEXT_FILE,
                            file_path=rel_path,
                            line_number=0,
                            rule="AI context file",
                            match=_ai_context_hint(rel_path),
                        ))

            for filename in files:
                full_path = os.path.join(dirpath, filename)
                rel_path = os.path.relpath(full_path, root_path).replace(os.sep, "/")

                if tracked is not None and rel_path not in tracked:
                    continue
                done += 1
                if self._progress:
                    self._draw_progress(done, total, rel_path)

                # Path exclusion check — skip before any other per-file work
                if self._is_excluded(rel_path):
                    continue

                # AI context file check (by filename / relative path)
                if self._warn_ai_context_files:
                    if (filename.lower() in _AI_CONTEXT_BASENAMES
                            or rel_path.lower() in _AI_CONTEXT_REL_PATHS):
                        # In a git repo, only flag tracked files —
                        # untracked/gitignored files won't be pushed.
                        if not is_git_repo or self._is_tracked(root_path, rel_path):
                            findings.append(Finding(
                                severity=Severity.CRITICAL,
                                category=FindingCategory.AI_CONTEXT_FILE,
                                file_path=rel_path,
                                line_number=0,
                                rule="AI context file",
                                match=_ai_context_hint(rel_path),
                            ))

                # Large file check
                try:
                    size = os.path.getsize(full_path)
                except OSError:
                    continue
                if size > self._max_file_size_bytes:
                    size_mb = size / (1024 * 1024)
                    findings.append(Finding(
                        severity=Severity.WARNING,
                        category=FindingCategory.LARGE_FILE,
                        file_path=rel_path,
                        line_number=0,
                        rule="Large file",
                        match=f"{size_mb:.1f} MB",
                    ))
                    continue  # skip content scanning for large files

                # Skip binary files for text content scanning
                _, ext = os.path.splitext(filename)
                if ext.lower() in BINARY_EXTENSIONS:
                    continue
                # None for binary (NUL bytes that are not UTF-16 or a few
                # stray NULs in UTF-8 text) or unreadable files
                text = _read_text(full_path)
                if text is None:
                    continue

                for line_number, line in enumerate(_split_lines(text), start=1):
                    # Secret patterns and banned strings are matched again
                    # with hidden characters removed (#157)
                    normalized = _normalize_line(line) if (
                        scan_secrets or self._banned_strings) else None

                    if scan_secrets:
                        for severity, patterns in (
                            (Severity.CRITICAL, _CRITICAL_PATTERNS),
                            (Severity.WARNING, _WARNING_PATTERNS),
                        ):
                            for pattern, rule_name in patterns:
                                hidden = _pattern_hit(pattern, line, normalized)
                                if hidden is None:
                                    continue
                                findings.append(Finding(
                                    severity=severity,
                                    category=FindingCategory.SECRET,
                                    file_path=rel_path,
                                    line_number=line_number,
                                    rule=_obfuscated_rule(rule_name, hidden),
                                    match="[redacted]",
                                    hidden=hidden,
                                ))

                    if self._scan_emails:
                        m = EMAIL_PATTERN.search(line)
                        if m and not self._is_email_excluded(m.group(0)):
                            findings.append(Finding(
                                severity=Severity.WARNING,
                                category=FindingCategory.EMAIL,
                                file_path=rel_path,
                                line_number=line_number,
                                rule="Email address",
                                match=m.group(0),
                            ))

                    if self._scan_todos:
                        if TODO_PATTERN.search(line):
                            findings.append(Finding(
                                severity=Severity.INFO,
                                category=FindingCategory.TODO,
                                file_path=rel_path,
                                line_number=line_number,
                                rule="TODO/FIXME/HACK/XXX comment",
                                match=line.rstrip()[:80],
                            ))

                    line_lower = line.lower()
                    forms_lower = [f.lower() for f in normalized[0]] if normalized else []
                    for banned in self._banned_strings:
                        needle = banned.lower()
                        if needle in line_lower:
                            hidden = ""
                        elif any(needle in form for form in forms_lower):
                            hidden = normalized[1]
                        else:
                            continue
                        findings.append(Finding(
                            severity=Severity.CRITICAL,
                            category=FindingCategory.BANNED_STRING,
                            file_path=rel_path,
                            line_number=line_number,
                            rule=_obfuscated_rule(f"Banned string: {banned}", hidden),
                            match="[redacted]",
                            hidden=hidden,
                        ))

        return findings, skipped_dirs

    def _check_ai_context_history(self, root_path: str, is_git_repo: bool = False) -> List[Finding]:
        """Check git history for AI context files deleted from the working tree.

        Only runs when is_git_repo is True. For each candidate file, skips those
        still present in the working tree (already caught by _unified_walk). Runs
        `git log --all --full-history --oneline -- <path>`; any output means the
        file existed in at least one commit.
        """
        if not self._warn_ai_context_files:
            return []
        if not is_git_repo:
            return []

        findings: List[Finding] = []
        for display_path, git_path in _AI_CONTEXT_HISTORY_CANDIDATES:
            if self._is_excluded(display_path):
                continue
            full = os.path.join(root_path, display_path.replace("/", os.sep))
            if os.path.exists(full):
                continue  # still present — _unified_walk already flagged it
            try:
                result = subprocess.run(
                    ["git", "-C", root_path, "log", "--all", "--full-history",
                     "--oneline", "--", git_path],
                    capture_output=True, text=True,
                )
            except FileNotFoundError:
                if self.debug:
                    print("[debug] git not found; skipping AI context history check",
                          file=sys.stderr)
                break
            if result.stdout.strip():
                findings.append(Finding(
                    severity=Severity.CRITICAL,
                    category=FindingCategory.AI_CONTEXT_FILE,
                    file_path=display_path,
                    line_number=0,
                    rule="AI context file in git history",
                    match=_ai_context_history_hint(display_path),
                ))
        return findings

    def _check_email_history(self, root_path: str, is_git_repo: bool = False) -> List[Finding]:
        """Check git history for email addresses using git log -G.

        Deduplicates by (email_lowercase, file_path), keeping the earliest commit.
        Gated on scan_for_emails and scan_email_history config bools.
        """
        if not self._scan_emails or not self._scan_email_history:
            return []
        if not is_git_repo:
            return []

        try:
            result = subprocess.run(
                ["git", "-C", root_path, "log", "--all",
                 "-G", EMAIL_PATTERN.pattern,
                 "--format=%H%x00%h%x00%aI",
                 "-p", "--diff-filter=ACMR"],
                capture_output=True, text=True, timeout=60,
            )
        except FileNotFoundError:
            if self.debug:
                print("[debug] git not found; skipping email history check",
                      file=sys.stderr)
            return []
        except subprocess.TimeoutExpired:
            if self.debug:
                print("[debug] git log timed out; skipping email history check",
                      file=sys.stderr)
            return []

        if result.returncode != 0:
            if self.debug:
                print(f"[debug] git log exited {result.returncode}; skipping email history check",
                      file=sys.stderr)
            return []

        # Parse output: format lines give commit info, diff lines give file+email
        current_hash = ""
        current_short = ""
        current_ts = ""
        current_file = ""
        # (email_lower, file_path) → Finding — keep last seen (= earliest commit in reverse-chron)
        seen: dict = {}

        for line in result.stdout.splitlines():
            # Format line: full_hash\x00short_hash\x00timestamp
            if "\x00" in line:
                parts = line.split("\x00", 2)
                if len(parts) == 3:
                    current_hash, current_short, current_ts = parts
                continue

            # diff --git a/path b/path
            if line.startswith("diff --git "):
                m = re.match(r"diff --git a/(.*) b/(.*)", line)
                if m:
                    current_file = m.group(2)
                continue

            # Added lines (not the +++ header) — strip leading '+' so it
            # doesn't get captured by EMAIL_PATTERN's [._%+\-] class.
            if line.startswith("+") and not line.startswith("+++"):
                content = line[1:]
                for m in EMAIL_PATTERN.finditer(content):
                    email = m.group(0)
                    if self._is_email_excluded(email):
                        continue
                    if self._is_excluded(current_file):
                        continue
                    key = (email.lower(), current_file)
                    # Overwrite: git log is reverse-chron, so last write = earliest commit
                    seen[key] = Finding(
                        severity=Severity.WARNING,
                        category=FindingCategory.EMAIL,
                        file_path=current_file,
                        line_number=0,
                        rule="Email address in git history",
                        match=email,
                        commit=current_short,
                        timestamp=current_ts,
                    )

        return list(seen.values())

    def _build_trufflehog_config(self, strings: List[str]) -> str:
        """Write a temporary truffleHog YAML config with a custom banned-strings detector.

        Uses a single case-insensitive alternation regex so all strings are checked
        in one pass. Returns the path to the temp file; caller must delete it.
        """
        # re.escape handles any regex special chars in the user's strings
        pattern = "(?i)(" + "|".join(re.escape(s) for s in strings) + ")"

        # YAML single-quoted strings: the only escape needed is '' for a literal '
        def sq(s):
            return "'" + s.replace("'", "''") + "'"

        kw_lines = "\n".join(f"      - {sq(s.lower())}" for s in strings)
        yaml_content = (
            "detectors:\n"
            "  - name: banned-strings\n"
            "    keywords:\n"
            f"{kw_lines}\n"
            "    regex:\n"
            f"      match: {sq(pattern)}\n"
            "    verify: []\n"
        )

        fd, path = tempfile.mkstemp(suffix=".yaml", prefix="gh-safe-repo-banned-")
        with os.fdopen(fd, "w") as f:
            f.write(yaml_content)
        return path

    def _try_trufflehog(self, root_path: str) -> Optional[List[Finding]]:
        # Resolve symlinks so the path we pass to trufflehog matches the
        # "file" paths it emits in JSON output.  Without this, on macOS
        # /tmp (→ /private/tmp) causes os.path.relpath() to produce a
        # traversal string rather than the correct relative path.
        root_path = os.path.realpath(root_path)

        disc = self._run_discovery()
        if disc["method"] == "none":
            return None

        is_git_repo = is_git_checkout(root_path)

        # Build optional banned-strings config file
        config_path: Optional[str] = None
        if self._banned_strings:
            config_path = self._build_trufflehog_config(self._banned_strings)

        # Build optional exclude-paths file (newline-separated regexes for truffleHog)
        exclude_path_file: Optional[str] = None
        if self._exclude_path_strings:
            fd, exclude_path_file = tempfile.mkstemp(
                suffix=".txt", prefix="gh-safe-repo-exclude-"
            )
            with os.fdopen(fd, "w") as f:
                f.write("\n".join(self._exclude_path_strings) + "\n")

        try:
            if disc["method"] == "native":
                # Native trufflehog on PATH
                if is_git_repo:
                    cmd = ["trufflehog", "git", f"file://{root_path}", "--json", "--no-update", "--results=verified,unverified"]
                else:
                    cmd = ["trufflehog", "filesystem", root_path, "--json", "--no-update", "--results=verified,unverified"]
                if config_path:
                    cmd += ["--config", config_path]
                if exclude_path_file:
                    cmd += ["--exclude-paths", exclude_path_file]

            else:
                # Container mode — mirror the shell wrapper's volume-mount logic
                image = os.environ.get(
                    "TRUFFLEHOG_IMAGE", "ghcr.io/trufflesecurity/trufflehog:latest"
                )
                runtime = disc["runtime_path"]
                volume_args = ["--volume", f"{root_path}:{root_path}:ro"]
                # A worktree or submodule .git file points outside root_path;
                # mount the repository it names too, or the git scan fails
                # inside the container (#103).
                git_dir = external_git_dir(root_path) if is_git_repo else None
                if git_dir:
                    volume_args += ["--volume", f"{git_dir}:{git_dir}:ro"]
                if config_path:
                    volume_args += ["--volume", f"{config_path}:{config_path}:ro"]
                if exclude_path_file:
                    volume_args += ["--volume", f"{exclude_path_file}:{exclude_path_file}:ro"]
                if is_git_repo:
                    th_args = ["git", f"file://{root_path}", "--json", "--no-update", "--results=verified,unverified"]
                else:
                    th_args = ["filesystem", root_path, "--json", "--no-update", "--results=verified,unverified"]
                if config_path:
                    th_args += ["--config", config_path]
                if exclude_path_file:
                    th_args += ["--exclude-paths", exclude_path_file]
                cmd = [runtime, "run", "--rm"] + volume_args + [image] + th_args

            # Stream stderr to the terminal in real-time so the user can
            # see trufflehog progress (especially useful for slow container
            # runs), and keep a copy: truffleHog reports a scan it could not
            # run there, not in its exit code (#134).  stdout is still
            # captured for JSON parsing.  On a TTY a status line shows the
            # phase and elapsed time meanwhile (#10).
            status = None
            if self._progress:
                if disc["method"] == "native":
                    status = _TruffleHogStatus("truffleHog", "starting")
                else:
                    status = _TruffleHogStatus(
                        "truffleHog via "
                        + safe_display(disc.get("runtime", "container")),
                        "starting container",
                    )
            stdout, stderr_text, returncode = self._run_teeing_stderr(cmd, status)
            result = subprocess.CompletedProcess(
                args=cmd, returncode=returncode,
                stdout=stdout, stderr=stderr_text,
            )

        except FileNotFoundError:
            self._trufflehog_failure = f"could not execute {cmd[0]!r} (not found)"
            if self.debug:
                print(
                    "[debug] trufflehog invocation failed (binary not found), falling back to regex",
                    file=sys.stderr,
                )
            return None
        finally:
            if config_path and os.path.exists(config_path):
                os.unlink(config_path)
            if exclude_path_file and os.path.exists(exclude_path_file):
                os.unlink(exclude_path_file)

        # truffleHog v3 exit codes (tested against v3.93.4):
        #   0 = scan completed — may have 0 or more unverified findings in JSON
        #   1 = scan completed with verified findings
        #   anything else = crash / wrong version / unexpected error
        # JSON stdout is the authoritative source of findings regardless of
        # exit code; the code below parses it in all (0, 1) cases.
        if result.returncode not in (0, 1):
            how = (
                "truffleHog" if disc["method"] == "native"
                else f"truffleHog via {disc.get('runtime', 'container')}"
            )
            self._trufflehog_failure = f"{how} exited with status {result.returncode}"
            if self.debug:
                print(
                    f"[debug] trufflehog exited with {result.returncode}, falling back to regex",
                    file=sys.stderr,
                )
            return None

        findings = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                # `trufflehog git` emits Git metadata; `trufflehog filesystem`
                # emits Filesystem metadata.  Both carry the same file/line fields.
                src = (
                    data["SourceMetadata"]["Data"].get("Git")
                    or data["SourceMetadata"]["Data"].get("Filesystem")
                )
                if not src:
                    continue
                raw_file = src.get("file", "")
                if os.path.isabs(raw_file):
                    file_path = os.path.relpath(raw_file, root_path)
                else:
                    file_path = raw_file
                line_number = int(src.get("line", 0))
                commit = src.get("commit", "")[:8]
                timestamp = src.get("timestamp", "")
                detector = data.get("DetectorName", "unknown detector")
                if detector == "banned-strings":
                    findings.append(Finding(
                        severity=Severity.CRITICAL,
                        category=FindingCategory.BANNED_STRING,
                        file_path=file_path,
                        line_number=line_number,
                        rule="Banned string found",
                        match="[redacted]",
                        commit=commit,
                        timestamp=timestamp,
                    ))
                else:
                    findings.append(Finding(
                        severity=Severity.CRITICAL,
                        category=FindingCategory.SECRET,
                        file_path=file_path,
                        line_number=line_number,
                        rule=f"Secret detected by truffleHog ({detector})",
                        match="[redacted]",
                        commit=commit,
                        timestamp=timestamp,
                    ))
            except (KeyError, TypeError, ValueError):
                continue

        # Exit 0 or 1 does not prove the scan ran: truffleHog logs "error
        # running scan" (e.g. "failed to read index file" on a repository
        # with no commits) and still exits with a code that reads as "done".
        # A scan that did not run must not be reported as clean (#134).
        scan_error = _trufflehog_scan_error(
            result.stderr, result.returncode, bool(findings)
        )
        if scan_error:
            self._trufflehog_failure = f"truffleHog could not scan: {scan_error}"
            if self.debug:
                print(
                    "[debug] trufflehog reported a scan error, falling back to regex",
                    file=sys.stderr,
                )
            return None

        return findings

    @staticmethod
    def _run_teeing_stderr(
        cmd: List[str], status: Optional["_TruffleHogStatus"] = None,
    ) -> Tuple[str, str, int]:
        """Run cmd; return (stdout, stderr, returncode), echoing stderr to
        ours as it arrives.

        stderr goes through a pipe we own, read on a thread in raw chunks so
        progress output is not held back until a newline, while
        communicate() collects stdout.

        With `status`, a one-line status (phase and elapsed time) is kept at
        the bottom of the terminal while the child runs, and cleared around
        every echoed chunk and at the end (#10). Callers pass one only when
        stderr is a TTY and not in --json mode.
        """
        read_fd, write_fd = os.pipe()
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=write_fd, text=True,
            )
        except BaseException:
            os.close(read_fd)
            raise
        finally:
            # The child has its own copy; ours must close or the reader
            # never sees EOF.
            os.close(write_fd)

        chunks: List[bytes] = []

        def pump():
            # The echo is escaped like every other untrusted output (#125):
            # truffleHog's JSON log escapes C0 controls but passes C1 and
            # bidi characters from file names through. The incremental
            # decoder keeps a multi-byte character split across two reads
            # intact. The child sees a pipe, not a TTY, so the container
            # runtime does not draw \r progress bars that escaping would
            # garble.
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

            def echo(text):
                if not text:
                    return
                if status is not None:
                    # Clears the status line, writes, then redraws it.
                    status.echo(safe_display(text, multiline=True), text)
                    return
                try:
                    sys.stderr.write(safe_display(text, multiline=True))
                    sys.stderr.flush()
                except (OSError, ValueError):
                    pass  # our stderr went away; keep draining

            with os.fdopen(read_fd, "rb", buffering=0) as stream:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        echo(decoder.decode(b"", final=True))
                        return
                    chunks.append(chunk)
                    echo(decoder.decode(chunk))

        reader = threading.Thread(target=pump, daemon=True)
        # Started before the reader, so the first echoed chunk already
        # finds the line drawn and the clock running.
        if status is not None:
            status.start()
        try:
            reader.start()
            stdout, _ = proc.communicate()
            reader.join()
        finally:
            if status is not None:
                status.stop()
        return (
            stdout,
            b"".join(chunks).decode("utf-8", errors="replace"),
            proc.returncode,
        )


# --- truffleHog status line (#10) ---

class _TruffleHogStatus:
    """Single-line stderr status while truffleHog runs: what it is doing and
    how long it has been running.

    Since #134 the child's stderr is a pipe, so podman draws no image-pull
    progress bars of its own, and truffleHog logs nothing between "running
    source" and "finished scanning". Without this a first container run (an
    image pull) or a large repository shows nothing for minutes.

    The phase is read from complete stderr lines: podman's "Trying to pull"
    and truffleHog's "running source" / "finished scanning" log messages.
    Only our own fixed strings are drawn, never text from the stream.

    Drawn the same way as the regex walk's progress line (carriage return
    plus erase-line), TTY-only. One lock serializes the ticker and the
    stderr relay: the line is cleared before each echoed chunk and redrawn
    only when the cursor is back at the start of a line, so a partial
    relayed line is never followed by the status text.
    """

    _CLEAR = "\r\033[K"
    PULLING = "pulling image"
    SCANNING = "scanning"
    FINISHING = "finishing"

    def __init__(self, label: str, initial_phase: str, interval: float = 1.0):
        self._label = label
        self._phase = initial_phase
        self._interval = interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._start = 0.0
        self._drawn = False
        self._at_line_start = True
        self._partial = ""

    # --- lifecycle ---

    def start(self) -> None:
        self._start = time.monotonic()
        with self._lock:
            self._draw()
        self._thread = threading.Thread(target=self._tick, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        with self._lock:
            self._clear()

    def _tick(self) -> None:
        while not self._stop.wait(self._interval):
            with self._lock:
                self._draw()

    # --- relay ---

    def echo(self, display_text: str, raw_text: str) -> None:
        """Write one escaped stderr chunk with the status line out of the
        way. raw_text (unescaped) is only matched to pick the phase."""
        with self._lock:
            self._feed(raw_text)
            self._clear()
            try:
                sys.stderr.write(display_text)
                sys.stderr.flush()
            except (OSError, ValueError):
                pass  # our stderr went away; keep draining
            self._at_line_start = display_text.endswith("\n")
            self._draw()

    def _feed(self, text: str) -> None:
        lines = (self._partial + text).split("\n")
        self._partial = lines.pop()[-4096:]
        for line in lines:
            if "Trying to pull" in line:
                self._phase = self.PULLING
            elif '"msg":"running source"' in line:
                self._phase = self.SCANNING
            elif '"msg":"finished scanning"' in line:
                self._phase = self.FINISHING

    # --- drawing (lock held) ---

    def _line(self) -> str:
        secs = int(time.monotonic() - self._start)
        return f"{self._label}: {self._phase}... {secs // 60}:{secs % 60:02d}"

    def _draw(self) -> None:
        if not self._at_line_start or self._stop.is_set():
            return
        width = shutil.get_terminal_size((80, 20)).columns - 1
        try:
            sys.stderr.write(self._CLEAR + self._line()[:width])
            sys.stderr.flush()
        except (OSError, ValueError):
            return
        self._drawn = True

    def _clear(self) -> None:
        if not self._drawn:
            return
        try:
            sys.stderr.write(self._CLEAR)
            sys.stderr.flush()
        except (OSError, ValueError):
            pass
        self._drawn = False


# --- Module-level utility ---

# The message truffleHog's main() logs when the scan itself fails.
_TRUFFLEHOG_SCAN_FAILED_MSG = "error running scan"


def _trufflehog_scan_error(stderr: str, returncode: int, has_findings: bool) -> Optional[str]:
    """Why a truffleHog run that exited 0 or 1 did not actually scan, or None.

    With --json, truffleHog writes its log to stderr as one JSON object per
    line. Two cases count as "did not scan":

    - a line at level error/fatal whose msg is "error running scan",
      whatever the exit code;
    - exit 1 with no finding on stdout and any error/fatal line: exit 1 is
      meant to say "findings", so here it can only be a fatal error.

    Other error-level lines (a detector or a single chunk failing) do not
    fail the scan. Lines that are not JSON (container runtime output) are
    ignored. The returned text has control characters removed and is capped,
    since it is printed.
    """
    first_error = None
    for line in (stderr or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        if str(entry.get("level", "")).lower() not in ("error", "fatal"):
            continue
        msg = str(entry.get("msg", ""))
        detail = str(entry.get("error") or msg or "unknown error")
        if msg == _TRUFFLEHOG_SCAN_FAILED_MSG:
            return _printable(detail)
        if first_error is None:
            first_error = detail
    if first_error is not None and returncode == 1 and not has_findings:
        return _printable(first_error)
    return None


def _printable(text: str, limit: int = 300) -> str:
    text = "".join(c if c.isprintable() else " " for c in text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _view_hint(f: "Finding") -> str:
    """Build a 'View with: ...' command string for a finding.

    File paths and commit hashes originate from untrusted content
    (trufflehog JSON, filesystem walk), so every interpolated value
    is shell-quoted via shlex.quote().
    """
    if not f.line_number and not f.commit:
        return ""
    if f.commit:
        ref = shlex.quote(f"{f.commit}:{f.file_path}")
        if not f.line_number:
            return f"git show {ref}"
        ctx = 4  # lines of context on each side
        start = max(1, f.line_number - ctx)
        end = f.line_number + ctx
        return f"git show {ref} | sed -n '{start},{end}p'"
    ctx = 4
    start = max(1, f.line_number - ctx)
    end = f.line_number + ctx
    hint = f"sed -n '{start},{end}p' {shlex.quote(f.file_path)}"
    # The terminal would hide the characters that split the match again
    return hint + " | cat -v" if f.hidden else hint


def format_findings(findings: List[Finding]) -> str:
    """
    Pure formatting: "[SEVERITY] rule in file:line" + view hint + match line.
    Used in tests to verify output shape without ANSI codes.

    Untrusted values (paths, rule and commit data, matched text) are passed
    through safe_display(), as in the CLI's own report (#125).
    """
    if not findings:
        return ""
    lines = []
    for f in findings:
        loc = f.file_path + (f":{f.line_number}" if f.line_number else "")
        if f.commit:
            loc += f" (commit {f.commit}"
            if f.timestamp:
                loc += f", {f.timestamp}"
            loc += ")"
        lines.append(f"[{f.severity.value}] {safe_display(f.rule)} in {safe_display(loc)}")
        hint = _view_hint(f)
        if hint:
            lines.append(f"  View with: {safe_display(hint)}")
        if f.match and f.match != "[redacted]":
            lines.append(f"  {safe_display(f.match[:80], multiline=True)}")
    return "\n".join(lines)
