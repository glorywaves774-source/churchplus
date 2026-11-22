"""Stage 10: ChurchPlus billing, payments and the platform console."""
import hashlib
import hmac
import json
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

import pytest

from app.cli import seed_plans
from app.models import (
    AuditLog, BillingPayment, CounsellingCase, Invoice, InvoiceStatus, Member, PaymentStatus, Plan, PlatformRole,
    PrayerRequest, Subscription, SubscriptionStatus, Tenant, TenantStatus, TenantUser,
)
from app.models.base import utcnow
from app.services import billing, members as member_svc
from app.utils.tenancy import tenant_bypass, tenant_context
from tests.conftest import make_user
from tests.test_design_system import assert_csp_clean

PW = "Correct-Horse-9"


def login(client, email, password=PW):
    return client.post("/login", data={"email": email, "password": password})


def logout(client):
    client.post("/logout")


def platform_user(db, email="boss@kayron.test", role=PlatformRole.SUPER_ADMIN):
    with tenant_bypass():
        u = make_user(email)
        u.platform_role = role
        db.session.add(u)
        db.session.commit()


def end_trial(db, tid, days_ago=1):
    with tenant_context(tid):
        sub = db.session.query(Subscription).one()
        sub.trial_ends_at = utcnow() - timedelta(days=days_ago)
        db.session.commit()


def sub_of(db, tid):
    db.session.expire_all()
    with tenant_context(tid):
        s = db.session.query(Subscription).one()
        db.session.expunge(s)
        return s


@pytest.fixture(autouse=True)
def _plans(db):
    with tenant_bypass():
        seed_plans()
        db.session.commit()


# --------------------------------------------------------------------------- #
# Billing run
# --------------------------------------------------------------------------- #
def test_trial_end_creates_invoice_then_grace_then_suspension(app, db, two_churches):
    tid = two_churches["word"]                       # Starter, GHS 100 a month
    end_trial(db, tid)
    stats = billing.billing_run()
    assert stats["invoiced"] >= 1
    with tenant_context(tid):
        inv = db.session.query(Invoice).one()
        assert inv.amount == Decimal("100.00") and inv.status == InvoiceStatus.OPEN and inv.number.startswith("INV-")
    assert sub_of(db, tid).status == SubscriptionStatus.PAST_DUE
    assert billing.billing_run()["invoiced"] == 0          # never invoiced twice
    later = utcnow() + timedelta(days=app.config["BILLING_GRACE_DAYS"] + 1)
    assert billing.billing_run(later)["suspended"] >= 1
    with tenant_bypass():
        t = db.session.get(Tenant, tid)
        assert t.status == TenantStatus.SUSPENDED and t.settings["suspended_reason"] == "billing"


def test_period_dates():
    assert billing.period_after(date(2026, 1, 31), billing.BillingCycle.MONTHLY) == date(2026, 2, 28)
    assert billing.period_after(date(2026, 3, 15), billing.BillingCycle.MONTHLY) == date(2026, 4, 14)
    assert billing.period_after(date(2026, 3, 15), billing.BillingCycle.ANNUAL) == date(2027, 3, 14)


# --------------------------------------------------------------------------- #
# Church side: pay online
# --------------------------------------------------------------------------- #
def test_pay_invoice_with_test_checkout_reactivates_church(client, app, db, two_churches):
    tid = two_churches["word"]
    end_trial(db, tid, days_ago=20)
    billing.billing_run(utcnow() + timedelta(days=30))       # invoice and then pause
    billing.billing_run(utcnow() + timedelta(days=60))
    login(client, "owner@word.test")
    res = client.get("/dashboard")
    assert res.status_code == 302 and "/church/unavailable" in res.headers["Location"]
    page = client.get("/church/unavailable").get_data(as_text=True)
    assert "Pay now" in page
    billing_page = client.get("/billing")
    assert billing_page.status_code == 200
    assert_csp_clean(billing_page.get_data(as_text=True))
    with tenant_context(tid):
        inv = db.session.query(Invoice).filter_by(status=InvoiceStatus.OPEN).one()
        inv_pid = inv.public_id
    res = client.post(f"/billing/invoices/{inv_pid}/pay", data={"provider": "test"})
    assert "/billing/test-checkout/" in res.headers["Location"]
    res = client.post(res.headers["Location"], data={"result": "success"}, follow_redirects=True)
    assert "Payment received" in res.get_data(as_text=True)
    with tenant_context(tid):
        assert db.session.query(Invoice).filter_by(public_id=inv_pid).one().status == InvoiceStatus.PAID
        assert db.session.query(BillingPayment).one().status == PaymentStatus.SUCCESS
    assert sub_of(db, tid).status == SubscriptionStatus.ACTIVE
    with tenant_bypass():
        assert db.session.get(Tenant, tid).status == TenantStatus.ACTIVE
    assert client.get("/dashboard").status_code == 200


