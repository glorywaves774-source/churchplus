"""Plan limits (users, members, branches, storage, modules).

Each church's limits come from its current subscription plan. ``None`` means
unlimited. Full subscription enforcement arrives in Stage 10; limits are
checked from the start so churches never exceed what they pay for.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.extensions import db
from app.models import Branch, Plan, Subscription, TenantUser
from app.utils.tenancy import require_tenant_id


@dataclass
class LimitStatus:
    resource: str
    used: int
    limit: int | None

    @property
    def allowed(self) -> bool:
        return self.limit is None or self.used < self.limit

    @property
    def label(self) -> str:
        return f"{self.used} of {self.limit}" if self.limit is not None else f"{self.used} (unlimited)"


def current_plan() -> Plan | None:
    require_tenant_id()
    sub = db.session.query(Subscription).order_by(Subscription.created_at.desc()).first()
    return sub.plan if sub else None


def _count(resource: str) -> int:
    if resource == "branches":
        return db.session.query(Branch).filter_by(is_active=True).count()
    if resource == "users":
        # Member-portal sign-ins are free; only staff accounts count towards the plan.
        return sum(1 for a in db.session.query(TenantUser).filter_by(is_suspended=False) if a.permission_codes())
    if resource == "members":
        try:
            from app.models import Member  # available from Stage 4
        except ImportError:
            return 0
        return db.session.query(Member).count()
    raise ValueError(resource)


def check(resource: str) -> LimitStatus:
    plan = current_plan()
    limit = getattr(plan, f"max_{resource}", None) if plan else None
    return LimitStatus(resource, _count(resource), limit)


def module_enabled(module: str) -> bool:
    plan = current_plan()
    return plan is None or not plan.modules or module in plan.modules
