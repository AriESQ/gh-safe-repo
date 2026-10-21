"""Post-condition verification (#77).

After an apply, re-read what GitHub actually holds and compare it against what
was asked for. The tool used to report success from the absence of exceptions;
a PATCH that GitHub accepted but ignored, or an apply that raised and was
downgraded to a warning, still produced a green "Done".

Settings are verified by re-running each plugin's own audit: the same
``fetch_current_state()`` → ``plan(current_state=...)`` pair that
``fix --dry-run`` (and ``audit``) uses. After a successful apply that audit plan must contain
only SKIP rows; any other row is a setting that did not stick.

Reads go through the REST API (never ``git ls-remote``, which over SSH can cost
a hardware-key touch), the per-process ``GET /repos`` cache is dropped before
every read, and reads use ``strict=True`` so "could not read" surfaces as
``unknown`` instead of as a false mismatch.

Public API (shared by ``fix`` and ``create``):

- ``Check`` / ``Verification`` — the result model; ``Verification.to_dict()``
  is the ``"verification"`` block of the ``--json`` document.
- ``verify_plugins(pairs, ...)`` — re-audit plugins after apply (``fix``).
- ``Probe`` — a dedicated read for a post-condition no plugin audit covers.
- ``verify(pairs, probes, ...)`` — plugin audits and probes in one set of
  retry rounds.
- ``CREATE_ONLY_KEYS`` — create-mode plan rows an audit cannot see.
  ``create_probes()`` turns them into probes; ``verify_create()`` is what
  ``create`` calls.
- ``pushed_ref_probes()`` / ``mirrored_ref_probes()`` (fatal: the code
  ``push`` / ``copy`` pushed is on GitHub, SHA for SHA, read via
  ``git/matching-refs``), ``default_branch_probe()`` (exit 3) and
  ``local_wiring_probes()`` (advisory: the user's git config points at the
  new repo) — passed to ``verify_create(extra_probes=...)``.
"""

import shlex
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, List, Optional, Sequence, Tuple

from .diff import ChangeCategory, Plan
from .errors import APIError

OK = "ok"
MISMATCH = "mismatch"
UNKNOWN = "unknown"
STATUSES = (OK, MISMATCH, UNKNOWN)

# Backoff between re-reads of settings that are wrong or unreadable. GitHub is
# eventually consistent right after a write (same budget as the PATCH retry in
# plugins/repository.py): ~7s worst case for the whole verification, because
# retries run in rounds across all pending plugins, not per plugin.
DEFAULT_DELAYS = (1, 2, 4)

# Rows that only appear in a create-mode plan (current_state=None) and that no
# audit plan ever emits. Absence from the post-apply audit says nothing about
# them, so verify_plugins() never reports them as ok; create verifies them with
# dedicated checks (README removed, description, topics, repo exists).
CREATE_ONLY_KEYS = frozenset({
    (ChangeCategory.REPO.value, "repository"),
    (ChangeCategory.REPO.value, "description"),
    (ChangeCategory.REPO.value, "topics"),
    (ChangeCategory.FILE.value, "readme"),
    (ChangeCategory.BRANCH_PROTECTION.value, "protected_branches"),
    (ChangeCategory.TAG_PROTECTION.value, "protected_tags"),
})


@dataclass
class Check:
    """One verified post-condition.

    ``fatal`` marks checks whose failure means the run did not do its job
    (code push missing under ``push``/``copy``): exit 1. Non-fatal
    mismatches are settings that ``fix`` would converge: exit 3. ``advisory``
    mismatches (the user's local repo not wired to the new remote) are
    warnings only: they never change the exit code. ``unknown`` is never a
    failure on its own.
    """
    category: str
    key: str
    status: str
    expected: Any = None
    actual: Any = None
    fatal: bool = False
    remediation: Optional[str] = None
    advisory: bool = False

    def __post_init__(self):
        if self.status not in STATUSES:
            raise ValueError(f"invalid check status {self.status!r}")

    @property
    def name(self) -> str:
        return f"{self.category}/{self.key}"

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "key": self.key,
            "status": self.status,
            "expected": self.expected,
            "actual": self.actual,
            "fatal": self.fatal,
            "advisory": self.advisory,
            "remediation": self.remediation,
        }


