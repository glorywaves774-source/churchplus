"""Church dashboard: real figures from the modules that exist so far."""
from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, current_app, g, redirect, render_template, url_for

from app.extensions import db
from app.models import AuditLog, Branch, Subscription, SubscriptionStatus, TenantUser
from app.services import audit, limits
from app.services.denominations import is_headquarters, member_church_summaries
from app.utils.context import church_required

bp = Blueprint("dashboard", __name__)


def _greeting(now: datetime) -> str:
    return "Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 17 else "Good evening"


def _local_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(g.tenant.timezone))
    except Exception:
        return datetime.now(timezone.utc)


def _has(endpoint: str) -> bool:
    return endpoint in current_app.view_functions


@bp.get("/dashboard")
@church_required
def index():
    if g.account.is_portal_only:
        return redirect(url_for("portal.index"))
    t = g.tenant
    branches_active = db.session.query(Branch).filter_by(is_active=True).count()
    users = db.session.query(TenantUser).filter_by(is_suspended=False).count()
    sub = db.session.query(Subscription).order_by(Subscription.created_at.desc()).first()
    trial_days = None
    if sub and sub.status == SubscriptionStatus.TRIALING and sub.trial_ends_at:
        end = sub.trial_ends_at if sub.trial_ends_at.tzinfo else sub.trial_ends_at.replace(tzinfo=timezone.utc)
        trial_days = max(0, (end - datetime.now(timezone.utc)).days)

    activity = [audit.describe(a) for a in
                db.session.query(AuditLog).filter(*[~AuditLog.action.startswith(p) for p in audit.PRIVATE_PREFIXES])
                .order_by(AuditLog.created_at.desc()).limit(8)]

    can = g.account.can
    checklist = [
        {"label": "Complete your church profile", "done": bool(t.phone and (t.address_line1 or t.city)),
         "url": url_for("tenants.settings") if can("church.manage_settings") else None},
        {"label": "Upload your church logo", "done": bool(t.logo_key),
         "url": url_for("tenants.settings") if can("church.manage_settings") else None},
        {"label": "Add your branches", "done": branches_active > 1,
         "url": url_for("tenants.branches") if can("branches.view") else None},
    ]
    if _has("auth.users"):
        checklist.append({"label": "Invite your team", "done": users > 1,
                          "url": url_for("auth.users") if can("users.view") else None})
    if _has("members.index"):
        checklist.append({"label": "Register your members", "done": limits.check("members").used > 0,
                          "url": url_for("members.index") if can("members.view") else None})

    quick = []
    for label, endpoint, icon, perm in [
        ("Register member", "members.create", "user-plus", "members.create"),
        ("First-timer", "visitors.create", "sparkles", "visitors.create"),
        ("Take attendance", "attendance.index", "clipboard-check", "attendance.create"),
        ("Schedule service", "worship.create", "calendar-plus", "worship.create"),
        ("Record giving", "finance.record_giving", "receipt", "finance.create"),
        ("New event", "events.create", "calendar-plus", "events.create"),
        ("Prayer request", "prayer.create", "heart", "prayer.create"),
        ("Children check-in", "children.index", "log-in", "children.create"),
        ("Add branch", "tenants.branch_create", "building-2", "branches.create"),
        ("Church settings", "tenants.settings", "settings", "church.manage_settings"),
        ("Member churches", "tenants.member_churches", "church", "church.view"),
    ]:
        if _has(endpoint) and can(perm) and (endpoint != "tenants.member_churches" or is_headquarters(t)):
            quick.append({"label": label, "url": url_for(endpoint), "icon": icon})

    member_churches = member_church_summaries(t) if is_headquarters(t) else None
    member_stats = birthdays = None
    if _has("members.index") and can("members.view"):
        from app.services import members as member_svc
        member_stats = member_svc.stats()
        birthdays = member_svc.upcoming_birthdays()
    upcoming_events = None
    if _has("events.index") and can("events.view"):
        from datetime import datetime as _dt, time as _time
        from app.models import Event, EventStatus
        from app.services import ministry
        upcoming_events = (ministry.events_query().filter(Event.status == EventStatus.PUBLISHED,
                                                          Event.starts_at >= _dt.combine(_local_now().date(), _time.min))
                           .order_by(Event.starts_at).limit(5).all())
    return render_template(
        "dashboard/index.html", greeting=_greeting(_local_now()), branches_active=branches_active,
        users=users, sub=sub, trial_days=trial_days, activity=activity, checklist=checklist,
        quick=quick, member_churches=member_churches, branch_limit=limits.check("branches"),
        member_stats=member_stats, birthdays=birthdays, upcoming_events=upcoming_events,
        breadcrumbs=[("Overview", None), ("Dashboard", None)],
    )
