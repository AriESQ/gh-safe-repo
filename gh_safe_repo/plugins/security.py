"""
Security plugin — enables Dependabot alerts, secret scanning, push protection,
private vulnerability reporting, and Dependabot security updates.

Dependabot alerts: available for public repos on free plan; private repos need paid plan.
Secret scanning: automatically enabled by GitHub for all public repos (no API call needed);
    for private repos on paid plans, enabled via PATCH security_and_analysis.
Push protection: blocks commits containing supported secrets.
Private vulnerability reporting: lets security researchers report vulnerabilities privately;
    public repos only, on every plan (SKIP for private repos, paid or not).
Dependabot security updates: auto-opens PRs for vulnerable dependencies.

Not available via REST API (UI-only or dependabot.yml):
    - Grouped security updates: configure via dependabot.yml groups with applies-to: security-updates
    - Automatic dependency submission: UI toggle only, no REST endpoint
    - Dependency graph: auto-enabled for public repos; no writable REST API for private
"""

import json

from ..diff import Change, ChangeCategory, ChangeType, Plan
from ..errors import APIError
from .base import BasePlugin

# What a brand-new repo holds, as far as this plugin can tell. GitHub turns
# some of these on by itself (secret scanning on public repos; Dependabot
# alerts when the account opted into "enable for new repositories"), which
# the API cannot distinguish from a human turning them on, so the base is
# "off" for all of them: a disabled toggle is never reported as ⚠ weakened,
# it is simply enabled. Keyed by plan key (see _plan_toggle).
GITHUB_DEFAULTS = {
    "dependabot_alerts": False,
    "secret_scanning": False,
    "dependabot_security_updates": False,
    "private_vulnerability_reporting": False,
    "enable_secret_scanning_push_protection": False,
    "enable_dependency_graph": False,
}

# Settings managed via PATCH /repos with security_and_analysis body.
# Maps config key → security_and_analysis sub-key.
SECURITY_ANALYSIS_SETTINGS = {
    "enable_secret_scanning_push_protection": "secret_scanning_push_protection",
}

# Settings managed via dedicated PUT endpoints (204 = enabled, DELETE = disable).
# Maps config key → (plan_key, URL suffix).
PUT_TOGGLE_SETTINGS = {
    "enable_dependabot_security_updates": ("dependabot_security_updates", "automated-security-fixes"),
    "enable_private_vulnerability_reporting": (
        "private_vulnerability_reporting", "private-vulnerability-reporting"
    ),
}

# Settings that are auto-enabled for public repos (no API call needed)
AUTO_PUBLIC_SETTINGS = {
    "enable_dependency_graph",
}


