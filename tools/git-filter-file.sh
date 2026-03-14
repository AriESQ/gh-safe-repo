#!/usr/bin/env bash
# git-filter-file — scrub a file from all git history.
#
# Usage: git-filter-file [--dry-run] [--force] [--keep] <repo> <file>
#
# Purpose: pre-sharing safety tool. Remove accidentally committed secrets or
# large files from every commit in history.
#
# By default the file is removed from history and the working tree. With --keep,
# the current on-disk content is re-added as a single fresh commit (useful when
# the file still exists and only the history needs scrubbing).

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
Usage: git-filter-file [--dry-run] [--force] [--keep] <repo> <file>

Scrub <file> from all git history in <repo>. By default the file is also
removed from the working tree. This is a destructive, irreversible operation
on local history — you must force-push to any remotes afterwards.

Options:
  --dry-run   Show what would happen without making any changes
  --force     Skip the remote-divergence check (see below)
  --keep      Re-add the file's current on-disk content as a fresh commit
              instead of deleting it (requires the file to exist on disk)
  -h, --help  Show this help

Arguments:
  <repo>   Path to the git repository (use . for the current directory)
  <file>   File to scrub. If the path contains a /, it is treated as a
           repo-relative path and matched exactly. A bare filename (no /)
           searches all of history — errors if the name is ambiguous.

Force mode (--force):
  Normally the script refuses to run when local and remote have diverged or
  when the local branch is behind the remote. --force bypasses that check.
  This is useful when you have cloned a public repo, made local changes, and
  plan to re-upload it as a new GitHub repository — you will never push back
  to the original remote. Be aware that after rewriting history with --force
  it will be very difficult or impossible to upstream your changes back to
  the original repository.

Examples:
  git-filter-file . secrets/api_key.txt
  git-filter-file --dry-run ~/projects/my-app config/credentials.json
  git-filter-file . api_key.txt                  # find anywhere in history
  git-filter-file --keep . config.json            # scrub history, keep file

Exit codes:
  0  Success
  1  Runtime failure (file not found, dirty tree, nothing to do)
  2  Usage error (bad arguments, not a git repo)

After running, you MUST:
  1. Force-push all branches:  git push --force-with-lease --all
  2. Rotate any exposed secrets — history rewrite does not invalidate tokens
  3. Alert collaborators to re-clone or reset their local copies
EOF
    exit "${1:-0}"
}

# ── Argument parsing ─────────────────────────────────────────────────────────
DRY_RUN=false
FORCE=false
KEEP=false
POSITIONALS=()

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --force)   FORCE=true ;;
        --keep)    KEEP=true ;;
        -h|--help) usage 0 ;;
        -*) err "Unknown option: $arg"; usage 2 ;;
        *)  POSITIONALS+=("$arg") ;;
    esac
done

