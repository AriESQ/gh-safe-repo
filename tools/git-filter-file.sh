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
#
# The rewrite is done by git-filter-repo (https://github.com/newren/git-filter-repo),
# the tool the git documentation recommends in place of `git filter-branch`.
# It is used as `git filter-repo` when installed, otherwise run on demand via
# `uvx git-filter-repo==<pinned version>` (no local install needed). See
# issues #52 and #136.

set -euo pipefail

# ── git-filter-repo pin (uvx fallback) ──────────────────────────────────────
# The `uvx` fallback runs this exact git-filter-repo release from PyPI, so a
# new upstream release cannot change what the script runs (#136). Dependabot
# cannot see a pin inside a shell script: bump it by hand (see "Bumping the
# git-filter-repo pin" in tools/README.md), updating GIT_FILTER_REPO_MIN_GIT
# to match. GIT_FILTER_REPO_VERSION in the environment overrides the pin.
GIT_FILTER_REPO_PIN="2.47.0"
# Minimum git for the pinned release, from the "Prerequisites" section of
# https://github.com/newren/git-filter-repo/blob/v2.47.0/README.md
# ("git >= 2.22.0 at a minimum"). git-filter-repo itself exits with
# "need git >= 2.22.0" when git-diff-tree lacks --combined-all-paths.
GIT_FILTER_REPO_MIN_GIT="2.22.0"
GIT_FILTER_REPO_VERSION="${GIT_FILTER_REPO_VERSION:-$GIT_FILTER_REPO_PIN}"

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

# _timed_fetch — git fetch with timeout; suppresses SSH/git noise.
# Usage: _timed_fetch <repo-path> [timeout-secs]
# Returns 0 on success, 1 on timeout or fetch failure.
_timed_fetch() {
    local repo="$1" secs="${2:-10}"
    git -C "$repo" fetch --quiet &>/dev/null &
    local pid=$!
    local elapsed=0
    while kill -0 "$pid" 2>/dev/null; do
        if [[ $elapsed -ge $secs ]]; then
            kill "$pid" 2>/dev/null
            wait "$pid" 2>/dev/null || true
            return 1
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
    wait "$pid" 2>/dev/null
}

# _resolve_filter_repo — set FILTER_REPO_CMD to the command array that runs
# git-filter-repo. Prefers an installed `git filter-repo`; falls back to
# `uvx git-filter-repo==$GIT_FILTER_REPO_VERSION` when uv is available, and
# sets FILTER_REPO_VIA_UVX. Returns 1 if neither works.
# There is deliberately no fallback to `git filter-branch`.
FILTER_REPO_CMD=()
FILTER_REPO_VIA_UVX=false
_resolve_filter_repo() {
    if git filter-repo --version &>/dev/null; then
        FILTER_REPO_CMD=(git filter-repo)
        return 0
    fi
    if command -v uvx &>/dev/null; then
        FILTER_REPO_CMD=(uvx "git-filter-repo==$GIT_FILTER_REPO_VERSION")
        FILTER_REPO_VIA_UVX=true
        return 0
    fi
    return 1
}

