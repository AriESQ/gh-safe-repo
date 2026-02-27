#!/usr/bin/env bash
# git-filter-file — scrub a tracked file from all git history, then re-add its
# current content as a single fresh commit.
#
# Usage: git-filter-file [--dry-run] <file-path>
#
# Purpose: pre-sharing safety tool. Remove accidentally committed secrets or
# large files from every commit in history, while preserving the file's current
# content at HEAD.

set -euo pipefail

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

# ── Usage ────────────────────────────────────────────────────────────────────
usage() {
    cat <<EOF
Usage: git-filter-file [--dry-run] <file-path>

Scrub all git history of <file-path>, then re-add its current content as a
single fresh commit. This is a destructive, irreversible operation on local
history — you must force-push to any remotes afterwards.

Options:
  --dry-run   Show what would happen without making any changes
  -h, --help  Show this help

Arguments:
  <file-path>  Path to the tracked file to scrub (absolute or repo-relative)

Examples:
  git-filter-file secrets/api_key.txt
  git-filter-file --dry-run config/credentials.json

After running, you MUST:
  1. Force-push all branches:  git push --force-with-lease --all
  2. Rotate any exposed secrets — history rewrite does not invalidate tokens
  3. Alert collaborators to re-clone or reset their local copies
EOF
    exit "${1:-0}"
}

# ── Argument parsing ─────────────────────────────────────────────────────────
DRY_RUN=false
TARGET=""

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        -h|--help) usage 0 ;;
        -*) err "Unknown option: $arg"; usage 1 ;;
        *)
            if [[ -n "$TARGET" ]]; then
                err "Too many arguments. Expected exactly one file path."
                usage 1
            fi
            TARGET="$arg"
            ;;
    esac
done

if [[ -z "$TARGET" ]]; then
    err "Missing required argument: <file-path>"
    usage 1
fi

# ── Git repo check ────────────────────────────────────────────────────────────
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    err "Not inside a git repository."
    exit 1
}

# ── Worktree guard ────────────────────────────────────────────────────────────
# filter-branch rewrites the shared object store — running inside a worktree
# would rewrite the main repo's history from an unexpected working directory.
if [[ "$(git rev-parse --git-dir)" != "$(git rev-parse --git-common-dir)" ]]; then
    err "Running inside a git worktree is not supported."
    echo "Switch to the main working tree and run this script from there."
    exit 1
fi

