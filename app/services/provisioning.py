"""Create a new church workspace with its defaults.

Used by the seed command and tests now; the onboarding screens in Stage 2 call
the same function, so there is one code path for creating churches.
"""
from __future__ import annotations

import re
from datetime import timedelta

from app.extensions import db
from app.models import (
    Branch, Permission, Plan, Role, Subscription, SubscriptionStatus, Tenant, TenantStatus,
    TenantUser, User,
)
from app.models.base import utcnow
from app.services import audit
from app.services.permissions import DEFAULT_ROLES, sync_permissions
from app.utils.tenancy import tenant_bypass, tenant_context

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    slug = _SLUG_RE.sub("-", value.lower()).strip("-")
    return slug[:70] or "church"


def unique_slug(name: str) -> str:
    base = slugify(name)
    slug, n = base, 2
    with tenant_bypass():
        while db.session.query(Tenant.id).filter_by(slug=slug).first():
            slug, n = f"{base}-{n}", n + 1
    return slug


def provision_church(*, name: str, owner: User, plan_code: str = "starter", country: str = "GH",
                     currency: str = "GHS", email: str | None = None, phone: str | None = None,
                     status: TenantStatus = TenantStatus.TRIAL, **extra) -> Tenant:
    """Create church, headquarters branch, default roles, owner account and trial subscription.

    The caller commits.
    """
    with tenant_bypass():
        sync_permissions()
        if owner.id is None:
            db.session.add(owner)
        tenant = Tenant(name=name, slug=unique_slug(name), country=country, currency=currency,
                        email=email, phone=phone, status=status, **extra)
        db.session.add(tenant)
        db.session.flush()
        perms = {p.code: p for p in db.session.query(Permission)}
        plan = db.session.query(Plan).filter_by(code=plan_code).one_or_none()

    with tenant_context(tenant.id):
        hq = Branch(name=f"{name} - Headquarters", code="HQ", is_headquarters=True, country=country)
        db.session.add(hq)

        roles = {}
        for slug, (label, description, codes) in DEFAULT_ROLES.items():
            role = Role(name=label, slug=slug, description=description, is_system=True,
                        is_branch_scoped=(slug == "branch_admin"),
                        permissions=[perms[c] for c in sorted(codes)])
            db.session.add(role)
            roles[slug] = role

        account = TenantUser(user_id=owner.id, is_owner=True, branch=hq, roles=[roles["church_owner"]])
        db.session.add(account)

        if plan is not None:
            now = utcnow()
            db.session.add(Subscription(
                plan_id=plan.id, status=SubscriptionStatus.TRIALING,
                trial_ends_at=now + timedelta(days=plan.trial_days),
                current_period_start=now, current_period_end=now + timedelta(days=plan.trial_days),
            ))

        from app.services.ledger import ensure_chart
        from app.services.operations import ensure_defaults
        ensure_defaults()
        ensure_chart()
        audit.record("tenant.provisioned", entity=tenant, actor=owner, plan=plan_code)
        db.session.flush()
    return tenant