@dataclass
class Verification:
    checks: List[Check] = field(default_factory=list)

    def add(self, check: Check) -> None:
        self.checks.append(check)

    def extend(self, other: "Verification") -> None:
        self.checks.extend(other.checks)

    def _with(self, status):
        return [c for c in self.checks if c.status == status]

    @property
    def ok(self) -> bool:
        """True only when every check is ok (an empty verification is ok)."""
        return all(c.status == OK for c in self.checks)

    @property
    def mismatches(self) -> List[Check]:
        return self._with(MISMATCH)

    @property
    def unknowns(self) -> List[Check]:
        return self._with(UNKNOWN)

    @property
    def has_fatal(self) -> bool:
        """A fatal check that is not ok: the caller must exit 1."""
        return any(c.fatal and c.status != OK for c in self.checks)

    @property
    def has_mismatch(self) -> bool:
        """A non-advisory mismatch: a setting ``fix`` would converge (exit 3)."""
        return any(c.status == MISMATCH and not c.advisory for c in self.checks)

    @property
    def advisories(self) -> List[Check]:
        """Advisory mismatches: warnings that never change the exit code."""
        return [c for c in self.checks if c.status == MISMATCH and c.advisory]

    def summary(self) -> dict:
        counts = {s: 0 for s in STATUSES}
        for c in self.checks:
            counts[c.status] += 1
        return counts

    def exit_code(self) -> int:
        """1 for a failed fatal check, 3 for a settings mismatch, else 0.

        Unknown checks never fail the run: the caller warns instead.
        """
        if self.has_fatal:
            return 1
        if self.has_mismatch:
            return 3
        return 0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checks": [c.to_dict() for c in self.checks],
            "summary": self.summary(),
        }


def _key(change) -> Tuple[str, str]:
    return (change.category.value, change.key)


def _mismatch(change, remediation):
    return Check(
        category=change.category.value,
        key=change.key,
        status=MISMATCH,
        expected=change.new,
        actual=change.old,
        remediation=remediation,
    )



def _audit(plugin, policy=None):
    """Re-read one plugin strictly and return its post-apply audit Plan.

    ``policy`` (fix: ``apply_merge_policy``) is applied to the re-audit so a
    row the policy PRESERVEd before the apply is PRESERVEd again here and
    never counted as a mismatch.
    """
    forget = getattr(plugin.client, "forget_repo", None)
    if callable(forget):
        forget(plugin.owner, plugin.repo)
    state = plugin.fetch_current_state(strict=True)
    audit = plugin.plan(current_state=state)
    return policy(audit) if policy is not None else audit


@dataclass
class Probe:
    """A dedicated post-condition read, for things no plugin audit covers.

    ``read()`` returns the actual value, or raises when it cannot tell (any
    exception is retried, then reported ``unknown``). ``matches(expected,
    actual)`` defaults to equality. ``fatal`` applies only to a final
    ``mismatch``: a probe that cannot be read is ``unknown`` and never fails
    the run on its own. ``advisory`` marks a mismatch as a warning that never
    changes the exit code. ``retry=False`` settles after the first read
    (local git config cannot become eventually consistent, so re-reading it
    would only spend the backoff budget).

    ``read()`` may also assign ``expected`` (e.g. refs read from a ``copy``
    source); it is read back only after the last attempt.
    """
    category: str
    key: str
    expected: Any
    read: Callable[[], Any]
    matches: Optional[Callable[[Any, Any], bool]] = None
    fatal: bool = False
    remediation: Optional[str] = None
    advisory: bool = False
    retry: bool = True

    @property
    def name(self) -> str:
        return f"{self.category}/{self.key}"

    def holds(self, actual) -> bool:
        if self.matches is not None:
            return bool(self.matches(self.expected, actual))
        return actual == self.expected


def _run_rounds(tasks, sleep, delays):
    """Run zero-arg ``tasks`` in retry rounds that share one backoff budget.

    Each task returns ``(value, settled)``; a raise or ``settled=False`` is
    retried after the next delay. Returns ``(results, attempts)``: one
    ``("value", v)`` or ``("error", e)`` per task, from its last attempt.
    """
    results = {}
    todo = list(range(len(tasks)))
    schedule = [0] + list(delays)
    for round_no, delay in enumerate(schedule):
        if not todo:
            break
        if round_no:
            sleep(delay)
        still = []
        for i in todo:
            try:
                value, settled = tasks[i]()
            except Exception as e:  # noqa: BLE001 — any failed read is "unknown"
                results[i] = ("error", e)
                still.append(i)
                continue
            results[i] = ("value", value)
            if not settled:
                still.append(i)
        todo = still
    return [results[i] for i in range(len(tasks))], len(schedule)


