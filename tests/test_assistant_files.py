"""Tests for the AI context file catalog: validation, loading and matching (#124)."""

import copy
import json
from datetime import date

import pytest

from gh_safe_repo.assistant_files import (
    CATEGORIES,
    MAX_NAME_LEN,
    MAX_NOTES_LEN,
    MAX_PATH_LEN,
    Catalog,
    load_catalog,
    matches,
    parse_catalog,
    validate_catalog,
    validate_entries,
    validate_entry,
)
from gh_safe_repo.errors import CatalogError, ConfigError
from gh_safe_repo.security_scanner import _AI_CONTEXT_FILES

TODAY = date(2026, 10, 27)


def make_entry(**overrides):
    entry = {
        "path": "CLAUDE.md",
        "match": "exact",
        "kind": "file",
        "category": "instructions",
        "tools": [{"name": "Claude Code", "org": "Anthropic"}],
        "appears_in": [
            {"url": "https://code.claude.com/docs/en/settings", "type": "official", "checked": "2026-10-27"}
        ],
    }
    for key, value in overrides.items():
        if value is _DROP:
            entry.pop(key)
        else:
            entry[key] = value
    return entry


_DROP = object()


def make_doc(*entries):
    return {"schema_version": 1, "entries": list(entries) or [make_entry()]}


def entry_ok(**overrides):
    return validate_entry(make_entry(**overrides), today=TODAY)


def entry_rejected(match_text=None, **overrides):
    with pytest.raises(CatalogError, match=match_text):
        validate_entry(make_entry(**overrides), today=TODAY)


def with_source(**overrides):
    src = {"url": "https://example.com/docs", "type": "community", "checked": "2026-10-01"}
    src.update(overrides)
    return [src]


# ---------------------------------------------------------------------------
# Shipped catalog
# ---------------------------------------------------------------------------

class TestShippedCatalog:
    def test_loads_cleanly(self):
        catalog = load_catalog()
        assert isinstance(catalog, Catalog)
        assert catalog.schema_version == 1
        assert len(catalog.entries) > 50

    def test_every_category_is_used(self):
        used = {e.category for e in load_catalog().entries}
        assert used == set(CATEGORIES)

    def test_official_sources_only_on_vendor_domains(self):
        # 'official' means the vendor's own docs; community lists never qualify.
        community_hosts = ("github.com/", "gist.github.com/", "agents.md")
        for entry in load_catalog().entries:
            for src in entry.appears_in:
                if src.type == "official":
                    assert not src.url.startswith(tuple("https://" + h for h in community_hosts)), entry.path

    def test_claude_code_entries_are_official(self):
        for entry in load_catalog().entries:
            if any(t.name == "Claude Code" for t in entry.tools) and entry.path != "AGENTS.md":
                assert any(s.type == "official" for s in entry.appears_in), entry.path

    @pytest.mark.parametrize("path", ["knowledge.md", "replit.md", "CONVENTIONS.md", ".rules"])
    def test_generic_names_are_flagged(self, path):
        entry = next(e for e in load_catalog().entries if e.path == path)
        assert entry.generic_name is True
        assert entry.notes


class TestCoversCurrentScannerList:
    """Guard for step 2: the catalog must keep everything the scanner flags today."""

    # The scanner matches basenames at any depth and paths with '/' from the root;
    # '.cursor' is a directory matched at any depth.
    @pytest.mark.parametrize("name", sorted(_AI_CONTEXT_FILES))
    def test_entry_is_matched(self, name):
        catalog = load_catalog()
        candidates = [name, name.lower(), name.upper()]
        if "/" not in name:
            candidates += [f"sub/{name}", f"a/b/{name.lower()}"]
        if name == ".cursor":
            candidates += [".cursor/rules/x.mdc", "pkg/.cursor/mcp.json"]
        for candidate in candidates:
            assert catalog.matching(candidate), f"{candidate!r} not covered by the catalog"


# ---------------------------------------------------------------------------
# Top-level document
# ---------------------------------------------------------------------------

