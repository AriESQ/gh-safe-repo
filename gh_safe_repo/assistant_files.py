"""
Catalog of AI-tool context, instruction and config files (#124).

The catalog is data (``data/ai_assistant_files.json``): one entry per path,
recording which tools read it, what kind of file it is (``category``) and
where that fact was documented. How worried gh-safe-repo is about each
category is a separate policy and does not live here.

Loading validates the whole document and raises ``CatalogError`` on the
first problem; a partly valid catalog is never returned. The same
validator (``validate_entries``) is meant for user-supplied extras later,
so it treats its input as untrusted: paths are confined to a small
character whitelist, globs are matched by hand (never compiled as a regex),
and text that will be printed to a terminal may not carry escape sequences.

Matching (``matches``) is case-insensitive, like the scanner, and works on
``/``-separated paths relative to the repository root:

- ``exact``  — the whole path equals ``path``.
- ``prefix`` — ``path`` names a directory; the directory itself and
  everything under it match.
- ``glob``   — the whole path matches ``path``, where ``*`` matches any run
  of characters within one segment and ``?`` one character; neither
  crosses ``/``.

``anchor = "root"`` (the default) compares against the path from the repo
root; ``anchor = "any"`` also tries every trailing run of segments, so
``CLAUDE.md`` with ``anchor = "any"`` matches ``pkg/CLAUDE.md``.
"""

import json
import unicodedata
from dataclasses import dataclass
from datetime import date
from importlib import resources
from typing import Any, Iterable, Optional, Tuple
from urllib.parse import urlsplit

from .errors import CatalogError

SCHEMA_VERSION = 1

MATCH_TYPES = ("exact", "prefix", "glob")
KINDS = ("file", "dir")
ANCHORS = ("root", "any")
CATEGORIES = (
    "instructions",
    "personal-settings",
    "mcp-config",
    "tool-settings",
    "ignore-file",
    "history",
)
SOURCE_TYPES = ("official", "community", "inferred")

MAX_PATH_LEN = 255
MAX_NAME_LEN = 100
MAX_NOTES_LEN = 500
MAX_URL_LEN = 2048

