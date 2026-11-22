"""Models, constraints, provisioning, roles and permissions."""
import pytest
from sqlalchemy.exc import IntegrityError

from app.models import (
    Branch, Denomination, Permission, Plan, Role, Subscription, SubscriptionStatus, Tenant,
    TenantKind, TenantStatus, TenantUser, User,
)
from app.services.permissions import ACTIONS, DEFAULT_ROLES, MODULES, all_codes, sync_permissions
from app.services.provisioning import provision_church, slugify
from app.utils.tenancy import tenant_bypass, tenant_context
from tests.conftest import make_user


def test_password_hashing_and_rules():
    u = make_user("pw@test.test", password="Correct-Horse-9")
    assert u.password_hash and "Correct-Horse-9" not in u.password_hash
    assert u.check_password("Correct-Horse-9")
    assert not u.check_password("wrong")
    with pytest.raises(ValueError):
        u.set_password("short")


def test_user_email_unique(db):
    with tenant_bypass():
        db.session.add_all([make_user("dup@test.test"), make_user("dup@test.test")])
        with pytest.raises(IntegrityError):
            db.session.flush()
        db.session.rollback()


def test_flask_login_uses_public_id_not_integer_id(db):
    with tenant_bypass():
        u = make_user("pid@test.test")
        db.session.add(u)
        db.session.flush()
    assert u.get_id() == f"{u.public_id}.1" and len(u.public_id) == 36


def test_provisioning_creates_complete_workspace(db, two_churches):
    with tenant_bypass():
        t = db.session.get(Tenant, two_churches["grace"])
        assert t.slug == "grace-chapel-international"
        assert t.status == TenantStatus.TRIAL and t.is_operational
        assert t.currency == "GHS" and t.country == "GH"
    with tenant_context(two_churches["grace"]):
        hq = db.session.query(Branch).one()
        assert hq.is_headquarters and hq.code == "HQ"
        assert {r.slug for r in db.session.query(Role)} == set(DEFAULT_ROLES)
        owner = db.session.query(TenantUser).one()
        assert owner.is_owner and owner.branch_id == hq.id
        sub = db.session.query(Subscription).one()
        assert sub.status == SubscriptionStatus.TRIALING and sub.plan.code == "growth"
        assert sub.trial_ends_at is not None


def test_slugs_are_unique(db, two_churches):
    with tenant_bypass():
        t = provision_church(name="Grace Chapel International", owner=make_user("x@grace2.test"))
        db.session.commit()
        assert t.slug == "grace-chapel-international-2"
    assert slugify("  St. Mary's  Cathedral!! ") == "st-mary-s-cathedral"


def test_branch_code_unique_per_church_only(db, two_churches):
    with tenant_context(two_churches["grace"]):
        db.session.add(Branch(name="East", code="EAST"))
        db.session.commit()
    with tenant_context(two_churches["word"]):
        db.session.add(Branch(name="East", code="EAST"))  # same code, other church: allowed
        db.session.commit()
    with tenant_context(two_churches["grace"]):
        db.session.add(Branch(name="East again", code="EAST"))
        with pytest.raises(IntegrityError):
            db.session.flush()
        db.session.rollback()


def test_deleting_church_cascades_to_its_records(db, two_churches):
    with tenant_bypass():
        t = db.session.get(Tenant, two_churches["word"])
        db.session.delete(t)
        db.session.commit()
        remaining = {b.tenant_id for b in db.session.query(Branch)}
    assert remaining == {two_churches["grace"]}


def test_denomination_structure(db):
    with tenant_bypass():
        denom = Denomination(name="Example Pentecostal Church Ghana", abbreviation="EPCG")
        hq = Tenant(name="EPCG Headquarters", slug="epcg-hq", kind=TenantKind.DENOMINATION_HQ, denomination=denom)
        local = Tenant(name="EPCG Madina", slug="epcg-madina", kind=TenantKind.DENOMINATION_MEMBER,
                       denomination=denom, parent=hq)
        db.session.add_all([denom, hq, local])
        db.session.commit()
        assert {c.slug for c in denom.churches} == {"epcg-hq", "epcg-madina"}
        assert hq.children == [local]


def test_permission_catalogue_sync_is_idempotent(db):
    with tenant_bypass():
        assert sync_permissions() == len(MODULES) * len(ACTIONS)
        assert sync_permissions() == 0
        sensitive = {p.module for p in db.session.query(Permission).filter_by(is_sensitive=True)}
    assert sensitive == {"prayer", "counselling", "children"}


def test_default_roles_only_use_known_permissions():
    codes = set(all_codes())
    for slug, (_, _, perms) in DEFAULT_ROLES.items():
        assert perms <= codes, slug


def test_role_permissions(db, two_churches):
    with tenant_context(two_churches["grace"]):
        roles = {r.slug: r for r in db.session.query(Role)}
        assert roles["church_owner"].has_permission("counselling.view")
        assert roles["finance_manager"].has_permission("finance.approve")
        assert not roles["accountant"].has_permission("finance.approve")
        assert not roles["church_admin"].has_permission("counselling.view")
        assert roles["auditor"].has_permission("finance.view")
        assert not roles["auditor"].has_permission("finance.edit")
        assert roles["member"].permissions == []


def test_custom_role_per_church(db, two_churches):
    with tenant_context(two_churches["grace"]):
        perm = db.session.query(Permission).filter_by(code="events.view").one()
        db.session.add(Role(name="Choir Coordinator", slug="choir", permissions=[perm]))
        db.session.commit()
        assert db.session.query(Role).count() == 16
    with tenant_context(two_churches["word"]):
        assert db.session.query(Role).filter_by(slug="choir").count() == 0


def test_suspended_account_has_no_permissions(db, two_churches):
    with tenant_context(two_churches["grace"]):
        acct = db.session.query(TenantUser).one()
        assert acct.can("members.view")
        acct.is_suspended = True
        assert not acct.can("members.view")


def test_platform_admin_gets_no_church_access_by_default(db, two_churches):
    from app.models import PlatformRole
    with tenant_bypass():
        admin = make_user("admin@kayron.test")
        admin.platform_role = PlatformRole.SUPER_ADMIN
        db.session.add(admin)
        db.session.commit()
        assert admin.is_platform_staff
        assert db.session.query(TenantUser).filter_by(user_id=admin.id).count() == 0


def test_default_plans_seeded(db):
    from app.cli import seed_plans
    with tenant_bypass():
        assert seed_plans() == 4
        assert seed_plans() == 0
        plans = {p.code: p for p in db.session.query(Plan)}
    assert str(plans["starter"].monthly_price) == "100.00"
    assert str(plans["growth"].monthly_price) == "250.00"
    assert str(plans["professional"].monthly_price) == "500.00"
    assert plans["enterprise"].monthly_price is None and not plans["enterprise"].is_public
