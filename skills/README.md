# skills

Agent skills shipped with `gh-safe-repo`. A skill is a plain Markdown file that
teaches a coding agent (Claude Code and compatible tools) how to drive this CLI
correctly — which flags are safe unattended, what the exit codes mean, and how
to read the failures.

| Skill | What it covers |
|---|---|
| [`gh-safe-repo/SKILL.md`](gh-safe-repo/SKILL.md) | Command selection and permissions, the plan-then-apply recipe, exit codes, `--json` shapes, plan-limit `SKIP`s, common failure modes |

## Why these live here and not in `.claude/skills/`

Claude Code auto-loads skills from `.claude/skills/` in the working directory.
Keeping them out of there is intentional: the skill is for *using* the tool, not
for *developing* it, so it should not activate every time someone opens this
repo to change the source. It also keeps them visible in a normal `ls`.

## Using one

No install, from any session:

> follow `~/path/to/gh-safe-repo/skills/gh-safe-repo/SKILL.md`

Or install it once so it can be invoked as `/gh-safe-repo` from any directory:

```bash
ln -s "$PWD/skills/gh-safe-repo" ~/.claude/skills/gh-safe-repo   # symlink: tracks this checkout
cp -r skills/gh-safe-repo ~/.claude/skills/                      # copy: frozen at today's version
```

Remove with `rm -rf ~/.claude/skills/gh-safe-repo`.

## Maintenance

`SKILL.md` is the contract handed to agents *instead of* the README, and nothing
fails when it goes stale — no test covers it and no user reads it. Update it in
the same commit as any change to flags, exit codes, `--json` shapes, prompt
behaviour, `SKIP` semantics, or failure modes.
