# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Status

All phases (1–4) are complete and verified. All TODOs and open questions are resolved.

- `docs/2026-02-07_github-safe-repo-defaults.md` — Research on what can/can't be automated on free vs. paid GitHub plans
- `docs/2026-02-22_github-repo-automation-implementation-plan.md` — Full implementation plan including architecture, phasing, and decisions

## Development Setup

```bash
uv sync                     # creates .venv, installs pytest
uv run pytest tests/ -v
uv tool install .           # install gh-safe-repo globally
```

The `.venv/` directory is gitignored. There are no non-stdlib runtime dependencies. Tests mock all subprocess calls — no real API calls are made.

## Dependency Policy

**Do not add Python dependencies during implementation.** If a task appears to require a new Python package, stop and ask the user before proceeding. Stdlib modules are always fine.

## Key External Interactions

API and tool quirks that affect future work. For full context on any of these, see `docs/LEARNINGS.md`.

**`gh api` subprocess pattern:**
- JSON bodies passed via `--input -` (stdin), not `--field` — nested objects require it
- Status code parsed from stderr via regex (`HTTP (\d{3})`); fragile if `gh` changes format
- Token injected as `GH_TOKEN` in child process env; HTTPS auth uses `x-access-token:{token}@github.com`

**truffleHog v3:**
- Exit code 1 = findings found (not error); any other non-zero = failure → fall back to regex
- `_try_trufflehog` returns `None` (failure, fall back to regex) vs `[]` (success, no findings)
- `trufflehog_mode` values: `auto` / `native` / `docker` / `off`; backwards-compat with legacy `use_trufflehog = false`

**GitHub API quirks:**
- Secret scanning auto-enabled for public repos (no API call needed)
- `sha_pinning_required` and `allowed_actions` use `PUT /actions/permissions` (not `/workflow` subpath); `enabled: true` required in body
- `PUT /actions/permissions/selected-actions` sets `github_owned_allowed`, `verified_allowed`, `patterns_allowed` — only works when `allowed_actions` is `"selected"`
- Secret scanning on private paid repos: `PATCH /repos` with `security_and_analysis` body (different from Dependabot's `PUT /vulnerability-alerts`)
- Push protection: `PATCH /repos` with `security_and_analysis.secret_scanning_push_protection` — batched with secret scanning in one call
- Private vulnerability reporting: `PUT /repos/{owner}/{repo}/private-vulnerability-reporting` (dedicated endpoint, not `security_and_analysis`)
- Dependabot security updates: `PUT /repos/{owner}/{repo}/automated-security-fixes` (same pattern as vulnerability-alerts)
- Toggle endpoint status detection: use `200 <= status < 300` not `status == 204` — different endpoints return 200 vs 204 and `gh api` sometimes infers 200 from exit code 0
- No REST API for: grouped security updates (use dependabot.yml), automatic dependency submission (UI only), dependency graph on private repos (UI only)

**Key design invariants:**
- CLI uses subcommands: `create`, `fix`, `scan` — all GitHub-targeting commands require `owner/repo` format
- `owner` in `owner/repo` is validated case-insensitively against the authenticated user (`build_context()` in `commands/_common.py`)
- `fix` has no secret scanning (settings-only); `create --local/--from` has automatic pre-flight scan
- `create` and `fix` both prompt for confirmation before applying; `--yes`/`-y` skips the prompt for scripted/batch use
- `enforce_admins = false` is intentional (owner bypass for tooling)
- Config with all merge strategies disabled (`allow_squash_merge`, `allow_merge_commit`, `allow_rebase_merge` all `false`) is rejected at `repo_settings()` with `ConfigError` — GitHub returns 422
- `auto_init` must be `false` when `--local` or `--from` is used (avoids push rejection)
- `bp_plugin.apply()` must run after code push (`--local`/`--from`) — branch must exist first
- Tag protection uses Rulesets API exclusively (`POST /repos/{owner}/{repo}/rulesets` with `target: "tag"`); no classic equivalent exists
- Tag protection only works on public repos or paid GitHub plans (free+private → SKIP), same as branch protection
- `is_public` derived from config after `apply_overrides()`, not from `args.public` directly
- `--from` uses `owner/repo` format; works with both private (default) and public destinations
- `--dry-run` makes zero API calls (including no repo-exists check)

## Critical GitHub Plan Limitations

Essential context for every implementation decision:

| Feature | Free + Public | Free + Private | Pro + Private |
|---------|:---:|:---:|:---:|
| Branch protection / Rulesets | ✅ | ❌ | ✅ |
| Tag protection (Rulesets) | ✅ | ❌ | ✅ |
| Dependabot alerts | ✅ | ❌ | ✅ |
| Dependabot security updates | ✅ | ❌ | ✅ |
| Secret scanning | ✅ | ❌ | ✅ |
| Push protection | ✅ | ❌ | ✅ |
| Private vulnerability reporting | ✅ | ✅ | ✅ |

The tool must detect repo visibility and plan level at runtime and gracefully skip unavailable features with clear messaging — never fail silently.

## Future Work

- **Shell completion.** Hand-rolled `--completion bash` subcommand or static file in `tools/`. No runtime dep.
- **Upstream contribution:** plan-limit detection (`GET /user → plan.name`) for `gh-repo-settings` (Go CLI).

## Pointers

- **CLI usage, configuration, installation:** see `README.md`
- **Architecture, module map, plugin pattern, adding new settings:** see `gh_safe_repo/README.md`
- **Test conventions and mocking patterns:** see `tests/README.md`
- **Tools (scrub-ai-context.sh, trufflehog wrapper):** see `tools/README.md`
- **API endpoint research:** see `docs/`
- **Implementation learnings (phase-by-phase technical notes):** see `docs/LEARNINGS.md`
