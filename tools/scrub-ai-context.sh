#!/usr/bin/env bash
# scrub-ai-context — remove AI agent context files from all git history, then
# re-add any that currently exist on disk as a single fresh commit.
#
# Usage: scrub-ai-context [--dry-run] [--push] [<path>...]
#
# If no paths are given, the script auto-detects known AI context files
# (CLAUDE.md, AGENTS.md, .cursorrules, copilot-instructions.md, .cursor)
# that have history in this repository.
#
# Handles both files and directories (e.g. .cursor/).
# Multiple targets are scrubbed in one filter-branch pass.

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
Multiple targets are removed in one filter-branch pass.

Options:
  --dry-run   Show what would happen without making any changes
  --push      Force-push all branches and tags after scrubbing
  -h, --help  Show this help

Examples:
  scrub-ai-context                    # auto-detect and scrub all known files
  scrub-ai-context CLAUDE.md          # scrub one specific file
  scrub-ai-context --dry-run          # preview auto-detected targets
  scrub-ai-context --push CLAUDE.md   # scrub and force-push

After running (without --push), you MUST:
  1. Force-push all branches:  git push --force-with-lease --all
  2. Force-push all tags:      git push --force-with-lease --tags
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

# ── Worktree guard ────────────────────────────────────────────────────────────
if [[ "$(git rev-parse --git-dir)" != "$(git rev-parse --git-common-dir)" ]]; then
    err "Running inside a git worktree is not supported."
    echo "Switch to the main working tree and run this script from there."
    exit 1
fi

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

# ── Validate each target and gather metadata ──────────────────────────────────
# Parallel arrays (bash 3 compatible):
#   RELATIVE_PATHS   — repo-relative paths for filter-branch
#   AT_HEAD          — "yes" if the path exists on disk right now
#   HISTORY_COUNTS   — number of commits containing this path
declare -a RELATIVE_PATHS=()
declare -a AT_HEAD=()
declare -a HISTORY_COUNTS=()

