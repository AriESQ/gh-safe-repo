# tools

Standalone helper scripts for the `gh-safe-repo` workflow.

## trufflehog

A transparent shell wrapper that runs truffleHog in a podman or docker container.
Use it if you want container-based truffleHog to appear as a native install for
tools other than `gh-safe-repo` (which detects and uses containers natively without
this wrapper).

```bash
# Install system-wide as "trufflehog"
cp tools/trufflehog ~/.local/bin/trufflehog
chmod +x ~/.local/bin/trufflehog
```

On first use the container runtime pulls `ghcr.io/trufflesecurity/trufflehog:latest`
automatically. The scan path is mounted read-only at the same absolute path inside
the container so JSON output paths are identical to a native run.

**Environment variables:**

| Variable | Default | Description |
|---|---|---|
| `TRUFFLEHOG_IMAGE` | `ghcr.io/trufflesecurity/trufflehog:latest` | Image to use |
| `CONTAINER_RUNTIME` | auto-detected | Override to `podman` or `docker` |

## Containerfile

Builds a local truffleHog image. Use this only if you want to pin a specific
version, build an air-gapped image, or add custom detectors.

```bash
podman build -t trufflehog:local -f tools/Containerfile tools/

# Point gh-safe-repo at the local image
export TRUFFLEHOG_IMAGE=trufflehog:local
```

The default `Containerfile` is just `FROM ghcr.io/trufflesecurity/trufflehog:latest`
— edit it to pin a version tag or add layers.

## scrub-ai-context.sh

Removes AI agent context files (`CLAUDE.md`, `AGENTS.md`, `.cursorrules`,
`copilot-instructions.md`, `.cursor/`) from all git history, then re-adds any that
currently exist on disk as a single fresh commit.

`gh-safe-repo`'s pre-flight scanner links to this script in its remediation hints
when it detects AI context files in history.

```bash
# Auto-detect and scrub all known AI context files
scrub-ai-context.sh

# Preview what would be scrubbed, no changes made
scrub-ai-context.sh --dry-run

# Scrub a specific file
scrub-ai-context.sh CLAUDE.md

# Scrub and force-push in one step
scrub-ai-context.sh --push
```

**What it does:**

Thin wrapper around `git-filter-file.sh`. For each target it calls
`git-filter-file.sh --keep --force --yes . <target>`, handling confirmation
once upfront.

1. Auto-detects known AI context files with history (if no paths given)
2. Confirms once, then calls `git-filter-file.sh` for each target
3. Each pass: backs up, rewrites history, purges objects, re-adds the file
4. Optionally force-pushes with `--push`

> **After running without `--push`** you must force-push all branches and tags
> to every remote, and alert collaborators to re-clone:
> ```bash
> git push --force-with-lease --all
> git push --force-with-lease --tags
> ```

See `scrub-ai-context-TESTING.md` for a manual test suite using throwaway repos.

## git-filter-file.sh

A general history-scrubbing tool: removes a file from all git history and the
working tree. With `--keep`, re-adds the current on-disk content as a fresh
commit instead of deleting it.

```bash
# Remove a file from all history and disk (repo-relative path)
git-filter-file.sh . secrets/api_key.txt

# Find and remove a file by name (anywhere in history)
git-filter-file.sh . api_key.txt

# Target a repo from anywhere
git-filter-file.sh ~/projects/my-app credentials.json

# Preview without making changes
git-filter-file.sh --dry-run . secret.txt

# Scrub history but keep the current file on disk
git-filter-file.sh --keep . config.json
```

The first positional argument is the repo path (`.` for cwd). The second is the
file to scrub — if it contains a `/`, it is matched as an exact repo-relative
path; a bare filename searches all of history (errors on ambiguity).

**Exit codes:** `0` success, `1` runtime failure (file not found, dirty tree),
`2` usage error (bad arguments, not a git repo).

Use `scrub-ai-context.sh` when you specifically need to scrub AI context files
(it auto-detects targets and handles `--push`). Use `git-filter-file.sh`
directly for arbitrary files.

See `git-filter-file-TESTING.md` for a manual test suite.

## api_drift

