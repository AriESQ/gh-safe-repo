"""
Actions in use (#111): what an Actions-policy change would block.

Before `fix` / `push` / `copy` change `allowed_actions`, `patterns_allowed`,
`verified_allowed`, `github_owned_allowed` or `sha_pinning_required`, this
module reads the repo's workflows on one branch, collects every `uses:`
reference, and evaluates each one against the policy before and after the
change. The result is a list of `PlanWarning`s attached to the plan. It never
changes what is applied: the plan rows are left exactly as the plugin and the
merge policy produced them (replace-and-warn).

Policy semantics (GitHub docs, "Allowing select actions and reusable
workflows to run"):

- `all`: every action runs.
- `selected`: local actions (`./`, `$/`), actions owned by the repo owner,
  GitHub-owned actions (`actions/*`, `github/*`) when `github_owned_allowed`,
  Marketplace actions by verified creators when `verified_allowed`, and
  anything matched by `patterns_allowed`. A `!pattern` blacklists.
- `local_only`: local actions and actions owned by the repo owner only.
- `sha_pinning_required`: every action (GitHub-owned included) must be
  pinned to a full 40-hex commit SHA. Reusable workflows may still use tags.

Verified-creator status has no REST endpoint, so an action that is only
allowed if its creator is verified is reported as "unknown", never as
blocked. `docker://` references run only under `all`: GitHub blocks them
under `selected` (even with verified creators allowed) and `local_only`.
Whitelist patterns are matched against `owner/repo[/path]@ref`, so a
pattern needs an `@` part (`astral-sh/setup-uv@*`) to match any ref.
Verified live on 2026-10-26; see docs/2026-10-26_actions-whitelist-probe.md.

Parsing is line-based (no YAML dependency): a `uses:` key, optionally after
a list dash, with quotes and trailing comments stripped. Expressions
(`${{ ... }}`) are skipped. Local composite actions (`./path` with an
`action.yml`) are followed so their own `uses:` lines count too.
"""

import base64
import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional
from urllib.parse import quote

from .diff import ChangeCategory, PlanWarning
from .errors import APIError
from .plugins.actions import GITHUB_DEFAULTS, _parse_bool, _parse_patterns

# Plan rows (ActionsPlugin keys) that change which actions may run.
POLICY_KEYS = (
    "allowed_actions",
    "github_owned_allowed",
    "verified_allowed",
    "patterns_allowed",
    "sha_pinning_required",
)

GITHUB_OWNERS = frozenset({"actions", "github"})
WORKFLOW_DIR = ".github/workflows"
MAX_PIN_LOOKUPS = 25        # API calls spent on remediation SHAs, at most
MAX_LOCAL_DEPTH = 3         # local composite actions followed this deep

_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_USES_RE = re.compile(r"^\s*(?:-\s+)?uses\s*:\s*(.*?)\s*$")

_UNPINNED = "not pinned to a full-length commit SHA"

# Statuses
ALLOWED = "allowed"
BLOCKED = "blocked"
UNKNOWN = "unknown"


# ── parsing ─────────────────────────────────────────────────────────────


def parse_uses(text: str) -> List[tuple]:
    """Return [(line_number, ref)] for every `uses:` in a workflow/action file."""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        m = _USES_RE.match(line)
        if not m:
            continue
        value = m.group(1)
        if value[:1] in ("'", '"'):
            q = value[0]
            end = value.find(q, 1)
            value = value[1:end] if end > 0 else value[1:]
        else:
            value = value.split(" #", 1)[0].strip()
        if not value or "${{" in value:
            continue
        out.append((n, value))
    return out


@dataclass
class ActionRef:
    """One `uses:` reference, classified."""
    uses: str
    kind: str                     # "action" | "reusable_workflow" | "local" | "docker" | "invalid"
    owner: str = ""
    repo: str = ""
    name: str = ""                # owner/repo[/path], without the @ref
    ref: str = ""

    @property
    def pinned(self) -> bool:
        return bool(_SHA_RE.match(self.ref))


