"""
Pre-flight security scanner for the --from --public workflow.

Detects hardcoded secrets, emails, large files, and TODOs before
a private repo is mirrored to a public repository.

truffleHog is used if installed; regex fallback otherwise.
Always runs locally — never in GitHub Actions.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Set, Tuple


# --- Module-level constants ---

BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".tiff", ".webp",
    ".mp3", ".mp4", ".avi", ".mov", ".mkv", ".wav", ".flac", ".ogg",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".rar", ".7z",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".class", ".pyc",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".sqlite", ".db",
}

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
TODO_PATTERN = re.compile(r"(?i)#\s*(?:TODO|FIXME|HACK|XXX)\b")


# --- Helpers ---

def _ai_context_hint(rel_path: str) -> str:
    """Build the remediation message for an AI context file finding."""
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
    def __init__(self, config, debug=False):
        self.debug = debug
        self._scan_secrets = config.getbool("pre_flight_scan", "scan_for_secrets", fallback=True)
        self._scan_emails  = config.getbool("pre_flight_scan", "scan_for_emails", fallback=True)
        self._scan_todos   = config.getbool("pre_flight_scan", "scan_for_todos", fallback=True)
        # trufflehog_mode: auto / native / docker / off
        mode = config.get("pre_flight_scan", "trufflehog_mode", fallback="auto")
        # Backwards-compat: use_trufflehog = false in old user configs → off
        if mode == "auto":
            old_flag = config.get("pre_flight_scan", "use_trufflehog", fallback=None)
            if old_flag is not None and old_flag.strip().lower() in ("false", "0", "no"):
                mode = "off"
        self._trufflehog_mode = mode
        # Discovery result cache: None = not yet run; dict after first _run_discovery() call
        self._discovery: Optional[dict] = None
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
            print(
                "\033[33mWarning:\033[0m unrecognised truffleHog version output "
                "— falling back to container or regex scanner",
                file=sys.stderr,
            )
            return None
        major = int(m.group(1))
        if major != 3:
            print(
                f"\033[33mWarning:\033[0m truffleHog v{m.group(0)} detected "
                f"(v3 required) — falling back to container or regex scanner",
                file=sys.stderr,
            )
            return None
        return m.group(0)   # e.g. "3.93.4"

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

        # Step 1: try native truffleHog (auto or native mode)
        if mode in ("auto", "native"):
            version = self._detect_native()
            if version:
                self._discovery = {"method": "native", "version": version}
                return self._discovery
            if mode == "native":
                print(
                    "\033[33mWarning:\033[0m truffleHog not found on PATH "
                    "(trufflehog_mode = native) — falling back to regex scanner",
                    file=sys.stderr,
                )
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
                print(
                    "\033[33mWarning:\033[0m trufflehog_mode = docker but no container runtime "
                    "(podman or docker) found — falling back to regex scanner",
                    file=sys.stderr,
                )
                self._discovery = {"method": "none"}
                return self._discovery

        # Step 3: nothing available (auto mode exhausted all options)
        print(
            "\033[33mWarning:\033[0m truffleHog not found and no container runtime available "
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
        return "regex only — see warning above"

    # --- Scanning ---

    def scan(self, root_path: str) -> List[Finding]:
        self.skipped_committed_dirs = []

        if self.debug:
            print(f"[debug] SKIP_DIRS: {sorted(SKIP_DIRS)}", file=sys.stderr)

        is_git_repo = os.path.isdir(os.path.join(root_path, ".git"))

        # truffleHog handles secrets (including full git history) when available
        secrets_via_trufflehog = False
        findings: List[Finding] = []
        if self._scan_secrets:
            trufflehog_results = self._try_trufflehog(root_path)
            if trufflehog_results is not None:
                findings.extend(trufflehog_results)
                secrets_via_trufflehog = True

        # Single walk: large files, AI context files, text content (secrets if no truffleHog)
        walk_findings, skipped = self._unified_walk(
            root_path, scan_secrets=not secrets_via_trufflehog, is_git_repo=is_git_repo
        )
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
        findings: List[Finding] = []
        skipped_dirs: Set[str] = set()

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

            # AI context directory check (.cursor)
            if self._warn_ai_context_files:
                for d in list(dirs):
                    if d.lower() == ".cursor":
                        full_path = os.path.join(dirpath, d)
                        rel_path = os.path.relpath(full_path, root_path).replace(os.sep, "/")
                        dirs.remove(d)
                        if not self._is_excluded(rel_path):
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

                # Path exclusion check — skip before any other per-file work
                if self._is_excluded(rel_path):
                    continue

                # AI context file check (by filename / relative path)
                if self._warn_ai_context_files:
                    if (filename.lower() in _AI_CONTEXT_BASENAMES
                            or rel_path.lower() in _AI_CONTEXT_REL_PATHS):
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

                # Text content scanning
                try:
                    with open(full_path, encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                except OSError:
                    continue

                for line_number, line in enumerate(lines, start=1):
                    if scan_secrets:
                        for pattern, rule_name in _CRITICAL_PATTERNS:
                            if pattern.search(line):
                                findings.append(Finding(
                                    severity=Severity.CRITICAL,
                                    category=FindingCategory.SECRET,
                                    file_path=rel_path,
                                    line_number=line_number,
                                    rule=rule_name,
                                    match="[redacted]",
                                ))
                        for pattern, rule_name in _WARNING_PATTERNS:
                            if pattern.search(line):
                                findings.append(Finding(
                                    severity=Severity.WARNING,
                                    category=FindingCategory.SECRET,
                                    file_path=rel_path,
                                    line_number=line_number,
                                    rule=rule_name,
                                    match="[redacted]",
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
                    for banned in self._banned_strings:
                        if banned.lower() in line_lower:
                            findings.append(Finding(
                                severity=Severity.CRITICAL,
                                category=FindingCategory.BANNED_STRING,
                                file_path=rel_path,
                                line_number=line_number,
                                rule=f"Banned string: {banned}",
                                match="[redacted]",
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

        is_git_repo = os.path.isdir(os.path.join(root_path, ".git"))

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
                    cmd = ["trufflehog", "git", f"file://{root_path}", "--json", "--no-update"]
                else:
                    cmd = ["trufflehog", "filesystem", root_path, "--json", "--no-update"]
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
                if config_path:
                    volume_args += ["--volume", f"{config_path}:{config_path}:ro"]
                if exclude_path_file:
                    volume_args += ["--volume", f"{exclude_path_file}:{exclude_path_file}:ro"]
                if is_git_repo:
                    th_args = ["git", f"file://{root_path}", "--json", "--no-update"]
                else:
                    th_args = ["filesystem", root_path, "--json", "--no-update"]
                if config_path:
                    th_args += ["--config", config_path]
                if exclude_path_file:
                    th_args += ["--exclude-paths", exclude_path_file]
                cmd = [runtime, "run", "--rm"] + volume_args + [image] + th_args

            # Stream stderr to the terminal in real-time so the user can
            # see trufflehog progress (especially useful for slow container
            # runs).  stdout is still captured for JSON parsing.
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=None, text=True,
            )
            stdout, _ = proc.communicate()
            result = subprocess.CompletedProcess(
                args=cmd, returncode=proc.returncode,
                stdout=stdout, stderr="",
            )

        except FileNotFoundError:
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

        return findings


# --- Module-level utility ---

def format_findings(findings: List[Finding]) -> str:
    """
    Pure formatting: "[SEVERITY] rule in file:line" + match line if not redacted.
    Used in tests to verify output shape without ANSI codes.
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
        lines.append(f"[{f.severity.value}] {f.rule} in {loc}")
        if f.match and f.match != "[redacted]":
            lines.append(f"  {f.match[:80]}")
    return "\n".join(lines)