Tracks where GitHub's published REST description (the OpenAPI files in
[github/rest-api-description](https://github.com/github/rest-api-description))
and GitHub's real behaviour disagree, for the endpoints `gh-safe-repo`
calls (#27). Stdlib only. Not part of the installed CLI.

- `api_drift/catalog.json` is the checked-in record. `endpoints` lists every
  tracked method and path, with `spec` (what the description said at the
  last snapshot: status codes, top-level response and request fields,
  required fields, `deprecated`) and, where it was measured, `observed`
  (status codes and fields seen live, with date and evidence). `quirks`
  lists each known gap: what the spec says, what GitHub did, the workaround
  in the code, evidence (probe doc, issue, PR, commit), the date it was
  recorded, a `kind` (`spec_gap`, `undocumented_semantics`,
  `cross_endpoint_inconsistency`, `eventual_consistency`) and a machine
  `check` describing the spec condition that makes it a gap.
- `api_drift/api_drift.py` reads both.

```bash
# The spec file gh-safe-repo talks to (gh sends no X-GitHub-Api-Version,
# so GitHub answers as 2022-11-28):
curl -fsSL -o /tmp/spec.json \
  https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.2022-11-28.json

uv run tools/api_drift/api_drift.py check --spec /tmp/spec.json          # human report
uv run tools/api_drift/api_drift.py check --spec /tmp/spec.json --json   # same, as JSON
uv run tools/api_drift/api_drift.py snapshot --spec /tmp/spec.json \
  --source-ref github/rest-api-description@<sha>                         # accept the spec
uv run tools/api_drift/api_drift.py probe OWNER/REPO --spec /tmp/spec.json  # live, GET only
```

**`check`** reports, in order:

| Section | Means | Fails? |
|---|---|---|
| Spec changes since snapshot | a tracked path or method is gone or deprecated, or its status codes, required fields, request fields or response fields changed | yes |
| Notices | new response fields only (nothing breaks) | no |
| Quirks whose spec condition changed | the spec no longer says what it said when the quirk was recorded: GitHub may have fixed or documented it, so the workaround may be removable. Re-test live before removing it | yes |
| Uncataloged gaps | an `observed` status code the spec does not document has no quirk entry | yes |
| Endpoints called but not in the catalog | the `gh_safe_repo` source calls a spec path the catalog does not track | yes |
| Paths called that the spec does not have | the source builds a path no spec template matches | yes |
| Known gaps still open | every quirk whose condition still holds: the current catalog of spec-vs-behaviour gaps | no |

Exit codes: `0` nothing new, `1` drift (review needed), `2` unreadable
catalog or spec, or a malformed catalog entry.

The source scan parses `gh_safe_repo/` with `ast`: arguments to
`repo_path()`, `call_api()`, `call_json()`, `get_json()` and
`get_json_pages()`, plus any other hyphenated or slashed string literal
that matches a spec path (this catches lookup tables such as the toggle
endpoints in `plugins/security.py`). Paths compare by template, so
`contents/README.md` is `contents/{path}`. It sees paths, not methods.

**`snapshot`** rewrites every `spec` block from the given file and records
the spec version, date and `--source-ref`. Run it after reviewing a `check`
report and updating the affected quirks, not instead of reviewing.

**`probe`** sends `gh api --method GET -i` to every tracked GET endpoint on
a repo you admin (filling `{branch}` from the default branch, `{ruleset_id}`
from the first ruleset, `{path}` with `README.md`) and reports status codes
the spec does not document and fields the spec does not list or marks
required but were missing. Anything not already in the catalog's `observed`
block exits `1`. It never sends a write. The write-side probes behind the
existing quirks (rulesets PATCH, duplicate POST, Actions disabled) are
reproduced by hand with the script in the appendix of
`docs/2026-10-22_github-api-read-shapes.md`, on a throwaway repo.

**Adding a quirk:** add an entry under `quirks` with one of these `check`
types; the condition is true while the gap still exists in the spec.

| `check.type` | True while |
|---|---|
| `method_absent` | the method is not documented on the path |
| `status_undocumented` / `status_documented` | `status` is not / is in the operation's responses |
| `field_optional` / `field_required` / `field_absent` | `field` is optional / required / not a property (`where`: `response` or `request`; `descend` walks nested objects) |
| `text_lacks` | the operation's (or `field`'s) description does not match the regex `pattern` |
| `none` | always (behaviour with no spec hook) |

**CI:** `.github/workflows/api-drift.yml` runs `check` every Monday and on
manual dispatch against the latest `2022-11-28` file from
`github/rest-api-description` main (the commit SHA is in the run summary).
On drift it comments on the open issue titled "API drift: REST description
changed for endpoints gh-safe-repo uses", or opens it, and fails. It also
prints, without failing, the same diff against the newer dated version
(`2026-03-10`). Permissions: `contents: read`, `issues: write`; actions are
GitHub-owned and SHA-pinned like `ci.yml`.
