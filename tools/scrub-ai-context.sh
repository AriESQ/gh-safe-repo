#!/usr/bin/env bash
# scrub-ai-context — remove AI agent context files from all git history, then
# re-add any that currently exist on disk as a single fresh commit.
#
# Usage: scrub-ai-context [--dry-run] [--push] [<path>...]
#
# Thin wrapper around git-filter-file. If no paths are given, auto-detects
# known AI context files (CLAUDE.md, AGENTS.md, .cursorrules,
# copilot-instructions.md, .cursor) that have history in this repository.

set -euo pipefail

# ── Resolve script directory (for calling git-filter-file) ────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
GIT_FILTER_FILE="$SCRIPT_DIR/git-filter-file.sh"

if [[ ! -x "$GIT_FILTER_FILE" ]]; then
    echo "Error: git-filter-file.sh not found at $GIT_FILTER_FILE" >&2
    exit 2
fi

# ── ANSI colours ────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
    BOLD='\033[1m'
    RED='\033[0;31m'
    YELLOW='\033[0;33m'
    GREEN='\033[0;32m'
    DIM='\033[2m'
    RESET='\033[0m'
else
    BOLD='' RED='' YELLOW='' GREEN='' DIM='' RESET=''
fi

err()  { echo -e "${RED}Error:${RESET} $*" >&2; }
warn() { echo -e "${YELLOW}Warning:${RESET} $*"; }
ok()   { echo -e "${GREEN}✓${RESET} $*"; }
bold() { echo -e "${BOLD}$*${RESET}"; }

# ── Known AI context file/directory names ────────────────────────────────────
KNOWN_AI_CONTEXT=(
    CLAUDE.md
    AGENTS.md
    .cursorrules
    copilot-instructions.md
    .cursor
)

# ── Usage ────────────────────────────────────────────────────────────────────
usage() {
    cat <<EOF
Usage: scrub-ai-context [--dry-run] [--push] [<path>...]

Scrub AI agent context files from all git history, then re-add any that
currently exist on disk as a single fresh commit.

If no paths are given, the script auto-detects known AI context files:
  ${KNOWN_AI_CONTEXT[*]}

Handles both regular files and directories (e.g. .cursor/).
Each target is scrubbed via git-filter-file in a separate pass.

Options:
  --dry-run   Show what would happen without making any changes
  --push      Force-push all branches and tags after scrubbing (skipped if
              any target failed). Tags are pushed with an explicit lease
              per tag, recorded from the remote before the first rewrite
  -h, --help  Show this help

Examples:
  scrub-ai-context                    # auto-detect and scrub all known files
  scrub-ai-context CLAUDE.md          # scrub one specific file
  scrub-ai-context --dry-run          # preview auto-detected targets
  scrub-ai-context --push CLAUDE.md   # scrub and force-push

After running (without --push), you MUST:
  1. Force-push all branches:  git push --force-with-lease --all
  2. Push the rewritten tags:  bash .git/filter-file-push-tags.sh
     (written by the last pass; each tag has an explicit lease)
  3. Alert collaborators to re-clone or reset their local copies
EOF
    exit "${1:-0}"
}

# ── Argument parsing ─────────────────────────────────────────────────────────
DRY_RUN=false
PUSH=false
TARGETS=()

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --push)    PUSH=true ;;
        -h|--help) usage 0 ;;
        -*) err "Unknown option: $arg"; usage 1 ;;
        *)  TARGETS+=("$arg") ;;
    esac
done

# ── Git repo check ────────────────────────────────────────────────────────────
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    err "Not inside a git repository."
    exit 1
}

# ── Resolve targets (auto-detect if none given) ───────────────────────────────
if [[ "${#TARGETS[@]}" -eq 0 ]]; then
    echo "No paths specified — scanning for known AI context files with history..."
    echo ""
    for name in "${KNOWN_AI_CONTEXT[@]}"; do
        count="$(git -C "$REPO_ROOT" log --all --oneline -- "$name" 2>/dev/null | wc -l | tr -d ' ')"
        if [[ "$count" -gt 0 ]]; then
            TARGETS+=("$name")
            echo "  Found: $name  ($count commit(s))"
        fi
    done
    echo ""
    if [[ "${#TARGETS[@]}" -eq 0 ]]; then
        ok "No known AI context files found in this repository's history."
        exit 0
    fi
fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
bold "scrub-ai-context$([ "$DRY_RUN" = true ] && echo " (dry run)" || true)"
echo ""
echo "  Will scrub ${#TARGETS[@]} target(s) via git-filter-file:"
for target in "${TARGETS[@]}"; do
    echo "    $target"
