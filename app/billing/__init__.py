"""Church-side subscription page, online payment and payment-provider webhooks."""
from __future__ import annotations

from functools import wraps

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for

from app.extensions import csrf, db, limiter
from app.models import BillingCycle, BillingPayment, Invoice, InvoiceStatus, PaymentStatus, Plan, TenantStatus
from app.services import audit, billing, limits
from app.services.billing import BillingError
from app.utils.context import login_required
from app.utils.tenancy import tenant_context

bp = Blueprint("billing", __name__)


def billing_access(code: str):
    """Like ``permission_required`` but also open while the church is suspended for non-payment,
    so the owner can pay and reopen the workspace."""
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if g.account is None:
                return redirect(url_for("auth.choose_church"))
            if g.tenant.status == TenantStatus.PENDING:
                return redirect(url_for("tenants.pending"))
            billing_hold = g.tenant.status == TenantStatus.SUSPENDED and (g.tenant.settings or {}).get("suspended_reason") == "billing"
            if g.tenant.status in (TenantStatus.SUSPENDED, TenantStatus.CANCELLED) and not billing_hold:
                return redirect(url_for("tenants.unavailable"))
            if g.account.is_suspended or not g.account.can(code):
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def _usage():
    return {k: limits.check(k) for k in ("users", "members", "branches")}


@bp.get("/billing")
@billing_access("billing.view")
def index():
    sub = billing.current_subscription()
    invoices = db.session.query(Invoice).order_by(Invoice.created_at.desc()).limit(36).all()
    plans = db.session.query(Plan).filter_by(is_active=True, is_public=True).order_by(Plan.sort_order).all()
    return render_template("billing/index.html", sub=sub, invoices=invoices, plans=plans, usage=_usage(),
                           providers=billing.providers(), price=billing.price,
                           breadcrumbs=[("Administration", None), ("Subscription", None)])


@bp.post("/billing/plan")
@billing_access("billing.manage_settings")
@limiter.limit("20 per hour")
def choose_plan():
    plan = db.session.query(Plan).filter_by(public_id=request.form.get("plan"), is_active=True).first_or_404()
    try:
        cycle = BillingCycle(request.form.get("cycle", "monthly"))
    except ValueError:
        abort(400)
    sub = billing.current_subscription()
    if sub is None:
        abort(400)
    for name, lim in _usage().items():
        cap = getattr(plan, f"max_{name}")
        if cap is not None and lim.used > cap:
            flash(f"{plan.name} allows {cap} {name}; you have {lim.used}. Reduce them first or choose a bigger plan.", "danger")
            return redirect(url_for("billing.index"))
    for inv in billing.open_invoices():
        inv.status = InvoiceStatus.VOID
        inv.note = "Replaced by a plan change"
    from datetime import date
    try:
        inv = billing.create_invoice(sub, plan, cycle, date.today())
    except BillingError as e:
        flash(str(e) + " Contact ChurchPlus to arrange it.", "info")
        db.session.rollback()
        return redirect(url_for("billing.index"))
    audit.record("billing.plan_chosen", entity=sub, actor=g.user, name=plan.name, cycle=cycle.value)
    db.session.commit()
    flash(f"Invoice {inv.number} created for {plan.name}. Pay it to switch plans.", "success")
    return redirect(url_for("billing.invoice", public_id=inv.public_id))


@bp.get("/billing/invoices/<public_id>")
@billing_access("billing.view")
def invoice(public_id: str):
    inv = db.session.query(Invoice).filter_by(public_id=public_id).first_or_404()
    return render_template("billing/invoice.html", inv=inv, providers=billing.providers(),
                           breadcrumbs=[("Subscription", url_for("billing.index")), (inv.number, None)])


