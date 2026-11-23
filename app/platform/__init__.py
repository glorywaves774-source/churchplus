"""ChurchPlus platform console for Kayron staff (Stage 10).

Shows churches, subscriptions, invoices and usage **counts** only. It never opens member
records, giving, prayer, counselling, children or safeguarding data: platform staff hold no
church roles, and nothing here queries those tables except to count members.

* Support admins can look; super admins can also approve, suspend, change plans and record payments.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from functools import wraps

from flask import Blueprint, current_app, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import func, or_
from sqlalchemy.orm.attributes import flag_modified

from app.extensions import db
from app.models import (
    BillingCycle, BillingPayment, Branch, Invoice, InvoiceStatus, Member, PaymentStatus, Plan, PlatformRole,
    Subscription, SubscriptionStatus, Tenant, TenantStatus, TenantUser,
)
from app.models.base import utcnow
from app.services import audit, billing
from app.services.billing import BillingError, aware
from app.utils.context import login_required
from app.utils.tenancy import tenant_bypass, tenant_context

bp = Blueprint("platform", __name__, url_prefix="/platform")


def platform_required(write: bool = False):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if not g.user.is_platform_staff:
                abort(403)
            if write and g.user.platform_role != PlatformRole.SUPER_ADMIN:
                abort(403)
            with tenant_bypass():
                return view(*args, **kwargs)
        return wrapped
    return decorator


def _latest_subs() -> dict[int, Subscription]:
    out = {}
    for s in db.session.query(Subscription).order_by(Subscription.created_at, Subscription.id):
        out[s.tenant_id] = s
    return out


@bp.get("")
@platform_required()
def index():
    tenants = db.session.query(Tenant).all()
    subs = _latest_subs()
    by_status = Counter(t.status.value for t in tenants)
    sub_status = Counter(s.status.value for s in subs.values())
    now = utcnow()
    trials = sorted([(t, subs[t.id]) for t in tenants if t.id in subs and subs[t.id].status == SubscriptionStatus.TRIALING
                     and aware(subs[t.id].trial_ends_at) and aware(subs[t.id].trial_ends_at) <= now + timedelta(days=7)],
                    key=lambda x: aware(x[1].trial_ends_at))
    overdue = (db.session.query(Invoice).filter(Invoice.status == InvoiceStatus.OPEN, Invoice.due_on < date.today())
               .order_by(Invoice.due_on).limit(20).all())
    month_ago = now - timedelta(days=30)
    revenue = (db.session.query(func.coalesce(func.sum(BillingPayment.amount), 0))
               .filter(BillingPayment.status == PaymentStatus.SUCCESS, BillingPayment.paid_at >= month_ago).scalar())
    months = [(date.today().replace(day=1) - timedelta(days=31 * i)).replace(day=1) for i in range(11, -1, -1)]
    signups = Counter((aware(t.created_at).year, aware(t.created_at).month) for t in tenants)
    chart = {"type": "bar", "labels": [m.strftime("%b %y") for m in months],
             "series": [{"label": "New churches", "data": [signups.get((m.year, m.month), 0) for m in months]}]}
    names = {t.id: t for t in tenants}
    pending = [t for t in tenants if t.status == TenantStatus.PENDING]
    from app.services.storage import config_problem
    storage_problem = config_problem(current_app.config)
    return render_template("platform/index.html", storage_problem=storage_problem,
                           storage_backend=current_app.config["STORAGE_BACKEND"],
                           storage_bucket=current_app.config.get("S3_BUCKET"), total=len(tenants), by_status=by_status, sub_status=sub_status,
                           mrr=billing.mrr(), revenue=Decimal(str(revenue or 0)), trials=trials, overdue=overdue,
                           names=names, chart=chart, pending=pending)


@bp.post("/storage-check")
@platform_required(write=True)
def storage_check():
    """Save, read back and delete a test file, and say in plain words what (if anything) is wrong."""
    from app.services.storage import check_round_trip
    ok, message = check_round_trip()
    audit.record("platform.storage_checked", actor=g.user, tenant_id=None, ok=ok)
    db.session.commit()
    flash(message, "success" if ok else "danger")
    return redirect(url_for("platform.index") + "#storage")


@bp.get("/churches")
@platform_required()
def churches():
    q = db.session.query(Tenant)
    text = request.args.get("q", "").strip()
    if text:
        q = q.filter(or_(Tenant.name.ilike(f"%{text}%"), Tenant.city.ilike(f"%{text}%"), Tenant.slug.ilike(f"%{text}%")))
    status = request.args.get("status", "")
    if status in {s.value for s in TenantStatus}:
        q = q.filter(Tenant.status == TenantStatus(status))
    rows = q.order_by(Tenant.created_at.desc()).limit(500).all()
    members = dict(db.session.query(Member.tenant_id, func.count(Member.id)).group_by(Member.tenant_id).all())
    subs = _latest_subs()
    return render_template("platform/churches.html", rows=rows, members=members, subs=subs, q=text, status=status,
                           statuses=list(TenantStatus))


@bp.get("/churches/<public_id>")
@platform_required()
def church(public_id: str):
    t = db.session.query(Tenant).filter_by(public_id=public_id).first_or_404()
    sub = (db.session.query(Subscription).filter_by(tenant_id=t.id).order_by(Subscription.created_at.desc(), Subscription.id.desc()).first())
    usage = {
        "Members": db.session.query(func.count(Member.id)).filter(Member.tenant_id == t.id).scalar(),
        "Staff accounts": sum(1 for a in db.session.query(TenantUser).filter_by(tenant_id=t.id, is_suspended=False) if a.permission_codes()),
        "Portal accounts": db.session.query(func.count(TenantUser.id)).filter(TenantUser.tenant_id == t.id, TenantUser.member_id.isnot(None)).scalar(),
        "Branches": db.session.query(func.count(Branch.id)).filter(Branch.tenant_id == t.id, Branch.is_active.is_(True)).scalar(),
    }
    owners = [a.user for a in db.session.query(TenantUser).filter_by(tenant_id=t.id, is_owner=True)]
    invoices = db.session.query(Invoice).filter_by(tenant_id=t.id).order_by(Invoice.created_at.desc()).limit(50).all()
    plans = db.session.query(Plan).filter_by(is_active=True).order_by(Plan.sort_order).all()
    return render_template("platform/church.html", t=t, sub=sub, usage=usage, owners=owners, invoices=invoices, plans=plans,
                           can_write=g.user.platform_role == PlatformRole.SUPER_ADMIN, today=date.today())


def _tenant(public_id):
    return db.session.query(Tenant).filter_by(public_id=public_id).first_or_404()


@bp.post("/churches/<public_id>/action")
@platform_required(write=True)
def church_action(public_id: str):
    t = _tenant(public_id)
    action = request.form.get("action")
    with tenant_context(t.id):
        t = db.session.get(Tenant, t.id)
        sub = billing.current_subscription()
        if action == "approve" and t.status == TenantStatus.PENDING:
            t.status = TenantStatus.TRIAL if sub and sub.status == SubscriptionStatus.TRIALING else TenantStatus.ACTIVE
            msg = "Church approved."
        elif action == "suspend" and t.status != TenantStatus.SUSPENDED:
            reason = (request.form.get("reason") or "").strip()[:200]
            if not reason:
                flash("Give a reason for suspending.", "danger")
                return redirect(url_for("platform.church", public_id=public_id))
            t.status = TenantStatus.SUSPENDED
            data = dict(t.settings or {})
            data["suspended_reason"] = reason
            t.settings = data
            flag_modified(t, "settings")
            msg = "Church suspended."
        elif action == "reactivate" and t.status == TenantStatus.SUSPENDED:
            t.status = TenantStatus.TRIAL if sub and sub.status == SubscriptionStatus.TRIALING else TenantStatus.ACTIVE
            data = dict(t.settings or {})
            data.pop("suspended_reason", None)
            t.settings = data
            flag_modified(t, "settings")
            if sub and sub.status == SubscriptionStatus.SUSPENDED:
                sub.status = SubscriptionStatus.PAST_DUE
                sub.grace_period_ends_at = utcnow() + timedelta(days=7)
            msg = "Church reactivated."
        elif action == "extend_trial" and sub is not None:
            days = max(1, min(request.form.get("days", 14, type=int), 90))
            base = max(aware(sub.trial_ends_at) or utcnow(), utcnow())
            sub.trial_ends_at = base + timedelta(days=days)
            sub.current_period_end = sub.trial_ends_at
            sub.status = SubscriptionStatus.TRIALING
            if t.status == TenantStatus.SUSPENDED and (t.settings or {}).get("suspended_reason") == "billing":
                t.status = TenantStatus.TRIAL
            msg = f"Trial extended by {days} days."
        elif action == "change_plan" and sub is not None:
            plan = db.session.query(Plan).filter_by(public_id=request.form.get("plan")).first_or_404()
            sub.plan_id = plan.id
            msg = f"Plan changed to {plan.name}."
        else:
            abort(400)
        audit.record(f"platform.{action}", entity=t, actor=g.user, tenant_id=t.id, name=t.name,
                     reason=request.form.get("reason") or None)
        db.session.commit()
    flash(msg, "success")
    return redirect(url_for("platform.church", public_id=public_id))


@bp.post("/churches/<public_id>/invoice")
@platform_required(write=True)
def create_invoice(public_id: str):
    t = _tenant(public_id)
    with tenant_context(t.id):
        sub = billing.current_subscription()
        plan = db.session.query(Plan).filter_by(public_id=request.form.get("plan")).first_or_404()
        try:
            cycle = BillingCycle(request.form.get("cycle", "monthly"))
            amount = Decimal(request.form.get("amount") or "0") if request.form.get("amount") else None
            if amount is not None and amount <= 0:
                raise ValueError
        except (ValueError, ArithmeticError):
            flash("Check the amount.", "danger")
            return redirect(url_for("platform.church", public_id=public_id))
        if amount is None and billing.price(plan, cycle) is None:
            flash("Enter the agreed amount for custom pricing.", "danger")
            return redirect(url_for("platform.church", public_id=public_id))
        inv = billing.create_invoice(sub, plan, cycle, date.today(), amount=amount,
                                     note=(request.form.get("note") or "").strip()[:200] or None)
        audit.record("platform.invoice_created", entity=inv, actor=g.user, tenant_id=t.id, name=inv.number)
        db.session.commit()
    flash(f"Invoice {inv.number} created.", "success")
    return redirect(url_for("platform.church", public_id=public_id))


@bp.post("/invoices/<public_id>/<action>")
@platform_required(write=True)
def invoice_action(public_id: str, action: str):
    inv = db.session.query(Invoice).filter_by(public_id=public_id).first_or_404()
    with tenant_context(inv.tenant_id):
        inv = db.session.get(Invoice, inv.id)
        if inv.status != InvoiceStatus.OPEN:
            flash("Only open invoices can be changed.", "warning")
        elif action == "mark_paid":
            ref = (request.form.get("reference") or "").strip()[:100]
            if not ref:
                flash("Enter the bank or MoMo reference.", "danger")
            else:
                pay = BillingPayment(invoice_id=inv.id, provider="manual", reference=ref, amount=inv.amount,
                                     currency=inv.currency, status=PaymentStatus.SUCCESS, paid_at=utcnow(),
                                     channel=(request.form.get("channel") or "bank")[:30], details={"recorded_by": g.user.email})
                db.session.add(pay)
                db.session.flush()
                billing.mark_paid(inv, pay, actor=g.user)
                flash(f"{inv.number} marked as paid.", "success")
        elif action == "void":
            inv.status = InvoiceStatus.VOID
            inv.note = (request.form.get("reason") or "Voided by ChurchPlus")[:255]
            audit.record("platform.invoice_voided", entity=inv, actor=g.user, tenant_id=inv.tenant_id, name=inv.number)
            flash(f"{inv.number} voided.", "success")
        else:
            abort(400)
        db.session.commit()
        tpid = db.session.get(Tenant, inv.tenant_id).public_id
    return redirect(request.form.get("next") if (request.form.get("next") or "").startswith("/platform") else url_for("platform.church", public_id=tpid))


@bp.get("/invoices")
@platform_required()
def invoices():
    q = db.session.query(Invoice)
    status = request.args.get("status", "open")
    if status in {s.value for s in InvoiceStatus}:
        q = q.filter(Invoice.status == InvoiceStatus(status))
    rows = q.order_by(Invoice.created_at.desc()).limit(300).all()
    names = {t.id: t for t in db.session.query(Tenant).filter(Tenant.id.in_({i.tenant_id for i in rows} or {-1}))}
    payments = (db.session.query(BillingPayment).order_by(BillingPayment.created_at.desc()).limit(50).all())
    return render_template("platform/invoices.html", rows=rows, names=names, status=status, payments=payments,
                           can_write=g.user.platform_role == PlatformRole.SUPER_ADMIN)


@bp.route("/plans", methods=["GET", "POST"])
@platform_required()
def plans():
    if request.method == "POST":
        if g.user.platform_role != PlatformRole.SUPER_ADMIN:
            abort(403)
        p = db.session.query(Plan).filter_by(public_id=request.form.get("plan")).first_or_404()
        f = request.form

        def num(name, cast=int):
            v = (f.get(name) or "").strip()
            return cast(v) if v else None
        try:
            p.monthly_price, p.annual_price = num("monthly_price", Decimal), num("annual_price", Decimal)
            p.max_users, p.max_members, p.max_branches = num("max_users"), num("max_members"), num("max_branches")
            p.trial_days = num("trial_days") or 14
        except (ValueError, ArithmeticError):
            flash("Prices and limits must be numbers.", "danger")
            return redirect(url_for("platform.plans"))
        p.is_active, p.is_public = f.get("is_active") == "y", f.get("is_public") == "y"
        p.description = (f.get("description") or "").strip()[:255] or None
        audit.record("platform.plan_updated", entity=p, actor=g.user, tenant_id=None, name=p.name)
        db.session.commit()
        flash(f"{p.name} saved. New prices apply to the next invoice.", "success")
        return redirect(url_for("platform.plans"))
    rows = db.session.query(Plan).order_by(Plan.sort_order).all()
    counts = Counter(s.plan_id for s in _latest_subs().values() if s.status != SubscriptionStatus.CANCELLED)
    return render_template("platform/plans.html", rows=rows, counts=counts,
                           can_write=g.user.platform_role == PlatformRole.SUPER_ADMIN)