class TestDocument:
    def test_minimal_document(self):
        catalog = validate_catalog(make_doc(), today=TODAY)
        assert catalog.entries[0].path == "CLAUDE.md"

    def test_catalog_error_is_config_error(self):
        assert issubclass(CatalogError, ConfigError)

    @pytest.mark.parametrize("version", [0, 2, "1", True, None])
    def test_bad_schema_version(self, version):
        doc = make_doc()
        doc["schema_version"] = version
        with pytest.raises(CatalogError, match="schema_version"):
            validate_catalog(doc, today=TODAY)

    def test_unknown_top_level_key(self):
        doc = make_doc()
        doc["extra"] = 1
        with pytest.raises(CatalogError, match="unknown key 'extra'"):
            validate_catalog(doc, today=TODAY)

    @pytest.mark.parametrize("key", ["schema_version", "entries"])
    def test_missing_top_level_key(self, key):
        doc = make_doc()
        del doc[key]
        with pytest.raises(CatalogError, match="missing required key"):
            validate_catalog(doc, today=TODAY)

    @pytest.mark.parametrize("entries", [[], {}, "x", None])
    def test_entries_must_be_non_empty_list(self, entries):
        with pytest.raises(CatalogError):
            validate_catalog({"schema_version": 1, "entries": entries}, today=TODAY)

    def test_not_an_object(self):
        with pytest.raises(CatalogError, match="expected an object"):
            validate_catalog([], today=TODAY)

    def test_one_bad_entry_rejects_whole_file(self):
        doc = make_doc(make_entry(), make_entry(path=".cursorrules", category="bogus"))
        with pytest.raises(CatalogError, match=r"entries\[1\]\.category"):
            validate_catalog(doc, today=TODAY)


class TestParsing:
    def test_round_trip(self):
        assert parse_catalog(json.dumps(make_doc()), today=TODAY).entries

    def test_invalid_json(self):
        with pytest.raises(CatalogError, match="invalid JSON"):
            parse_catalog("{not json", today=TODAY)

    def test_duplicate_json_key(self):
        text = '{"schema_version": 1, "schema_version": 1, "entries": []}'
        with pytest.raises(CatalogError, match="duplicate key"):
            parse_catalog(text, today=TODAY)

    @pytest.mark.parametrize("const", ["NaN", "Infinity", "-Infinity"])
    def test_non_standard_constants(self, const):
        with pytest.raises(CatalogError, match="non-standard"):
            parse_catalog('{"schema_version": %s, "entries": []}' % const, today=TODAY)

    def test_load_from_path(self, tmp_path):
        p = tmp_path / "cat.json"
        p.write_text(json.dumps(make_doc()), encoding="utf-8")
        assert load_catalog(str(p), today=TODAY).entries[0].path == "CLAUDE.md"

    def test_load_missing_file(self, tmp_path):
        with pytest.raises(CatalogError, match="cannot read"):
            load_catalog(str(tmp_path / "missing.json"))


# ---------------------------------------------------------------------------
# Entry keys and enums
# ---------------------------------------------------------------------------