# _version_ge A B — true if dotted version A >= B (numeric, per component;
# missing components count as 0).
_version_ge() {
    local -a a b
    local i x y
    IFS=. read -r -a a <<< "$1"
    IFS=. read -r -a b <<< "$2"
    for ((i = 0; i < ${#a[@]} || i < ${#b[@]}; i++)); do
        x="${a[i]:-0}" y="${b[i]:-0}"
        if ((10#$x > 10#$y)); then return 0; fi
        if ((10#$x < 10#$y)); then return 1; fi
    done
    return 0
}

# _check_filter_repo_version — validate the uvx version and check that git
# is new enough for the pinned release. On a problem, prints it to stdout
# (first line is the summary) and returns 1.
# Only applies to the uvx path: an installed `git filter-repo` was packaged
# for the user's system, its release is unknown (--version prints a commit
# hash, not a release number), and it runs its own git-version checks.
_check_filter_repo_version() {
    [[ "$FILTER_REPO_VIA_UVX" = true ]] || return 0
    if [[ ! "$GIT_FILTER_REPO_VERSION" =~ ^[0-9]+(\.[0-9]+)*$ ]]; then
        echo "GIT_FILTER_REPO_VERSION='$GIT_FILTER_REPO_VERSION' is not a version number (expected e.g. $GIT_FILTER_REPO_PIN)."
        return 1
    fi
    # The minimum git is recorded only for the pinned release. With an
    # override, git-filter-repo's own checks still run before it rewrites.
    [[ "$GIT_FILTER_REPO_VERSION" = "$GIT_FILTER_REPO_PIN" ]] || return 0
    local raw ver
    raw="$(git --version 2>/dev/null || true)"
    if [[ "$raw" =~ ([0-9]+\.[0-9]+(\.[0-9]+)?) ]]; then
        ver="${BASH_REMATCH[1]}"
    else
        echo "Could not determine the git version ('git --version' printed: '$raw')."
        return 1
    fi
    if ! _version_ge "$ver" "$GIT_FILTER_REPO_MIN_GIT"; then
        cat <<EOF
git $ver is too old for git-filter-repo $GIT_FILTER_REPO_PIN (needs git >= $GIT_FILTER_REPO_MIN_GIT).
Either:
  - upgrade git (https://git-scm.com/downloads), or
  - install a git-filter-repo that matches your git (e.g. your distro's
    git-filter-repo package), which the script then uses instead of uvx, or
  - run an older release via uvx: GIT_FILTER_REPO_VERSION=<version>
EOF
        return 1
    fi
}

_filter_repo_install_help() {
    cat >&2 <<EOF
git-filter-repo is required but was not found.

Install one of:
  - uv (recommended; runs git-filter-repo on demand, no install needed):
      https://docs.astral.sh/uv/getting-started/installation/
  - git-filter-repo itself, e.g.:
      brew install git-filter-repo        # macOS
      apt install git-filter-repo         # Debian/Ubuntu
      pipx install git-filter-repo        # any platform
    See https://github.com/newren/git-filter-repo/blob/main/INSTALL.md
EOF
}

# ── Usage ────────────────────────────────────────────────────────────────────
usage() {
    cat <<EOF
Usage: git-filter-file [--dry-run] [--force] [--keep] [--yes] <repo> <file>

Scrub <file> from all git history in <repo>. By default the file is also
removed from the working tree. This is a destructive, irreversible operation
on local history — you must force-push to any remotes afterwards.

Options:
  --dry-run   Show what would happen without making any changes
  --force     Skip the remote-divergence check (see below)
  --keep      Re-add the file's current on-disk content as a fresh commit
              instead of deleting it (requires the file to exist on disk)
  --yes       Skip the interactive confirmation prompt
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

Requirements:
  git-filter-repo, used as 'git filter-repo' if installed, otherwise run via
  'uvx git-filter-repo==$GIT_FILTER_REPO_PIN' (needs uv: https://docs.astral.sh/uv/,
  and git >= $GIT_FILTER_REPO_MIN_GIT). Set GIT_FILTER_REPO_VERSION to run a
  different release via uvx.

  git-filter-repo removes the 'origin' remote after rewriting; this script
  restores the remote configuration it removed, so the force-push below
  goes to the same place as before.

Exit codes:
  0  Success
  1  Runtime failure (file not found, dirty tree, nothing to do,
     git-filter-repo unavailable, git too old for the pinned release)
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
YES=false
POSITIONALS=()

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --force)   FORCE=true ;;
        --keep)    KEEP=true ;;
        --yes)     YES=true ;;
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
# filter-repo rewrites the shared object store — running inside a worktree
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
# git-filter-repo resets the working tree to the rewritten HEAD when it
# finishes, which would discard uncommitted changes. We pass --force to
# filter-repo (see below), which also skips its own clean-tree check, so this
# check is what protects uncommitted work.
if ! git -C "$REPO_ROOT" diff --quiet 2>/dev/null || \
   ! git -C "$REPO_ROOT" diff --cached --quiet 2>/dev/null; then
    err "Working tree has uncommitted changes."
    echo "Rewriting history requires a clean working tree." >&2
    echo "Commit or stash your changes first, then re-run." >&2
    echo "" >&2
    echo "To stash:   git stash" >&2
    echo "To restore: git stash pop  (after this script completes)" >&2
    exit 1
fi

# ── Locate git-filter-repo ────────────────────────────────────────────────────
# Checked before the network fetch and confirmation prompt so a missing tool
# fails fast. A dry run only warns, so the plan can still be previewed.
FILTER_REPO_OK=true
if ! _resolve_filter_repo; then
    FILTER_REPO_OK=false
    if [[ "$DRY_RUN" = false ]]; then
        err "git-filter-repo not found."
        _filter_repo_install_help
        exit 1
    fi
    FILTER_REPO_CMD=(git filter-repo)
fi

# Pinned-version / git-version check (uvx path only). A dry run only warns.
FILTER_REPO_VERSION_PROBLEM=""
if [[ "$FILTER_REPO_OK" = true ]] && \
   ! FILTER_REPO_VERSION_PROBLEM="$(_check_filter_repo_version)"; then
    if [[ "$DRY_RUN" = false ]]; then
        err "${FILTER_REPO_VERSION_PROBLEM%%$'\n'*}"
        if [[ "$FILTER_REPO_VERSION_PROBLEM" == *$'\n'* ]]; then
            echo "${FILTER_REPO_VERSION_PROBLEM#*$'\n'}" >&2
        fi
        exit 1
    fi
else
    FILTER_REPO_VERSION_PROBLEM=""
fi

# ── Collect remote info ───────────────────────────────────────────────────────
REMOTES="$(git -C "$REPO_ROOT" remote 2>/dev/null || true)"

# ── Fetch from remote and check sync status ─────────────────────────────────
# After the history rewrite, local and remote will diverge. If the
# branch was already behind or diverged before the rewrite, the resulting state
# is very hard to reason about. Fetch first, then check.
UNPUSHED=0
DIVERGED=false
FETCH_OK=false
if [[ -n "$REMOTES" ]]; then
    _do_fetch=true
    if [[ "$YES" = false ]]; then
        echo ""
        echo "Remote detected — need to fetch to verify local/remote are in sync."
        echo "This requires network access; SSH key touch may be needed."
        read -r -p "Fetch from remote? [Y/n] " _fetch_ans
        if [[ "${_fetch_ans,,}" == "n" || "${_fetch_ans,,}" == "no" ]]; then
            _do_fetch=false
            warn "Skipped — remote sync status will be unknown."
        fi
    fi

    if [[ "$_do_fetch" = true ]]; then
        echo -n "Fetching (touch your key now, 10s timeout)... "
        if _timed_fetch "$REPO_ROOT" 10; then
            echo "done."
            FETCH_OK=true
        else
            echo ""
            err "Fetch timed out after 10 seconds."
            echo "Check your SSH key or network connection and retry." >&2
            exit 1
        fi
    fi

    if [[ "$FETCH_OK" = true ]]; then
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

if [[ -n "$REMOTES" && "$FETCH_OK" = true ]]; then
    if [[ "$DIVERGED" = true ]]; then
        echo -e "  Remote:     ${RED}diverged${RESET} — local ${LOCAL_REF} ($AHEAD ahead, $BEHIND behind)"
    elif [[ "$UNPUSHED" -eq 0 ]]; then
        echo -e "  Remote:     ${GREEN}verified in sync${RESET} — local ${LOCAL_REF} & remote ${REMOTE_REF} match"
    else
        echo -e "  Remote:     ${YELLOW}local ${LOCAL_REF} is ${UNPUSHED} ahead of remote ${REMOTE_REF}${RESET}"
    fi
elif [[ -n "$REMOTES" ]]; then
    echo -e "  Remote:     ${YELLOW}sync status unknown (fetch skipped)${RESET}"
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
    echo "    cd $REPO_ROOT && git push --force-with-lease --all"
    echo "    cd $REPO_ROOT && git push --force-with-lease --tags"
    if [[ "$DIVERGED" = true ]]; then
        echo ""
        echo -e "  ${RED}--force enabled,${RESET} upstreaming changes will be very difficult."
    fi
    echo ""
fi

if [[ "$DRY_RUN" = true ]]; then
    echo -e "${DIM}-- dry run: no changes made --${RESET}"
    echo ""
    if [[ "$FILTER_REPO_OK" = false ]]; then
        warn "git-filter-repo not found — a real run would fail. Install it first:"
        _filter_repo_install_help 2>&1 | sed '1,2d'
        echo ""
    fi
    if [[ -n "$FILTER_REPO_VERSION_PROBLEM" ]]; then
        warn "a real run would fail:"
        while IFS= read -r _line; do
            echo "  $_line"
        done <<< "$FILTER_REPO_VERSION_PROBLEM"
        echo ""
    fi
    echo "Would run:"
    echo "  ${FILTER_REPO_CMD[*]} --force --invert-paths --path '$RELATIVE_PATH'"
    echo "    (rewrites all branches and tags, prunes commits left empty,"
    echo "     then expires reflogs and repacks to purge the old objects)"
    if [[ -n "$REMOTES" ]]; then
        echo "  restore remote config removed by git-filter-repo: $(echo "$REMOTES" | tr '\n' ' ')"
        echo "  git fetch  (refresh remote tracking refs for --force-with-lease)"
    fi
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
if [[ "$YES" = false ]]; then
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

# ── Save remote configuration ────────────────────────────────────────────────
# git-filter-repo deletes the 'origin' remote after rewriting (a guard against
# pushing rewritten history to the wrong place by accident). This script's
# purpose is to force-push the scrubbed history back to the same remote, so
# record every remote.* and branch upstream setting now and restore whatever
# filter-repo removed afterwards. Each entry is stored as "key<newline>value".
SAVED_REMOTE_CONFIG=()
if [[ -n "$REMOTES" ]]; then
    while IFS= read -r -d '' _entry; do
        SAVED_REMOTE_CONFIG+=("$_entry")
    done < <(git -C "$REPO_ROOT" config --local --null --get-regexp \
        '^(remote\..*|branch\..*\.(remote|merge|pushremote))$' 2>/dev/null || true)
fi

# _restore_remote_config — re-add any saved remote/upstream config entries
# that are now missing. Entries that still exist are left untouched.
_restore_remote_config() {
    local entry key value
    for entry in "${SAVED_REMOTE_CONFIG[@]}"; do
        key="${entry%%$'\n'*}"
        value="${entry#*$'\n'}"
        if ! git -C "$REPO_ROOT" config --local --get-all "$key" 2>/dev/null \
                | grep -qxF -- "$value"; then
            git -C "$REPO_ROOT" config --local --add "$key" "$value" || return 1
        fi
    done
}

# ── Run git-filter-repo ───────────────────────────────────────────────────────
echo ""
echo "Rewriting history to remove '$RELATIVE_PATH' from $HISTORY_COUNT commit(s)..."
echo -e "${DIM}Using: ${FILTER_REPO_CMD[*]}${RESET}"
echo ""

# --force: filter-repo refuses to run on anything but a fresh clone (it checks
# for a single reflog entry, no stashes, a packed repo, etc.). This script is
# meant to run on the user's working repo, and it performs its own safety
# checks above (worktree guard, clean tree, remote sync, confirmation prompt),
# so we bypass filter-repo's fresh-clone check deliberately.
# --invert-paths --path: drop exactly this path (a file, or a directory and
# everything under it; matched literally, not as a glob) from every commit on
# every branch and tag. Commits that become empty are pruned; filter-repo then
# expires reflogs and repacks so the old objects are purged from the local
# object store.
if ! (cd "$REPO_ROOT" && "${FILTER_REPO_CMD[@]}" \
        --force \
        --invert-paths \
        --path "$RELATIVE_PATH"); then
    err "git-filter-repo failed. Your repository may be in an inconsistent state."
    if [[ -n "$BACKUP_PATH" ]]; then
        echo "Backup is at: $BACKUP_PATH"
    fi
    if [[ ${#SAVED_REMOTE_CONFIG[@]} -gt 0 ]]; then
        _restore_remote_config || true
    fi
    echo "To recover, check: git reflog"
    exit 1
fi

ok "History rewritten — '$RELATIVE_PATH' removed from all commits."
ok "Objects purged from local repository."

# ── Restore remotes removed by git-filter-repo ───────────────────────────────
if [[ ${#SAVED_REMOTE_CONFIG[@]} -gt 0 ]]; then
    if _restore_remote_config; then
        ok "Restored remote configuration removed by git-filter-repo."
    else
        warn "Could not fully restore remote configuration. Check 'git remote -v'."
        echo "  Re-add a missing remote with: git remote add <name> <url>"
    fi
fi

# ── Refresh remote tracking refs ────────────────────────────────────────────
# git-filter-repo deletes the 'origin' remote together with its tracking refs
# (and rewrites tracking refs of any other remote, leaving them pointing at
# commits that do not exist on that remote). Without accurate tracking refs,
# --force-with-lease refuses to push. A fetch recreates/updates them from the
# real remote state so force-with-lease works correctly in the post-push step.
if [[ -n "$REMOTES" ]]; then
    echo -n "Refreshing remote tracking refs (touch your key, 10s timeout)... "
    if _timed_fetch "$REPO_ROOT" 10; then
        echo "done."
    else
        echo ""
        warn "Fetch timed out — run 'git fetch' before pushing, or use --force instead of --force-with-lease."
    fi
fi

if [[ "$KEEP" = true ]]; then
    # ── Re-add current content ───────────────────────────────────────────────
    # Ensure parent directory exists (the rewrite may have removed it)
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
    echo "       cd $REPO_ROOT && git log --all --oneline -- '$RELATIVE_PATH'  # should be empty"
fi
echo ""

if [[ -n "$REMOTES" ]]; then
    STEP=$((STEP + 1))
    echo "  $STEP. Force-push ALL branches and tags to every remote."
    echo "     git-filter-repo removed 'origin'; this script restored it. Confirm the"
    echo "     remote URLs are where you intend to push first:"
    echo "       cd $REPO_ROOT && git remote -v"
    echo "       cd $REPO_ROOT && git push --force-with-lease --all"
    echo "       cd $REPO_ROOT && git push --force-with-lease --tags"
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
