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
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from enum import Enum
from typing import Generator, List, Optional


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


# --- Dataclass ---

@dataclass
class Finding:
    severity: Severity
    category: FindingCategory
    file_path: str      # relative to scanned root
    line_number: int    # 0 = file-level (large file, etc.)
    rule: str           # human-readable rule name
    match: str          # "[redacted]" for secrets, literal for emails/todos


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


# --- Scanner class ---

class SecurityScanner:
    def __init__(self, config, debug=False):
        self.debug = debug
        self._scan_secrets = config.getbool("pre_flight_scan", "scan_for_secrets", fallback=True)
        self._scan_emails  = config.getbool("pre_flight_scan", "scan_for_emails", fallback=True)
        self._scan_todos   = config.getbool("pre_flight_scan", "scan_for_todos", fallback=True)
        self._use_trufflehog = config.getbool("pre_flight_scan", "use_trufflehog", fallback=True)
        # float() then int() to allow decimal config values like "0.001" for tests
        self._max_file_size_bytes = int(
            float(config.get("pre_flight_scan", "max_file_size_mb", fallback="100")) * 1024 * 1024
        )
        # Banned strings: split on newlines and commas, strip whitespace, drop empties
        raw = config.get("pre_flight_scan", "banned_strings", fallback="")
        self._banned_strings = [s.strip() for s in re.split(r"[\n,]", raw) if s.strip()]

    def scan(self, root_path: str) -> List[Finding]:
        findings = []

        # Always scan for large files (truffleHog doesn't report sizes)
        findings.extend(self._scan_large_files(root_path))

        if self._scan_secrets and self._use_trufflehog:
            trufflehog_results = self._try_trufflehog(root_path)
            if trufflehog_results is not None:
                # truffleHog handled secrets; still run regex for emails/TODOs
                findings.extend(trufflehog_results)
                findings.extend(self._scan_regex(root_path, secrets=False))
            else:
                # truffleHog unavailable or failed; fall back to regex for everything
                findings.extend(self._scan_regex(root_path, secrets=True))
        else:
            findings.extend(self._scan_regex(root_path, secrets=self._scan_secrets))

        return findings

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

        # Use `trufflehog git` when scanning a git repo so the full commit
        # history is scanned — not just the working-tree snapshot.
        # `trufflehog filesystem` is the fallback for non-git directories
        # (e.g. the --scan command pointed at an arbitrary directory).
        is_git_repo = os.path.isdir(os.path.join(root_path, ".git"))
        if is_git_repo:
            cmd = ["trufflehog", "git", f"file://{root_path}", "--json", "--no-update"]
        else:
            cmd = ["trufflehog", "filesystem", root_path, "--json", "--no-update"]

        config_path = None
        if self._banned_strings:
            config_path = self._build_trufflehog_config(self._banned_strings)
            cmd += ["--config", config_path]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            if self.debug:
                print("[debug] trufflehog not found, falling back to regex scanner", file=sys.stderr)
            if config_path:
                os.unlink(config_path)
            return None
        finally:
            if config_path and os.path.exists(config_path):
                os.unlink(config_path)

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
                file_path = os.path.relpath(src.get("file", ""), root_path)
                line_number = int(src.get("line", 0))
                detector = data.get("DetectorName", "unknown detector")
                if detector == "banned-strings":
                    findings.append(Finding(
                        severity=Severity.CRITICAL,
                        category=FindingCategory.BANNED_STRING,
                        file_path=file_path,
                        line_number=line_number,
                        rule="Banned string found",
                        match="[redacted]",
                    ))
                else:
                    findings.append(Finding(
                        severity=Severity.CRITICAL,
                        category=FindingCategory.SECRET,
                        file_path=file_path,
                        line_number=line_number,
                        rule=f"Secret detected by truffleHog ({detector})",
                        match="[redacted]",
                    ))
            except (KeyError, TypeError, ValueError):
                continue

        return findings

    def _scan_regex(self, root_path: str, secrets: bool = True) -> List[Finding]:
        findings = []
        for file_path in self._walk_text_files(root_path):
            rel_path = os.path.relpath(file_path, root_path)
            try:
                with open(file_path, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except OSError:
                continue

            for line_number, line in enumerate(lines, start=1):
                if secrets:
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
                    if m:
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

        return findings

    def _scan_large_files(self, root_path: str) -> List[Finding]:
        findings = []
        for dirpath, dirs, files in os.walk(root_path, followlinks=False):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for filename in files:
                full_path = os.path.join(dirpath, filename)
                try:
                    size = os.path.getsize(full_path)
                except OSError:
                    continue
                if size > self._max_file_size_bytes:
                    rel_path = os.path.relpath(full_path, root_path)
                    size_mb = size / (1024 * 1024)
                    findings.append(Finding(
                        severity=Severity.WARNING,
                        category=FindingCategory.LARGE_FILE,
                        file_path=rel_path,
                        line_number=0,
                        rule="Large file",
                        match=f"{size_mb:.1f} MB",
                    ))
        return findings

    def _walk_text_files(self, root_path: str) -> Generator[str, None, None]:
        for dirpath, dirs, files in os.walk(root_path, followlinks=False):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for filename in files:
                _, ext = os.path.splitext(filename)
                if ext.lower() in BINARY_EXTENSIONS:
                    continue
                yield os.path.join(dirpath, filename)


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
        lines.append(f"[{f.severity.value}] {f.rule} in {loc}")
        if f.match and f.match != "[redacted]":
            lines.append(f"  {f.match[:80]}")
    return "\n".join(lines)
