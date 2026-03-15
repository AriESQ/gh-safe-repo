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
