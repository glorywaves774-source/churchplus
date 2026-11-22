"""Tenant isolation engine.

ChurchPlus is fail-closed: every query against a tenant-owned model is
automatically filtered to the active tenant. If no tenant is active, the query
is refused rather than returning every church's data.

How it works
------------
* ``tenant_context(tenant_id)`` sets the active tenant for a block of code.
  The request lifecycle sets it from the signed-in user (Stage 3).
* A ``do_orm_execute`` hook adds ``WHERE tenant_id = :active`` to every ORM
  SELECT, UPDATE and DELETE that touches a tenant-owned model, including
  relationship lazy-loads and joined aliases.
* A ``load`` hook re-checks every loaded row (defence in depth).
* A ``before_flush`` hook stamps new rows with the active tenant and refuses
  writes that would cross tenants or move a row to another tenant.
* ``tenant_bypass()`` is the only way around the rules. It is reserved for
  platform jobs (provisioning, migrations, seeding, super-admin aggregates)
  and should never wrap code that renders church data to a user.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria


class TenantIsolationError(RuntimeError):
    """Raised when code would read or write data outside the active tenant."""


class TenantContextMissing(TenantIsolationError):
    """Raised when tenant-owned data is touched with no active tenant."""


_current_tenant: ContextVar[int | None] = ContextVar("churchplus_tenant", default=None)
_bypass: ContextVar[bool] = ContextVar("churchplus_tenant_bypass", default=False)


class TenantOwned:
    """Marker base for every model that belongs to a tenant.

    Concrete mixins (``TenantScopedMixin`` / ``OptionalTenantMixin``) live in
    ``app.models.base`` and define the ``tenant_id`` column.
    """

    __tenant_required__ = True


# --------------------------------------------------------------------------- #
# Context helpers
# --------------------------------------------------------------------------- #
def current_tenant_id() -> int | None:
    return _current_tenant.get()


def is_bypassed() -> bool:
    return _bypass.get()


def set_current_tenant(tenant_id: int | None):
    """Set the tenant for the current request. Returns a token for reset."""
    return _current_tenant.set(tenant_id)


def reset_tenant_state() -> None:
    """Clear tenant state. Called at the end of every request."""
    _current_tenant.set(None)
    _bypass.set(False)


@contextmanager
def tenant_context(tenant_id: int) -> Iterator[int]:
    if tenant_id is None:
        raise ValueError("tenant_context requires a tenant id")
    # Entering a church context always re-enables isolation, even inside a bypass.
    token = _current_tenant.set(int(tenant_id))
    bypass_token = _bypass.set(False)
    try:
        yield int(tenant_id)
    finally:
        _bypass.reset(bypass_token)
        _current_tenant.reset(token)


@contextmanager
def tenant_bypass() -> Iterator[None]:
    """Temporarily disable tenant scoping. Platform-level code only."""
    token = _bypass.set(True)
    try:
        yield
    finally:
        _bypass.reset(token)


def require_tenant_id() -> int:
    tid = current_tenant_id()
    if tid is None:
        raise TenantContextMissing("No active tenant. Wrap the call in tenant_context().")
    return tid


def assert_same_tenant(*objects) -> None:
    """Ensure related objects (e.g. a member and a branch) share one tenant."""
    ids = {getattr(o, "tenant_id", None) for o in objects if o is not None}
    if len(ids) > 1:
        raise TenantIsolationError("Objects belong to different churches.")


# --------------------------------------------------------------------------- #
# SQLAlchemy hooks
# --------------------------------------------------------------------------- #
def _touches_tenant_models(state) -> bool:
    try:
        mappers = state.all_mappers
    except Exception:  # pragma: no cover - defensive for exotic statements
        return False
    return any(issubclass(m.class_, TenantOwned) for m in mappers)


def _scope_queries(state) -> None:
    if not (state.is_select or state.is_update or state.is_delete):
        return
    if is_bypassed():
        return
    tid = current_tenant_id()
    if tid is None:
        if _touches_tenant_models(state):
            raise TenantContextMissing(
                "Refusing to query church data without an active tenant."
            )
        # Statement shapes we cannot inspect (e.g. counts over subqueries) fall
        # through to the criteria below with an id that matches no church.
        tid = -1
    # Always applied (it is a no-op for statements without tenant models), so
    # joins, subqueries, counts and eager loads are all covered.
    state.statement = state.statement.options(*[
        with_loader_criteria(
            model,
            lambda cls: cls.tenant_id == tid,
            include_aliases=True,
            track_closure_variables=True,
        )
        for model in _tenant_models(state.session)
    ])


_tenant_model_cache: tuple | None = None


def _tenant_models(session) -> tuple:
    global _tenant_model_cache
    if _tenant_model_cache is None:
        bind_mappers = []
        from app.extensions import db
        for mapper in db.Model.registry.mappers:
            if issubclass(mapper.class_, TenantOwned):
                bind_mappers.append(mapper.class_)
        _tenant_model_cache = tuple(bind_mappers)
    return _tenant_model_cache


def _check_flush(session: Session, flush_context, instances) -> None:
    if is_bypassed():
        # Platform code may write anywhere; NOT NULL constraints still apply.
        return

    tid = current_tenant_id()

    for obj in session.new:
        if not isinstance(obj, TenantOwned):
            continue
        related = getattr(obj, "tenant", None)
        if obj.tenant_id is None and related is not None:
            obj.tenant_id = related.id
        if obj.tenant_id is None:
            if tid is None:
                if obj.__tenant_required__:
                    raise TenantContextMissing(
                        f"Cannot create {type(obj).__name__} without an active tenant."
                    )
                continue
            obj.tenant_id = tid
        elif tid is None or obj.tenant_id != tid:
            raise TenantIsolationError(
                f"Cannot create {type(obj).__name__} for another church."
            )

    from sqlalchemy import inspect as sa_inspect

    for obj in session.dirty:
        if not isinstance(obj, TenantOwned):
            continue
        history = sa_inspect(obj).attrs.tenant_id.history
        if history.has_changes():
            raise TenantIsolationError("Records cannot be moved between churches.")
        if tid is None or obj.tenant_id != tid:
            raise TenantIsolationError(
                f"Cannot modify {type(obj).__name__} belonging to another church."
            )

    for obj in session.deleted:
        if isinstance(obj, TenantOwned) and (tid is None or obj.tenant_id != tid):
            raise TenantIsolationError(
                f"Cannot delete {type(obj).__name__} belonging to another church."
            )


def _check_loaded(target, context) -> None:
    if is_bypassed():
        return
    tid = current_tenant_id()
    if tid is None or target.tenant_id != tid:
        # Should be unreachable thanks to _scope_queries; kept as a safety net.
        raise TenantIsolationError(
            f"Blocked load of {type(target).__name__} from another church."
        )


_installed = False


def install_tenant_guards() -> None:
    """Register the hooks once per process."""
    global _installed
    if _installed:
        return
    event.listen(Session, "do_orm_execute", _scope_queries)
    event.listen(Session, "before_flush", _check_flush)
    event.listen(TenantOwned, "load", _check_loaded, propagate=True)
    _installed = True