# ── Resolve file path ─────────────────────────────────────────────────────────
if [[ "$TARGET" = /* ]]; then
    ABS_PATH="$TARGET"
else
    ABS_PATH="$REPO_ROOT/$TARGET"
fi

# Resolve symlinks / .. / ./
ABS_PATH="$(realpath "$ABS_PATH" 2>/dev/null)" || {
    err "Cannot resolve path: $TARGET"
    exit 1
}

if [[ ! -f "$ABS_PATH" ]]; then
    err "File does not exist: $ABS_PATH"
    exit 1
fi

# ── Confirm file is tracked by git ────────────────────────────────────────────
# git ls-files outputs the repo-relative path, which is what filter-branch needs
RELATIVE_PATH="$(git -C "$REPO_ROOT" ls-files --error-unmatch "$ABS_PATH" 2>/dev/null)" || {
    err "'$TARGET' is not tracked by git."
    echo "Only tracked files can be scrubbed from history."
    echo "If the file is staged but never committed, use: git rm --cached '$TARGET'"
    exit 1
}

# ── Check the file actually has history ───────────────────────────────────────
HISTORY_COUNT="$(git -C "$REPO_ROOT" log --oneline -- "$RELATIVE_PATH" 2>/dev/null | wc -l | tr -d ' ')"
if [[ "$HISTORY_COUNT" -eq 0 ]]; then
    err "'$RELATIVE_PATH' has no commit history to scrub."
    echo "If it is only staged, use: git rm --cached '$RELATIVE_PATH'"
    exit 1
fi

# ── Check for uncommitted changes to this file ────────────────────────────────
HAS_DIRTY=false
if ! git -C "$REPO_ROOT" diff --quiet HEAD -- "$RELATIVE_PATH" 2>/dev/null; then
    HAS_DIRTY=true
fi
if ! git -C "$REPO_ROOT" diff --quiet --cached -- "$RELATIVE_PATH" 2>/dev/null; then
    HAS_DIRTY=true
fi

# ── Collect remote info ───────────────────────────────────────────────────────
REMOTES="$(git -C "$REPO_ROOT" remote 2>/dev/null || true)"

# Unpushed commits (suppress if no upstream is configured)
UNPUSHED=0
UNPUSHED_OUTPUT="$(git -C "$REPO_ROOT" log '@{u}..' --oneline 2>/dev/null || true)"
if [[ -n "$UNPUSHED_OUTPUT" ]]; then
    UNPUSHED="$(echo "$UNPUSHED_OUTPUT" | wc -l | tr -d ' ')"
fi

# ── Summary banner ────────────────────────────────────────────────────────────
echo ""
bold "git-filter-file$([ "$DRY_RUN" = true ] && echo " (dry run)" || true)"
echo ""
echo "  File:       $RELATIVE_PATH"
echo "  Commits:    $HISTORY_COUNT commit(s) contain this file"
echo "  Operation:  Remove from all history; re-add current content as one commit"
echo ""

if [[ "$HAS_DIRTY" = true ]]; then
    warn "The file has uncommitted changes. The working-tree content (including"
    echo "         those changes) is what will be re-committed after the history rewrite."
    echo ""
fi

if [[ -n "$REMOTES" ]]; then
    warn "This repo has remote(s): $(echo "$REMOTES" | tr '\n' ' ')"
    echo "         After scrubbing you MUST force-push to every remote:"
    echo "           git push --force-with-lease --all"
    echo "           git push --force-with-lease --tags"
    echo ""
fi

if [[ "$UNPUSHED" -gt 0 ]]; then
    warn "$UNPUSHED unpushed commit(s) exist. Those will also be rewritten."
    echo ""
fi

if [[ "$DRY_RUN" = true ]]; then
    echo -e "${DIM}-- dry run: no changes made --${RESET}"
    echo ""
    echo "Would run:"
    echo "  git filter-branch --force --index-filter \\"
    echo "    \"git rm --cached --ignore-unmatch '$RELATIVE_PATH'\" \\"
    echo "    --prune-empty --tag-name-filter cat -- --all"
    echo "  (expire reflog + gc to purge objects)"
    echo "  cp <backup> '$ABS_PATH'"
    echo "  git add '$RELATIVE_PATH'"
    echo "  git commit -m 'Add $RELATIVE_PATH (history scrubbed by git-filter-file)'"
    echo ""
    exit 0
fi

# ── Final confirmation ────────────────────────────────────────────────────────
echo -e "${BOLD}${RED}This will permanently rewrite git history.${RESET} A backup of the current"
echo "file content will be saved to .git/filter-file-backups/ before proceeding."
echo ""
read -r -p "Proceed? [y/N] " CONFIRM
if [[ "${CONFIRM,,}" != "y" && "${CONFIRM,,}" != "yes" ]]; then
    echo "Aborted."
    exit 0
fi

# ── Backup ────────────────────────────────────────────────────────────────────
BACKUP_DIR="$REPO_ROOT/.git/filter-file-backups"
mkdir -p "$BACKUP_DIR"

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
SAFE_NAME="$(echo "$RELATIVE_PATH" | tr '/' '_')"
BACKUP_PATH="$BACKUP_DIR/${TIMESTAMP}_${SAFE_NAME}"

cp "$ABS_PATH" "$BACKUP_PATH"
ok "Backup saved: $BACKUP_PATH"

# ── Save current working-tree content ────────────────────────────────────────
TMPFILE="$(mktemp)"
cp "$ABS_PATH" "$TMPFILE"
trap 'rm -f "$TMPFILE"' EXIT

# ── Run git filter-branch ─────────────────────────────────────────────────────
echo ""
echo "Rewriting history to remove '$RELATIVE_PATH' from $HISTORY_COUNT commit(s)..."
echo "(git filter-branch may print a deprecation warning — this is expected)"
echo ""

# FILTER_BRANCH_SQUELCH_WARNING suppresses the deprecation notice on git >= 2.24
if ! FILTER_BRANCH_SQUELCH_WARNING=1 git -C "$REPO_ROOT" filter-branch \
        --force \
        --index-filter "git rm --cached --ignore-unmatch '$RELATIVE_PATH'" \
        --prune-empty \
        --tag-name-filter cat \
        -- --all; then
    err "git filter-branch failed. Your repository may be in an inconsistent state."
    echo "Backup is at: $BACKUP_PATH"
    echo "To recover, check: git reflog"
    exit 1
fi

ok "History rewritten — '$RELATIVE_PATH' removed from all commits."

# ── Purge saved refs and loose objects ───────────────────────────────────────
# filter-branch saves originals under refs/original/; delete them so gc can
# actually remove the objects from disk.
echo "Expiring reflog and running gc to purge objects..."
git -C "$REPO_ROOT" for-each-ref --format="%(refname)" refs/original/ \
    | xargs -r -n1 git -C "$REPO_ROOT" update-ref -d
git -C "$REPO_ROOT" reflog expire --expire=now --all
git -C "$REPO_ROOT" gc --prune=now --quiet

ok "Objects purged from local repository."

# ── Re-add current content ────────────────────────────────────────────────────
# Ensure parent directory exists (filter-repo may have removed it)
mkdir -p "$(dirname "$ABS_PATH")"
cp "$TMPFILE" "$ABS_PATH"

git -C "$REPO_ROOT" add "$RELATIVE_PATH"
git -C "$REPO_ROOT" commit \
    --message "$(printf 'Add %s\n\nFile re-added after full history scrub.\nGenerated by git-filter-file — no prior history for this file.' "$RELATIVE_PATH")"

ok "Re-committed '$RELATIVE_PATH' as a fresh single commit."

# ── Post-operation instructions ───────────────────────────────────────────────
echo ""
bold "Done. Required next steps:"
echo ""
echo "  1. Verify the file content is correct:"
echo "       cat '$ABS_PATH'"
echo ""

if [[ -n "$REMOTES" ]]; then
    echo "  2. Force-push ALL branches and tags to every remote:"
    echo "       git push --force-with-lease --all"
    echo "       git push --force-with-lease --tags"
    echo ""
    echo "  3. If hosted on GitHub, cached content may linger."
    echo "     Contact GitHub Support to purge server-side caches, or"
    echo "     make the repository private temporarily."
    echo ""
    echo "  4. Alert all collaborators — they must re-clone or run:"
    echo "       git fetch --all && git reset --hard origin/<branch>"
    echo ""
fi

echo "  $([ -n "$REMOTES" ] && echo 5 || echo 2). Rotate any secrets that were exposed. The history rewrite does NOT"
echo "     invalidate tokens, API keys, or passwords that were committed."
echo ""
echo -e "${DIM}Backup of original file: $BACKUP_PATH${RESET}"
echo ""
