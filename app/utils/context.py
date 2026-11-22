"""Per-request identity: who is signed in and which church they are working in.

Order on every request:
1. tenant state is cleared (``app/__init__.py``);
2. ``load_request_context`` finds the signed-in user's account in the church
   chosen in their session, and activates that church for the request.

A user can only ever activate a church they hold an active account in; the
session only stores *which* of their churches is selected.
"""
from __future__ import annotations

from functools import wraps

from flask import current_app, abort, flash, g, redirect, request, session, url_for
from flask_login import current_user

from app.extensions import db
from app.models import Tenant, TenantStatus, TenantUser
from app.utils.tenancy import set_current_tenant, tenant_bypass

SESSION_TENANT_KEY = "cp_tenant"


def accounts_for(user) -> list[TenantUser]:
    """All church accounts a user holds (platform-level lookup)."""
    with tenant_bypass():
        return (db.session.query(TenantUser).join(Tenant, Tenant.id == TenantUser.tenant_id)
                .filter(TenantUser.user_id == user.id)
                .order_by(Tenant.name).all())


def select_church(tenant_id: int | None) -> None:
    if tenant_id is None:
        session.pop(SESSION_TENANT_KEY, None)
    else:
        session[SESSION_TENANT_KEY] = int(tenant_id)


def load_request_context() -> None:
    g.user = None
    g.tenant = None
    g.account = None
    if not current_user.is_authenticated:
        return
    user = current_user._get_current_object()
    if not user.is_active:                      # suspended after signing in
        from flask_login import logout_user
        logout_user()
        session.clear()
        return
    g.user = user
    tenant_id = session.get(SESSION_TENANT_KEY)
    if tenant_id is None:
        return
    with tenant_bypass():
        account = (db.session.query(TenantUser)
                   .filter_by(tenant_id=int(tenant_id), user_id=g.user.id).one_or_none())
        tenant = db.session.get(Tenant, int(tenant_id)) if account else None
    if account is None or tenant is None:
        select_church(None)
        return
    g.tenant = tenant
    g.account = account
    set_current_tenant(tenant.id)


# --------------------------------------------------------------------------- #
# View decorators
# --------------------------------------------------------------------------- #
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            if request.method == "GET":
                session["cp_next"] = request.full_path if request.query_string else request.path
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)
    return wrapped


def church_required(view):
    """Signed in, a church selected, the account usable and the church operational."""
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if g.account is None:
            return redirect(url_for("auth.choose_church"))
        if g.account.is_suspended:
            flash("Your account in this church has been suspended. Contact your church administrator.", "danger")
            return redirect(url_for("auth.choose_church"))
        if g.tenant.status == TenantStatus.PENDING:
            return redirect(url_for("tenants.pending"))
        if g.tenant.status in (TenantStatus.SUSPENDED, TenantStatus.CANCELLED):
            return redirect(url_for("tenants.unavailable"))
        return view(*args, **kwargs)
    return wrapped


def permission_required(*codes: str):
    """Require every listed permission in the active church."""
    def decorator(view):
        @wraps(view)
        @church_required
        def wrapped(*args, **kwargs):
            if not all(g.account.can(c) for c in codes):
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def _billing_notice():
    from datetime import timedelta
    from app.models import Subscription, SubscriptionStatus
    from app.models.base import utcnow
    sub = db.session.query(Subscription).order_by(Subscription.created_at.desc(), Subscription.id.desc()).first()
    if sub is None:
        return None
    def aware(d):
        from datetime import timezone
        return d.replace(tzinfo=timezone.utc) if d is not None and d.tzinfo is None else d
    if sub.status == SubscriptionStatus.PAST_DUE:
        when = aware(sub.grace_period_ends_at)
        return {"kind": "danger", "text": "Your ChurchPlus payment is due" + (f". Please pay by {when:%d %b} to avoid interruption." if when else "."),
                "url": url_for("billing.index")}
    if sub.status == SubscriptionStatus.TRIALING and sub.trial_ends_at and aware(sub.trial_ends_at) - utcnow() <= timedelta(days=5):
        return {"kind": "info", "text": f"Your free trial ends on {aware(sub.trial_ends_at):%d %b}. Choose a plan to keep going.",
                "url": url_for("billing.index")}
    return None


def shell_context() -> dict:
    """Identity shown in the app shell (sidebar chip and profile menu)."""
    if not getattr(g, "user", None):
        return {}
    ctx = {}
    links = [("My account", url_for("auth.account"), "user-round")]
    if g.get("account") is not None:
        roles = ", ".join(r.name for r in g.account.roles) or "Member"
        branch = g.account.branch.name if g.account.branch else None
        ctx["shell_church"] = {
            "name": g.tenant.name,
            "subtitle": " · ".join(x for x in (branch.replace(f"{g.tenant.name} - ", "") if branch else None, g.tenant.city) if x) or "Church workspace",
        }
        if g.account.can("church.manage_settings"):
            links.append(("Church settings", url_for("tenants.settings"), "settings"))
        if g.account.can("billing.view") and "billing.index" in current_app.view_functions:
            ctx["billing_notice"] = _billing_notice()
        role_label = roles
    else:
        role_label = "Platform staff" if g.user.is_platform_staff else None
    if "cp_accounts" not in g:
        g.cp_accounts = accounts_for(g.user)
    if len(g.cp_accounts) > 1:
        links.append(("Switch church", url_for("auth.choose_church"), "repeat"))
    ctx["shell_user"] = {"name": g.user.full_name, "email": g.user.email, "role": role_label,
                         "links": links, "logout_url": url_for("auth.logout")}
    return ctx