for target in "${TARGETS[@]}"; do
    # Resolve to absolute, then back to repo-relative
    if [[ "$target" = /* ]]; then
        abs="$target"
    else
        abs="$REPO_ROOT/$target"
    fi

    # realpath only if path exists; for deleted-from-HEAD paths it won't
    if [[ -e "$abs" ]]; then
        abs="$(realpath "$abs")"
    fi

    rel="${abs#"$REPO_ROOT/"}"

    # Must have at least one commit in history
    hist="$(git -C "$REPO_ROOT" log --all --oneline -- "$rel" 2>/dev/null | wc -l | tr -d ' ')"
    if [[ "$hist" -eq 0 ]]; then
        err "'$target' has no commit history to scrub."
        echo "  If it is only staged, use: git rm --cached '$target'"
        exit 1
    fi

    RELATIVE_PATHS+=("$rel")
    HISTORY_COUNTS+=("$hist")

    if [[ -f "$abs" || -d "$abs" ]]; then
        AT_HEAD+=("yes")
    else
        AT_HEAD+=("no")
    fi
done

# ── Require clean working tree ────────────────────────────────────────────────
if ! git -C "$REPO_ROOT" diff --quiet 2>/dev/null || \
   ! git -C "$REPO_ROOT" diff --cached --quiet 2>/dev/null; then
    err "Working tree has uncommitted changes."
    echo "git filter-branch requires a clean working tree."
    echo "Commit or stash your changes first, then re-run."
    echo ""
    echo "To stash:   git stash"
    echo "To restore: git stash pop  (after this script completes)"
    exit 1
fi

# ── Collect remote info ───────────────────────────────────────────────────────
REMOTES="$(git -C "$REPO_ROOT" remote 2>/dev/null || true)"

UNPUSHED_OUTPUT="$(git -C "$REPO_ROOT" log '@{u}..' --oneline 2>/dev/null || true)"
UNPUSHED=0
if [[ -n "$UNPUSHED_OUTPUT" ]]; then
    UNPUSHED="$(echo "$UNPUSHED_OUTPUT" | wc -l | tr -d ' ')"
fi

# ── Summary banner ────────────────────────────────────────────────────────────
echo ""
bold "scrub-ai-context$([ "$DRY_RUN" = true ] && echo " (dry run)" || true)"
echo ""
echo "  Targets:"
for i in "${!RELATIVE_PATHS[@]}"; do
    rel="${RELATIVE_PATHS[$i]}"
    hist="${HISTORY_COUNTS[$i]}"
    head="${AT_HEAD[$i]}"
    note=""
    [[ "$head" = "no" ]] && note="  ${DIM}(not at HEAD — history-only)${RESET}"
    echo -e "    $rel  ($hist commit(s))$note"
done
echo ""
echo "  Operation:  Remove from all history; re-add current content as one commit"
echo "              (targets not present at HEAD are scrubbed only — no re-add)"
echo ""

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

# ── Build filter-branch index-filter command ──────────────────────────────────
# -r enables recursive removal so directories work too; harmless for files.
GIT_RM_ARGS="git rm --cached --ignore-unmatch -r"
for rel in "${RELATIVE_PATHS[@]}"; do
    GIT_RM_ARGS="$GIT_RM_ARGS '$rel'"
done

if [[ "$DRY_RUN" = true ]]; then
    echo -e "${DIM}-- dry run: no changes made --${RESET}"
    echo ""
    echo "Would run:"
    echo "  git filter-branch --force --index-filter \\"
    echo "    \"$GIT_RM_ARGS\" \\"
    echo "    --prune-empty --tag-name-filter cat -- --all"
    echo "  (expire reflog + gc to purge objects)"
    echo ""
    for i in "${!RELATIVE_PATHS[@]}"; do
        [[ "${AT_HEAD[$i]}" = "yes" ]] || continue
        echo "  cp -r <backup> '${RELATIVE_PATHS[$i]}'"
    done
    echo "  git add <targets-at-head>"
    echo "  git commit -m 'Add AI context files (history scrubbed by scrub-ai-context)'"
    [[ "$PUSH" = true ]] && echo "  git push --force-with-lease --all && git push --force-with-lease --tags"
    echo ""
    exit 0
fi

# ── Final confirmation ────────────────────────────────────────────────────────
echo -e "${BOLD}${RED}This will permanently rewrite git history.${RESET} Backups of current file"
echo "content will be saved to .git/filter-file-backups/ before proceeding."
echo ""
read -r -p "Proceed? [y/N] " CONFIRM
if [[ "${CONFIRM,,}" != "y" && "${CONFIRM,,}" != "yes" ]]; then
    echo "Aborted."
    exit 0
fi

# ── Backup files/dirs that exist at HEAD ─────────────────────────────────────
BACKUP_DIR="$REPO_ROOT/.git/filter-file-backups"
mkdir -p "$BACKUP_DIR"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

declare -a BACKUP_PATHS=()

for i in "${!RELATIVE_PATHS[@]}"; do
    if [[ "${AT_HEAD[$i]}" = "yes" ]]; then
        rel="${RELATIVE_PATHS[$i]}"
        abs="$REPO_ROOT/$rel"
        safe_name="$(echo "$rel" | tr '/' '_')"
        backup="$BACKUP_DIR/${TIMESTAMP}_${safe_name}"
        cp -r "$abs" "$backup"
        BACKUP_PATHS+=("$backup")
        ok "Backup saved: $backup"
    else
        BACKUP_PATHS+=("")   # placeholder to keep array indices aligned
    fi
done

# ── Save working-tree content to temp area ────────────────────────────────────
# filter-branch will remove the files from the working tree too.
TMPDIR_CONTENT="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_CONTENT"' EXIT

declare -a TMP_COPIES=()
for i in "${!RELATIVE_PATHS[@]}"; do
    if [[ "${AT_HEAD[$i]}" = "yes" ]]; then
        rel="${RELATIVE_PATHS[$i]}"
        abs="$REPO_ROOT/$rel"
        basename_part="$(basename "$rel")"
        tmp_copy="$TMPDIR_CONTENT/$basename_part"
        cp -r "$abs" "$tmp_copy"
        TMP_COPIES+=("$tmp_copy")
    else
        TMP_COPIES+=("")
    fi
done

# ── Run git filter-branch ─────────────────────────────────────────────────────
echo ""
echo "Rewriting history to remove ${#RELATIVE_PATHS[@]} path(s)..."
echo "(git filter-branch may print a deprecation warning — this is expected)"
echo ""

if ! FILTER_BRANCH_SQUELCH_WARNING=1 git -C "$REPO_ROOT" filter-branch \
        --force \
        --index-filter "$GIT_RM_ARGS" \
        --prune-empty \
        --tag-name-filter cat \
        -- --all; then
    err "git filter-branch failed. Your repository may be in an inconsistent state."
    echo "Backups are in: $BACKUP_DIR"
    echo "To recover, check: git reflog"
    exit 1
fi

ok "History rewritten — targets removed from all commits."

# ── Purge saved refs and loose objects ───────────────────────────────────────
echo "Expiring reflog and running gc to purge objects..."
while IFS= read -r ref; do
    git -C "$REPO_ROOT" update-ref -d "$ref"
done < <(git -C "$REPO_ROOT" for-each-ref --format="%(refname)" refs/original/)
git -C "$REPO_ROOT" reflog expire --expire=now --all
git -C "$REPO_ROOT" gc --prune=now --quiet

ok "Objects purged from local repository."

# ── Re-add files that existed at HEAD ────────────────────────────────────────
READD_RELS=()

for i in "${!RELATIVE_PATHS[@]}"; do
    [[ "${AT_HEAD[$i]}" = "yes" ]] || continue
    rel="${RELATIVE_PATHS[$i]}"
    abs="$REPO_ROOT/$rel"
    tmp="${TMP_COPIES[$i]}"

    mkdir -p "$(dirname "$abs")"
    cp -r "$tmp" "$abs"
    git -C "$REPO_ROOT" add "$rel"
    READD_RELS+=("$rel")
done

if [[ "${#READD_RELS[@]}" -gt 0 ]]; then
    joined="$(IFS=', '; echo "${READD_RELS[*]}")"
    git -C "$REPO_ROOT" commit \
        --message "$(printf 'Add AI context files\n\nRe-added after full history scrub: %s\nGenerated by scrub-ai-context — no prior history for these files.' "$joined")"
    ok "Re-committed ${#READD_RELS[@]} file(s) as a fresh single commit."
else
    ok "No files to re-add (all targets were history-only)."
fi

# ── Optional force-push ───────────────────────────────────────────────────────
if [[ "$PUSH" = true ]]; then
    if [[ -z "$REMOTES" ]]; then
        warn "No remotes configured — skipping push."
    else
        echo ""
        echo "Force-pushing all branches and tags..."
        git -C "$REPO_ROOT" push --force-with-lease --all
        git -C "$REPO_ROOT" push --force-with-lease --tags
        ok "Force-push complete."
    fi
fi

# ── Post-operation instructions ───────────────────────────────────────────────
echo ""
bold "Done. Required next steps:"
echo ""

step=1
echo "  $step. Verify file contents are correct:"
for rel in "${READD_RELS[@]}"; do
    echo "       cat '$rel'"
done
echo ""

if [[ -n "$REMOTES" && "$PUSH" = false ]]; then
    step=$((step + 1))
    echo "  $step. Force-push ALL branches and tags to every remote:"
    echo "       git push --force-with-lease --all"
    echo "       git push --force-with-lease --tags"
    echo ""
    step=$((step + 1))
    echo "  $step. If hosted on GitHub, cached content may linger."
    echo "     Make the repository private temporarily, or contact GitHub Support"
    echo "     to request a server-side cache purge."
    echo ""
    step=$((step + 1))
    echo "  $step. Alert all collaborators — they must re-clone or run:"
    echo "       git fetch --all && git reset --hard origin/<branch>"
    echo ""
fi

step=$((step + 1))
echo "  $step. AI context files contain no secrets, but if any other committed"
echo "     files contained secrets, rotate those credentials now."
echo "     History rewriting does NOT invalidate tokens or API keys."
echo ""
echo -e "${DIM}Backups of original files: $BACKUP_DIR${RESET}"
echo ""