_PATH_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-/*?"
)
_GLOB_CHARS = frozenset("*?")

_TOP_KEYS = {"schema_version": True, "entries": True}  # key -> required
_ENTRY_KEYS = {
    "path": True,
    "match": True,
    "kind": True,
    "category": True,
    "tools": True,
    "appears_in": True,
    "anchor": False,
    "spec": False,
    "notes": False,
    "generic_name": False,
    "may_hold_secrets": False,
}
_TOOL_KEYS = {"name": True, "org": True}
_SOURCE_KEYS = {"url": True, "type": True, "checked": True}
_SPEC_KEYS = {"name": True, "org": True, "url": True}

_DATA_PACKAGE = "gh_safe_repo"
_DATA_DIR = "data"
_DATA_FILE = "ai_assistant_files.json"


# --- Model ---

@dataclass(frozen=True)
class Tool:
    name: str
    org: Optional[str]


@dataclass(frozen=True)
class Source:
    url: str
    type: str
    checked: date


@dataclass(frozen=True)
class Spec:
    name: str
    org: Optional[str]
    url: str


@dataclass(frozen=True)
class CatalogEntry:
    path: str
    match: str
    kind: Tuple[str, ...]
    category: str
    tools: Tuple[Tool, ...]
    appears_in: Tuple[Source, ...]
    anchor: str = "root"
    spec: Optional[Spec] = None
    notes: Optional[str] = None
    generic_name: bool = False
    may_hold_secrets: bool = False


@dataclass(frozen=True)
class Catalog:
    schema_version: int
    entries: Tuple[CatalogEntry, ...]

    def matching(self, rel_path: str) -> Tuple[CatalogEntry, ...]:
        """Every entry that matches ``rel_path``, in catalog order."""
        return tuple(e for e in self.entries if matches(rel_path, e))


# --- Field validators (each raises CatalogError naming the location) ---

def _fail(where: str, msg: str):
    raise CatalogError(f"AI assistant file catalog: {where}: {msg}")


def _check_keys(obj: Any, spec: dict, where: str) -> None:
    if not isinstance(obj, dict):
        _fail(where, f"expected an object, got {type(obj).__name__}")
    unknown = sorted(set(obj) - set(spec))
    if unknown:
        _fail(where, f"unknown key {_show(unknown[0])}")
    for key, required in spec.items():
        if required and key not in obj:
            _fail(where, f"missing required key {_show(key)}")


def _show(value: Any) -> str:
    """repr() is safe to print: it escapes control and escape characters."""
    return repr(value)


def _enum(value: Any, allowed: Iterable[str], where: str) -> str:
    allowed = tuple(allowed)
    if not isinstance(value, str) or value not in allowed:
        _fail(where, f"{_show(value)} is not one of {', '.join(allowed)}")
    return value


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        _fail(where, f"expected true or false, got {_show(value)}")
    return value


def validate_text(value: Any, where: str, max_len: int) -> str:
    """A single line of printable text: no control, format or separator characters.

    Rejects ESC (ANSI sequences), NUL, newlines, DEL/C1 controls (Unicode Cc),
    invisible formatting such as bidi overrides (Cf), and line/paragraph
    separators (Zl/Zp), since catalog text is printed to terminals.
    """
    if not isinstance(value, str):
        _fail(where, f"expected a string, got {type(value).__name__}")
    if not value.strip():
        _fail(where, "must not be empty")
    if len(value) > max_len:
        _fail(where, f"longer than {max_len} characters")
    for ch in value:
        if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"):
            _fail(where, f"contains control or non-printing character {_show(ch)}")
    return value


def _optional_text(value: Any, where: str, max_len: int) -> Optional[str]:
    return None if value is None else validate_text(value, where, max_len)


def validate_url(value: Any, where: str) -> str:
    """An ``https://`` URL with a host and no credentials. Data only; never fetched."""
    if not isinstance(value, str):
        _fail(where, f"expected a string, got {type(value).__name__}")
    if len(value) > MAX_URL_LEN:
        _fail(where, f"longer than {MAX_URL_LEN} characters")
    if any(not (0x21 <= ord(ch) <= 0x7E) for ch in value):
        _fail(where, "URL must be printable ASCII with no spaces")
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError as exc:
        _fail(where, f"invalid URL ({exc})")
    # Literal prefix check: urlsplit() lowercases the scheme, so it alone
    # would accept "HTTPS://"; the catalog keeps one canonical form.
    if not value.startswith("https://") or parts.scheme != "https":
        _fail(where, f"URL must use https://, got {_show(value)}")
    if not host:
        _fail(where, "URL has no host")
    if "@" in parts.netloc:
        _fail(where, "URL must not contain credentials")
    return value


def validate_date(value: Any, where: str, today: Optional[date] = None) -> date:
    """An ISO 8601 calendar date (``YYYY-MM-DD``) that is not in the future."""
    if not isinstance(value, str) or len(value) != 10:
        _fail(where, f"expected an ISO 8601 date (YYYY-MM-DD), got {_show(value)}")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        _fail(where, f"expected an ISO 8601 date (YYYY-MM-DD), got {_show(value)}")
    if parsed > (today or date.today()):
        _fail(where, f"date {value} is in the future")
    return parsed


def validate_path(value: Any, match: str, where: str) -> str:
    """A canonical relative path in the whitelisted character set.

    Rejects absolute and home-relative paths, ``..`` and ``.`` segments,
    empty segments (``a//b``, a trailing ``/``), backslashes, control
    characters and anything outside ``[A-Za-z0-9._-/*?]``. ``*`` and ``?``
    are allowed only when ``match`` is ``glob``, and a glob must use one.
    """
    if not isinstance(value, str):
        _fail(where, f"expected a string, got {type(value).__name__}")
    if not value:
        _fail(where, "must not be empty")
    if len(value) > MAX_PATH_LEN:
        _fail(where, f"longer than {MAX_PATH_LEN} characters")
    if value.startswith("/") or value.startswith("~"):
        _fail(where, f"path must be relative, got {_show(value)}")
    for ch in value:
        if ch not in _PATH_CHARS:
            _fail(where, f"character {_show(ch)} is not allowed in a path")
    for segment in value.split("/"):
        if segment == "":
            _fail(where, f"empty path segment in {_show(value)}")
        if segment in (".", ".."):
            _fail(where, f"{_show(segment)} segment not allowed in {_show(value)}")
    has_glob = any(ch in _GLOB_CHARS for ch in value)
    if match == "glob" and not has_glob:
        _fail(where, "a glob path must contain * or ?")
    if match != "glob" and has_glob:
        _fail(where, f"* and ? are only allowed when match is \"glob\", not {_show(match)}")
    return value


def _kind(value: Any, where: str) -> Tuple[str, ...]:
    if isinstance(value, str):
        return (_enum(value, KINDS, where),)
    if not isinstance(value, list) or not value:
        _fail(where, "expected \"file\", \"dir\" or a non-empty list of them")
    kinds = tuple(_enum(k, KINDS, f"{where}[{i}]") for i, k in enumerate(value))
    if len(set(kinds)) != len(kinds):
        _fail(where, "duplicate kind")
    return kinds


def _tool(obj: Any, where: str) -> Tool:
    _check_keys(obj, _TOOL_KEYS, where)
    return Tool(
        name=validate_text(obj["name"], f"{where}.name", MAX_NAME_LEN),
        org=_optional_text(obj["org"], f"{where}.org", MAX_NAME_LEN),
    )


def _source(obj: Any, where: str, today: Optional[date]) -> Source:
    _check_keys(obj, _SOURCE_KEYS, where)
    return Source(
        url=validate_url(obj["url"], f"{where}.url"),
        type=_enum(obj["type"], SOURCE_TYPES, f"{where}.type"),
        checked=validate_date(obj["checked"], f"{where}.checked", today),
    )


def _spec(obj: Any, where: str) -> Spec:
    _check_keys(obj, _SPEC_KEYS, where)
    return Spec(
        name=validate_text(obj["name"], f"{where}.name", MAX_NAME_LEN),
        org=_optional_text(obj["org"], f"{where}.org", MAX_NAME_LEN),
        url=validate_url(obj["url"], f"{where}.url"),
    )


def _nonempty_list(value: Any, where: str) -> list:
    if not isinstance(value, list) or not value:
        _fail(where, "expected a non-empty list")
    return value


def validate_entry(obj: Any, where: str = "entry", today: Optional[date] = None) -> CatalogEntry:
    """Validate one catalog entry and return it as a ``CatalogEntry``."""
    _check_keys(obj, _ENTRY_KEYS, where)
    match = _enum(obj["match"], MATCH_TYPES, f"{where}.match")
    path = validate_path(obj["path"], match, f"{where}.path")
    kind = _kind(obj["kind"], f"{where}.kind")
    if match == "prefix" and "dir" not in kind:
        _fail(f"{where}.kind", "a prefix entry names a directory, so kind must include \"dir\"")
    tools = tuple(
        _tool(t, f"{where}.tools[{i}]")
        for i, t in enumerate(_nonempty_list(obj["tools"], f"{where}.tools"))
    )
    sources = tuple(
        _source(s, f"{where}.appears_in[{i}]", today)
        for i, s in enumerate(_nonempty_list(obj["appears_in"], f"{where}.appears_in"))
    )
    spec = obj.get("spec")
    return CatalogEntry(
        path=path,
        match=match,
        kind=kind,
        category=_enum(obj["category"], CATEGORIES, f"{where}.category"),
        tools=tools,
        appears_in=sources,
        anchor=_enum(obj.get("anchor", "root"), ANCHORS, f"{where}.anchor"),
        spec=None if spec is None else _spec(spec, f"{where}.spec"),
        notes=_optional_text(obj.get("notes"), f"{where}.notes", MAX_NOTES_LEN),
        generic_name=_bool(obj.get("generic_name", False), f"{where}.generic_name"),
        may_hold_secrets=_bool(obj.get("may_hold_secrets", False), f"{where}.may_hold_secrets"),
    )


def validate_entries(
    items: Any, where: str = "entries", today: Optional[date] = None
) -> Tuple[CatalogEntry, ...]:
    """Validate a list of entries; duplicate paths (case-insensitive) are rejected.

    This is the reusable entry point for any list of entries, including a
    future user-supplied ``extra_ai_context_files``.
    """
    entries = []
    seen = {}
    for i, obj in enumerate(_nonempty_list(items, where)):
        entry = validate_entry(obj, f"{where}[{i}]", today)
        key = entry.path.lower()
        if key in seen:
            _fail(f"{where}[{i}].path", f"duplicate of {where}[{seen[key]}] ({_show(entry.path)})")
        seen[key] = i
        entries.append(entry)
    return tuple(entries)


def validate_catalog(doc: Any, today: Optional[date] = None) -> Catalog:
    """Validate a parsed catalog document."""
    _check_keys(doc, _TOP_KEYS, "top level")
    version = doc["schema_version"]
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        _fail("schema_version", f"unsupported version {_show(version)} (expected {SCHEMA_VERSION})")
    return Catalog(schema_version=version, entries=validate_entries(doc["entries"], today=today))


# --- Loading ---

def _reject_duplicate_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise CatalogError(f"AI assistant file catalog: duplicate key {_show(key)}")
        obj[key] = value
    return obj


def _reject_constant(name):
    raise CatalogError(f"AI assistant file catalog: non-standard JSON constant {name}")


def parse_catalog(text: str, today: Optional[date] = None) -> Catalog:
    """Parse and validate catalog JSON text."""
    try:
        doc = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise CatalogError(f"AI assistant file catalog: invalid JSON ({exc})") from None
    return validate_catalog(doc, today)


def load_catalog(path: Optional[str] = None, today: Optional[date] = None) -> Catalog:
    """Load the shipped catalog (or ``path``) and validate all of it."""
    try:
        if path is None:
            text = resources.files(_DATA_PACKAGE).joinpath(_DATA_DIR).joinpath(_DATA_FILE).read_text(encoding="utf-8")
        else:
            with open(path, encoding="utf-8") as f:
                text = f.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise CatalogError(f"AI assistant file catalog: cannot read {path or _DATA_FILE}: {exc}") from None
    return parse_catalog(text, today)


# --- Matching ---

def _glob_segment(name: str, pattern: str) -> bool:
    """Match one path segment against ``*``/``?`` by hand (no regex)."""
    n, p = 0, 0
    star_p, star_n = -1, 0
    while n < len(name):
        if p < len(pattern) and (pattern[p] == "?" or pattern[p] == name[n]):
            n += 1
            p += 1
        elif p < len(pattern) and pattern[p] == "*":
            star_p, star_n = p, n
            p += 1
        elif star_p != -1:
            p = star_p + 1
            star_n += 1
            n = star_n
        else:
            return False
    while p < len(pattern) and pattern[p] == "*":
        p += 1
    return p == len(pattern)


def _match_segments(segs: list, pat: list, match: str) -> bool:
    if match == "prefix":
        return segs[: len(pat)] == pat
    if len(segs) != len(pat):
        return False
    if match == "exact":
        return segs == pat
    return all(_glob_segment(s, p) for s, p in zip(segs, pat))


def matches(rel_path: str, entry: CatalogEntry) -> bool:
    """True when ``rel_path`` (``/``-separated, relative to the repo root) matches ``entry``.

    Case-insensitive. Pure: never touches the filesystem. ``rel_path`` comes
    from a repository walk, not the catalog, so it is not validated here;
    a leading ``./`` and trailing ``/`` are tolerated.
    """
    path = rel_path.lower()
    while path.startswith("./"):
        path = path[2:]
    segs = [s for s in path.split("/") if s]
    if not segs:
        return False
    pat = entry.path.lower().split("/")
    if entry.anchor == "root":
        return _match_segments(segs, pat, entry.match)
    return any(_match_segments(segs[i:], pat, entry.match) for i in range(len(segs)))