def _plugin_task(plugin, policy=None):
    def task():
        audit = _audit(plugin, policy)
        return audit, not audit.has_changes()
    return task


def _probe_task(probe):
    def task():
        actual = probe.read()
        return actual, probe.holds(actual) or not probe.retry
    return task


def verify(
    pairs: Iterable[Tuple[Any, Plan]] = (),
    probes: Iterable[Probe] = (),
    *,
    remediation: Optional[str] = None,
    sleep: Optional[Callable[[float], None]] = None,
    delays: Sequence[float] = DEFAULT_DELAYS,
    policy: Optional[Callable[[Plan], Plan]] = None,
) -> Verification:
    """Re-audit plugins and run probes in one set of retry rounds.

    Probe checks come first, in order, then plugin checks. See
    ``verify_plugins()`` for how audits map to checks. ``remediation`` is
    attached to plugin mismatches only; probes carry their own. ``policy``
    transforms each re-audit plan before it is read (``fix`` passes its
    merge policy so PRESERVE rows are not mismatches).
    """
    sleep = sleep or time.sleep
    probes = list(probes)

    pending = []
    for plugin, applied in pairs:
        if not applied.actionable_changes:
            continue
        expected = [
            c for c in applied.actionable_changes
            if _key(c) not in CREATE_ONLY_KEYS
        ]
        pending.append((plugin, expected))

    tasks = [_probe_task(p) for p in probes] + [_plugin_task(p, policy) for p, _ in pending]
    results, attempts = _run_rounds(tasks, sleep, delays)
    probe_results, plugin_results = results[:len(probes)], results[len(probes):]

    verification = Verification()
    for probe, (kind, value) in zip(probes, probe_results):
        if kind == "error":
            verification.add(Check(
                category=probe.category, key=probe.key, status=UNKNOWN,
                expected=probe.expected, actual=None, advisory=probe.advisory,
                remediation=(
                    f"Could not read {probe.name} back after {attempts} "
                    f"attempts ({value}); check it on GitHub."
                ),
            ))
        elif probe.holds(value):
            verification.add(Check(
                category=probe.category, key=probe.key, status=OK,
                expected=probe.expected, actual=value,
            ))
        else:
            verification.add(Check(
                category=probe.category, key=probe.key, status=MISMATCH,
                expected=probe.expected, actual=value, fatal=probe.fatal,
                remediation=probe.remediation, advisory=probe.advisory,
            ))

    for (plugin, expected), (kind, value) in zip(pending, plugin_results):
        if kind == "error":
            for c in expected:
                verification.add(Check(
                    category=c.category.value,
                    key=c.key,
                    status=UNKNOWN,
                    expected=c.new,
                    actual=None,
                    remediation=(
                        f"Could not read this setting back after "
                        f"{attempts} attempts ({value}). Check it in "
                        f"https://github.com/{plugin.owner}/{plugin.repo}/settings"
                    ),
                ))
            continue

        audit = value
        wrong = {_key(c): c for c in audit.actionable_changes}
        seen = set()
        for c in expected:
            seen.add(_key(c))
            bad = wrong.get(_key(c))
            if bad is None:
                verification.add(Check(
                    category=c.category.value, key=c.key, status=OK,
                    expected=c.new, actual=c.new,
                ))
            else:
                verification.add(_mismatch(bad, remediation))
        # Rows the applied plan never touched but that are now wrong.
        for k, bad in wrong.items():
            if k not in seen:
                verification.add(_mismatch(bad, remediation))
    return verification


