"""Cross-tenant security tests: one church must never reach another church's data."""
import pytest
from sqlalchemy import func, select, update

from app.models import AuditLog, Branch, Role, Subscription, TenantUser
from app.services import audit
from app.utils.tenancy import (
    TenantContextMissing, TenantIsolationError, assert_same_tenant, current_tenant_id,
    tenant_bypass, tenant_context,
)


def test_queries_only_return_active_church_rows(db, two_churches):
    with tenant_context(two_churches["grace"]):
        branches = db.session.query(Branch).all()
        assert len(branches) == 1
        assert all(b.tenant_id == two_churches["grace"] for b in branches)
        assert db.session.query(Role).count() == 15
        assert db.session.query(Subscription).count() == 1


def test_counts_and_core_selects_are_scoped(db, two_churches):
    with tenant_context(two_churches["word"]):
        assert db.session.scalar(select(func.count()).select_from(Branch)) == 1
        assert db.session.scalar(select(func.count(Role.id))) == 15
        assert db.session.scalars(select(Branch.name)).all() == ["Living Word Assembly - Headquarters"]


def test_joins_are_scoped(db, two_churches):
    with tenant_context(two_churches["grace"]):
        rows = db.session.query(TenantUser).join(Role, TenantUser.roles).all()
        assert len(rows) == 1 and rows[0].tenant_id == two_churches["grace"]


def test_get_by_id_of_other_church_returns_nothing(db, two_churches):
    with tenant_bypass():
        other_id = db.session.query(Branch.id).filter_by(tenant_id=two_churches["word"]).scalar()
    db.session.expunge_all()
    with tenant_context(two_churches["grace"]):
        assert db.session.get(Branch, other_id) is None
        assert db.session.query(Branch).filter_by(id=other_id).first() is None


def test_no_tenant_context_is_refused(db, two_churches):
    with pytest.raises(TenantContextMissing):
        db.session.query(Branch).all()
    with pytest.raises(TenantContextMissing):
        db.session.scalars(select(Role)).all()


def test_uninspectable_statements_fail_closed_without_context(db, two_churches):
    # Query.count() wraps the entity in a subquery; it must return nothing, not everything.
    assert db.session.query(Role).count() == 0


def test_cannot_create_record_for_another_church(db, two_churches):
    with tenant_context(two_churches["grace"]):
        db.session.add(Branch(name="Sneaky", code="X1", tenant_id=two_churches["word"]))
        with pytest.raises(TenantIsolationError):
            db.session.flush()
        db.session.rollback()


def test_new_records_are_stamped_with_active_church(db, two_churches):
    with tenant_context(two_churches["word"]):
        b = Branch(name="Tema Branch", code="TEMA")
        db.session.add(b)
        db.session.commit()
        assert b.tenant_id == two_churches["word"]


def test_cannot_create_without_context(db, two_churches):
    db.session.add(Branch(name="Orphan", code="ORP"))
    with pytest.raises(TenantContextMissing):
        db.session.flush()
    db.session.rollback()


def test_cannot_move_record_to_another_church(db, two_churches):
    with tenant_context(two_churches["grace"]):
        b = db.session.query(Branch).one()
        b.tenant_id = two_churches["word"]
        with pytest.raises(TenantIsolationError):
            db.session.flush()
        db.session.rollback()


def test_cannot_modify_or_delete_object_loaded_under_other_church(db, two_churches):
    with tenant_bypass():
        other = db.session.query(Branch).filter_by(tenant_id=two_churches["word"]).one()
    with tenant_context(two_churches["grace"]):
        other.name = "Hijacked"
        with pytest.raises(TenantIsolationError):
            db.session.flush()
        db.session.rollback()
    with tenant_bypass():
        other = db.session.query(Branch).filter_by(tenant_id=two_churches["word"]).one()
    with tenant_context(two_churches["grace"]):
        db.session.delete(other)
        with pytest.raises(TenantIsolationError):
            db.session.flush()
        db.session.rollback()


def test_bulk_update_only_touches_active_church(db, two_churches):
    with tenant_context(two_churches["grace"]):
        db.session.execute(update(Branch).values(city="Accra"))
        db.session.commit()
    with tenant_bypass():
        cities = dict(db.session.query(Branch.tenant_id, Branch.city))
    assert cities[two_churches["grace"]] == "Accra"
    assert cities[two_churches["word"]] is None


def test_relationship_lazy_load_is_scoped(db, two_churches):
    with tenant_context(two_churches["grace"]):
        account = db.session.query(TenantUser).one()
        assert account.user.email == "owner@grace.test"
        assert [r.slug for r in account.roles] == ["church_owner"]
        assert account.tenant.slug == "grace-chapel-international"


def test_platform_audit_rows_hidden_from_churches(db, two_churches):
    with tenant_bypass():
        audit.record("platform.settings_changed", tenant_id=None)
        db.session.commit()
    with tenant_context(two_churches["grace"]):
        actions = [a.action for a in db.session.query(AuditLog)]
        assert actions == ["tenant.provisioned"]
    with tenant_bypass():
        assert db.session.query(AuditLog).count() == 3


def test_tenant_context_inside_bypass_restores_isolation(db, two_churches):
    with tenant_bypass():
        with tenant_context(two_churches["grace"]):
            assert db.session.query(Branch).count() == 1
        assert db.session.query(Branch).count() == 2


def test_context_is_restored_after_block(two_churches):
    assert current_tenant_id() is None
    with tenant_context(two_churches["grace"]):
        with tenant_context(two_churches["word"]):
            assert current_tenant_id() == two_churches["word"]
        assert current_tenant_id() == two_churches["grace"]
    assert current_tenant_id() is None


def test_assert_same_tenant(db, two_churches):
    with tenant_bypass():
        a = db.session.query(Branch).filter_by(tenant_id=two_churches["grace"]).one()
        b = db.session.query(Branch).filter_by(tenant_id=two_churches["word"]).one()
    assert_same_tenant(a, a)
    with pytest.raises(TenantIsolationError):
        assert_same_tenant(a, b)


def test_tenant_state_does_not_leak_between_requests(app, client, two_churches):
    from app.utils.tenancy import set_current_tenant
    set_current_tenant(two_churches["grace"])  # simulate a leftover value in this worker

    @app.get("/_probe")
    def _probe():
        return {"tenant": current_tenant_id()}

    assert client.get("/_probe").get_json() == {"tenant": None}


def test_isolation_violation_in_request_returns_404(app, client, two_churches):
    @app.get("/_violate")
    def _violate():
        from app.extensions import db as _db
        _db.session.query(Branch).all()  # no tenant context -> blocked
        return "unreachable"

    res = client.get("/_violate")
    assert res.status_code == 404
