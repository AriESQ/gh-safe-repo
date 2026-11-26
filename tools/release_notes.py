#!/usr/bin/env python3
"""Check a release tag and print its notes from CHANGELOG.md (#129).

Run by .github/workflows/release.yml on a tag push, and by hand to preview:

    uv run tools/release_notes.py v0.14.0
    uv run tools/release_notes.py v0.14.0 --repo AriESQ/gh-safe-repo -o notes.md

Checks, each failing with exit 1 and a one-line reason on stderr:

- the tag is ``vX.Y.Z`` (plain numbers, no pre-release suffix);
- ``[project] version`` in pyproject.toml is ``X.Y.Z``;
- uv.lock, when present, records the same version for the project;
- CHANGELOG.md has a ``## [X.Y.Z] - YYYY-MM-DD`` section (not only
  ``[Unreleased]``) with a real date and a non-empty body.

On success the notes are the section body (heading dropped), plus a
``**Full changelog**`` compare link from the previous section's version when
``--repo`` is given. Stdlib only; runs on Python 3.10 (no tomllib).
"""

import argparse
import datetime
import os
import re
import sys

TAG_RE = re.compile(r"^v(\d+\.\d+\.\d+)$")
# "## [0.14.0] - 2026-11-25" (Keep a Changelog). Captures version and the rest.
HEADING_RE = re.compile(r"^## \[([^\]]+)\](.*)$")
DATE_RE = re.compile(r"^\s*-\s*(\d{4}-\d{2}-\d{2})\s*$")


class ReleaseError(Exception):
    """A check failed; the message is the reason shown to the user."""


def version_from_tag(tag):
    m = TAG_RE.match(tag)
    if not m:
        raise ReleaseError(
            f"tag {tag!r} is not a release tag: expected vX.Y.Z (e.g. v0.14.0)"
        )
    return m.group(1)


def pyproject_version(text):
    """``version`` from the ``[project]`` table, by regex (3.10 has no tomllib)."""
    section = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped
            continue
        if section == "[project]":
            m = re.match(r'^version\s*=\s*"([^"]+)"\s*(#.*)?$', stripped)
            if m:
                return m.group(1)
    return None


def uv_lock_version(text, name):
    """The version uv.lock records for package ``name``, or None."""
    for block in text.split("[[package]]")[1:]:
        m_name = re.search(r'^name\s*=\s*"([^"]+)"', block, re.M)
        if m_name and m_name.group(1) == name:
            m_ver = re.search(r'^version\s*=\s*"([^"]+)"', block, re.M)
            return m_ver.group(1) if m_ver else None
    return None


def pyproject_name(text):
    section = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped
            continue
        if section == "[project]":
            m = re.match(r'^name\s*=\s*"([^"]+)"', stripped)
            if m:
                return m.group(1)
    return None


def changelog_sections(text):
    """[(version, rest_of_heading, body)] in file order."""
    sections = []
    current = None
    for line in text.splitlines():
        m = HEADING_RE.match(line)
        if m:
            current = [m.group(1), m.group(2), []]
            sections.append(current)
        elif line.startswith("## "):
            current = None  # some other level-2 heading ends the section
        elif current is not None:
            current[2].append(line)
    return [(v, rest, "\n".join(body).strip()) for v, rest, body in sections]


def extract_section(text, version):
    """(body, previous_version) for ``version``; raise ReleaseError otherwise."""
    sections = changelog_sections(text)
    for i, (v, rest, body) in enumerate(sections):
        if v != version:
            continue
        m = DATE_RE.match(rest)
        if not m:
            raise ReleaseError(
                f"CHANGELOG.md section [{version}] has no date: the heading must "
                f"read '## [{version}] - YYYY-MM-DD'"
            )
        try:
            datetime.date.fromisoformat(m.group(1))
        except ValueError:
            raise ReleaseError(
                f"CHANGELOG.md section [{version}] has an invalid date {m.group(1)!r}"
            )
        if not body:
            raise ReleaseError(f"CHANGELOG.md section [{version}] is empty")
        previous = next(
            (pv for pv, _r, _b in sections[i + 1:] if re.fullmatch(r"\d+\.\d+\.\d+", pv)),
            None,
        )
        return body, previous
    where = (
        " (the entries may still be under [Unreleased]: rename it to "
        f"'## [{version}] - YYYY-MM-DD')"
        if any(v == "Unreleased" for v, _r, _b in sections) else ""
    )
    raise ReleaseError(f"CHANGELOG.md has no section for [{version}]{where}")


def build_notes(tag, *, changelog, pyproject, uv_lock=None, repo=None):
    """Run every check and return the release notes (str)."""
    version = version_from_tag(tag)

    found = pyproject_version(pyproject)
    if found is None:
        raise ReleaseError("pyproject.toml has no [project] version")
    if found != version:
        raise ReleaseError(
            f"tag {tag} does not match pyproject.toml version {found!r}"
        )

    if uv_lock is not None:
        name = pyproject_name(pyproject)
        locked = uv_lock_version(uv_lock, name) if name else None
        if locked is not None and locked != version:
            raise ReleaseError(
                f"uv.lock records {name} {locked!r}, not {version!r}: run `uv lock`"
            )

    body, previous = extract_section(changelog, version)
    notes = body + "\n"
    if repo and previous:
        notes += (
            f"\n**Full changelog**: https://github.com/{repo}/compare/"
            f"v{previous}...{tag}\n"
        )
    return notes


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def main(argv=None):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("tag", help="release tag, vX.Y.Z")
    p.add_argument("--changelog", default=os.path.join(root, "CHANGELOG.md"))
    p.add_argument("--pyproject", default=os.path.join(root, "pyproject.toml"))
    p.add_argument("--uv-lock", default=os.path.join(root, "uv.lock"),
                   help="checked when the file exists")
    p.add_argument("--repo", help="OWNER/REPO, for the Full changelog compare link")
    p.add_argument("-o", "--output", help="write the notes here instead of stdout")
    args = p.parse_args(argv)

    try:
        notes = build_notes(
            args.tag,
            changelog=_read(args.changelog),
            pyproject=_read(args.pyproject),
            uv_lock=_read(args.uv_lock) if os.path.exists(args.uv_lock) else None,
            repo=args.repo,
        )
    except (ReleaseError, OSError) as e:
        print(f"release_notes: {e}", file=sys.stderr)
        return 1

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(notes)
    else:
        sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