def test_failed_test_payment_leaves_invoice_open(client, db, two_churches):
    tid = two_churches["word"]
    end_trial(db, tid)
    billing.billing_run()
    login(client, "owner@word.test")
    with tenant_context(tid):
        inv_pid = db.session.query(Invoice).one().public_id
    loc = client.post(f"/billing/invoices/{inv_pid}/pay", data={"provider": "test"}).headers["Location"]
    client.post(loc, data={"result": "failed"}, follow_redirects=True)
    db.session.expire_all()
    with tenant_context(tid):
        assert db.session.query(Invoice).one().status == InvoiceStatus.OPEN
        assert db.session.query(BillingPayment).one().status == PaymentStatus.FAILED


def test_choose_plan_creates_invoice_and_respects_limits(client, db, two_churches):
    tid = two_churches["grace"]
    login(client, "owner@grace.test")
    with tenant_bypass():
        starter = db.session.query(Plan).filter_by(code="starter").one()
        starter_pid, starter_limit = starter.public_id, starter.max_members
        prof = db.session.query(Plan).filter_by(code="professional").one().public_id
    client.post("/billing/plan", data={"plan": prof, "cycle": "annual"})
    with tenant_context(tid):
        inv = db.session.query(Invoice).one()
        assert inv.billing_cycle == "annual" and inv.amount > 0
    with tenant_context(tid):
        t = db.session.get(Tenant, tid)
        for i in range((starter_limit or 0) + 1):
            db.session.add(Member(membership_number=f"X-{i}", first_name="M", last_name=str(i)))
        db.session.commit()
    res = client.post("/billing/plan", data={"plan": starter_pid, "cycle": "monthly"}, follow_redirects=True)
    assert "Reduce them first" in res.get_data(as_text=True)


def test_other_church_cannot_open_or_pay_invoice(client, db, two_churches):
    end_trial(db, two_churches["word"])
    billing.billing_run()
    with tenant_context(two_churches["word"]):
        pid = db.session.query(Invoice).one().public_id
    login(client, "owner@grace.test")
    assert client.get(f"/billing/invoices/{pid}").status_code == 404
    assert client.post(f"/billing/invoices/{pid}/pay", data={"provider": "test"}).status_code == 404


def test_past_due_banner_shown_to_billing_users(client, db, two_churches):
    end_trial(db, two_churches["word"])
    billing.billing_run()
    login(client, "owner@word.test")
    assert "Your ChurchPlus payment is due" in client.get("/members").get_data(as_text=True)


# --------------------------------------------------------------------------- #
# Webhooks
# --------------------------------------------------------------------------- #
def _paystack_payment(app, db, tid):
    end_trial(db, tid)
    billing.billing_run()
    with tenant_context(tid):
        inv = db.session.query(Invoice).one()
        pay = BillingPayment(invoice_id=inv.id, provider="paystack", reference="CPtest-ref1", amount=inv.amount)
        db.session.add(pay)
        db.session.commit()
        return inv.amount


def test_paystack_webhook_requires_valid_signature_and_verifies(client, app, db, two_churches):
    app.config["PAYSTACK_SECRET_KEY"] = "sk_test_secret"
    tid = two_churches["word"]
    amount = _paystack_payment(app, db, tid)
    body = json.dumps({"event": "charge.success", "data": {"reference": "CPtest-ref1"}}).encode()
    bad = client.post("/webhooks/paystack", data=body, headers={"X-Paystack-Signature": "nope", "Content-Type": "application/json"})
    assert bad.status_code == 401
    sig = hmac.new(b"sk_test_secret", body, hashlib.sha512).hexdigest()
    verified = {"status": True, "data": {"status": "success", "amount": int(amount * 100), "currency": "GHS", "channel": "mobile_money"}}
    with mock.patch("app.services.billing._http", return_value=verified) as http:
        ok = client.post("/webhooks/paystack", data=body, headers={"X-Paystack-Signature": sig, "Content-Type": "application/json"})
    assert ok.status_code == 200 and "transaction/verify/CPtest-ref1" in http.call_args[0][0]
    with tenant_context(tid):
        assert db.session.query(Invoice).one().status == InvoiceStatus.PAID
        assert db.session.query(BillingPayment).one().channel == "mobile_money"