def verify_plugins(
    pairs: Iterable[Tuple[Any, Plan]],
    *,
    remediation: Optional[str] = None,
    sleep: Optional[Callable[[float], None]] = None,
    delays: Sequence[float] = DEFAULT_DELAYS,
    policy: Optional[Callable[[Plan], Plan]] = None,
) -> Verification:
    """Re-audit plugins after apply and turn the result into checks.

    ``pairs`` is ``[(plugin, plan_that_was_applied), ...]`` — the same plugin
    instances that ran ``apply()`` (so e.g. ``BranchProtectionPlugin.branches``
    is already narrowed) and the per-plugin plan each one was asked to apply.

    - A plugin whose applied plan has no actionable rows (all SKIP, or empty)
      is not re-read at all: nothing was written, so nothing can have failed.
    - Every non-SKIP row in the post-apply audit plan is a ``mismatch``
      (expected = the row's ``new``, actual = its ``old``), including rows for
      keys the applied plan never touched.
    - Every actionable row of the applied plan with no non-SKIP counterpart in
      the audit is ``ok``, except ``CREATE_ONLY_KEYS``.
    - A plugin whose read raises, or that still mismatches, is re-read after
      each delay in ``delays``. Retries run in rounds, so the whole call waits
      at most ``sum(delays)``. A plugin that still cannot be read yields
      ``unknown`` checks for every key it was asked to change.

    ``remediation`` is attached to mismatch checks (e.g. the exact
    ``gh-safe-repo fix owner/repo --yes`` command). ``sleep`` defaults to
    ``time.sleep`` resolved at call time, so tests can inject it or patch
    ``gh_safe_repo.verify.time`` (not ``time.sleep`` itself: that is global
    and also catches ``subprocess``'s own wait polling, #109).
    ``policy`` is applied to every re-audit plan first (the ``fix`` merge
    policy): a PRESERVE row is not actionable and so never a mismatch.
    """
    return verify(pairs, (), remediation=remediation, sleep=sleep, delays=delays,
                  policy=policy)


# ── create: dedicated checks for CREATE_ONLY_KEYS ───────────────────────────

def _read_repo(client, owner, repo):
    """Fresh GET /repos (never the per-process cache), or None on 404."""
    try:
        return client.get_repo_data(owner, repo, refresh=True)
    except APIError as e:
        if e.status_code == 404:
            return None
        raise


def _read_repo_required(client, owner, repo):
    data = _read_repo(client, owner, repo)
    if data is None:
        raise APIError(f"GET /repos/{owner}/{repo} returned 404", status_code=404)
    return data


def _subset(expected, actual):
    return actual is not None and set(expected) <= set(actual)


def _split(value):
    return [v.strip() for v in str(value or "").split(",") if v.strip()]


def _repo_probes(c, client, owner, repo, private):
    url = f"https://github.com/{owner}/{repo}"
    k = c.key
    if k == "repository":
        def read_exists():
            return "exists" if _read_repo(client, owner, repo) is not None else "missing"

        want = "private" if private else "public"

        def read_visibility():
            data = _read_repo_required(client, owner, repo)
            return "private" if data.get("private") else "public"
        return [
            Probe(
                "repo", "repository", "exists", read_exists, fatal=True,
                remediation=(
                    f"{owner}/{repo} was not found after creation. Check {url} "
                    "and that the active `gh` account is the one you expect."
                ),
            ),
            Probe(
                "repo", "visibility", want, read_visibility, fatal=True,
                remediation=(
                    f"{owner}/{repo} is not {want}. Fix it now: gh repo edit "
                    f"{owner}/{repo} --visibility {want} "
                    "--accept-visibility-change-consequences"
                ),
            ),
        ]
    if k == "description":
        def read_description():
            return _read_repo_required(client, owner, repo).get("description") or ""
        return [Probe(
            "repo", "description", c.new, read_description,
            remediation=(
                f"gh repo edit {owner}/{repo} --description {shlex.quote(str(c.new))}"
            ),
        )]
    if k == "topics":
        topics = sorted(_split(c.new))

        def read_topics():
            data = client.get_json(client.repo_path(owner, repo, "topics"))
            return sorted((data or {}).get("names") or [])
        return [Probe(
            "repo", "topics", topics, read_topics, matches=_subset,
            remediation=(
                f"gh repo edit {owner}/{repo} "
                + " ".join(f"--add-topic {shlex.quote(t)}" for t in topics)
            ),
        )]
    return []