class TestEntryKeys:
    def test_unknown_entry_key(self):
        entry_rejected("unknown key 'severity'", severity="CRITICAL")

    @pytest.mark.parametrize("key", ["path", "match", "kind", "category", "tools", "appears_in"])
    def test_missing_required_key(self, key):
        entry_rejected("missing required key", **{key: _DROP})

    def test_unknown_tool_key(self):
        entry_rejected("unknown key 'url'", tools=[{"name": "X", "org": None, "url": "https://x.io"}])

    def test_unknown_source_key(self):
        entry_rejected("unknown key 'note'", appears_in=[{**with_source()[0], "note": "x"}])

    def test_unknown_spec_key(self):
        entry_rejected("unknown key 'version'",
                       spec={"name": "AGENTS.md", "org": None, "url": "https://agents.md", "version": 1})

    def test_tool_org_required_but_nullable(self):
        assert entry_ok(tools=[{"name": "Cline", "org": None}]).tools[0].org is None
        entry_rejected("missing required key 'org'", tools=[{"name": "Cline"}])

    @pytest.mark.parametrize("field", ["tools", "appears_in"])
    @pytest.mark.parametrize("value", [[], None, {}, "x"])
    def test_lists_must_be_non_empty(self, field, value):
        entry_rejected("non-empty list", **{field: value})

    def test_defaults(self):
        e = entry_ok()
        assert (e.anchor, e.spec, e.notes, e.generic_name, e.may_hold_secrets) == (
            "root", None, None, False, False)

    def test_optional_fields(self):
        e = entry_ok(anchor="any", generic_name=True, may_hold_secrets=True, notes="hi",
                     spec={"name": "AGENTS.md", "org": "Agentic AI Foundation", "url": "https://agents.md"})
        assert e.anchor == "any" and e.generic_name and e.may_hold_secrets
        assert e.spec.org == "Agentic AI Foundation"


class TestEnums:
    @pytest.mark.parametrize("value", list(CATEGORIES))
    def test_category_accepted(self, value):
        assert entry_ok(category=value).category == value

    @pytest.mark.parametrize("value", ["Instructions", "secrets", "", None, 1])
    def test_category_rejected(self, value):
        entry_rejected("category", category=value)

    @pytest.mark.parametrize("value", ["EXACT", "regex", "suffix", None])
    def test_match_rejected(self, value):
        entry_rejected("match", match=value)

    @pytest.mark.parametrize("value", ["root", "any"])
    def test_anchor_accepted(self, value):
        assert entry_ok(anchor=value).anchor == value

    @pytest.mark.parametrize("value", ["Root", "nested", None])
    def test_anchor_rejected(self, value):
        entry_rejected("anchor", anchor=value)

    @pytest.mark.parametrize("value", ["official", "community", "inferred"])
    def test_source_type_accepted(self, value):
        assert entry_ok(appears_in=with_source(type=value)).appears_in[0].type == value

    @pytest.mark.parametrize("value", ["Official", "vendor", None])
    def test_source_type_rejected(self, value):
        entry_rejected("type", appears_in=with_source(type=value))

    @pytest.mark.parametrize("field", ["generic_name", "may_hold_secrets"])
    @pytest.mark.parametrize("value", [1, "true", None])
    def test_flags_must_be_bool(self, field, value):
        entry_rejected("true or false", **{field: value})


class TestKind:
    def test_string(self):
        assert entry_ok(kind="dir", match="prefix", path=".cursor").kind == ("dir",)

    def test_list(self):
        assert entry_ok(kind=["file", "dir"], match="prefix", path=".clinerules").kind == ("file", "dir")

    @pytest.mark.parametrize("value", ["folder", [], ["file", "file"], ["file", "link"], None, 1])
    def test_rejected(self, value):
        entry_rejected("kind", kind=value)

    def test_prefix_requires_dir(self):
        entry_rejected("must include \"dir\"", kind="file", match="prefix", path=".cursor")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

