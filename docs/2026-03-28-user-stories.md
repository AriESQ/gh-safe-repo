# gh-safe-repo User Stories

## Core Design Tension

The tool is **declarative** — "make this repo match this config" — but users expect two different modes of declaration:

1. **`create`**: Apply the full template. Preferences and security together. The user is saying "set up a repo the way I like it."
2. **`fix`**: Enforce only what matters for compliance. The user is saying "make sure nothing dangerous has drifted" — not "undo every preference I deliberately changed in the UI."

Today, both commands run through the same plan/apply pipeline with the same config. There's no concept of a setting being "important enough to enforce on fix" vs "just a starting preference for create." The tool treats `has_wiki = false` with the same weight as `enable_secret_scanning_push_protection = true`.

This creates real problems: a user who runs `fix` weekly to catch security drift (#4) gets their deliberate merge strategy changes overwritten. A user who only cares about security (#11) can't omit preference sections without the tool backfilling defaults. A user with hand-tuned branch protection (#19) gets it flattened.

The design question is: **how should the tool know which settings to enforce vs which to leave alone, and where does that boundary live** — in the code, in the config file structure, or in some combination?

---

## User Stories

### 1. "I just want my new repo to not embarrass me" (Novice)

Sam is a bootcamp grad pushing their first portfolio project to GitHub. They have heard horror stories about accidentally leaking API keys and want basic protection but have no idea what branch protection rules or Dependabot even are. They run `gh-safe-repo create my-portfolio --public` and expect sensible defaults without needing to understand config.ini.

Friction: not knowing what the tool skipped or applied, and being confused if the summary output uses jargon like "rulesets" or "push protection" without plain-language explanation.

### 2. "I want all six of my side projects to look the same" (Medium)

Jordan maintains several open-source utilities and has noticed each repo has slightly different settings — some have wikis enabled, some allow rebase merges, some don't. They want to write one config.ini and run `gh-safe-repo fix` across all repos to bring them into alignment. The workflow is a simple loop: `for repo in repo1 repo2 repo3; do gh-safe-repo fix jordan/$repo --yes; done`.

Friction: if one repo is private on a free plan and others are public, the tool will skip features for that repo only, and Jordan needs the output to clearly distinguish "skipped because config says off" from "skipped because plan doesn't support it."

### 3. "I like squash-merge only and I want wikis off everywhere" (Medium)

Priya has strong opinions about merge strategies. She exclusively uses squash merges to keep history clean and considers the wiki tab visual clutter. She edits her config.ini to set `allow_squash_merge = true`, `allow_merge_commit = false`, `allow_rebase_merge = false`, and `has_wiki = false`. She wants `create` to honor these on every new repo without re-specifying them.

Friction: figuring out which config section these belong to (`[repo]`) versus security-related sections, since the config.ini layout groups things by API concern rather than by user mental model.

### 4. "I run fix every Monday morning as hygiene" (Medium)

Alex treats `gh-safe-repo fix` like a lint pass. Every week they run it against their main repo to catch drift — maybe they toggled something in the GitHub UI during debugging and forgot to revert it. They want the output to be concise when nothing changed ("all settings compliant") and detailed only when something was actually modified.

Friction: if the tool always prints the full list of settings even when no changes were made, the signal-to-noise ratio drops and Alex starts ignoring the output entirely. Worse, if fix re-asserts preference settings that Alex intentionally changed in the UI, it undoes their deliberate choices.

### 5. "Personal repos are public and loose, work repos are locked down" (Expert)

Morgan freelances and keeps two config files: `~/.config/gh-safe-repo/personal.ini` with relaxed settings (wiki on, all merge strategies allowed, no tag protection) and `work.ini` with strict settings (squash only, branch protection requiring reviews, Dependabot everything). They invoke the tool with `--config path/to/work.ini` for client projects.

Friction: ensuring the tool resolves config precedence correctly — if a project-local config exists, does it override or merge with the explicit `--config` flag? Morgan needs deterministic, documented precedence.

### 6. "I upgraded to Pro and want to enable everything I was missing" (Medium)

Taylor has been using `gh-safe-repo` on their private repos for months and always saw "SKIP: branch protection requires a paid plan" messages. They just upgraded to Pro and want to re-run `fix` to retroactively enable all the features that were previously unavailable. They expect `fix` to detect the plan change automatically and apply branch protection, tag rulesets, secret scanning, and Dependabot without any config changes.

Friction: if the tool caches plan info or if Taylor doesn't realize they need to re-run `fix` (not just `create`) on existing repos.

### 7. "I'm seeding a new repo from an existing project" (Novice)

Lee wants to fork an open-source project to contribute patches. They use `gh-safe-repo create lee/my-fork --from upstream-org/cool-project` to create a repo seeded from the upstream. They expect the tool to clone the code, push it to the new repo, and apply all security defaults in one shot.

Friction: Lee may not understand that `--from` creates a fresh repo (not a GitHub fork), so they lose the "forked from" link and can't easily open PRs upstream. The distinction needs to be clear in help text or warnings.

### 8. "I want to scan but I don't have truffleHog" (Novice)

Casey has been told by a senior dev to "scan for secrets before pushing." They run `gh-safe-repo scan` in their project directory. They don't have truffleHog installed and don't use Docker. The tool should fall back to its built-in regex scanner gracefully, find a hardcoded Stripe test key in a config file, and tell Casey exactly which file and line to fix.

Friction: if the output says "truffleHog not found, falling back to regex" without reassuring Casey that the regex scanner is still useful, they might think the scan was incomplete and give up.

### 9. "Push my local project to a new private repo in one command" (Medium)

Rin has a local git repo with a few commits and wants to publish it as a private GitHub repo with all security settings applied. They run `gh-safe-repo create rin/new-project --local .`. The tool should create the repo (without auto-init to avoid push conflicts), push the local commits, then apply branch protection and other settings.

Friction: if their local branch is named `main` but the tool assumes `master` (or vice versa), or if they have uncommitted changes that the pre-flight scan flags.

### 10. "Pin Actions to SHAs and restrict to first-party only" (Expert)

Dana has been bitten by a supply-chain attack via a compromised third-party Action. They configure `[actions]` with `allowed_actions = selected`, `github_owned_allowed = true`, `verified_allowed = false`, and expect the tool to lock down Actions permissions. They also want existing workflows to break loudly if they reference unpinned tags.

Friction: the tool sets the API-level policy but doesn't lint workflow YAML files for unpinned refs — Dana might expect `scan` to catch `uses: some-org/action@v1` (tag, not SHA) and flag it. Gap between API-level enforcement and file-level validation.

### 11. "I only care about security, don't touch anything else" (Medium)

A developer inherits a team's repo and wants to enable secret scanning, push protection, Dependabot alerts, and branch protection — but explicitly does not want the tool touching wiki settings, merge strategies, or any "preference" options. They only care about the security posture.

Friction: does omitting a config section mean "don't touch" or "apply safe defaults"? If the tool applies defaults for omitted sections, it silently changes their merge strategy — the opposite of what they wanted. They need a clear mental model for what absence means.

### 12. "I just leaked a secret and I'm panicking" (Novice)

Gets a panicked email from GitHub: a secret was detected in their public repo. Installs `gh-safe-repo`, runs `scan` to find anything else that might be exposed, then runs `fix` to turn on push protection so it can't happen again. Wants to move fast and not think about configuration.

Friction: `scan` checks the working tree but not git history — the user may get false confidence the incident is resolved. Enabling push protection now doesn't retroactively remove the secret from history. The tool needs to guide toward credential rotation and history scrubbing, not just future prevention.

### 13. "What exactly did fix change? My collaborator is asking" (Medium)

Runs `fix` on a shared repo, then gets asked "what did you change and why?" Needs a clear before/after record they can paste into Slack or a PR comment.

Friction: they have to scroll through terminal output and reconstruct it manually. A `--dry-run` after the fact shows everything compliant — the evidence is gone. They wish `fix` had a structured summary or report they could save.

### 14. "I want to run fix weekly in GitHub Actions" (Expert)

Sets up a scheduled CI workflow running `fix --yes` across all their repos. Needs no interactive prompts, predictable exit codes, and clean logs.

Friction: token scope requirements are trial-and-error, output is human-oriented (colors, prompts), and partial success (some settings applied, one API call failed) has ambiguous exit codes. Needs machine-parseable output and clear exit code semantics.

### 15. "This five-year-old repo has zero protections" (Novice)

Takes over maintenance of an ancient repo with no branch protection, no secret scanning, Actions set to allow all. Runs `fix` hoping for one-shot modernization.

Friction: the wall of changes is overwhelming. Actions lockdown to first-party only may break existing workflows that use popular third-party actions. Everything is presented as one atomic "accept all or nothing" prompt. The user needs guidance on staged rollout but the tool doesn't offer it.

### 16. "I disagree with some of the tool's defaults" (Medium)

Likes most defaults but wants force-push allowed (rebase workflow), unrestricted Actions (community actions), and wiki enabled. Wants to override just these three without redeclaring everything.

Friction: unclear whether config is "merge with defaults" or "replace section." Do they need to duplicate all the defaults they agree with just to change one value? And will future tool updates silently re-enable settings they deliberately turned off?

### 17. "I need to temporarily relax settings for a migration" (Expert)

Needs to do a large-scale migration — rewriting git history with `git filter-repo`, force-pushing to main. Branch protection blocks this. Wants to snapshot current state, relax it, migrate, then restore.

Friction: the tool is declarative with no snapshot/restore concept. Manual customizations beyond what config.ini tracks (specific status check contexts, bypass lists) would get lost on restore. In practice they end up toggling settings in the GitHub UI and hoping they remember to turn everything back on.

### 18. "Setting up 30 student repos for a class" (Medium)

CS instructor scripting `create` in a loop with a shared config. Needs branch protection on main (prevent force-push over grading branches), Actions disabled, and secret scanning on because students routinely commit API keys.

Friction: student repos are private on free plan — branch protection and secret scanning unavailable. The instructor either needs GitHub Education benefits (org-level, which this tool doesn't target) or must make repos public (unacceptable for plagiarism reasons). Seeing 30x "SKIP: not available on free plan" is demoralizing. Needs upfront guidance before creating anything.

### 19. "Fix just overwrote my carefully crafted branch protection" (Medium)

Had nuanced branch protection configured manually in the GitHub UI — specific teams as required reviewers, particular status checks, deploy bot bypass list. Ran `fix` just to enable Dependabot, but it flattened their branch protection to the tool's generic defaults.

Friction: `fix` is declarative, not additive. The user expected "turn on things that are off" but got "make everything match this config." Their bespoke setup is gone. They wish the tool had a mode that only adds missing protections without touching settings that are already more restrictive, or at minimum had shown a clear diff before applying.

### 20. "Fix fails on my archived repo with a wall of errors" (Expert)

Runs `fix` against an archived repo — every API call to change settings fails because GitHub rejects writes. Also relevant for transferred repos where visibility or plan eligibility changed since the last run.

Friction: no upfront archived-repo detection, so they get a cascade of API errors instead of one clear message like "this repo is archived; unarchive it first." For transferred repos, features that "used to work" are now skipped, and the user needs the tool to explain the situation rather than failing halfway through.

---

## Design Analysis

### Three levels of user intent

Working through the stories above reveals three distinct levels of signal about what the user wants, each with different implications for how the tool should behave:

1. **`create` with no config** — the user has no preconceptions. The tool has maximum freedom to apply safe defaults. This is the moment we have the freest choice, and since it also communicates the tool's scope and values, we might play it cautiously — but it's also where opinionated defaults add the most value.

2. **Any command with a .ini config** — the user has exercised explicit intent. Every section they declared is a deliberate choice. Sections they omitted are silence, not consent.

3. **`fix` on an existing repo** — the repo's current state is itself a signal about intent. If a setting has been changed from GitHub's defaults, someone (the user, a collaborator, a previous tool run) made a deliberate choice. That divergence is evidence of intent that the tool should respect.

### The three-way comparison

Today, plugins do a two-way comparison: `desired` (from config) vs `current` (from API). But each plugin also knows `GITHUB_DEFAULTS` — what a brand-new repo would look like. This enables a **three-way comparison** that detects deliberate user choices:

| current == GH default? | desired == current? | Meaning | fix should... |
|---|---|---|---|
| Yes | Yes | Untouched, already at desired | Skip (no-op) |
| Yes | No | Untouched, we want to change it | **Apply** — no one cared enough to set it |
| No | Yes | Someone changed it to what we want | Skip (no-op) |
| No | No | **Someone deliberately changed it** | **Depends on tier and direction** |

The fourth case is the critical signal. The setting has diverged from GitHub's default AND it differs from our desired value. Someone made a choice.

### Setting tiers: security vs preference

Not all settings carry equal weight. The tool's defaults mix two concerns:

- **Security settings** — protect the repo from real threats (secret scanning, branch protection rules, Actions lockdown). These have a compliance dimension: weakening them is risky.
- **Preference settings** — reflect workflow and aesthetic choices (wiki, merge strategy, projects tab, conversation resolution). Reasonable people disagree on these.

Classification by section:

- **`[repo]`** — all preferences (visibility, wiki, merge strategy are choices)
- **`[actions]`** — enforcement settings (`allowed_actions`, `sha_pinning_required`, `default_workflow_permissions`, `can_approve_pull_request_reviews`, `fork_pr_approval_policy`) are security; `enabled` is preference
- **`[branch_protection]`** — structural protection (`require_pull_request`, `required_approving_reviews`, `dismiss_stale_reviews`, `allow_force_pushes`, `allow_deletions`) is security; policy knobs (`require_conversation_resolution`, `enforce_admins`, `use_rulesets`) are preference
- **`[tag_protection]`** — all security
- **`[security]`** — all security
- **`[pre_flight_scan]`** — all preference (scan tuning, not repo state)

### How tier interacts with divergence

When fix encounters the fourth case (deliberate divergence that conflicts with config), the combination of tier and direction determines the right action:

- **Preference + diverged**: Leave it alone. Someone chose this. This is the core fix for stories #4, #11, #16, #19.
- **Security + diverged toward weaker**: Flag it prominently — someone weakened protection. Apply the safer value. This is what stories #12 and #15 need.
- **Security + diverged toward stricter**: Leave it alone — they made it MORE restrictive than our default. Don't weaken their choice. This addresses story #19 for security settings too (e.g., user set `required_approving_reviews=3`, our config says `1` — don't downgrade).

### Directional comparison for security settings

"Stricter" needs to be defined per setting:

- **Booleans**: `require_pull_request=true` is stricter than `false`. `allow_force_pushes=false` is stricter than `true`. Each boolean has a known "safe" direction.
- **Numeric**: `required_approving_reviews` — higher is stricter.
- **Enum-like**: `allowed_actions` — `"selected"` is stricter than `"all"`. `default_workflow_permissions` — `"read"` is stricter than `"write"`.

### Missing user story: copied upstream repo

The existing stories don't cover a common scenario: a user copies someone else's public repo via `create --from upstream-org/project`, then later runs `fix` on it. The upstream project may have deliberate preference settings (specific merge strategy, wiki enabled for documentation, etc.) that the user hasn't thought about and shouldn't silently overwrite.

The `--from` command already supports this workflow (source can be any public repo, destination must be owned by the authenticated user), but fix currently treats the copied repo's settings as if the user chose them — which they didn't. The three-way comparison handles this correctly: settings that match GitHub defaults were never touched by anyone; settings that diverge were set by the upstream project and should be respected as deliberate choices.

**Supported `--from` matrix** (for reference):

| Source | Destination | Supported? |
|--------|------------|------------|
| Other user's public repo | Your private repo | Yes |
| Other user's public repo | Your public repo | Yes |
| Your own repo (any visibility) | Your repo (any visibility) | Yes |
| Other user's private repo | Any | No (API 403) |

### Control model options

Three approaches were evaluated against all 20 stories:

**Model A: Implicit default + override flag** — `fix` defaults to security-only, `fix --all` applies full config. Strongest for security-focused users (#4, #11, #12, #15) and the `--from` + `fix` scenario. Ambiguity about what `--all` means when combined with `--config`.

**Model B: Explicit scope flag** — `fix --security` (default) or `fix --full`. Most readable in CI scripts (#14). Same strengths as A with slightly more ceremony.

**Model C: Config-section driven** — No new flags; fix applies security always + any config sections the user declared. Most natural for config-heavy users (#2, #5, #16). Has a "config-file trap": if a user has a full config.ini from initial setup, fix silently enforces all preferences even when they only wanted security hygiene.

**Key finding**: The three-way comparison (divergence detection) largely neutralizes Model C's trap. Even with a full config, fix won't overwrite settings that someone deliberately changed from GitHub defaults. This means the models converge: the divergence check provides a safety net regardless of which control model is chosen, and the flag/config question becomes about user ergonomics rather than correctness.

### What `create` vs `fix` should do (summary)

| Scenario | Security settings | Preference settings |
|----------|------------------|-------------------|
| `create` (any config) | Apply all | Apply all (user has no preconceptions) |
| `fix` — setting at GH default | Apply | Apply (no one cared) |
| `fix` — setting diverged from GH default | Apply if stricter or equally strict; flag + apply if weaker | Leave alone (respect deliberate choice) |
| `fix` — user config declares section | Apply | Apply per config (explicit intent) |

### Stories addressed by the three-way comparison + tiers

| Story | How it's addressed |
|-------|-------------------|
| #4 (weekly hygiene) | fix doesn't overwrite diverged preferences |
| #11 (security only) | fix defaults to security; preferences only applied if at GH default or user declared in config |
| #12 (leaked secret) | Security settings always enforced; weaker-than-default flagged prominently |
| #15 (old repo, wall of changes) | Diverged preferences are respected, reducing the wall to security-only changes |
| #16 (disagree with defaults) | Deliberately-changed settings are detected and respected without needing config |
| #19 (custom BP overwritten) | Hand-tuned BP detected via divergence; preference-tier BP settings left alone; security-tier BP not downgraded below user's stricter setting |
| NEW (--from + fix) | Upstream's deliberate settings detected as diverged, respected |

---

## Architectural Vision

### The core metaphor: three-way merge

Git's three-way merge resolves conflicts between two authors by comparing both against a common ancestor. gh-safe-repo's `fix` command faces the same problem: the tool wants one thing, a human chose another, and the question is whose intent wins.

The three inputs:

- **Base** — GitHub's defaults for a newly-created repo. Every plugin already has this as its `GITHUB_DEFAULTS` dict. This is the common ancestor.
- **Theirs** — the repo's current state, fetched from the GitHub API. This represents what humans (or previous tool runs) have done. Any setting that differs from the base was a deliberate act.
- **Ours** — the tool's desired state, from `SAFE_DEFAULTS` overlaid with the user's config.ini. This is what we want the repo to look like.

The merge:

| Base → Theirs | Base → Ours | Analogy | Action |
|---|---|---|---|
| Unchanged | Unchanged | No diff | Nothing to do |
| Unchanged | Changed | Only we changed it | **Apply** — no one cared about this setting |
| Changed | Unchanged | Only they changed it | **Preserve** — someone made a choice, we have no opinion |
| Changed | Changed (same) | Both agree | Nothing to do |
| Changed | Changed (different) | **Conflict** | Resolve by tier + direction |

The first four cases are mechanical. The fifth — both sides changed the setting to different values — is where the tool's judgment matters. This is the only case that needs policy.

### Conflict resolution policy

Conflicts are resolved using two pieces of metadata about each setting:

**Tier** — is this a security setting or a preference?

**Direction** — for security settings, is the human's choice stricter or weaker than ours?

| Tier | Direction | Resolution | Rationale |
|---|---|---|---|
| Preference | (any) | **Preserve theirs** | Reasonable people disagree. Respect the human's choice. |
| Security | Theirs is stricter | **Preserve theirs** | They chose MORE protection than we'd apply. Don't weaken it. |
| Security | Theirs is weaker | **Apply ours + warn** | Someone weakened a security control. This is what fix is for. |

This resolves the core design tension: `fix` enforces security without steamrolling preferences, and it never downgrades a stricter-than-default security posture.

### How `create` differs

`create` has no "theirs" — the repo doesn't exist yet. There is no three-way merge; it's a straight application of "ours" (safe defaults + config). Every setting gets applied. This is the moment of maximum freedom and maximum opinionation.

### The config file as an override signal

The three-way merge is the default policy. A user's config.ini can override it.

When a user explicitly declares a section in their config file (e.g., writes `[repo]` with `has_wiki = true`), they are saying: "I want this enforced, even if someone deliberately changed it." The config section is an intent signal that overrides divergence-respect for preference settings in that section.

This gives us three tiers of user control:

1. **No config** — fix uses the three-way merge. Security enforced, preferences respected.
2. **Config with specific sections** — those sections override divergence-respect. "I declared `[repo]`, so enforce my repo preferences even on repos where someone changed them."
3. **`--all` flag** — override everything, ignore divergence entirely. The escape hatch for full-template enforcement (story #2: batch alignment, story #15: one-shot modernization).

### What the plan output should show

Today the plan has four states: ADD, UPDATE, DELETE, SKIP. The three-way merge introduces a fifth: the tool looked at a setting, found a conflict, and chose to preserve the human's value. This is not a SKIP (the tool *could* change it) and not a no-op (there IS a difference). It's a deliberate policy decision that the user should see.

Proposed plan actions for `fix`:

| Action | Meaning | When |
|---|---|---|
| **UPDATE** | Changing a setting | Security enforcement, or preference no one touched |
| **PRESERVE** | Respecting an existing choice | Preference someone changed, or security someone made stricter |
| **SKIP** | Cannot change | GitHub plan limitation, or feature unavailable |
| *(hidden)* | Already at desired value | No diff — not shown (reduces noise, addresses story #4) |

The PRESERVE action is the key UX addition. It tells the user: "I see this is different from my desired value, and I'm leaving it alone because someone chose this." It answers story #13 ("what did fix change, and what did it leave?") and makes the tool's reasoning transparent.

For the UPDATE action on security settings that were weakened, an additional warning indicator (e.g., `⚠ weakened from default`) flags that someone actively relaxed a security control.

Example `fix` output:

```
  Auditing owner/my-repo...

  Category            Action    Setting                          Value
  ──────────────────────────────────────────────────────────────────────────
  Repository          PRESERVE  has_wiki                         true (changed from default)
  Repository          PRESERVE  allow_rebase_merge               false (changed from default)
  Actions             UPDATE    allowed_actions                  all → selected
  Actions             UPDATE    default_workflow_permissions      write → read
  Branch Protection   UPDATE    allow_force_pushes               true → false  ⚠ weakened
  Branch Protection   PRESERVE  required_approving_reviews       3 (stricter than 1)
  Security            UPDATE    dependabot_alerts                disabled → enabled
  Security            SKIP      secret_scanning                  Requires paid plan

  3 update(s), 2 preserved, 1 skipped.
```

The user sees exactly what's changing, what's being respected, and why. No wall of "Already at desired value" lines for settings that match.

### Architectural changes (structural, not implementation)

The changes are layered so each concern stays in its natural home:

**1. Setting metadata lives in `config_manager.py`**

Each setting gets two intrinsic properties: its **tier** (security/preference) and its **safe direction** (for security settings, which value is "stricter"). These are properties of the settings themselves, not of any particular repo or run. They live alongside `SAFE_DEFAULTS` as parallel data structures.

```
SAFE_DEFAULTS     →  what we want each setting to be
SETTING_TIERS     →  security or preference
SAFE_DIRECTIONS   →  for security settings, which direction is "stricter"
```

`ConfigManager` also gains awareness of which sections the user explicitly declared in their config file, so the policy layer can distinguish "user said this" from "fell through to defaults."

**2. Plugins remain mechanical diff generators**

Plugins don't change their role. They still fetch current state, compare against desired, and produce Changes. But they annotate each Change with its tier and the GitHub default value, so the policy layer has the context it needs. The plugin knows its section and its `GITHUB_DEFAULTS` — it's the natural place to attach this metadata.

Plugins do NOT make policy decisions. They report what differs. This keeps them testable and single-purpose.

**3. The policy layer lives in `fix.py`**

A new function (call it `resolve_plan` or `apply_merge_policy`) takes the raw plan from the plugins and applies the three-way merge logic. For each Change:

- Look up the GitHub default for that setting
- Determine if the current value diverged from the default
- If no divergence: pass through (apply)
- If diverged: resolve the conflict using tier + direction + config-section intent
- Transform the Change type as needed (UPDATE → PRESERVE, or UPDATE + warning)

This function is the only place that understands the merge policy. It's easy to test in isolation: give it a plan with known changes, assert the output.

`create.py` does not use this function. It applies the raw plan directly.

**4. The `Change` dataclass carries merge context**

`Change` gets two new optional fields:

- `tier` — `"security"` or `"preference"` (set by plugins)
- `github_default` — the value a fresh repo would have (set by plugins, used by policy layer)

The existing `old` field already carries the current value. Combined with `github_default`, the policy layer can compute divergence without re-fetching anything.

A new `ChangeType.PRESERVE` is added for the plan output.

**5. Plan display adapts to the richer model**

`print_plan` handles the new PRESERVE type. Settings already at their desired value are omitted from fix output (they currently show as SKIP with "Already at desired value" — removing them reduces noise). The JSON output (`format_plan_json`) includes tier and the merge reasoning for machine consumers.

### What this does NOT change

- **`create` behavior** — unchanged. Full template application, no merge logic.
- **`scan` behavior** — unchanged. Scans are independent of settings tiers.
- **Plugin architecture** — plugins still follow fetch → diff → apply. They gain annotations but not logic.
- **Config file format** — no new syntax. Existing config.ini files work as before.
- **CLI surface** — one new optional flag (`--all` on `fix`). Everything else stays the same.
- **Apply logic** — `plugin.apply()` methods are unchanged. They operate on `plan.actionable_changes`, which the policy layer has already filtered. PRESERVE changes are not actionable.

### How each user story maps to this architecture

| Story | Mechanism |
|-------|-----------|
| #1 Novice create | `create` applies full defaults. Unchanged. |
| #2 Batch fix across repos | `fix --all` for full alignment. Or config with declared sections. |
| #3 Squash-merge preferences | Config `[repo]` section. Works today, unchanged. |
| #4 Weekly hygiene | Three-way merge: preferences diverged from default → PRESERVE. Security drift → UPDATE. |
| #5 Two configs | `--config` selects the file. Declared sections override divergence-respect. |
| #6 Upgraded to Pro | Security settings always enforced. Previously-SKIPped features now apply. Unchanged. |
| #7 --from seed | `create` applies full defaults. Unchanged. |
| #8 No truffleHog | `scan` fallback. Unchanged. |
| #9 --local push | `create` applies full defaults. Unchanged. |
| #10 Actions SHA pinning | Actions security settings always enforced by fix. |
| #11 Security only, don't touch rest | Default fix: security enforced, preferences diverged → PRESERVE. Exact match. |
| #12 Leaked secret panic | Security always enforced. Weakened-from-default gets `⚠` flag. |
| #13 What did fix change? | PRESERVE entries explain what was left alone and why. UPDATE entries show changes. |
| #14 CI scheduled fix | `fix --yes` applies security. Predictable. `--all --yes` for full enforcement. |
| #15 Old repo, wall of changes | Diverged preferences → PRESERVE, not UPDATE. Wall is only security changes. |
| #16 Disagree with defaults | Deliberately-changed settings detected as diverged → PRESERVE. No config needed. |
| #17 Temporary relaxation | Out of scope. Snapshot/restore is a different feature. |
| #18 Student repos | `create` in a loop. Plan limitations shown. Unchanged. |
| #19 Custom BP overwritten | BP preferences (enforce_admins, conversation_resolution) → PRESERVE if diverged. BP security (required_reviews at 3 vs our 1) → PRESERVE because stricter. Only weaker security settings get fixed. |
| #20 Archived repo | Pre-flight detection. Orthogonal to this design. |
| NEW --from then fix | Upstream's settings diverge from GH defaults → PRESERVE for preferences, directional merge for security. |

### Remaining design questions

1. **Safe direction definitions**: Should the "stricter" direction for each security setting live in a data structure in `config_manager.py` (alongside tiers), or should it be computed per-plugin? A data structure is more testable and auditable; per-plugin is more flexible for complex cases.

2. **PRESERVE verbosity**: Should PRESERVE entries show by default, or only with `--verbose`? Showing them is more transparent (story #13) but adds lines. Hiding them is cleaner for story #4 (concise when nothing changed). Could default to showing them and let `--quiet` suppress them.

3. **`--all` semantics**: Should `--all` mean "apply everything in config regardless of divergence" or "apply all safe defaults regardless of config"? The former respects the config; the latter is a full factory-reset to safe defaults. The former seems more useful and less dangerous.

4. **Config section + divergence interaction**: When a user declares `[branch_protection]` in their config and the repo has `required_approving_reviews=3` (diverged, stricter), should the config override it back to 1? Or should "diverged stricter" always win, even over explicit config? Leaning toward: explicit config wins — the user said what they want. The three-way merge is a default, not an absolute.
