#!/usr/bin/env bash
# gen-completions — regenerate the checked-in shell completion scripts in
# completions/ from the CLI's own parser (`gh-safe-repo --completion SHELL`).
#
# Usage: tools/gen-completions.sh
#
# Run after adding or changing a command or option; tests/test_completion.py
# fails while the files are out of date.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/completions"
PYTHON="${PYTHON:-python3}"

mkdir -p "$OUT"
"$PYTHON" "$ROOT/gh-safe-repo" --completion bash > "$OUT/gh-safe-repo.bash"
"$PYTHON" "$ROOT/gh-safe-repo" --completion zsh  > "$OUT/_gh-safe-repo"
"$PYTHON" "$ROOT/gh-safe-repo" --completion fish > "$OUT/gh-safe-repo.fish"
echo "Wrote $OUT/{gh-safe-repo.bash,_gh-safe-repo,gh-safe-repo.fish}"