class TestPath:
    @pytest.mark.parametrize("path", [
        "CLAUDE.md", ".claude/settings.local.json", ".github/copilot-instructions.md",
        "a-b_c.d/e", "x" * MAX_PATH_LEN,
    ])
    def test_accepted(self, path):
        assert entry_ok(path=path).path == path

    @pytest.mark.parametrize("path, why", [
        ("/etc/passwd", "relative"),
        ("~/.claude/settings.json", "relative"),
        ("~user/x", "relative"),
        ("../CLAUDE.md", "'..' segment"),
        ("a/../b", "'..' segment"),
        ("a/..", "'..' segment"),
        ("./CLAUDE.md", "'.' segment"),
        ("a/./b", "'.' segment"),
        ("a\\b", "not allowed"),
        ("a\x00b", "not allowed"),
        ("a\x1b[31mb", "not allowed"),
        ("a\nb", "not allowed"),
        ("a\tb", "not allowed"),
        ("a b", "not allowed"),
        ("a[bc]", "not allowed"),
        ("a{b,c}", "not allowed"),
        ("a+b", "not allowed"),
        ("café.md", "not allowed"),
        ("x" * (MAX_PATH_LEN + 1), "longer than"),
        ("", "empty"),
        ("a//b", "empty path segment"),
        (".claude/", "empty path segment"),
    ])
    def test_rejected(self, path, why):
        entry_rejected(why, path=path)

    @pytest.mark.parametrize("value", [None, 1, ["CLAUDE.md"]])
    def test_not_a_string(self, value):
        entry_rejected("expected a string", path=value)

    @pytest.mark.parametrize("match", ["exact", "prefix"])
    def test_wildcards_only_in_globs(self, match):
        entry_rejected("only allowed when match is", path=".aider*", match=match, kind="dir")

    def test_glob_needs_wildcard(self):
        entry_rejected("must contain", path="CLAUDE.md", match="glob")

    @pytest.mark.parametrize("path", [".aider*", ".cursor/rules/*.mdc", "?.md", "*"])
    def test_glob_accepted(self, path):
        assert entry_ok(path=path, match="glob").path == path

    def test_duplicate_paths_rejected(self):
        with pytest.raises(CatalogError, match=r"duplicate of entries\[0\]"):
            validate_entries([make_entry(), make_entry()], today=TODAY)

    def test_duplicate_paths_case_insensitive(self):
        with pytest.raises(CatalogError, match="duplicate"):
            validate_entries([make_entry(), make_entry(path="claude.md")], today=TODAY)

    def test_distinct_paths_accepted(self):
        assert len(validate_entries([make_entry(), make_entry(path="AGENTS.md")], today=TODAY)) == 2


# ---------------------------------------------------------------------------
# URLs and dates
# ---------------------------------------------------------------------------

class TestUrl:
    @pytest.mark.parametrize("url", [
        "https://agents.md", "https://code.claude.com/docs/en/settings",
        "https://example.com/a?b=c#d",
    ])
    def test_accepted(self, url):
        assert entry_ok(appears_in=with_source(url=url)).appears_in[0].url == url

    @pytest.mark.parametrize("url, why", [
        ("http://example.com", "https"),
        ("HTTPS://example.com", "https"),
        ("ftp://example.com", "https"),
        ("javascript:alert(1)", "https"),
        ("file:///etc/passwd", "https"),
        ("//example.com", "https"),
        ("example.com", "https"),
        ("https://", "no host"),
        ("https:///path", "no host"),
        ("https://user:pw@example.com", "credentials"),
        ("https://exa mple.com", "printable ASCII"),
        ("https://example.com/\x1b[31m", "printable ASCII"),
        ("https://exämple.com", "printable ASCII"),
        ("https://example.com/" + "a" * 2048, "longer than"),
    ])
    def test_rejected(self, url, why):
        entry_rejected(why, appears_in=with_source(url=url))

    def test_spec_url_validated(self):
        entry_rejected("https", spec={"name": "AGENTS.md", "org": None, "url": "http://agents.md"})

    def test_not_a_string(self):
        entry_rejected("expected a string", appears_in=with_source(url=None))


class TestDate:
    @pytest.mark.parametrize("value", ["2026-10-27", "2020-02-29", "1999-01-01"])
    def test_accepted(self, value):
        assert entry_ok(appears_in=with_source(checked=value)).appears_in[0].checked == date.fromisoformat(value)

    @pytest.mark.parametrize("value", [
        "2026-10-28", "2099-01-01",
    ])
    def test_future_rejected(self, value):
        entry_rejected("in the future", appears_in=with_source(checked=value))

    @pytest.mark.parametrize("value", [
        "2026-13-01", "2026-02-30", "26-10-27", "2026/10/27", "20261027", "2026-10-27T00:00:00",
        "October 27, 2026", "", None, 20261027,
    ])
    def test_malformed_rejected(self, value):
        entry_rejected("ISO 8601", appears_in=with_source(checked=value))

    def test_defaults_to_real_today(self):
        # No `today` passed: the shipped catalog's dates must not be in the future.
        validate_entry(make_entry(appears_in=with_source(checked="2000-01-01")))