def test_paystack_amount_mismatch_is_not_accepted(client, app, db, two_churches):
    app.config["PAYSTACK_SECRET_KEY"] = "sk_test_secret"
    tid = two_churches["word"]
    _paystack_payment(app, db, tid)
    body = json.dumps({"event": "charge.success", "data": {"reference": "CPtest-ref1"}}).encode()
    sig = hmac.new(b"sk_test_secret", body, hashlib.sha512).hexdigest()
    cheap = {"status": True, "data": {"status": "success", "amount": 100, "currency": "GHS"}}
    with mock.patch("app.services.billing._http", return_value=cheap):
        client.post("/webhooks/paystack", data=body, headers={"X-Paystack-Signature": sig, "Content-Type": "application/json"})
    with tenant_context(tid):
        assert db.session.query(Invoice).one().status == InvoiceStatus.OPEN


def test_hubtel_callback_is_confirmed_with_hubtel(client, app, db, two_churches):
    app.config.update(HUBTEL_PAY_CLIENT_ID="id", HUBTEL_PAY_CLIENT_SECRET="secret", HUBTEL_MERCHANT_ACCOUNT="2020")
    tid = two_churches["word"]
    end_trial(db, tid)
    billing.billing_run()
    with tenant_context(tid):
        inv = db.session.query(Invoice).one()
        db.session.add(BillingPayment(invoice_id=inv.id, provider="hubtel", reference="CPhub-1", amount=inv.amount))
        db.session.commit()
        amount = inv.amount
    payload = {"ResponseCode": "0000", "Data": {"ClientReference": "CPhub-1", "Status": "Success", "Amount": float(amount)}}
    with mock.patch("app.services.billing._http", return_value={"data": {"status": "Unpaid"}}):
        client.post("/webhooks/hubtel", json=payload)               # forged or early callback: not trusted
    with tenant_context(tid):
        assert db.session.query(Invoice).one().status == InvoiceStatus.OPEN
    with mock.patch("app.services.billing._http", return_value={"data": {"status": "Paid", "amount": str(amount), "paymentMethod": "mobilemoney"}}):
        client.post("/webhooks/hubtel", json=payload)
    with tenant_context(tid):
        assert db.session.query(Invoice).one().status == InvoiceStatus.PAID


def test_paystack_checkout_never_sends_a_persons_email(app, db, two_churches):
    app.config["PAYSTACK_SECRET_KEY"] = "sk_test_secret"
    tid = two_churches["word"]
    end_trial(db, tid)
    billing.billing_run()
    with app.test_request_context():
        with tenant_context(tid):
            inv = db.session.query(Invoice).one()
            t = db.session.get(Tenant, tid)
            with mock.patch("app.services.billing._http",
                            return_value={"status": True, "data": {"authorization_url": "https://checkout.paystack.com/x"}}) as http:
                co = billing.start_checkout(inv, t, "paystack", callback_url="https://a/billing/return", cancel_url="https://a")
            sent = http.call_args[1]["data"]
            assert co.url.startswith("https://checkout.paystack.com/")
            assert sent["email"] != "owner@word.test" and sent["amount"] == int(inv.amount * 100)


# --------------------------------------------------------------------------- #
# Platform console
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/platform", "/platform/churches", "/platform/invoices", "/platform/plans"])
def test_platform_pages(client, db, two_churches, path):
    platform_user(db)
    login(client, "boss@kayron.test")
    res = client.get(path)
    assert res.status_code == 200
    assert_csp_clean(res.get_data(as_text=True))


def test_platform_staff_land_on_console_and_church_users_are_kept_out(client, db, two_churches):
    platform_user(db)
    res = login(client, "boss@kayron.test")
    assert res.headers["Location"].endswith("/platform")
    logout(client)
    login(client, "owner@grace.test")
    assert client.get("/platform").status_code == 403