def classify(uses: str) -> ActionRef:
    if uses.startswith("./") or uses.startswith("$/"):
        return ActionRef(uses, "local")
    if uses.startswith("docker://"):
        return ActionRef(uses, "docker")
    name, sep, ref = uses.partition("@")
    parts = name.split("/")
    if not sep or not ref or len(parts) < 2 or not parts[0] or not parts[1]:
        return ActionRef(uses, "invalid")
    path = "/".join(parts[2:])
    kind = (
        "reusable_workflow"
        if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml"))
        else "action"
    )
    return ActionRef(uses, kind, owner=parts[0], repo=parts[1], name=name, ref=ref)


# ── policy ──────────────────────────────────────────────────────────────


def _glob_re(pattern: str):
    # `*` matches any run of characters, `/` included: the docs' examples
    # (`octocat/*` allows reusable workflows under .github/workflows/,
    # `*/octocat**@*`) only hold if it does.
    return re.compile(
        "".join(".*" if ch == "*" else re.escape(ch) for ch in pattern),
        re.IGNORECASE,
    )


def pattern_matches(pattern: str, ref: ActionRef) -> bool:
    """Does one whitelist pattern (without a leading `!`) match `ref`?

    GitHub matches every pattern against the full `name@ref`, so a pattern
    without `@` (e.g. `astral-sh/setup-uv`) matches no ref at all: GitHub
    blocked `astral-sh/setup-uv@v5` under exactly that whitelist (verified
    live). `gradle/*` does match `gradle/actions/setup-gradle@v4`.
    """
    pattern = pattern.strip()
    if not pattern:
        return False
    return bool(_glob_re(pattern).fullmatch(f"{ref.name}@{ref.ref}"))


def normalise_policy(state: dict) -> dict:
    """The five policy keys from a plugin state dict, typed, defaults filled."""
    def get(k):
        v = state.get(k)
        return GITHUB_DEFAULTS[k] if v is None else v
    return {
        "allowed_actions": get("allowed_actions"),
        "github_owned_allowed": _parse_bool(get("github_owned_allowed")),
        "verified_allowed": _parse_bool(get("verified_allowed")),
        "patterns_allowed": _parse_patterns(get("patterns_allowed")),
        "sha_pinning_required": _parse_bool(get("sha_pinning_required")),
    }


def evaluate(ref: ActionRef, policy: dict, repo_owner: str):
    """Return (status, [reasons]) for `ref` under `policy`."""
    if ref.kind == "local":
        return ALLOWED, []
    if ref.kind == "invalid":
        return ALLOWED, []          # not ours to judge; GitHub rejects the workflow anyway
    allowed = policy["allowed_actions"]
    if ref.kind == "docker":
        if allowed == "all":
            return ALLOWED, []
        return BLOCKED, ["docker:// images run only when allowed_actions = all"]

    status, reasons = ALLOWED, []
    if allowed != "all":
        patterns = policy["patterns_allowed"] if allowed == "selected" else []
        blocks = [p[1:].strip() for p in patterns if p.startswith("!")]
        allows = [p for p in patterns if not p.startswith("!")]
        own = ref.owner.lower() == (repo_owner or "").lower()
        if any(pattern_matches(p, ref) for p in blocks):
            status = BLOCKED
            reasons.append("blacklisted by a ! pattern in patterns_allowed")
        elif own:
            pass
        elif allowed == "local_only":
            status = BLOCKED
            reasons.append("allowed_actions = local_only")
        elif policy["github_owned_allowed"] and ref.owner.lower() in GITHUB_OWNERS:
            pass
        elif any(pattern_matches(p, ref) for p in allows):
            pass
        elif policy["verified_allowed"]:
            status = UNKNOWN
            reasons.append("allowed only if its creator is verified (not checkable via the API)")
        else:
            status = BLOCKED
            reasons.append(
                "not GitHub-owned or in patterns_allowed"
                if policy["github_owned_allowed"]
                else "not in patterns_allowed"
            )

    if policy["sha_pinning_required"] and ref.kind == "action" and not ref.pinned:
        status = BLOCKED
        reasons.append(_UNPINNED)
    return status, reasons


