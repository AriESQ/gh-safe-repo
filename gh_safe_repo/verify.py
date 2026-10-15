"""Post-condition verification (#77).

After an apply, re-read what GitHub actually holds and compare it against what
was asked for. The tool used to report success from the absence of exceptions;
a PATCH that GitHub accepted but ignored, or an apply that raised and was
downgraded to a warning, still produced a green "Done".

Settings are verified by re-running each plugin's own audit: the same
``fetch_current_state()`` → ``plan(current_state=...)`` pair that
``fix --dry-run`` uses. After a successful apply that audit plan must contain
only SKIP rows; any other row is a setting that did not stick.

Reads go through the REST API (never ``git ls-remote``, which over SSH can cost
a hardware-key touch), the per-process ``GET /repos`` cache is dropped before
every read, and reads use ``strict=True`` so "could not read" surfaces as
``unknown`` instead of as a false mismatch.

Public API (shared by ``fix`` and, later, ``create``):

- ``Check`` / ``Verification`` — the result model; ``Verification.to_dict()``
  is the ``"verification"`` block of the ``--json`` document.
- ``verify_plugins(pairs, ...)`` — re-audit plugins after apply.
- ``CREATE_ONLY_KEYS`` — create-mode plan rows an audit cannot see; they are
  left to dedicated checks.
"""

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, List, Optional, Sequence, Tuple

from .diff import ChangeCategory, Plan

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
    (code push missing under ``create --local/--from``): exit 1. Non-fatal
    mismatches are settings that ``fix`` would converge: exit 3. ``unknown``
    is never a failure on its own.
    """
    category: str
    key: str
    status: str
    expected: Any = None
    actual: Any = None
    fatal: bool = False
    remediation: Optional[str] = None

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
        return any(c.status == MISMATCH for c in self.checks)

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


def _audit(plugin):
    """Re-read one plugin strictly and return its post-apply audit Plan."""
    forget = getattr(plugin.client, "forget_repo", None)
    if callable(forget):
        forget(plugin.owner, plugin.repo)
    state = plugin.fetch_current_state(strict=True)
    return plugin.plan(current_state=state)


def verify_plugins(
    pairs: Iterable[Tuple[Any, Plan]],
    *,
    remediation: Optional[str] = None,
    sleep: Optional[Callable[[float], None]] = None,
    delays: Sequence[float] = DEFAULT_DELAYS,
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
    ``time.sleep`` resolved at call time, so tests can patch or inject it.
    """
    sleep = sleep or time.sleep

    pending = []
    for plugin, applied in pairs:
        expected = [
            c for c in applied.actionable_changes
            if _key(c) not in CREATE_ONLY_KEYS
        ]
        if not applied.actionable_changes:
            continue
        pending.append((plugin, expected))

    # result per pending index: ("plan", Plan) or ("error", Exception)
    results = {}
    todo = list(range(len(pending)))
    schedule = [0] + list(delays)
    for round_no, delay in enumerate(schedule):
        if not todo:
            break
        if round_no:
            sleep(delay)
        still = []
        for i in todo:
            plugin, _ = pending[i]
            try:
                audit = _audit(plugin)
            except Exception as e:  # noqa: BLE001 — any failed read is "unknown"
                results[i] = ("error", e)
                still.append(i)
                continue
            results[i] = ("plan", audit)
            if audit.has_changes():
                still.append(i)
        todo = still

    verification = Verification()
    for i, (plugin, expected) in enumerate(pending):
        kind, value = results[i]
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
                        f"{len(schedule)} attempts ({value}). Check it in "
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