def _readme_probe(client, owner, repo):
    path = client.repo_path(owner, repo, "contents/README.md")

    def read_readme():
        status, _ = client.call_api("GET", path)
        if status == 404:
            return "absent"
        if status is not None and 200 <= status < 300:
            return "present"
        raise APIError(f"GET {path} returned {status}", status_code=status)
    api = f"repos/{owner}/{repo}/contents/README.md"
    return Probe(
        "file", "readme", "absent", read_readme,
        remediation=(
            f"Remove it: gh api -X DELETE {api} -f message='Remove "
            f"auto-generated README' -f sha=\"$(gh api {api} --jq .sha)\""
        ),
    )


def create_probes(pairs: Iterable[Tuple[Any, Plan]], *, private: bool) -> List[Probe]:
    """Dedicated checks for the create-only rows of each applied plan.

    One probe per ``CREATE_ONLY_KEYS`` row the create plan actually contained,
    built from the plugin that planned it (so a SKIPped plugin, or a plain
    ``new`` (no ``copy`` source) adds none):

    - ``repo/repository`` → ``repo/repository`` (the repo exists) and
      ``repo/visibility`` (private/public as asked). Both fatal: a missing
      repo means create did nothing, and a repo meant to be private that came
      out public is a disclosure, not a setting to converge later.
    - ``repo/description`` / ``repo/topics`` → copied from the ``copy``
      source (topics: the source's are a subset of what the repo has).
    - ``file/readme`` → the auto_init README is gone (GET contents → 404).
    - ``branch_protection/protected_branches`` /
      ``tag_protection/protected_tags`` → our protection covers the
      (narrowed) branches / the configured tag patterns.

    The ``repo/repository`` probe, when present, is first.
    """
    probes: List[Probe] = []
    for plugin, applied in pairs:
        owner, repo, client = plugin.owner, plugin.repo, plugin.client
        fix_cmd = f"Run `gh-safe-repo fix {owner}/{repo} --yes`."
        for c in applied.actionable_changes:
            k = _key(c)
            if k not in CREATE_ONLY_KEYS:
                continue
            if c.category == ChangeCategory.REPO:
                new = _repo_probes(c, client, owner, repo, private)
                if c.key == "repository":
                    probes[:0] = new[:1]
                    probes.extend(new[1:])
                else:
                    probes.extend(new)
            elif c.category == ChangeCategory.FILE:
                probes.append(_readme_probe(client, owner, repo))
            elif c.category == ChangeCategory.BRANCH_PROTECTION:
                probes.append(Probe(
                    "branch_protection", "protected_branches",
                    list(plugin.branches), plugin.protected_branches,
                    matches=_subset, remediation=fix_cmd,
                ))
            elif c.category == ChangeCategory.TAG_PROTECTION:
                probes.append(Probe(
                    "tag_protection", "protected_tags", _split(c.new),
                    plugin.protected_tags, matches=_subset, remediation=fix_cmd,
                ))
    return probes


def verify_create(
    pairs: Sequence[Tuple[Any, Plan]],
    *,
    private: bool,
    extra_probes: Iterable[Probe] = (),
    remediation: Optional[str] = None,
    sleep: Optional[Callable[[float], None]] = None,
    delays: Sequence[float] = DEFAULT_DELAYS,
) -> Verification:
    """Verify a ``create`` apply: create-only probes plus the plugin audits.

    The repo-exists probe runs alone first. If the repo is missing, or cannot
    be read at all, nothing else can be checked, so the rest is skipped rather
    than spending a second retry budget on reads that must fail. Otherwise
    every other probe, ``extra_probes`` (the hook for code and local-wiring
    checks) and every plugin audit share one set of retry rounds.
    """
    pairs = list(pairs)
    probes = create_probes(pairs, private=private)
    v = Verification()
    if probes and probes[0].name == "repo/repository":
        v = verify(probes=probes[:1], sleep=sleep, delays=delays)
        if not v.ok:
            return v
        probes = probes[1:]
    v.extend(verify(
        pairs, probes + list(extra_probes),
        remediation=remediation, sleep=sleep, delays=delays,
    ))
    return v


# ── push / copy: pushed code, default branch, local wiring ──────

def _remote_refs(client, owner, repo, namespace, names):
    """GitHub's SHA for each of ``names`` under refs/<namespace>/ (None if absent)."""
    remote = client.matching_refs(owner, repo, namespace)
    return {n: remote.get(n) for n in names}