def policy_after(baseline: dict, plan) -> Optional[dict]:
    """The policy once `plan`'s actionable Actions rows are applied.

    None when no actionable row touches a policy key (nothing to scan).
    """
    after = dict(baseline)
    changed = False
    for c in plan.actionable_changes:
        if c.category == ChangeCategory.ACTIONS and c.key in POLICY_KEYS:
            after[c.key] = c.new
            changed = True
    return normalise_policy(after) if changed else None


# ── workflow sources ────────────────────────────────────────────────────


class ApiSource:
    """Read files on one branch through the contents API (read-only)."""

    def __init__(self, client, owner, repo, branch):
        self.client, self.owner, self.repo, self.branch = client, owner, repo, branch
        self.label = f"{owner}/{repo}"

    def _path(self, path):
        q = f"?ref={quote(self.branch, safe='')}" if self.branch else ""
        return self.client.repo_path(self.owner, self.repo, f"contents/{quote(path)}") + q

    def list_dir(self, path) -> List[str]:
        try:
            entries = self.client.get_json(self._path(path))
        except APIError as e:
            if e.status_code == 404:
                return []
            raise
        if not isinstance(entries, list):
            return []
        return [e.get("path") for e in entries if e.get("type") == "file" and e.get("path")]

    def read(self, path) -> Optional[str]:
        try:
            data = self.client.get_json(self._path(path))
        except APIError as e:
            if e.status_code == 404:
                return None
            raise
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            return None
        try:
            return base64.b64decode(data.get("content") or "").decode("utf-8", "replace")
        except (ValueError, TypeError):
            return None