if [[ ${#POSITIONALS[@]} -lt 2 ]]; then
    err "Expected two arguments: <repo> <file>"
    usage 2
fi
if [[ ${#POSITIONALS[@]} -gt 2 ]]; then
    err "Too many arguments. Expected: <repo> <file>"
    usage 2
fi

REPO_ARG="${POSITIONALS[0]}"
FILE_ARG="${POSITIONALS[1]}"

# ── Resolve repo path ───────────────────────────────────────────────────────
REPO_ROOT="$(git -C "$REPO_ARG" rev-parse --show-toplevel 2>/dev/null)" || {
    err "'$REPO_ARG' is not inside a git repository."
    exit 2
}

# ── Worktree guard ────────────────────────────────────────────────────────────
# filter-branch rewrites the shared object store — running inside a worktree
# would rewrite the main repo's history from an unexpected working directory.
if [[ "$(git -C "$REPO_ROOT" rev-parse --git-dir)" != "$(git -C "$REPO_ROOT" rev-parse --git-common-dir)" ]]; then
    err "Running inside a git worktree is not supported."
    echo "Switch to the main working tree and run this script from there." >&2
    exit 2
fi

# ── Locate file in git history ───────────────────────────────────────────────
# If FILE_ARG contains a /, treat it as an exact repo-relative path.
# Otherwise search all of history for any file with that name.
RELATIVE_PATH=""

if [[ "$FILE_ARG" == */* ]]; then
    # Exact repo-relative path — verify it exists in history
    if [[ -n "$(git -C "$REPO_ROOT" log --all --oneline -1 -- "$FILE_ARG" 2>/dev/null)" ]]; then
        RELATIVE_PATH="$FILE_ARG"
    else
        _not_found=true
        if [[ -f "$REPO_ROOT/$FILE_ARG" ]]; then
            echo "File exists on disk: $REPO_ROOT/$FILE_ARG"
        else
            echo "File not found on disk."
        fi
        echo "File not found in git history."
        echo "The path is treated as repo-relative (it contains a /)."
        echo "Nothing to do."
        exit 1
    fi
else
    # Bare filename — search all history
    _found="$(git -C "$REPO_ROOT" log --all --diff-filter=A --name-only --pretty=format: \
        -- "*/$FILE_ARG" "$FILE_ARG" 2>/dev/null | grep -v '^$' | sort -u)" || true
    _match_count="$(echo "$_found" | grep -c -v '^$' 2>/dev/null)" || _match_count=0

    if [[ "$_match_count" -eq 1 ]]; then
        RELATIVE_PATH="$_found"
    elif [[ "$_match_count" -gt 1 ]]; then
        echo "Multiple files named '$FILE_ARG' found in history:"
        echo "$_found" | sed 's/^/  /'
        echo ""
        echo "Specify the repo-relative path instead (e.g. dir/$FILE_ARG)."
        exit 1
    else
        if [[ -f "$REPO_ROOT/$FILE_ARG" ]]; then
            echo "File exists on disk: $REPO_ROOT/$FILE_ARG"
        else
            echo "File not found on disk."
        fi
        echo "File not found in git history."
        echo "Nothing to do."
        exit 1
    fi
fi

# ── Check if file is on disk ────────────────────────────────────────────────
ABS_PATH="$REPO_ROOT/$RELATIVE_PATH"
FILE_ON_DISK=false
if [[ -f "$ABS_PATH" ]]; then
    FILE_ON_DISK=true
fi

# ── Validate --keep ──────────────────────────────────────────────────────────
if [[ "$KEEP" = true && "$FILE_ON_DISK" = false ]]; then
    echo "--keep requires the file to exist on disk."
    echo "File not found: $ABS_PATH"
    exit 1
fi

# ── Check the file actually has history ───────────────────────────────────────
HISTORY_COUNT="$(git -C "$REPO_ROOT" log --all --oneline -- "$RELATIVE_PATH" 2>/dev/null | wc -l | tr -d ' ')"
if [[ "$HISTORY_COUNT" -eq 0 ]]; then
    echo "'$RELATIVE_PATH' has no commit history to scrub."
    echo "If it is only staged, use: git rm --cached '$RELATIVE_PATH'"
    exit 1
fi

# ── Require clean working tree ────────────────────────────────────────────────
# git filter-branch refuses to run with uncommitted changes anywhere in the repo.
# Error early with a clear message rather than letting filter-branch fail cryptically.
if ! git -C "$REPO_ROOT" diff --quiet 2>/dev/null || \
   ! git -C "$REPO_ROOT" diff --cached --quiet 2>/dev/null; then
    err "Working tree has uncommitted changes."
    echo "git filter-branch requires a clean working tree." >&2
    echo "Commit or stash your changes first, then re-run." >&2
    echo "" >&2
    echo "To stash:   git stash" >&2
    echo "To restore: git stash pop  (after this script completes)" >&2
    exit 1
fi

# ── Collect remote info ───────────────────────────────────────────────────────
REMOTES="$(git -C "$REPO_ROOT" remote 2>/dev/null || true)"

# ── Require branch is up to date with remote ────────────────────────────────
# After filter-branch rewrites history, local and remote will diverge. If the
# branch was already behind or diverged before the rewrite, the resulting state
# is very hard to reason about. Fetch first, then check.
UNPUSHED=0
DIVERGED=false
if [[ -n "$REMOTES" ]]; then
    git -C "$REPO_ROOT" fetch --quiet 2>/dev/null || true

    LOCAL_REF="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo "unknown")"
    REMOTE_REF="$(git -C "$REPO_ROOT" rev-parse --short '@{u}' 2>/dev/null || echo "unknown")"
    BEHIND="$(git -C "$REPO_ROOT" rev-list 'HEAD..@{u}' --count 2>/dev/null || echo 0)"
    AHEAD="$(git -C "$REPO_ROOT" rev-list '@{u}..HEAD' --count 2>/dev/null || echo 0)"

    if [[ "$BEHIND" -gt 0 && "$AHEAD" -gt 0 ]]; then
        if [[ "$FORCE" = true ]]; then
            DIVERGED=true
        else
            err "Local branch has diverged from remote ($AHEAD ahead, $BEHIND behind)."
            echo "Resolve with 'git pull --rebase' or 'git merge' before rewriting history." >&2
            echo "Or use --force to skip this check (will make upstreaming very difficult)." >&2
            exit 1
        fi
    elif [[ "$BEHIND" -gt 0 ]]; then
        if [[ "$FORCE" = true ]]; then
            DIVERGED=true
        else
            err "Local branch is $BEHIND commit(s) behind remote."
            echo "Run 'git pull' to incorporate remote changes before rewriting history." >&2
            echo "Or use --force to skip this check (will make upstreaming very difficult)." >&2
            exit 1
        fi
    fi
    UNPUSHED="$AHEAD"
fi

# ── Summary banner ────────────────────────────────────────────────────────────
echo ""
bold "git-filter-file$([ "$DRY_RUN" = true ] && echo " (dry run)" || true)"
echo ""
echo "  Repo:       $REPO_ROOT"
echo "  File:       $RELATIVE_PATH"
echo "  Commits:    $HISTORY_COUNT commit(s) contain this file"
if [[ "$KEEP" = true ]]; then
    echo "  Operation:  Remove from all history; re-add current content as one commit"
else
    echo "  Operation:  Remove from all history and working tree"
fi

if [[ -n "$REMOTES" && -n "$LOCAL_REF" && -n "$REMOTE_REF" ]]; then
    if [[ "$DIVERGED" = true ]]; then
        echo -e "  Remote:     ${RED}diverged${RESET} — local ${LOCAL_REF} ($AHEAD ahead, $BEHIND behind)"
    elif [[ "$UNPUSHED" -eq 0 ]]; then
        echo -e "  Remote:     ${GREEN}verified in sync${RESET} — local ${LOCAL_REF} & remote ${REMOTE_REF} match"
    else
        echo -e "  Remote:     ${YELLOW}local ${LOCAL_REF} is ${UNPUSHED} ahead of remote ${REMOTE_REF}${RESET}"
    fi
elif [[ -z "$REMOTES" ]]; then
    echo -e "  Remote:     ${DIM}none (local-only repo)${RESET}"
fi
if [[ "$UNPUSHED" -gt 0 ]]; then
    echo ""
    echo "  $UNPUSHED unpushed commit(s) will also be rewritten."
fi
echo ""

if [[ -n "$REMOTES" ]]; then
    echo "  After scrubbing you MUST force-push to every remote:"
    echo "    git push --force-with-lease --all"
    echo "    git push --force-with-lease --tags"
    if [[ "$DIVERGED" = true ]]; then
        echo ""
        echo -e "  ${RED}--force enabled,${RESET} upstreaming changes will be very difficult."
    fi
    echo ""
fi

if [[ "$DRY_RUN" = true ]]; then
    echo -e "${DIM}-- dry run: no changes made --${RESET}"
    echo ""
    echo "Would run:"
    echo "  git filter-branch --force --index-filter \\"
    echo "    \"git rm --cached --ignore-unmatch '$RELATIVE_PATH'\" \\"
    echo "    --prune-empty --tag-name-filter cat -- --all"
    echo "  git reflog expire --expire=now --all"
    echo "  git gc --prune=now --quiet"
    if [[ "$KEEP" = true ]]; then
        echo "  cp <backup> '$ABS_PATH'"
        echo "  git add '$RELATIVE_PATH'"
        echo "  git commit -m 'Add $RELATIVE_PATH (history scrubbed by git-filter-file)'"
    elif [[ "$FILE_ON_DISK" = true ]]; then
        echo "  rm '$ABS_PATH'  (file deleted from working tree)"
    fi
    echo ""
    exit 0
fi

# ── Final confirmation ────────────────────────────────────────────────────────
echo -e "${BOLD}${RED}This will permanently rewrite git history.${RESET}"
if [[ "$KEEP" = true ]]; then
    echo "A backup of the current file content will be saved to .git/filter-file-backups/ before proceeding."
fi
echo ""
read -r -p "Proceed? [y/N] " CONFIRM
if [[ "${CONFIRM,,}" != "y" && "${CONFIRM,,}" != "yes" ]]; then
    echo "Aborted."
    exit 0
fi

# ── Backup ────────────────────────────────────────────────────────────────────
BACKUP_PATH=""
if [[ "$KEEP" = true ]]; then
    BACKUP_DIR="$REPO_ROOT/.git/filter-file-backups"
    mkdir -p "$BACKUP_DIR"

    TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
    SAFE_NAME="$(echo "$RELATIVE_PATH" | tr '/' '_')"
    BACKUP_PATH="$BACKUP_DIR/${TIMESTAMP}_${SAFE_NAME}"

    cp "$ABS_PATH" "$BACKUP_PATH"
    ok "Backup saved: $BACKUP_PATH"
fi

# ── Save current working-tree content ────────────────────────────────────────
TMPFILE=""
if [[ "$KEEP" = true ]]; then
    TMPFILE="$(mktemp)"
    cp "$ABS_PATH" "$TMPFILE"
    trap 'rm -f "$TMPFILE"' EXIT
fi

# ── Run git filter-branch ─────────────────────────────────────────────────────
echo ""
echo "Rewriting history to remove '$RELATIVE_PATH' from $HISTORY_COUNT commit(s)..."
echo ""

# FILTER_BRANCH_SQUELCH_WARNING suppresses the deprecation notice on git >= 2.24
if ! FILTER_BRANCH_SQUELCH_WARNING=1 git -C "$REPO_ROOT" filter-branch \
        --force \
        --index-filter "git rm --cached --ignore-unmatch '$RELATIVE_PATH'" \
        --prune-empty \
        --tag-name-filter cat \
        -- --all; then
    err "git filter-branch failed. Your repository may be in an inconsistent state."
    if [[ -n "$BACKUP_PATH" ]]; then
        echo "Backup is at: $BACKUP_PATH"
    fi
    echo "To recover, check: git reflog"
    exit 1
fi

ok "History rewritten — '$RELATIVE_PATH' removed from all commits."

# ── Purge saved refs and loose objects ───────────────────────────────────────
# filter-branch saves originals under refs/original/; delete them so gc can
# actually remove the objects from disk.
echo "Expiring reflog and running gc to purge objects..."
while IFS= read -r ref; do
    git -C "$REPO_ROOT" update-ref -d "$ref"
done < <(git -C "$REPO_ROOT" for-each-ref --format="%(refname)" refs/original/)
git -C "$REPO_ROOT" reflog expire --expire=now --all
git -C "$REPO_ROOT" gc --prune=now --quiet

ok "Objects purged from local repository."

if [[ "$KEEP" = true ]]; then
    # ── Re-add current content ───────────────────────────────────────────────
    # Ensure parent directory exists (filter-branch may have removed it)
    mkdir -p "$(dirname "$ABS_PATH")"
    cp "$TMPFILE" "$ABS_PATH"

    git -C "$REPO_ROOT" add "$RELATIVE_PATH"
    git -C "$REPO_ROOT" commit \
        --message "$(printf 'Add %s\n\nFile re-added after full history scrub.\nGenerated by git-filter-file — no prior history for this file.' "$RELATIVE_PATH")"

    ok "Re-committed '$RELATIVE_PATH' as a fresh single commit."
else
    # ── Delete file from working tree (if it exists) ─────────────────────────
    if [[ "$FILE_ON_DISK" = true ]]; then
        rm -f "$ABS_PATH"
        ok "Deleted '$RELATIVE_PATH' from working tree and all history."
    else
        ok "Removed '$RELATIVE_PATH' from all history."
    fi
fi

# ── Post-operation instructions ───────────────────────────────────────────────
echo ""
bold "Done. Required next steps:"
echo ""
STEP=1
if [[ "$KEEP" = true ]]; then
    echo "  $STEP. Verify the file content is correct:"
    echo "       cat '$ABS_PATH'"
else
    echo "  $STEP. Verify the file is gone from history:"
    echo "       git log --all --oneline -- '$RELATIVE_PATH'  # should be empty"
fi
echo ""

if [[ -n "$REMOTES" ]]; then
    STEP=$((STEP + 1))
    echo "  $STEP. Force-push ALL branches and tags to every remote:"
    echo "       git push --force-with-lease --all"
    echo "       git push --force-with-lease --tags"
    echo ""
    STEP=$((STEP + 1))
    echo "  $STEP. If hosted on GitHub, cached content may linger."
    echo "     Contact GitHub Support to purge server-side caches, or"
    echo "     make the repository private temporarily."
    echo ""
    STEP=$((STEP + 1))
    echo "  $STEP. Alert all collaborators — they must re-clone or run:"
    echo "       git fetch --all && git reset --hard origin/<branch>"
    echo ""
fi

STEP=$((STEP + 1))
echo "  $STEP. Rotate any secrets that were exposed. The history rewrite does NOT"
echo "     invalidate tokens, API keys, or passwords that were committed."
echo ""
if [[ -n "$BACKUP_PATH" ]]; then
    echo -e "${DIM}Backup of original file: $BACKUP_PATH${RESET}"
fi
echo ""