# ---------------------------------------------------------------------------
# Text fields (printed to terminals)
# ---------------------------------------------------------------------------

class TestText:
    @pytest.mark.parametrize("text", [
        "Written on 'Yes, don't ask again'; holds ${VAR} paths.",
        "Agentic AI Foundation (Linux Foundation)",
        "Mistral AI — café 日本",
        "x" * MAX_NOTES_LEN,
    ])
    def test_notes_accepted(self, text):
        assert entry_ok(notes=text).notes == text

    @pytest.mark.parametrize("text", [
        "\x1b[31mred\x1b[0m",          # ANSI colour
        "\x1b]0;pwned\x07",            # OSC: set terminal title
        "\x1b]8;;https://evil/\x1b\\link\x1b]8;;\x1b\\",  # OSC 8 hyperlink
        "\x9b31m",                     # 8-bit CSI (C1)
        "a\x00b",                      # NUL
        "line1\nline2",                # newline
        "a\rb",                        # carriage return overwrite
        "tab\there",
        "bell\x07",
        "del\x7f",
        "bidi \u202egnp.exe",     # right-to-left override (Trojan Source)
        "zero\u200bwidth",     # zero-width space (Cf)
        "line\u2028sep",     # line separator
    ])
    def test_notes_injection_rejected(self, text):
        entry_rejected("control or non-printing", notes=text)

    def test_notes_too_long(self):
        entry_rejected("longer than", notes="x" * (MAX_NOTES_LEN + 1))

    @pytest.mark.parametrize("value", ["", "   ", 5, ["x"]])
    def test_notes_bad_type_or_empty(self, value):
        entry_rejected(None, notes=value)

    def test_tool_name_injection_rejected(self):
        entry_rejected("tools\\[0\\]\\.name", tools=[{"name": "Claude\x1b[2J", "org": None}])

    def test_tool_org_injection_rejected(self):
        entry_rejected("tools\\[0\\]\\.org", tools=[{"name": "Claude", "org": "Anth\x1b[0mropic"}])

    def test_spec_name_injection_rejected(self):
        entry_rejected("spec\\.name", spec={"name": "A\x1b[1m", "org": None, "url": "https://agents.md"})

    def test_tool_name_too_long(self):
        entry_rejected("longer than", tools=[{"name": "x" * (MAX_NAME_LEN + 1), "org": None}])

    def test_error_message_escapes_bad_input(self):
        # The error itself is printed; it must not carry the raw escape.
        with pytest.raises(CatalogError) as exc:
            validate_entry(make_entry(notes="\x1b[31mred"), today=TODAY)
        assert "\x1b" not in str(exc.value)
        with pytest.raises(CatalogError) as exc:
            validate_entry(make_entry(category="\x1b[31m"), today=TODAY)
        assert "\x1b" not in str(exc.value)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _e(path, match="exact", anchor="root", kind="file"):
    if match == "prefix":
        kind = "dir"
    return validate_entry(make_entry(path=path, match=match, anchor=anchor, kind=kind), today=TODAY)