class GitSource:
    """Read files on one branch of a local git repo (`push`: no API calls)."""

    def __init__(self, local_path, branch):
        self.local_path, self.branch = local_path, branch or "HEAD"
        self.label = local_path

    def _git(self, *args) -> Optional[str]:
        try:
            r = subprocess.run(["git", "-C", self.local_path, *args],
                               capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return r.stdout if r.returncode == 0 else None

    def list_dir(self, path) -> List[str]:
        out = self._git("ls-tree", "--name-only", self.branch, path.rstrip("/") + "/")
        return [line for line in (out or "").splitlines() if line]

    def read(self, path) -> Optional[str]:
        return self._git("show", f"{self.branch}:{path}")


@dataclass
class Usage:
    ref: ActionRef
    workflow: str        # the workflow file that (transitively) uses it
    line: int
    file: str = ""       # file holding the `uses:` line (a local action.yml when nested)

    def __post_init__(self):
        self.file = self.file or self.workflow


def collect_usages(source) -> tuple:
    """Return (usages, workflow_count) for every workflow on the source branch."""
    files = [p for p in source.list_dir(WORKFLOW_DIR) if p.endswith((".yml", ".yaml"))]
    usages: List[Usage] = []
    local_files: Dict[str, Optional[tuple]] = {}   # dir -> (path, text) | None

    def local_action(d):
        if d not in local_files:
            local_files[d] = None
            for name in ("action.yml", "action.yaml"):
                text = source.read(f"{d}/{name}")
                if text is not None:
                    local_files[d] = (f"{d}/{name}", text)
                    break
        return local_files[d]

    def walk(text, workflow, file, chain):
        for line, uses in parse_uses(text):
            ref = classify(uses)
            usages.append(Usage(ref, workflow, line, file))
            if ref.kind != "local" or not uses.startswith("./"):
                continue
            d = uses[2:].rstrip("/")
            if d in chain or len(chain) >= MAX_LOCAL_DEPTH:
                continue
            found = local_action(d)
            if found:
                walk(found[1], workflow, found[0], chain | {d})

    for path in sorted(files):
        text = source.read(path)
        if text is not None:
            walk(text, path, path, frozenset())
    return usages, len(files)


# ── warnings ────────────────────────────────────────────────────────────


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def whitelist_warnings(plan) -> List[PlanWarning]:
    """`⚠ removes whitelisted action X` for each pattern an UPDATE drops."""
    out = []
    for c in plan.actionable_changes:
        if c.category == ChangeCategory.ACTIONS and c.key == "patterns_allowed":
            dropped = sorted(set(_parse_patterns(c.old)) - set(_parse_patterns(c.new)))
            for p in dropped:
                out.append(PlanWarning(
                    kind="whitelist_pattern_removed",
                    message=f"removes whitelisted action {p}",
                    details={"pattern": p},
                ))
    return out


def resolve_pin(client, ref: ActionRef) -> Optional[str]:
    """Commit SHA `ref.ref` resolves to now, or None (one API call)."""
    try:
        data = client.get_json(
            client.repo_path(ref.owner, ref.repo, f"commits/{quote(ref.ref, safe='')}")
        )
    except APIError:
        return None
    sha = data.get("sha") if isinstance(data, dict) else None
    return sha if sha and _SHA_RE.match(sha) else None


def usage_warnings(usages, workflow_count, before, after, repo_owner, *,
                   branch=None, source=None, client=None) -> List[PlanWarning]:
    """Warnings for refs the change newly blocks or newly makes uncertain.

    A ref already blocked under `before` is not reported: the change does
    not break it. `client` (optional) resolves a remediation SHA for refs
    blocked by SHA pinning (`pin`), at most MAX_PIN_LOOKUPS calls.
    """
    blocked, unknown = [], []
    for u in usages:
        was, _ = evaluate(u.ref, before, repo_owner)
        now, reasons = evaluate(u.ref, after, repo_owner)
        if now == BLOCKED and was != BLOCKED:
            blocked.append((u, reasons))
        elif now == UNKNOWN and was == ALLOWED:
            unknown.append((u, reasons))

    out = []
    where = {"branch": branch, "source": source, "workflows_scanned": workflow_count}

    def item(u, reasons):
        return {"uses": u.ref.uses, "kind": u.ref.kind, "workflow": u.workflow,
                "file": u.file, "line": u.line, "reasons": reasons}

    if blocked:
        pins: Dict[str, Optional[str]] = {}
        if client is not None:
            for u, reasons in blocked:
                if (_UNPINNED in reasons
                        and u.ref.uses not in pins and len(pins) < MAX_PIN_LOOKUPS):
                    pins[u.ref.uses] = resolve_pin(client, u.ref)
        items = []
        for u, reasons in blocked:
            d = item(u, reasons)
            sha = pins.get(u.ref.uses)
            if sha:
                d["pin"] = f"{u.ref.name}@{sha}"
            items.append(d)
        n = len({u.ref.uses for u, _ in blocked})
        m = len({u.workflow for u, _ in blocked})
        out.append(PlanWarning(
            kind="actions_blocked",
            message=f"would block {_plural(n, 'action')} in {_plural(m, 'workflow')}",
            details={**where, "actions": items},
        ))
    if unknown:
        n = len({u.ref.uses for u, _ in unknown})
        m = len({u.workflow for u, _ in unknown})
        out.append(PlanWarning(
            kind="actions_unknown",
            message=f"cannot confirm {_plural(n, 'action')} in {_plural(m, 'workflow')} will still run",
            details={**where, "actions": [item(u, r) for u, r in unknown]},
        ))
    return out


def scan_skipped(reason) -> PlanWarning:
    return PlanWarning(
        kind="actions_scan_skipped",
        message=f"workflows not checked against the new Actions policy: {reason}",
        details={"reason": reason},
    )


def plan_warnings(plan, baseline, repo_owner, source=None, *, client=None,
                  skip_reason=None) -> List[PlanWarning]:
    """Every #111 warning for one plan. Never raises on a read failure.

    `baseline` is the Actions state the plan was diffed against (the repo's
    current state for `fix`, GitHub's defaults for a new repo). `source` is an
    ApiSource / GitSource for the workflows, or None (`skip_reason` says why).
    """
    warnings = whitelist_warnings(plan)
    after = policy_after(baseline, plan)
    if after is None:
        return warnings
    if source is None:
        if skip_reason:
            warnings.append(scan_skipped(skip_reason))
        return warnings
    try:
        usages, count = collect_usages(source)
    except APIError as e:
        warnings.append(scan_skipped(f"could not read workflows ({e})"))
        return warnings
    warnings += usage_warnings(
        usages, count, normalise_policy(baseline), after, repo_owner,
        branch=getattr(source, "branch", None), source=source.label, client=client,
    )
    return warnings