done
echo ""

if [[ "$DRY_RUN" = true ]]; then
    echo -e "${DIM}-- dry run: passing --dry-run to git-filter-file for each target --${RESET}"
    echo ""
    for target in "${TARGETS[@]}"; do
        bold "--- $target ---"
        local_args=(--dry-run --force)
        if [[ -f "$REPO_ROOT/$target" || -d "$REPO_ROOT/$target" ]]; then
            local_args+=(--keep)
        fi
        "$GIT_FILTER_FILE" "${local_args[@]}" . "$target"
        echo ""
    done
    exit 0
fi

# ── Confirmation ─────────────────────────────────────────────────────────────
echo -e "${BOLD}${RED}This will permanently rewrite git history${RESET} once per target."
echo "Backups will be saved to .git/filter-file-backups/ before each pass."
echo ""
read -r -p "Proceed? [y/N] " CONFIRM
if [[ "${CONFIRM,,}" != "y" && "${CONFIRM,,}" != "yes" ]]; then
    echo "Aborted."
    exit 0
fi

# ── Scrub each target ───────────────────────────────────────────────────────
FAILED=()

# One tag snapshot for all passes (#135): the first git-filter-file pass
# records the remote's tags and the local tags before any rewrite, and the
# later passes reuse it. The last pass then writes a tag push
# (.git/filter-file-push-tags.sh) whose leases compare against the remote
# as it was before the first rewrite, covering every tag any pass changed.
GIT_FILTER_FILE_TAG_SNAPSHOT="$(mktemp)"
export GIT_FILTER_FILE_TAG_SNAPSHOT
trap 'rm -f "$GIT_FILTER_FILE_TAG_SNAPSHOT"' EXIT
TAG_PUSH_FILE="$(git -C "$REPO_ROOT" rev-parse --absolute-git-dir)/filter-file-push-tags.sh"
rm -f "$TAG_PUSH_FILE"   # a stale one from an earlier run must not be pushed

for target in "${TARGETS[@]}"; do
    echo ""
    bold "--- Scrubbing: $target ---"

    # --force: after first rewrite, local diverges from remote
    # --yes: we already confirmed above
    # --keep: re-add current content (only if file/dir exists on disk)
    local_args=(--force --yes)
    if [[ -f "$REPO_ROOT/$target" || -d "$REPO_ROOT/$target" ]]; then
        local_args+=(--keep)
    fi
    if "$GIT_FILTER_FILE" "${local_args[@]}" . "$target"; then
        ok "Completed: $target"
    else
        err "Failed: $target"
        FAILED+=("$target")
    fi
done

# ── Optional force-push ───────────────────────────────────────────────────────
REMOTES="$(git -C "$REPO_ROOT" remote 2>/dev/null || true)"

PUSHED=false
if [[ "$PUSH" = true ]]; then
    if [[ -z "$REMOTES" ]]; then
        warn "No remotes configured — skipping push."
    elif [[ "${#FAILED[@]}" -gt 0 ]]; then
        warn "Some targets failed — skipping push so a partly scrubbed history is not published."
    else
        echo ""
        echo "Force-pushing all branches..."
        git -C "$REPO_ROOT" push --force-with-lease --all
        if [[ -f "$TAG_PUSH_FILE" ]]; then
            echo "Pushing rewritten tags (explicit lease per tag)..."
            if ! bash "$TAG_PUSH_FILE"; then
                err "Tag push failed. A tag rejected with \"stale info\" was changed on the"
                echo "remote after the scrub started; check it with 'git ls-remote --tags'." >&2
                echo "Retry the remaining tags with: bash $TAG_PUSH_FILE" >&2
                exit 1
            fi
        fi
        ok "Force-push complete."
        PUSHED=true
    fi
fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
bold "Done."

if [[ "${#FAILED[@]}" -gt 0 ]]; then
    echo ""
    err "Failed targets: ${FAILED[*]}"
    echo "Check output above for details."
fi

if [[ -n "$REMOTES" && "$PUSHED" = false ]]; then
    echo ""
    echo "  Force-push ALL branches and tags (the last pass above printed the details):"
    echo "    cd $REPO_ROOT && git push --force-with-lease --all"
    if [[ -f "$TAG_PUSH_FILE" ]]; then
        echo "    bash $TAG_PUSH_FILE   # tags, one explicit lease per tag"
    fi
fi
echo ""
