"""
Security plugin — enables Dependabot alerts, secret scanning, push protection,
private vulnerability reporting, and Dependabot security updates.

Dependabot alerts: available for public repos on free plan; private repos need paid plan.
Secret scanning: automatically enabled by GitHub for all public repos (no API call needed);
    for private repos on paid plans, enabled via PATCH security_and_analysis.
Push protection: blocks commits containing supported secrets.
Private vulnerability reporting: lets security researchers report vulnerabilities privately.
Dependabot security updates: auto-opens PRs for vulnerable dependencies.

Not available via REST API (UI-only or dependabot.yml):
    - Grouped security updates: configure via dependabot.yml groups with applies-to: security-updates
    - Automatic dependency submission: UI toggle only, no REST endpoint
    - Dependency graph: auto-enabled for public repos; no writable REST API for private
"""

from ..diff import Change, ChangeCategory, ChangeType, Plan
from .base import BasePlugin

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
    def __init__(self, client, owner, repo, config, is_public=False, is_paid_plan=False):
        super().__init__(client, owner, repo, config)
        self.is_public = is_public
        self.is_paid_plan = is_paid_plan

    def _is_enabled(self, suffix):
        """Check a toggle endpoint: 2xx = enabled, 404 = disabled."""
        path = self.client.repo_path(self.owner, self.repo, suffix)
        status, _ = self.client.call_api("GET", path)
        # gh api infers 200 when stderr has no status; actual API may return 200 or 204
        return status is not None and 200 <= status < 300

    def fetch_current_state(self) -> dict:
        result = {}

        result["dependabot_alerts"] = self._is_enabled("vulnerability-alerts")
        result["dependabot_security_updates"] = self._is_enabled("automated-security-fixes")
        result["private_vulnerability_reporting"] = self._is_enabled(
            "private-vulnerability-reporting"
        )

        # Check secret scanning and security_and_analysis settings
        if self.is_public:
            result["secret_scanning"] = True
            result["enable_dependency_graph"] = True
        else:
            try:
                data = self.client.get_repo_data(self.owner, self.repo)
                sa = data.get("security_and_analysis") or {}
                ss = sa.get("secret_scanning") or {}
                result["secret_scanning"] = ss.get("status") == "enabled"

                for config_key, api_key in SECURITY_ANALYSIS_SETTINGS.items():
                    entry = sa.get(api_key) or {}
                    result[config_key] = entry.get("status") == "enabled"
            except Exception:
                result["secret_scanning"] = False
                for config_key in SECURITY_ANALYSIS_SETTINGS:
                    result[config_key] = False

        # For public repos, still check the non-auto security_and_analysis settings
        if self.is_public:
            try:
                data = self.client.get_repo_data(self.owner, self.repo)
                sa = data.get("security_and_analysis") or {}
                for config_key, api_key in SECURITY_ANALYSIS_SETTINGS.items():
                    entry = sa.get(api_key) or {}
                    result[config_key] = entry.get("status") == "enabled"
            except Exception:
                for config_key in SECURITY_ANALYSIS_SETTINGS:
                    result.setdefault(config_key, False)

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

        # Private vulnerability reporting (PUT toggle)
        self._plan_toggle(
            plan, is_audit, current_state,
            config_key="enable_private_vulnerability_reporting",
            plan_key="private_vulnerability_reporting",
        )

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
                plan.add(Change(
                    type=ChangeType.SKIP,
                    category=ChangeCategory.SECURITY,
                    key=plan_key,
                    reason="Already at desired value",
                ))
            else:
                plan.add(Change(
                    type=ChangeType.UPDATE,
                    category=ChangeCategory.SECURITY,
                    key=plan_key,
                    old=False,
                    new=True,
                ))
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

        if sa_body:
            path = self.client.repo_path(self.owner, self.repo)
            self.client.call_json("PATCH", path, {"security_and_analysis": sa_body})