class SecurityPlugin(BasePlugin):
    section = "security"
    github_defaults = GITHUB_DEFAULTS

    def __init__(self, client, owner, repo, config, is_public=False, is_paid_plan=False):
        super().__init__(client, owner, repo, config)
        self.is_public = is_public
        self.is_paid_plan = is_paid_plan

    def _is_enabled(self, suffix, strict=False):
        """Check a toggle endpoint: 2xx = enabled, 404 = disabled.

        Endpoints that answer 200 with a JSON ``{"enabled": bool}`` body
        (private-vulnerability-reporting does, even when off) are read from
        the body, so "200, enabled: false" is not mistaken for "on".

        Any other status (403, 5xx, no status at all) is ambiguous. Planning
        (strict=False) keeps treating it as disabled, so `fix` proposes the
        change; verification (strict=True) raises APIError so the setting is
        reported as unknown instead of as a mismatch (#77).
        """
        path = self.client.repo_path(self.owner, self.repo, suffix)
        status, text = self.client.call_api("GET", path)
        # gh api infers 200 when stderr has no status; actual API may return 200 or 204
        if status is not None and 200 <= status < 300:
            try:
                body = json.loads(text) if text and text.strip() else None
            except (json.JSONDecodeError, ValueError, TypeError):
                body = None
            if isinstance(body, dict) and isinstance(body.get("enabled"), bool):
                return body["enabled"]
            return True
        if status == 404 or not strict:
            return False
        raise APIError(f"GET {path} returned {status}", status_code=status)

    def fetch_current_state(self, strict: bool = False) -> dict:
        result = {}

        result["dependabot_alerts"] = self._is_enabled("vulnerability-alerts", strict)
        result["dependabot_security_updates"] = self._is_enabled(
            "automated-security-fixes", strict
        )
        result["private_vulnerability_reporting"] = self._is_enabled(
            "private-vulnerability-reporting", strict
        )

        # Secret scanning and push protection come from GET /repos
        # (security_and_analysis). A failed read is "disabled" when planning
        # and re-raised when verifying.
        try:
            data = self.client.get_repo_data(self.owner, self.repo)
        except Exception:
            if strict:
                raise
            data = None
        sa = ((data or {}).get("security_and_analysis") or {})

        if self.is_public:
            result["secret_scanning"] = True
            result["enable_dependency_graph"] = True
        else:
            ss = sa.get("secret_scanning") or {}
            result["secret_scanning"] = ss.get("status") == "enabled"

        for config_key, api_key in SECURITY_ANALYSIS_SETTINGS.items():
            entry = sa.get(api_key) or {}
            result[config_key] = entry.get("status") == "enabled"

        return result

    def plan(self, current_state=None) -> Plan:
        plan = Plan()
        is_audit = current_state is not None

        if not self.is_public and not self.is_paid_plan:
            for key in [
                "dependabot_alerts", "secret_scanning",
                "dependabot_security_updates",
                "private_vulnerability_reporting",
                "enable_dependency_graph",
                "enable_secret_scanning_push_protection",
            ]:
                plan.add(Change(
                    type=ChangeType.SKIP,
                    category=ChangeCategory.SECURITY,
                    key=key,
                    reason="Requires a public repo or paid GitHub plan",
                ))
            return plan

        # Dependabot alerts
        self._plan_toggle(
            plan, is_audit, current_state,
            config_key="enable_dependabot_alerts",
            plan_key="dependabot_alerts",
        )

        # Secret scanning
        if self.is_public:
            plan.add(Change(
                type=ChangeType.SKIP,
                category=ChangeCategory.SECURITY,
                key="secret_scanning",
                reason="Automatically enabled for public repositories by GitHub",
            ))
        else:
            self._plan_toggle(
                plan, is_audit, current_state,
                config_key=None,  # always enable for private paid
                plan_key="secret_scanning",
                always_enable=True,
            )

        # Dependabot security updates (PUT toggle)
        self._plan_toggle(
            plan, is_audit, current_state,
            config_key="enable_dependabot_security_updates",
            plan_key="dependabot_security_updates",
        )

        # Private vulnerability reporting (PUT toggle): public repos only, on
        # every plan; a paid plan does not unlock it for private repos.
        if self.is_public:
            self._plan_toggle(
                plan, is_audit, current_state,
                config_key="enable_private_vulnerability_reporting",
                plan_key="private_vulnerability_reporting",
            )
        else:
            plan.add(Change(
                type=ChangeType.SKIP,
                category=ChangeCategory.SECURITY,
                key="private_vulnerability_reporting",
                reason="Only available for public repositories",
            ))

        # security_and_analysis settings (push protection)
        for config_key in SECURITY_ANALYSIS_SETTINGS:
            self._plan_toggle(
                plan, is_audit, current_state,
                config_key=config_key,
                plan_key=config_key,
            )

        # Dependency graph: auto for public, no writable API for private
        if self.is_public:
            plan.add(Change(
                type=ChangeType.SKIP,
                category=ChangeCategory.SECURITY,
                key="enable_dependency_graph",
                reason="Automatically enabled for public repositories by GitHub",
            ))
        else:
            plan.add(Change(
                type=ChangeType.SKIP,
                category=ChangeCategory.SECURITY,
                key="enable_dependency_graph",
                reason="No REST API available; enable via repository settings UI",
            ))

        return plan

    def _plan_toggle(self, plan, is_audit, current_state,
                     config_key, plan_key, always_enable=False):
        """Helper to plan an enable/disable toggle setting."""
        if always_enable:
            desired = True
        elif config_key:
            desired = self.config.getbool("security", config_key, fallback=True)
        else:
            desired = True

        if not desired:
            return  # user disabled in config, nothing to do

        if is_audit:
            current = current_state.get(plan_key, False)
            if current:
                plan.add(self.annotate(Change(
                    type=ChangeType.SKIP,
                    category=ChangeCategory.SECURITY,
                    key=plan_key,
                    reason="Already at desired value",
                )))
            else:
                plan.add(self.annotate(Change(
                    type=ChangeType.UPDATE,
                    category=ChangeCategory.SECURITY,
                    key=plan_key,
                    old=False,
                    new=True,
                )))
        else:
            plan.add(Change(
                type=ChangeType.ADD,
                category=ChangeCategory.SECURITY,
                key=plan_key,
                new=True,
            ))

    def apply(self, plan: Plan) -> None:
        # Collect security_and_analysis fields to batch into one PATCH
        sa_body = {}

        for change in plan.actionable_changes:
            if change.category != ChangeCategory.SECURITY:
                continue

            if change.key == "dependabot_alerts":
                path = self.client.repo_path(self.owner, self.repo, "vulnerability-alerts")
                self.client.call_json("PUT", path)

            elif change.key == "secret_scanning":
                sa_body["secret_scanning"] = {"status": "enabled"}

            elif change.key in SECURITY_ANALYSIS_SETTINGS:
                api_key = SECURITY_ANALYSIS_SETTINGS[change.key]
                sa_body[api_key] = {"status": "enabled"}

            else:
                # Check PUT toggle settings (dependabot_security_updates,
                # private_vulnerability_reporting)
                for cfg_key, (plan_key, suffix) in PUT_TOGGLE_SETTINGS.items():
                    if change.key == plan_key:
                        path = self.client.repo_path(self.owner, self.repo, suffix)
                        self.client.call_json("PUT", path)
                        break

        # GitHub requires secret_scanning to be present alongside
        # secret_scanning_push_protection — otherwise the PATCH is silently ignored.
        if "secret_scanning_push_protection" in sa_body and "secret_scanning" not in sa_body:
            sa_body["secret_scanning"] = {"status": "enabled"}

        if sa_body:
            path = self.client.repo_path(self.owner, self.repo)
            self.client.call_json("PATCH", path, {"security_and_analysis": sa_body})
