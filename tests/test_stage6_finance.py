"""Stage 6: finance and double-entry accounting."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.cli import seed_plans
from app.models import (
    Account, AccountType, BankReconciliation, Budget, Contribution, ContributionStatus, Expense, ExpenseStatus,
    JournalEntry, JournalLine, Member, PaymentMethod, Pledge, PledgeStatus, Role, Service, TenantUser,
)
from app.services import finance as fin, ledger
from app.services.ledger import LedgerError, Line
from app.utils.tenancy import tenant_bypass, tenant_context
from tests.conftest import make_user
from tests.test_design_system import assert_csp_clean

PW = "Correct-Horse-9"
TODAY = date.today()
D = Decimal


def login(client, email, password=PW):
    return client.post("/login", data={"email": email, "password": password})


def add_person(db, tid, email, role_slug):
    with tenant_context(tid):
        role = db.session.query(Role).filter_by(slug=role_slug).one()
        user = make_user(email)
        with tenant_bypass():
            db.session.add(user)
            db.session.flush()
        db.session.add(TenantUser(user_id=user.id, roles=[role]))
        db.session.commit()


def acc_pid(db, tid, code):
    db.session.expire_all()
    with tenant_context(tid):
        return db.session.query(Account).filter_by(code=code).one().public_id


def bal(db, tid, code, as_of=None):
    db.session.expire_all()
    with tenant_context(tid):
        return ledger.balance(ledger.account(code), as_of=as_of)


def first(db, tid, model, **f):
    db.session.expire_all()
    with tenant_context(tid):
        obj = db.session.query(model).filter_by(**f).order_by(model.id).first()
        db.session.expunge(obj)
        return obj


def give(client, db, tid, amount="100.00", code="4000", deposit="1000", **kw):
    data = {"amount": amount, "income_account": acc_pid(db, tid, code), "deposit_account": acc_pid(db, tid, deposit),
            "method": "cash", "received_on": TODAY.isoformat()}
    data.update(kw)
    return client.post("/finance/giving/new", data=data, follow_redirects=True)


@pytest.fixture(autouse=True)
def _plans(db):
    with tenant_bypass():
        seed_plans()
        db.session.commit()


@pytest.fixture()
def grace(client, two_churches):
    login(client, "owner@grace.test")
    return two_churches["grace"]


# --------------------------------------------------------------------------- #
# Ledger rules
# --------------------------------------------------------------------------- #
def test_new_church_gets_chart_of_accounts(db, grace):
    with tenant_context(grace):
        codes = {a.code for a in db.session.query(Account)}
    assert {"1000", "1010", "1020", "2000", "3000", "4000", "4010", "5010", "5900"} <= codes


def test_money_parsing():
    assert ledger.money("1,250.505") == D("1250.51")
    assert ledger.money(" 0.1 ") == D("0.10")
    for bad in ("", "abc", "NaN", "Infinity", "1e30"):
        with pytest.raises(LedgerError):
            ledger.money(bad)


def test_post_rejects_unbalanced_and_bad_lines(db, grace):
    with tenant_context(grace):
        cash, tithe = ledger.account("1000"), ledger.account("4000")
        with pytest.raises(LedgerError, match="must be equal"):
            ledger.post(TODAY, "x", [Line(cash, debit=D("10")), Line(tithe, credit=D("9.99"))])
        with pytest.raises(LedgerError, match="either a debit or a credit"):
            ledger.post(TODAY, "x", [Line(cash, debit=D("10"), credit=D("10")), Line(tithe, credit=D("10"))])
        with pytest.raises(LedgerError, match="at least two"):
            ledger.post(TODAY, "x", [Line(cash, debit=D("10"))])
        with pytest.raises(LedgerError, match="no amounts"):
            ledger.post(TODAY, "x", [Line(cash), Line(tithe)])
        with pytest.raises(LedgerError, match="cannot be negative"):
            ledger.post(TODAY, "x", [Line(cash, debit=D("-5")), Line(tithe, debit=D("5"))])
        e = ledger.post(TODAY, "Opening cash", [Line(cash, debit=D("500")), Line(ledger.account("3000"), credit=D("500"))])
        assert e.number == "JV-000001" and e.total == D("500.00")
        db.session.commit()


def test_database_refuses_two_sided_lines(db, grace):
    with tenant_context(grace):
        e = ledger.post(TODAY, "ok", [Line(ledger.account("1000"), debit=D("1")), Line(ledger.account("3000"), credit=D("1"))])
        db.session.add(JournalLine(entry_id=e.id, account_id=ledger.account("1000").id, debit=D("1"), credit=D("1")))
        with pytest.raises(IntegrityError):
            db.session.flush()
        db.session.rollback()


def test_reversal_nets_to_zero_and_cannot_repeat(db, grace):
    with tenant_context(grace):
        cash, fund = ledger.account("1000"), ledger.account("3000")
        e = ledger.post(TODAY, "Opening", [Line(cash, debit=D("250")), Line(fund, credit=D("250"))])
        rev = ledger.reverse(e, TODAY, "typo")
        assert ledger.balance(cash) == D("0.00") and e.status.value == "void" and rev.reversal_of_id == e.id
        with pytest.raises(LedgerError):
            ledger.reverse(e, TODAY, "again")
        with pytest.raises(LedgerError):
            ledger.reverse(rev, TODAY, "reverse the reversal")
        db.session.commit()


# --------------------------------------------------------------------------- #
# Giving
# --------------------------------------------------------------------------- #
def test_record_giving_receipt_and_books(client, db, grace):
    client.post("/members/new", data={"first_name": "Abena", "last_name": "Mensah", "branch": "", "date_joined": "2020-01-01"},
                follow_redirects=True)
    number = first(db, grace, Member).membership_number
    html = give(client, db, grace, "1,250.50", member_number=number, method="mobile_money", deposit="1020",
                reference="MP231122.1043.A12345").get_data(as_text=True)
    assert "RCP-000001" in html and "Abena Mensah" in html and "GHS 1,250.50" in html
    assert_csp_clean(html)
    assert bal(db, grace, "1020") == D("1250.50") and bal(db, grace, "4000") == D("1250.50")
    c = first(db, grace, Contribution)
    assert c.member_id and c.journal_entry_id and c.method == PaymentMethod.MOBILE_MONEY
    stmt = client.get(f"/finance/statements/{first(db, grace, Member).public_id}").get_data(as_text=True)
    assert "RCP-000001" in stmt and "1,250.50" in stmt


def test_giving_validation(client, db, grace):
    future = (TODAY + timedelta(days=2)).isoformat()
    assert "cannot be in the future" in give(client, db, grace, received_on=future).get_data(as_text=True)
    assert "more than zero" in give(client, db, grace, "0").get_data(as_text=True)
    assert "is not an amount" in give(client, db, grace, "ten cedis").get_data(as_text=True)
    assert "No member with that number" in give(client, db, grace, member_number="GCI-999999").get_data(as_text=True)
    assert "Choose a valid giving type" in give(client, db, grace, code="5010").get_data(as_text=True)    # expense acct
    assert "Choose a valid cash" in give(client, db, grace, deposit="4000").get_data(as_text=True)       # income acct
    with tenant_context(grace):
        assert db.session.query(Contribution).count() == 0 and db.session.query(JournalEntry).count() == 0


def test_void_receipt_reverses_entry(client, db, grace):
    give(client, db, grace, "300")
    c = first(db, grace, Contribution)
    client.post(f"/finance/receipts/{c.public_id}/void", data={"reason": ""}, follow_redirects=True)
    assert first(db, grace, Contribution).status == ContributionStatus.POSTED            # reason required
    client.post(f"/finance/receipts/{c.public_id}/void", data={"reason": "Entered twice"})
    assert first(db, grace, Contribution).status == ContributionStatus.VOID
    assert bal(db, grace, "1000") == D("0.00") and bal(db, grace, "4000") == D("0.00")
    with tenant_context(grace):
        assert db.session.query(JournalEntry).count() == 2          # original + reversal, nothing deleted


def test_service_offering_bulk(client, db, grace):
    client.post("/services/new", data={"title": "Sunday First Service", "kind": "service", "service_date": TODAY.isoformat(),
                                       "status": "scheduled", "branch": "", "department": "", "service_type": ""})
    svc = first(db, grace, Service)
    res = client.post("/finance/giving/offering", data={
        "service": svc.public_id,
        "line_type": [acc_pid(db, grace, "4010"), acc_pid(db, grace, "4000"), acc_pid(db, grace, "4020")],
        "line_amount": ["1,520.00", "3,400.50", ""],
        "line_account": [acc_pid(db, grace, "1000")] * 3, "line_method": ["cash", "cash", "cash"]})
    assert res.status_code == 302
    assert bal(db, grace, "1000") == D("4920.50")
    with tenant_context(grace):
        assert db.session.query(Contribution).filter_by(service_id=svc.id).count() == 2


# --------------------------------------------------------------------------- #
# Pledges and campaigns
# --------------------------------------------------------------------------- #
def test_pledge_is_not_income_until_paid(client, db, grace):
    client.post("/finance/campaigns", data={"name": "New Auditorium", "income_account": acc_pid(db, grace, "4050"),
                                            "target": "500,000"})
    camp = first(db, grace, __import__("app.models", fromlist=["Campaign"]).Campaign)
    client.post("/finance/pledges", data={"pledger_name": "Kwame Asante", "campaign": camp.public_id, "amount": "1000",
                                          "pledge_date": TODAY.isoformat(), "income_account": ""})
    p = first(db, grace, Pledge)
    assert p.status == PledgeStatus.OPEN and bal(db, grace, "4050") == D("0.00")      # promise only
    give(client, db, grace, "400", pledge=p.public_id)
    assert bal(db, grace, "4050") == D("400.00") and first(db, grace, Pledge).status == PledgeStatus.OPEN
    give(client, db, grace, "600", pledge=p.public_id)
    assert first(db, grace, Pledge).status == PledgeStatus.FULFILLED
    c = first(db, grace, Contribution)
    client.post(f"/finance/receipts/{c.public_id}/void", data={"reason": "bounced"})
    assert first(db, grace, Pledge).status == PledgeStatus.OPEN                         # back to open
    html = client.get("/finance/campaigns").get_data(as_text=True)
    assert "New Auditorium" in html and "600.00" in html


# --------------------------------------------------------------------------- #
# Expenses: approval workflow and accounts payable
# --------------------------------------------------------------------------- #
def test_expense_workflow_with_separation_of_duties(app, client, db, grace):
    give(client, db, grace, "5000", deposit="1010")
    add_person(db, grace, "accountant@grace.test", "accountant")
    add_person(db, grace, "finance@grace.test", "finance_manager")
    acct = app.test_client()
    login(acct, "accountant@grace.test")
    acct.post("/finance/expenses/new", data={"payee": "ECG", "description": "Electricity – October", "amount": "820.40",
                                             "expense_account": acc_pid(db, grace, "5010"),
                                             "expense_date": TODAY.isoformat(), "department": "", "branch": ""})
    e = first(db, grace, Expense)
    assert e.voucher_number == "PV-000001" and e.status == ExpenseStatus.SUBMITTED
    assert bal(db, grace, "5010") == D("0.00")                          # nothing in the books until approved

    assert acct.post(f"/finance/expenses/{e.public_id}/approve").status_code == 403   # accountants cannot approve
    fm = app.test_client()
    login(fm, "finance@grace.test")
    fm.post(f"/finance/expenses/{e.public_id}/approve", data={"note": "OK"})
    assert first(db, grace, Expense).status == ExpenseStatus.APPROVED
    assert bal(db, grace, "5010") == D("820.40") and bal(db, grace, "2000") == D("820.40")   # owed

    acct.post(f"/finance/expenses/{e.public_id}/pay", data={"payment_account": acc_pid(db, grace, "1010"),
                                                            "method": "bank_transfer", "reference": "CHQ 001234",
                                                            "paid_on": TODAY.isoformat()})
    assert first(db, grace, Expense).status == ExpenseStatus.PAID
    assert bal(db, grace, "2000") == D("0.00") and bal(db, grace, "1010") == D("4179.60")
    assert_csp_clean(acct.get(f"/finance/expenses/{e.public_id}").get_data(as_text=True))


def test_cannot_approve_own_voucher(client, db, grace):
    client.post("/finance/expenses/new", data={"payee": "Fuel station", "description": "Fuel", "amount": "200",
                                               "expense_account": acc_pid(db, grace, "5070"), "expense_date": TODAY.isoformat()})
    e = first(db, grace, Expense)
    html = client.post(f"/finance/expenses/{e.public_id}/approve", follow_redirects=True).get_data(as_text=True)
    assert "cannot approve a voucher you submitted" in html
    assert first(db, grace, Expense).status == ExpenseStatus.SUBMITTED


def test_reject_and_void_expense(app, client, db, grace):
    add_person(db, grace, "finance@grace.test", "finance_manager")
    client.post("/finance/expenses/new", data={"payee": "A", "description": "x", "amount": "50",
                                               "expense_account": acc_pid(db, grace, "5080"), "expense_date": TODAY.isoformat()})
    client.post("/finance/expenses/new", data={"payee": "B", "description": "y", "amount": "75",
                                               "expense_account": acc_pid(db, grace, "5080"), "expense_date": TODAY.isoformat()})
    a, b = (first(db, grace, Expense, payee=p) for p in ("A", "B"))
    fm = app.test_client()
    login(fm, "finance@grace.test")
    fm.post(f"/finance/expenses/{a.public_id}/reject", data={"note": "Not budgeted"})
    fm.post(f"/finance/expenses/{b.public_id}/approve")
    fm.post(f"/finance/expenses/{b.public_id}/void", data={"note": "Supplier cancelled"})
    assert first(db, grace, Expense, payee="A").status == ExpenseStatus.REJECTED
    assert first(db, grace, Expense, payee="B").status == ExpenseStatus.VOID
    assert bal(db, grace, "5080") == D("0.00") and bal(db, grace, "2000") == D("0.00")


# --------------------------------------------------------------------------- #
# Transfers, statements and reconciliation
# --------------------------------------------------------------------------- #
def test_transfer_with_charges(client, db, grace):
    give(client, db, grace, "1000", deposit="1020")
    client.post("/finance/transfer", data={"from_account": acc_pid(db, grace, "1020"), "to_account": acc_pid(db, grace, "1010"),
                                           "amount": "900", "fee": "9", "on": TODAY.isoformat(), "memo": "MoMo to bank"})
    assert bal(db, grace, "1020") == D("91.00") and bal(db, grace, "1010") == D("900.00") and bal(db, grace, "5090") == D("9.00")
    html = client.post("/finance/transfer", data={"from_account": acc_pid(db, grace, "1010"), "to_account": acc_pid(db, grace, "1010"),
                                                  "amount": "1", "on": TODAY.isoformat()}).get_data(as_text=True)
    assert "two different accounts" in html


def test_financial_statements_agree(client, db, grace):
    client.post("/accounting/journals/new", data={
        "entry_date": (TODAY - timedelta(days=40)).isoformat(), "description": "Opening balances", "opening": "1",
        "account": [acc_pid(db, grace, "1010"), acc_pid(db, grace, "1000"), acc_pid(db, grace, "3000")],
        "debit": ["10000", "500", ""], "credit": ["", "", "10500"], "memo": ["", "", ""]})
    give(client, db, grace, "2000")
    give(client, db, grace, "750", code="4010", deposit="1010")
    add_person(db, grace, "finance@grace.test", "finance_manager")
    client.post("/finance/expenses/new", data={"payee": "Landlord", "description": "Rent", "amount": "1500",
                                               "expense_account": acc_pid(db, grace, "5020"), "expense_date": TODAY.isoformat()})
    e = first(db, grace, Expense)
    from flask import current_app
    fm = current_app.test_client()
    login(fm, "finance@grace.test")
    fm.post(f"/finance/expenses/{e.public_id}/approve")       # owed, not yet paid

    tb = client.get("/accounting/reports/trial-balance").get_data(as_text=True)
    assert "Debits and credits do not agree" not in tb and "14,750.00" in tb
    month_start = TODAY.replace(day=1).isoformat()
    inc = client.get(f"/accounting/reports/income-statement?from={month_start}&to={TODAY}").get_data(as_text=True)
    assert "2,750.00" in inc and "1,500.00" in inc and "GHS 1,250.00" in inc
    bs = client.get("/accounting/reports/balance-sheet").get_data(as_text=True)
    assert "do not equal" not in bs and "13,250.00" in bs and "1,500.00" in bs      # assets; payable
    cf = client.get(f"/accounting/reports/cash-flow?from={month_start}&to={TODAY}").get_data(as_text=True)
    assert "Tithes" in cf and "2,750.00" in cf
    for path in ("/accounting", "/accounting/accounts", "/accounting/journals", "/accounting/journals/new",
                 "/accounting/reports/trial-balance", "/accounting/reports/income-statement",
                 "/accounting/reports/balance-sheet", "/accounting/reports/cash-flow", "/accounting/ledger",
                 "/accounting/reconciliations"):
        res = client.get(path)
        assert res.status_code == 200, path
        assert_csp_clean(res.get_data(as_text=True))
    csv = client.get("/accounting/reports/trial-balance?format=csv").get_data(as_text=True)
    assert "Total,14750.00,14750.00" in csv
    with tenant_context(grace):
        assert ledger.trial_balance(TODAY)["balanced"] and ledger.balance_sheet(TODAY)["balanced"]
        assert ledger.cash_flow(TODAY.replace(day=1), TODAY)["reconciles"]


def test_manual_journal_must_balance(client, db, grace):
    html = client.post("/accounting/journals/new", data={
        "entry_date": TODAY.isoformat(), "description": "Bad", "account": [acc_pid(db, grace, "1000"), acc_pid(db, grace, "3000")],
        "debit": ["100", ""], "credit": ["", "99"], "memo": ["", ""]}).get_data(as_text=True)
    assert "must be equal" in html
    with tenant_context(grace):
        assert db.session.query(JournalEntry).count() == 0


def test_bank_reconciliation(client, db, grace):
    give(client, db, grace, "1000", deposit="1010")
    give(client, db, grace, "250", deposit="1010")
    client.post("/accounting/reconciliations", data={"account": acc_pid(db, grace, "1010"),
                                                     "statement_date": TODAY.isoformat(), "statement_balance": "1000"})
    rec = first(db, grace, BankReconciliation)
    with tenant_context(grace):
        lines = {l.debit: l.id for l in fin.reconciliation_lines(db.session.get(BankReconciliation, rec.id))}
    client.post(f"/accounting/reconciliations/{rec.public_id}", data={"cleared": [str(lines[D('1000.00')]), str(lines[D('250.00')])], "complete": "1"})
    assert first(db, grace, BankReconciliation).status.value == "in_progress"          # difference −250
    client.post(f"/accounting/reconciliations/{rec.public_id}", data={"cleared": [str(lines[D('1000.00')])], "complete": "1"})
    assert first(db, grace, BankReconciliation).status.value == "completed"
    # The uncleared 250 carries forward to the next reconciliation.
    client.post("/accounting/reconciliations", data={"account": acc_pid(db, grace, "1010"),
                                                     "statement_date": TODAY.isoformat(), "statement_balance": "1250"})
    rec2 = first(db, grace, BankReconciliation, statement_balance=D("1250"))
    with tenant_context(grace):
        assert [l.debit for l in fin.reconciliation_lines(db.session.get(BankReconciliation, rec2.id))] == [D("250.00")]


def test_budget_vs_actual(client, db, grace):
    year = TODAY.year
    client.post("/finance/budgets", data={"name": f"{year} budget", "start_date": f"{year}-01-01", "end_date": f"{year}-12-31"})
    b = first(db, grace, Budget)
    client.post(f"/finance/budgets/{b.public_id}", data={f"amt_{acc_pid(db, grace, '4000')}": "120,000",
                                                         f"amt_{acc_pid(db, grace, '5010')}": "6,000"})
    give(client, db, grace, "10000")
    with tenant_context(grace):
        r = fin.budget_vs_actual(db.session.get(Budget, b.id))
    inc = r["rows"]["income"][0]
    assert inc["budget"] == D("120000.00") and inc["actual"] == D("10000.00") and inc["variance"] == D("-110000.00")
    html = client.get(f"/finance/budgets/{b.public_id}").get_data(as_text=True)
    assert "120,000.00" in html and "10,000.00" in html
    assert_csp_clean(html)


# --------------------------------------------------------------------------- #
# Access control
# --------------------------------------------------------------------------- #
def test_finance_permissions(app, client, db, grace):
    give(client, db, grace, "100")
    c = first(db, grace, Contribution)
    add_person(db, grace, "auditor@grace.test", "auditor")
    add_person(db, grace, "officer@grace.test", "membership_officer")
    au = app.test_client()
    login(au, "auditor@grace.test")
    assert au.get("/finance").status_code == 200 and au.get("/accounting/reports/balance-sheet").status_code == 200
    assert au.get("/finance/giving/new").status_code == 403
    assert au.post(f"/finance/receipts/{c.public_id}/void", data={"reason": "x"}).status_code == 403
    assert au.get("/accounting/journals/new").status_code == 403
    mo = app.test_client()
    login(mo, "officer@grace.test")
    for path in ("/finance", "/finance/giving", "/accounting", f"/finance/receipts/{c.public_id}"):
        assert mo.get(path).status_code == 403, path


def test_other_church_cannot_reach_finance(app, client, db, grace):
    give(client, db, grace, "999")
    client.post("/finance/expenses/new", data={"payee": "X", "description": "x", "amount": "10",
                                               "expense_account": acc_pid(db, grace, "5080"), "expense_date": TODAY.isoformat()})
    c, e = first(db, grace, Contribution), first(db, grace, Expense)
    with tenant_context(grace):
        j = db.session.query(JournalEntry).first().public_id
    other = app.test_client()
    login(other, "owner@word.test")
    for path in (f"/finance/receipts/{c.public_id}", f"/finance/expenses/{e.public_id}", f"/accounting/journals/{j}"):
        assert other.get(path).status_code == 404, path
    assert other.post(f"/finance/receipts/{c.public_id}/void", data={"reason": "x"}).status_code == 404
    assert other.post(f"/finance/expenses/{e.public_id}/approve").status_code == 404
    # Using another church's account ids is refused.
    html = other.post("/finance/giving/new", data={"amount": "5", "income_account": acc_pid(db, grace, "4000"),
                                                   "deposit_account": acc_pid(db, grace, "1000"), "method": "cash",
                                                   "received_on": TODAY.isoformat()}, follow_redirects=True).get_data(as_text=True)
    assert "Choose a valid" in html
    assert "999.00" not in other.get("/finance").get_data(as_text=True)
    assert "999.00" not in other.get("/accounting/reports/trial-balance").get_data(as_text=True)
    assert bal(db, grace, "1000") == D("999.00")


def test_finance_pages_render(client, db, grace):
    give(client, db, grace, "10")
    for path in ("/finance", "/finance/giving", "/finance/giving/new", "/finance/giving/offering", "/finance/pledges",
                 "/finance/campaigns", "/finance/expenses", "/finance/expenses/new", "/finance/transfer",
                 "/finance/budgets", "/finance/giving?format=csv", "/dashboard"):
        res = client.get(path)
        assert res.status_code == 200, path
        if "csv" not in path:
            assert_csp_clean(res.get_data(as_text=True))