@bp.post("/billing/invoices/<public_id>/pay")
@billing_access("billing.manage_settings")
@limiter.limit("30 per hour")
def pay(public_id: str):
    inv = db.session.query(Invoice).filter_by(public_id=public_id).first_or_404()
    base = (current_app.config.get("PUBLIC_BASE_URL") or request.host_url).rstrip("/")
    try:
        co = billing.start_checkout(inv, g.tenant, request.form.get("provider", ""),
                                    callback_url=base + url_for("billing.returned"),
                                    cancel_url=base + url_for("billing.invoice", public_id=public_id))
    except BillingError as e:
        db.session.rollback()
        flash(str(e), "danger")
        return redirect(url_for("billing.invoice", public_id=public_id))
    audit.record("billing.checkout_started", entity=inv, actor=g.user, name=inv.number, provider=co.payment.provider)
    db.session.commit()
    return redirect(co.url)


@bp.get("/billing/return")
@login_required
def returned():
    """Where the provider sends the browser back. We verify with the provider before trusting it."""
    ref = request.args.get("reference") or request.args.get("trxref") or request.args.get("clientReference") or ""
    pay = None
    for provider in ("paystack", "hubtel", "test"):
        pay = billing.payment_by_reference(provider, ref)
        if pay:
            break
    if pay is None or g.account is None or pay.tenant_id != g.tenant.id:
        flash("We could not find that payment.", "warning")
        return redirect(url_for("billing.index") if g.account else url_for("core.index"))
    with tenant_context(pay.tenant_id):
        pay = db.session.get(BillingPayment, pay.id)
        ok = billing.verify(pay)
        db.session.commit()
        inv_id = pay.invoice.public_id
    flash("Payment received. Thank you!" if ok else "We have not received confirmation of this payment yet. "
          "If money left your account, it will show here within a few minutes.", "success" if ok else "warning")
    return redirect(url_for("billing.invoice", public_id=inv_id))


# --------------------------------------------------------------------------- #
# Test checkout (development and automated tests only)
# --------------------------------------------------------------------------- #
@bp.route("/billing/test-checkout/<reference>", methods=["GET", "POST"])
@login_required
def test_checkout(reference: str):
    if not current_app.config.get("BILLING_TEST_CHECKOUT"):
        abort(404)
    pay = billing.payment_by_reference("test", reference)
    if pay is None or g.account is None or pay.tenant_id != g.tenant.id:
        abort(404)
    if request.method == "POST":
        with tenant_context(pay.tenant_id):
            pay = db.session.get(BillingPayment, pay.id)
            pay.details = {**(pay.details or {}), "test_result": "success" if request.form.get("result") == "success" else "failed"}
            db.session.commit()
        return redirect(url_for("billing.returned", reference=reference))
    return render_template("billing/test_checkout.html", pay=pay)


# --------------------------------------------------------------------------- #
# Webhooks (server to server; no session, no CSRF – authenticity is checked instead)
# --------------------------------------------------------------------------- #
@bp.post("/webhooks/paystack")
@csrf.exempt
@limiter.limit("120 per minute")
def paystack_webhook():
    raw = request.get_data()
    if not billing.paystack_signature_ok(raw, request.headers.get("X-Paystack-Signature")):
        return jsonify({"ok": False}), 401
    event = request.get_json(silent=True) or {}
    if event.get("event") == "charge.success":
        pay = billing.payment_by_reference("paystack", (event.get("data") or {}).get("reference", ""))
        if pay is not None:
            with tenant_context(pay.tenant_id):
                pay = db.session.get(BillingPayment, pay.id)
                billing.verify(pay)
                db.session.commit()
    return jsonify({"ok": True})


@bp.post("/webhooks/hubtel")
@csrf.exempt
@limiter.limit("120 per minute")
def hubtel_webhook():
    data = (request.get_json(silent=True) or {}).get("Data") or {}
    pay = billing.payment_by_reference("hubtel", str(data.get("ClientReference", "")))
    if pay is not None:
        with tenant_context(pay.tenant_id):
            pay = db.session.get(BillingPayment, pay.id)
            billing.verify(pay)            # Hubtel callbacks are unsigned: confirm with Hubtel's status API
            db.session.commit()
    return jsonify({"ok": True})
