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

1. Auto-detects known AI context files with history (if no paths given)
2. Backs up current file content to `.git/filter-file-backups/`
3. Runs `git filter-branch` to remove targets from all commits in one pass
4. Expires the reflog and runs `git gc --prune=now` to purge objects
5. Re-adds any files that were present at HEAD as a single fresh commit
6. Optionally force-pushes with `--push`

> **After running without `--push`** you must force-push all branches and tags
> to every remote, and alert collaborators to re-clone:
> ```bash
> git push --force-with-lease --all
> git push --force-with-lease --tags
> ```

See `scrub-ai-context-TESTING.md` for a manual test suite using throwaway repos.

## git-filter-file.sh

A more general history-scrubbing tool: removes any single tracked file from all
git history, then re-adds its current content as a fresh commit.

```bash
# Remove a file from all history (with confirmation prompt)
git-filter-file.sh secret.txt

# Preview without making changes
git-filter-file.sh --dry-run secret.txt
```

Use `scrub-ai-context.sh` when you specifically need to scrub AI context files
(it handles multiple targets and directories in one pass). Use `git-filter-file.sh`
for a single arbitrary file.

See `git-filter-file-TESTING.md` for a manual test suite.