def test_platform_sees_counts_but_never_confidential_records(client, db, two_churches):
    tid = two_churches["grace"]
    with tenant_context(tid):
        t = db.session.get(Tenant, tid)
        m = member_svc.create_member(t, None, first_name="Secretive", last_name="Person", phone="0244111222")
        owner = db.session.query(TenantUser).first()
        db.session.add(PrayerRequest(name="Hidden Prayer", request="PRIVATE-PRAYER-TEXT", is_confidential=True))
        db.session.add(CounsellingCase(reference="CC-00009", member_id=m.id, category="Marriage", counsellor_id=owner.id,
                                       opened_on=date.today()))
        db.session.commit()
        pid = t.public_id
    platform_user(db)
    login(client, "boss@kayron.test")
    html = client.get(f"/platform/churches/{pid}").get_data(as_text=True)
    assert "Grace Chapel International" in html and ">1<" in html.replace(" ", "")
    for secret in ("Secretive", "PRIVATE-PRAYER-TEXT", "CC-00009", "Marriage", "0244111222"):
        assert secret not in html
    for church_page in ("/members", "/counselling", "/prayer", "/finance", "/children"):
        assert client.get(church_page).status_code in (302, 403), church_page


def test_platform_actions_and_support_is_read_only(client, db, two_churches):
    tid = two_churches["word"]
    with tenant_bypass():
        pid = db.session.get(Tenant, tid).public_id
    platform_user(db, "support@kayron.test", PlatformRole.SUPPORT_ADMIN)
    login(client, "support@kayron.test")
    assert client.post(f"/platform/churches/{pid}/action", data={"action": "suspend", "reason": "x"}).status_code == 403
    logout(client)
    platform_user(db)
    login(client, "boss@kayron.test")
    client.post(f"/platform/churches/{pid}/action", data={"action": "suspend", "reason": "Requested by church"})
    with tenant_bypass():
        assert db.session.get(Tenant, tid).status == TenantStatus.SUSPENDED
    client.post(f"/platform/churches/{pid}/action", data={"action": "reactivate"})
    client.post(f"/platform/churches/{pid}/action", data={"action": "extend_trial", "days": "10"})
    assert sub_of(db, tid).status == SubscriptionStatus.TRIALING
    with tenant_bypass():
        assert db.session.get(Tenant, tid).status in (TenantStatus.TRIAL, TenantStatus.ACTIVE)
        plan = db.session.query(Plan).filter_by(code="growth").one().public_id
    client.post(f"/platform/churches/{pid}/invoice", data={"plan": plan, "cycle": "monthly", "amount": "180.00", "note": "Discount"})
    with tenant_context(tid):
        inv = db.session.query(Invoice).one()
        assert inv.amount == Decimal("180.00")
        ipid = inv.public_id
    with tenant_bypass():
        assert db.session.query(Plan).filter_by(code="growth").one().monthly_price != Decimal("180.00")
    client.post(f"/platform/invoices/{ipid}/mark_paid", data={"reference": "GCB-778899", "channel": "bank"})
    with tenant_context(tid):
        assert db.session.query(Invoice).one().status == InvoiceStatus.PAID
        actions = {a.action for a in db.session.query(AuditLog)}
    assert {"platform.suspend", "platform.reactivate", "platform.extend_trial", "billing.invoice_paid"} <= actions
    with tenant_bypass():
        assert db.session.get(Tenant, tid).status == TenantStatus.ACTIVE


def test_platform_plan_prices(client, db, two_churches):
    platform_user(db)
    login(client, "boss@kayron.test")
    with tenant_bypass():
        p = db.session.query(Plan).filter_by(code="starter").one()
        pid = p.public_id
    client.post("/platform/plans", data={"plan": pid, "monthly_price": "120", "annual_price": "1200", "max_users": "5",
                                         "max_members": "300", "max_branches": "1", "trial_days": "30", "is_active": "y",
                                         "is_public": "y"})
    with tenant_bypass():
        p = db.session.query(Plan).filter_by(code="starter").one()
        assert p.monthly_price == Decimal("120") and p.trial_days == 30 and p.max_members == 300
