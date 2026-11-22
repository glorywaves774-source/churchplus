"""Denominations: a headquarters church overseeing member churches.

Headquarters can register member churches and see a summary of each
(status, branches, users). It cannot open a member church's records: each
member church remains its own isolated workspace.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func

from app.extensions import db
from app.models import (
    Branch, Denomination, Tenant, TenantKind, TenantStatus, TenantUser, User,
)
from app.services import audit
from app.services.provisioning import provision_church
from app.utils.tenancy import TenantIsolationError, tenant_bypass, tenant_context


def ensure_denomination(hq: Tenant) -> Denomination:
    """Create the denomination record for a headquarters church if missing."""
    with tenant_bypass():
        if hq.denomination is not None:
            return hq.denomination
        name, n = hq.name, 2
        while db.session.query(Denomination.id).filter_by(name=name).first():
            name, n = f"{hq.name} ({n})", n + 1
        denom = Denomination(name=name, country=hq.country, website=hq.website)
        db.session.add(denom)
        hq.denomination = denom
        hq.kind = TenantKind.DENOMINATION_HQ
        db.session.flush()
        return denom


def is_headquarters(tenant: Tenant) -> bool:
    return tenant is not None and tenant.kind == TenantKind.DENOMINATION_HQ


def create_member_church(hq: Tenant, *, name: str, city: str | None, admin_first: str, admin_last: str,
                         admin_email: str, actor: User) -> tuple[Tenant, User, bool]:
    """Register a member church under ``hq``. Returns (church, admin user, user_is_new).

    A new administrator gets an account without a password; they choose one
    through the invitation link.
    """
    if not is_headquarters(hq):
        raise TenantIsolationError("Only a denomination headquarters can add member churches.")
    denom = ensure_denomination(hq)
    email = admin_email.strip().lower()
    with tenant_bypass():
        admin = db.session.query(User).filter_by(email=email).one_or_none()
        is_new = admin is None
        if is_new:
            admin = User(email=email, first_name=admin_first.strip(), last_name=admin_last.strip())
        plan_code = _plan_code_of(hq)
        church = provision_church(
            name=name.strip(), owner=admin, plan_code=plan_code, country=hq.country, currency=hq.currency,
            timezone=hq.timezone, city=city, status=TenantStatus.ACTIVE if hq.status == TenantStatus.ACTIVE else hq.status,
            kind=TenantKind.DENOMINATION_MEMBER, denomination=denom, parent=hq,
        )
    audit.record("denomination.member_church_added", tenant_id=hq.id, actor=actor, entity=church,
                 church=church.name)
    db.session.flush()
    return church, admin, is_new


def _plan_code_of(tenant: Tenant) -> str:
    from app.models import Subscription
    with tenant_context(tenant.id):
        sub = db.session.query(Subscription).order_by(Subscription.created_at.desc()).first()
        return sub.plan.code if sub else "starter"


@dataclass
class MemberChurchSummary:
    public_id: str
    name: str
    city: str | None
    status: str
    branches: int
    users: int
    created_at: object


def member_church_summaries(hq: Tenant) -> list[MemberChurchSummary]:
    """Aggregate figures only — no access to member churches' records."""
    with tenant_bypass():
        churches = (db.session.query(Tenant).filter(Tenant.parent_tenant_id == hq.id)
                    .order_by(Tenant.name).all())
        ids = [c.id for c in churches]
        branch_counts = dict(db.session.query(Branch.tenant_id, func.count(Branch.id))
                             .filter(Branch.tenant_id.in_(ids)).group_by(Branch.tenant_id)) if ids else {}
        user_counts = dict(db.session.query(TenantUser.tenant_id, func.count(TenantUser.id))
                           .filter(TenantUser.tenant_id.in_(ids)).group_by(TenantUser.tenant_id)) if ids else {}
    return [MemberChurchSummary(c.public_id, c.name, c.city, c.status.value, branch_counts.get(c.id, 0),
                                user_counts.get(c.id, 0), c.created_at) for c in churches]
