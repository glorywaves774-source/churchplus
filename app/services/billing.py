"""ChurchPlus subscription billing (Stage 10).

* Invoices are created for trials that end and periods that renew (``billing_run``, daily).
* Churches pay online through **Paystack** (cards, mobile money) or **Hubtel** (mobile money,
  cards). ChurchPlus never sees or stores card numbers: we create a checkout, the provider
  collects the money, and we **verify the payment with the provider** before marking an
  invoice paid (webhooks are also signature-checked for Paystack).
* Unpaid invoices: the subscription becomes *past due* with a grace period; after it, the
  church workspace is suspended (members' data is kept) until the invoice is paid.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from flask import current_app
from sqlalchemy.orm.attributes import flag_modified

from app.extensions import db
from app.models import (
    BillingCycle, BillingPayment, Invoice, InvoiceStatus, PaymentStatus, Plan, Subscription, SubscriptionStatus,
    Tenant, TenantStatus,
)
from app.models.base import utcnow
from app.services import audit
from app.utils.tenancy import tenant_bypass, tenant_context


class BillingError(ValueError):
    pass


def aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def price(plan: Plan, cycle: BillingCycle | str) -> Decimal | None:
    cycle = BillingCycle(cycle)
    if cycle == BillingCycle.ANNUAL:
        return plan.annual_price if plan.annual_price is not None else (plan.monthly_price * 10 if plan.monthly_price else None)
    return plan.monthly_price


def period_after(start: date, cycle: BillingCycle) -> date:
    """End date (inclusive) of a period that starts on ``start``."""
    months = 12 if cycle == BillingCycle.ANNUAL else 1
    y, m = divmod(start.month - 1 + months, 12)
    try:
        nxt = start.replace(year=start.year + y, month=m + 1)
    except ValueError:                                   # 31st → shorter month
        nxt = (start.replace(day=1, year=start.year + y, month=m + 1) + timedelta(days=31)).replace(day=1) - timedelta(days=1)
        return nxt
    return nxt - timedelta(days=1)


def current_subscription() -> Subscription | None:
    return db.session.query(Subscription).order_by(Subscription.created_at.desc(), Subscription.id.desc()).first()


def create_invoice(sub: Subscription, plan: Plan, cycle: BillingCycle, start: date, *, note: str | None = None,
                   due_in_days: int = 7, amount: Decimal | None = None) -> Invoice:
    amount = amount if amount is not None else price(plan, cycle)
    if amount is None:
        raise BillingError(f"{plan.name} has custom pricing. Create the invoice from the platform console.")
    end = period_after(start, cycle)
    inv = Invoice(subscription_id=sub.id, plan_id=plan.id, billing_cycle=cycle.value, currency=plan.currency,
                  amount=amount, period_start=start, period_end=end, due_on=date.today() + timedelta(days=due_in_days),
                  description=f"ChurchPlus {plan.name} ({cycle.value}) {start:%d %b %Y} – {end:%d %b %Y}", note=note)
    db.session.add(inv)
    db.session.flush()
    inv.number = f"INV-{date.today():%Y}-{inv.id:06d}"
    audit.record("billing.invoice_created", entity=inv, actor=None, name=inv.number, amount=str(inv.amount))
    return inv


def open_invoices():
    return db.session.query(Invoice).filter(Invoice.status == InvoiceStatus.OPEN).order_by(Invoice.due_on)


# --------------------------------------------------------------------------- #
# Applying a confirmed payment
# --------------------------------------------------------------------------- #
def mark_paid(inv: Invoice, payment: BillingPayment | None = None, *, actor=None) -> None:
    """Idempotent: a paid invoice stays paid. Extends the subscription and reopens the church."""
    if inv.status == InvoiceStatus.PAID:
        return
    inv.status, inv.paid_at = InvoiceStatus.PAID, utcnow()
    sub = db.session.get(Subscription, inv.subscription_id) if inv.subscription_id else current_subscription()
    if sub is not None:
        sub.plan_id = inv.plan_id
        sub.billing_cycle = BillingCycle(inv.billing_cycle)
        sub.status = SubscriptionStatus.ACTIVE
        start = datetime.combine(inv.period_start, datetime.min.time(), tzinfo=timezone.utc)
        end = datetime.combine(inv.period_end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        if aware(sub.current_period_end) is None or aware(sub.current_period_end) < end:
            sub.current_period_start, sub.current_period_end = start, end
        sub.grace_period_ends_at = None
    with tenant_bypass():
        t = db.session.get(Tenant, inv.tenant_id)
        if t.status == TenantStatus.TRIAL or (t.status == TenantStatus.SUSPENDED and (t.settings or {}).get("suspended_reason") == "billing"):
            t.status = TenantStatus.ACTIVE
            data = dict(t.settings or {})
            data.pop("suspended_reason", None)
            t.settings = data
            flag_modified(t, "settings")
    audit.record("billing.invoice_paid", entity=inv, actor=actor, tenant_id=inv.tenant_id, name=inv.number,
                 provider=getattr(payment, "provider", "manual"), amount=str(inv.amount))


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #
@dataclass
class Checkout:
    payment: BillingPayment
    url: str


def _http(url, *, data=None, headers=None, method="POST"):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Content-Type": "application/json", "Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=25) as res:          # noqa: S310 - fixed provider URLs
        return json.loads(res.read().decode() or "{}")


def _hubtel_auth() -> str:
    cfg = current_app.config
    return "Basic " + base64.b64encode(f"{cfg['HUBTEL_PAY_CLIENT_ID']}:{cfg['HUBTEL_PAY_CLIENT_SECRET']}".encode()).decode()


def providers() -> list[str]:
    cfg = current_app.config
    out = []
    if "paystack" in cfg["BILLING_PROVIDERS"] and cfg.get("PAYSTACK_SECRET_KEY"):
        out.append("paystack")
    if "hubtel" in cfg["BILLING_PROVIDERS"] and cfg.get("HUBTEL_PAY_CLIENT_ID") and cfg.get("HUBTEL_MERCHANT_ACCOUNT"):
        out.append("hubtel")
    if cfg.get("BILLING_TEST_CHECKOUT"):
        out.append("test")
    return out


def billing_email(tenant: Tenant) -> str:
    """The church's own office email for payment receipts – never a person's sign-in email."""
    return tenant.email or f"billing+{tenant.public_id[:12]}@churchplus.app"


def start_checkout(inv: Invoice, tenant: Tenant, provider: str, *, callback_url: str, cancel_url: str) -> Checkout:
    if inv.status != InvoiceStatus.OPEN:
        raise BillingError("This invoice is not open.")
    if provider not in providers():
        raise BillingError("That payment method is not available.")
    ref = f"CP{inv.id}-{secrets.token_hex(6)}"
    pay = BillingPayment(invoice_id=inv.id, provider=provider, reference=ref, amount=inv.amount, currency=inv.currency)
    db.session.add(pay)
    db.session.flush()
    cfg = current_app.config
    try:
        if provider == "paystack":
            res = _http("https://api.paystack.co/transaction/initialize",
                        headers={"Authorization": f"Bearer {cfg['PAYSTACK_SECRET_KEY']}"},
                        data={"email": billing_email(tenant), "amount": int(inv.amount * 100), "currency": inv.currency,
                              "reference": ref, "callback_url": callback_url,
                              "metadata": {"invoice": inv.number, "church": tenant.slug, "cancel_action": cancel_url}})
            if not res.get("status"):
                raise BillingError(res.get("message") or "Paystack could not start the payment.")
            return Checkout(pay, res["data"]["authorization_url"])
        if provider == "hubtel":
            res = _http("https://payproxyapi.hubtel.com/items/initiate", headers={"Authorization": _hubtel_auth()},
                        data={"totalAmount": float(inv.amount), "description": inv.description[:100],
                              "callbackUrl": callback_url.split("?")[0].rsplit("/billing", 1)[0] + "/webhooks/hubtel",
                              "returnUrl": callback_url, "cancellationUrl": cancel_url,
                              "merchantAccountNumber": cfg["HUBTEL_MERCHANT_ACCOUNT"], "clientReference": ref})
            url = (res.get("data") or {}).get("checkoutUrl")
            if not url:
                raise BillingError(res.get("message") or "Hubtel could not start the payment.")
            return Checkout(pay, url)
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as e:
        if isinstance(e, BillingError):
            raise
        raise BillingError(f"The payment service did not respond ({type(e).__name__}). Please try again.") from e
    from flask import url_for
    return Checkout(pay, url_for("billing.test_checkout", reference=ref))


def verify(pay: BillingPayment) -> bool:
    """Ask the provider whether the payment really succeeded for the right amount."""
    cfg = current_app.config
    if pay.status == PaymentStatus.SUCCESS:
        return True
    ok, channel = False, None
    try:
        if pay.provider == "paystack":
            res = _http(f"https://api.paystack.co/transaction/verify/{pay.reference}", method="GET",
                        headers={"Authorization": f"Bearer {cfg['PAYSTACK_SECRET_KEY']}"})
            d = res.get("data") or {}
            ok = d.get("status") == "success" and int(d.get("amount", 0)) == int(pay.amount * 100) and d.get("currency") == pay.currency
            channel = d.get("channel")
        elif pay.provider == "hubtel":
            res = _http(f"https://api-txnstatus.hubtel.com/transactions/{cfg['HUBTEL_MERCHANT_ACCOUNT']}/status"
                        f"?clientReference={pay.reference}", method="GET", headers={"Authorization": _hubtel_auth()})
            d = res.get("data") or {}
            ok = str(d.get("status", "")).lower() == "paid" and Decimal(str(d.get("amount", "0"))) >= pay.amount
            channel = d.get("paymentMethod")
        elif pay.provider == "test":
            ok = (pay.details or {}).get("test_result") == "success" and bool(cfg.get("BILLING_TEST_CHECKOUT"))
            channel = "test"
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        current_app.logger.warning("Payment verification failed for %s: %s", pay.reference, e)
        return False
    if ok:
        pay.status, pay.paid_at, pay.channel = PaymentStatus.SUCCESS, utcnow(), (channel or "")[:30] or None
        mark_paid(pay.invoice, pay)
    elif pay.provider == "test" or (pay.details or {}).get("final"):
        pay.status = PaymentStatus.FAILED
    return ok


def paystack_signature_ok(raw: bytes, signature: str | None) -> bool:
    secret = current_app.config.get("PAYSTACK_SECRET_KEY")
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature)


def payment_by_reference(provider: str, reference: str) -> BillingPayment | None:
    with tenant_bypass():
        return db.session.query(BillingPayment).filter_by(provider=provider, reference=reference).first()


# --------------------------------------------------------------------------- #
# Daily billing run
# --------------------------------------------------------------------------- #
def suspend_for_billing(t: Tenant) -> None:
    t.status = TenantStatus.SUSPENDED
    data = dict(t.settings or {})
    data["suspended_reason"] = "billing"
    t.settings = data
    flag_modified(t, "settings")


def billing_run(now: datetime | None = None) -> dict:
    now = now or utcnow()
    grace = timedelta(days=current_app.config["BILLING_GRACE_DAYS"])
    stats = {"invoiced": 0, "past_due": 0, "suspended": 0}
    with tenant_bypass():
        tenant_ids = [t.id for t in db.session.query(Tenant).filter(
            Tenant.status.in_([TenantStatus.TRIAL, TenantStatus.ACTIVE, TenantStatus.SUSPENDED]))]
    for tid in tenant_ids:
        with tenant_context(tid):
            t = db.session.get(Tenant, tid)
            sub = current_subscription()
            if sub is None or sub.status == SubscriptionStatus.CANCELLED:
                continue
            plan = sub.plan
            due_from = None
            if sub.status == SubscriptionStatus.TRIALING and aware(sub.trial_ends_at) and aware(sub.trial_ends_at) <= now:
                due_from = aware(sub.trial_ends_at).date()
            elif sub.status == SubscriptionStatus.ACTIVE and aware(sub.current_period_end) and aware(sub.current_period_end) <= now:
                due_from = aware(sub.current_period_end).date()
            if due_from is not None:
                if price(plan, sub.billing_cycle) is None:
                    continue                    # custom pricing: invoiced by hand
                exists = db.session.query(Invoice.id).filter(Invoice.period_start == due_from,
                                                             Invoice.status != InvoiceStatus.VOID).first()
                if not exists:
                    create_invoice(sub, plan, sub.billing_cycle, due_from, due_in_days=current_app.config["BILLING_GRACE_DAYS"])
                    stats["invoiced"] += 1
                sub.status = SubscriptionStatus.PAST_DUE
                sub.grace_period_ends_at = now + grace
                audit.record("billing.past_due", entity=sub, actor=None, name=t.name)
                stats["past_due"] += 1
            elif (sub.status == SubscriptionStatus.PAST_DUE and aware(sub.grace_period_ends_at)
                  and aware(sub.grace_period_ends_at) <= now and open_invoices().first() is not None
                  and t.status != TenantStatus.SUSPENDED):
                sub.status = SubscriptionStatus.SUSPENDED
                suspend_for_billing(t)
                audit.record("billing.suspended", entity=t, actor=None, name=t.name)
                stats["suspended"] += 1
            db.session.commit()
    return stats


def mrr() -> Decimal:
    """Monthly recurring revenue from paying subscriptions (platform-wide)."""
    total = Decimal("0")
    with tenant_bypass():
        for sub in db.session.query(Subscription).filter(Subscription.status == SubscriptionStatus.ACTIVE):
            p = price(sub.plan, sub.billing_cycle)
            if p:
                total += (p / 12) if sub.billing_cycle == BillingCycle.ANNUAL else p
    return total.quantize(Decimal("0.01"))