def pushed_ref_probes(client, owner, repo, *, branches, tags,
                      remediation=None) -> List[Probe]:
    """Fatal probes: what ``push`` pushed is on GitHub, SHA for SHA.

    ``branches`` / ``tags`` are ``{name: sha}`` as ``push_local`` recorded
    them (``LocalWiring.pushed`` / ``.tags``; a tag's sha is the tag object
    for an annotated tag, which is also what the API reports). With no
    recorded branches the probe only requires GitHub to have at least one
    branch. Tags are checked only when some were pushed. Reads use the REST
    API (``GitHubClient.matching_refs``), never ``git ls-remote``.
    """
    probes = []
    if branches:
        names = sorted(branches)
        probes.append(Probe(
            "code", "branches", dict(branches),
            lambda: _remote_refs(client, owner, repo, "heads", names),
            fatal=True, remediation=remediation,
        ))
    else:
        probes.append(Probe(
            "code", "branches", "at least one branch",
            lambda: sorted(client.matching_refs(owner, repo, "heads")),
            matches=lambda _expected, actual: bool(actual),
            fatal=True, remediation=remediation,
        ))
    if tags:
        tag_names = sorted(tags)
        probes.append(Probe(
            "code", "tags", dict(tags),
            lambda: _remote_refs(client, owner, repo, "tags", tag_names),
            fatal=True, remediation=remediation,
        ))
    return probes


def mirrored_ref_probes(client, src_owner, src_repo, owner, repo, *,
                        remediation=None) -> List[Probe]:
    """Fatal probes: after ``copy``, every branch and tag of the
    source is on the new repo at the same SHA.

    The source refs are read (once, through the same API) by the first
    attempt that gets them; a failed source read is retried and ends
    ``unknown`` like any other unreadable probe.
    """
    probes = []
    for key, namespace in (("branches", "heads"), ("tags", "tags")):
        probe = Probe("code", key, None, None, fatal=True, remediation=remediation)

        def read(probe=probe, namespace=namespace):
            if probe.expected is None:
                probe.expected = client.matching_refs(src_owner, src_repo, namespace)
            return _remote_refs(client, owner, repo, namespace, sorted(probe.expected))
        probe.read = read
        probes.append(probe)
    return probes


def default_branch_probe(client, owner, repo, expected) -> Probe:
    """The new repo's default branch is the one create chose (exit 3 if not)."""
    return Probe(
        "repo", "default_branch", expected,
        lambda: _read_repo_required(client, owner, repo).get("default_branch"),
        remediation=(
            f"gh repo edit {owner}/{repo} --default-branch {shlex.quote(expected)}"
        ),
    )


def _git_config(path, key):
    """`git config --get key` in path, or None when unset or git fails."""
    try:
        r = subprocess.run(
            ["git", "-C", path, "config", "--get", key],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def local_wiring_probes(local_path, url, branch, *, foreign_origin=None) -> List[Probe]:
    """Advisory probes: the user's repo now points at the new remote.

    Local git config reads only (``remote.origin.url``,
    ``branch.<b>.remote`` / ``.merge``), never ``@{u}`` and never the
    network. A mismatch is a warning (exit code unchanged) whose remediation
    is the exact command to run. ``foreign_origin`` is an ``origin`` that
    already pointed elsewhere and was deliberately left untouched: the
    origin check then says so, and no tracking check is made (any upstream
    would be the old remote's).
    """
    where = shlex.quote(local_path)
    push = f" && git push -u origin {branch}:{branch}" if branch else ""
    if foreign_origin:
        origin_fix = (
            f"'origin' already pointed at {foreign_origin} and was left "
            f"unchanged. To use the new repo instead: cd {where} && "
            f"git remote set-url origin {url}{push}"
        )
    else:
        origin_fix = f"cd {where} && git remote add origin {url}{push}"
    probes = [Probe(
        "local", "origin", url,
        lambda: _git_config(local_path, "remote.origin.url"),
        advisory=True, retry=False, remediation=origin_fix,
    )]
    if branch and not foreign_origin:
        probes.append(Probe(
            "local", "tracking",
            {"remote": "origin", "merge": f"refs/heads/{branch}"},
            lambda: {
                "remote": _git_config(local_path, f"branch.{branch}.remote"),
                "merge": _git_config(local_path, f"branch.{branch}.merge"),
            },
            advisory=True, retry=False,
            remediation=f"cd {where} && git push -u origin {branch}:{branch}",
        ))
    return probes