class TestMatches:
    @pytest.mark.parametrize("rel, expected", [
        ("CLAUDE.md", True),
        ("claude.md", True),
        ("Claude.MD", True),
        ("sub/CLAUDE.md", False),
        ("CLAUDE.md.bak", False),
        ("XCLAUDE.md", False),
        ("CLAUDE.md/inner", False),
    ])
    def test_exact_root(self, rel, expected):
        assert matches(rel, _e("CLAUDE.md")) is expected

    @pytest.mark.parametrize("rel, expected", [
        ("CLAUDE.md", True), ("sub/CLAUDE.md", True), ("a/b/claude.md", True),
        ("sub/CLAUDE.md.bak", False), ("sub/notCLAUDE.md", False),
    ])
    def test_exact_any(self, rel, expected):
        assert matches(rel, _e("CLAUDE.md", anchor="any")) is expected

    def test_exact_multi_segment_root(self):
        e = _e(".github/copilot-instructions.md")
        assert matches(".github/copilot-instructions.md", e)
        assert matches(".GitHub/Copilot-Instructions.md", e)
        assert not matches("pkg/.github/copilot-instructions.md", e)
        assert not matches("copilot-instructions.md", e)

    @pytest.mark.parametrize("rel, expected", [
        (".cursor", True), (".cursor/", True), (".cursor/rules/a.mdc", True), (".CURSOR/mcp.json", True),
        (".cursorrules", False), (".cursor-old/x", False), ("sub/.cursor", False),
    ])
    def test_prefix_root(self, rel, expected):
        assert matches(rel, _e(".cursor", match="prefix")) is expected

    def test_prefix_any(self):
        e = _e(".cursor", match="prefix", anchor="any")
        assert matches("pkg/.cursor", e)
        assert matches("pkg/.cursor/rules/x.mdc", e)
        assert not matches("pkg/.cursorrules", e)

    def test_multi_segment_prefix(self):
        e = _e(".agents/skills", match="prefix")
        assert matches(".agents/skills/x/SKILL.md", e)
        assert not matches(".agents/skillsets", e)
        assert not matches(".agents", e)

    @pytest.mark.parametrize("rel, expected", [
        (".aider.chat.history.md", True),
        (".aider", True),
        (".AIDER.conf.yml", True),
        ("aider", False),
        ("sub/.aider.chat.history.md", False),
        (".aider.tags.cache.v3/x", False),   # '*' does not cross '/'
    ])
    def test_glob_star(self, rel, expected):
        assert matches(rel, _e(".aider*", match="glob")) is expected

    @pytest.mark.parametrize("rel, expected", [
        (".cursor/rules/a.mdc", True),
        (".cursor/rules/.mdc", True),
        (".cursor/rules/sub/a.mdc", False),
        (".cursor/rules/a.md", False),
        (".cursor/rules/a.mdcx", False),
    ])
    def test_glob_segmented(self, rel, expected):
        assert matches(rel, _e(".cursor/rules/*.mdc", match="glob")) is expected

    @pytest.mark.parametrize("rel, expected", [
        ("a.md", True), ("ab.md", False), (".md", False), ("/.md", False),
    ])
    def test_glob_question(self, rel, expected):
        assert matches(rel, _e("?.md", match="glob")) is expected

    def test_glob_backtracking(self):
        e = _e("*a*b*c", match="glob")
        assert matches("xaybzc", e)
        assert matches("abcabc", e)
        assert not matches("acb", e)

    def test_glob_is_not_regex(self):
        # Regex metacharacters in the *input* are literal; '.' in the pattern is literal.
        e = _e("a.md", match="exact")
        assert not matches("aXmd", e)
        g = _e("*.md", match="glob")
        assert not matches("readme_md", g)
        assert matches("(x)+.md", g)

    def test_glob_any_anchor(self):
        e = _e(".github/instructions/*.instructions.md", match="glob", anchor="any")
        assert matches("pkg/.github/instructions/py.instructions.md", e)
        assert not matches("pkg/.github/instructions/py.md", e)

    @pytest.mark.parametrize("rel", ["", "/", "./"])
    def test_empty_path_never_matches(self, rel):
        assert not matches(rel, _e("*", match="glob"))

    def test_leading_dot_slash_tolerated(self):
        assert matches("./CLAUDE.md", _e("CLAUDE.md"))

    def test_catalog_matching_returns_all_in_order(self):
        doc = make_doc(
            make_entry(path=".claude", match="prefix", kind="dir"),
            make_entry(path=".claude/settings.local.json", category="personal-settings"),
        )
        catalog = validate_catalog(doc, today=TODAY)
        hits = catalog.matching(".claude/settings.local.json")
        assert [e.path for e in hits] == [".claude", ".claude/settings.local.json"]
        assert catalog.matching("README.md") == ()
