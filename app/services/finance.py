"""Church finance on top of the ledger: giving and receipts, pledges, campaigns,
expenses with approval, transfers, budgets and bank reconciliation."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal

from flask import abort, url_for
from sqlalchemy import func

from app.extensions import db
from app.models import (
    Account, AccountType, BankReconciliation, Budget, Campaign, Contribution, ContributionStatus, EntrySource,
    Expense, ExpenseStatus, JournalEntry, JournalLine, Member, PaymentMethod, Pledge, PledgeStatus,
    ReconciliationStatus,
)
from app.services import audit, ledger, search
from app.services import members as member_svc
from app.services.ledger import Line, LedgerError, ZERO, money
from app.services.members import next_sequence


# --------------------------------------------------------------------------- #
# Scoping (branch-limited roles see their branch only)
# --------------------------------------------------------------------------- #
def _scoped(model):
    q = db.session.query(model)
    limit = member_svc.branch_limit_id()
    if limit is not None:
        q = q.filter(model.branch_id == limit)
    return q


def contributions_query():
    return _scoped(Contribution)


def expenses_query():
    return _scoped(Expense)


def get_or_404(model, public_id):
    obj = (_scoped(model) if hasattr(model, "branch_id") else db.session.query(model)).filter(
        model.public_id == public_id).first()
    if obj is None:
        abort(404)
    return obj


def _require(acc: Account | None, *types: AccountType, money_only=False, label="account") -> Account:
    if acc is None or not acc.is_active or acc.type not in types or (money_only and not acc.subtype.is_money):
        raise LedgerError(f"Choose a valid {label}.")
    return acc


# --------------------------------------------------------------------------- #
# Giving
# --------------------------------------------------------------------------- #
def record_contribution(*, received_on: date, amount, income_account: Account | None, deposit_account: Account | None,
                        method: PaymentMethod, member: Member | None = None, giver_name: str | None = None,
                        reference: str | None = None, pledge: Pledge | None = None, campaign: Campaign | None = None,
                        service=None, branch_id: int | None = None, notes: str | None = None,
                        original_currency: str | None = None, original_amount=None, actor=None) -> Contribution:
    amount = money(amount)
    if amount <= 0:
        raise LedgerError("The amount must be more than zero.")
    if received_on > date.today():
        raise LedgerError("The date cannot be in the future.")
    if pledge is not None:
        if pledge.status == PledgeStatus.CANCELLED:
            raise LedgerError("That pledge was cancelled.")
        income_account = pledge.income_account
        campaign = campaign or pledge.campaign
        member = member or pledge.member
    if campaign is not None and income_account is None:
        income_account = campaign.income_account
    _require(income_account, AccountType.INCOME, label="giving type")
    _require(deposit_account, AccountType.ASSET, money_only=True, label="cash, bank or mobile money account")
    if branch_id is None and member is not None:
        branch_id = member.branch_id
    c = Contribution(
        receipt_number=f"RCP-{next_sequence('receipt'):06d}", received_on=received_on, amount=amount,
        member_id=member.id if member else None, giver_name=(giver_name or None) if not member else None,
        income_account_id=income_account.id, deposit_account_id=deposit_account.id, method=method,
        reference=(reference or None), pledge_id=pledge.id if pledge else None,
        campaign_id=campaign.id if campaign else None, service_id=getattr(service, "id", None),
        branch_id=branch_id, notes=notes or None, recorded_by_id=getattr(actor, "id", None),
        original_currency=original_currency or None,
        original_amount=money(original_amount) if original_amount not in (None, "") else None,
    )
    db.session.add(c)
    db.session.flush()
    who = member.display_name if member else (giver_name or "Anonymous")
    entry = ledger.post(received_on, f"{income_account.name} – {who} ({c.receipt_number})", [
        Line(deposit_account, debit=amount, branch_id=branch_id, memo=method.value),
        Line(income_account, credit=amount, branch_id=branch_id),
    ], source=EntrySource.CONTRIBUTION, reference=c.receipt_number, actor=actor)
    c.journal_entry_id = entry.id
    if pledge is not None:
        refresh_pledge(pledge)
    audit.record("finance.contribution_recorded", entity=c, actor=actor, name=f"{c.receipt_number} {who}",
                 amount=str(amount))
    return c


def void_contribution(c: Contribution, reason: str, actor) -> None:
    if c.status == ContributionStatus.VOID:
        raise LedgerError("This receipt is already void.")
    if not reason or not reason.strip():
        raise LedgerError("Give a reason for voiding the receipt.")
    entry = db.session.get(JournalEntry, c.journal_entry_id)
    ledger.reverse(entry, date.today(), f"receipt {c.receipt_number} voided – {reason.strip()}", actor)
    c.status, c.void_reason = ContributionStatus.VOID, reason.strip()[:255]
    if c.pledge:
        refresh_pledge(c.pledge)
    audit.record("finance.contribution_voided", entity=c, actor=actor, name=c.receipt_number, reason=reason)


def giving_summary(start: date, end: date) -> dict:
    q = contributions_query().filter(Contribution.status == ContributionStatus.POSTED,
                                     Contribution.received_on >= start, Contribution.received_on <= end)
    by_type = defaultdict(lambda: ZERO)
    total = ZERO
    for c in q.all():
        by_type[c.income_account.name] += c.amount
        total += c.amount
    return {"total": total, "by_type": sorted(by_type.items(), key=lambda x: -x[1]), "count": q.count()}


def member_statement(member: Member, start: date, end: date) -> dict:
    rows = (db.session.query(Contribution).filter(Contribution.member_id == member.id,
                                                  Contribution.status == ContributionStatus.POSTED,
                                                  Contribution.received_on >= start, Contribution.received_on <= end)
            .order_by(Contribution.received_on).all())
    by_type = defaultdict(lambda: ZERO)
    for c in rows:
        by_type[c.income_account.name] += c.amount
    return {"rows": rows, "by_type": sorted(by_type.items()), "total": sum((c.amount for c in rows), ZERO)}


# --------------------------------------------------------------------------- #
# Pledges and campaigns
# --------------------------------------------------------------------------- #
def pledge_paid(p: Pledge) -> Decimal:
    return sum((c.amount for c in p.payments if c.status == ContributionStatus.POSTED), ZERO)


def refresh_pledge(p: Pledge) -> None:
    db.session.flush()
    db.session.refresh(p)
    if p.status != PledgeStatus.CANCELLED:
        p.status = PledgeStatus.FULFILLED if pledge_paid(p) >= p.amount else PledgeStatus.OPEN


def campaign_progress(campaign: Campaign) -> dict:
    raised = (db.session.query(func.coalesce(func.sum(Contribution.amount), 0))
              .filter(Contribution.campaign_id == campaign.id, Contribution.status == ContributionStatus.POSTED).scalar())
    raised = money(Decimal(str(raised)))
    pledged = (db.session.query(func.coalesce(func.sum(Pledge.amount), 0))
               .filter(Pledge.campaign_id == campaign.id, Pledge.status != PledgeStatus.CANCELLED).scalar())
    pledged = money(Decimal(str(pledged)))
    pct = int(min(100, (raised / campaign.target_amount) * 100)) if campaign.target_amount else None
    return {"raised": raised, "pledged": pledged, "percent": pct}


# --------------------------------------------------------------------------- #
# Expenses: submitted → approved (accounts payable) → paid
# --------------------------------------------------------------------------- #
def create_expense(*, expense_date: date, payee: str, description: str, expense_account: Account | None, amount,
                   department=None, branch_id=None, actor=None) -> Expense:
    amount = money(amount)
    if amount <= 0:
        raise LedgerError("The amount must be more than zero.")
    _require(expense_account, AccountType.EXPENSE, label="expense category")
    e = Expense(voucher_number=f"PV-{next_sequence('voucher'):06d}", expense_date=expense_date, payee=payee.strip(),
                description=description.strip(), expense_account_id=expense_account.id, amount=amount,
                department_id=getattr(department, "id", None), branch_id=branch_id,
                requested_by_id=getattr(actor, "id", None))
    db.session.add(e)
    db.session.flush()
    audit.record("finance.expense_submitted", entity=e, actor=actor, name=f"{e.voucher_number} {e.payee}",
                 amount=str(amount))
    return e


def approve_expense(e: Expense, actor, note: str | None = None) -> None:
    if e.status != ExpenseStatus.SUBMITTED:
        raise LedgerError("Only submitted vouchers can be approved.")
    if e.requested_by_id and e.requested_by_id == actor.id:
        raise LedgerError("You cannot approve a voucher you submitted. Another approver must do it.")
    entry = ledger.post(e.expense_date, f"{e.expense_account.name} – {e.payee} ({e.voucher_number})", [
        Line(e.expense_account, debit=e.amount, branch_id=e.branch_id, memo=e.description[:255]),
        Line(ledger.account(ledger.PAYABLE), credit=e.amount, branch_id=e.branch_id, memo=e.payee),
    ], source=EntrySource.EXPENSE, reference=e.voucher_number, actor=actor)
    e.status, e.accrual_entry_id = ExpenseStatus.APPROVED, entry.id
    e.approved_by_id, e.approved_at, e.decision_note = actor.id, datetime.now(timezone.utc), note or None
    audit.record("finance.expense_approved", entity=e, actor=actor, name=e.voucher_number, amount=str(e.amount))


def reject_expense(e: Expense, actor, note: str) -> None:
    if e.status != ExpenseStatus.SUBMITTED:
        raise LedgerError("Only submitted vouchers can be rejected.")
    if not note or not note.strip():
        raise LedgerError("Give a reason for rejecting.")
    e.status, e.decision_note = ExpenseStatus.REJECTED, note.strip()[:255]
    e.approved_by_id, e.approved_at = actor.id, datetime.now(timezone.utc)
    audit.record("finance.expense_rejected", entity=e, actor=actor, name=e.voucher_number, reason=note)


def pay_expense(e: Expense, *, paid_on: date, payment_account: Account | None, method: PaymentMethod,
                reference: str | None, actor) -> None:
    if e.status != ExpenseStatus.APPROVED:
        raise LedgerError("Only approved vouchers can be paid.")
    if paid_on > date.today():
        raise LedgerError("The payment date cannot be in the future.")
    if paid_on < e.expense_date:
        raise LedgerError("The payment date is before the voucher date.")
    _require(payment_account, AccountType.ASSET, money_only=True, label="cash, bank or mobile money account")
    entry = ledger.post(paid_on, f"Payment {e.voucher_number} – {e.payee}", [
        Line(ledger.account(ledger.PAYABLE), debit=e.amount, branch_id=e.branch_id, memo=e.payee),
        Line(payment_account, credit=e.amount, branch_id=e.branch_id, memo=method.value),
    ], source=EntrySource.PAYMENT, reference=e.voucher_number, actor=actor)
    e.status, e.payment_entry_id = ExpenseStatus.PAID, entry.id
    e.paid_on, e.payment_account_id, e.payment_method, e.payment_reference = paid_on, payment_account.id, method, reference or None
    audit.record("finance.expense_paid", entity=e, actor=actor, name=e.voucher_number, amount=str(e.amount))


def void_expense(e: Expense, reason: str, actor) -> None:
    if e.status not in (ExpenseStatus.APPROVED, ExpenseStatus.PAID, ExpenseStatus.SUBMITTED):
        raise LedgerError("This voucher cannot be voided.")
    if not reason or not reason.strip():
        raise LedgerError("Give a reason for voiding.")
    for entry_id in (e.payment_entry_id, e.accrual_entry_id):
        if entry_id:
            ledger.reverse(db.session.get(JournalEntry, entry_id), date.today(),
                           f"voucher {e.voucher_number} voided – {reason.strip()}", actor)
    e.status, e.decision_note = ExpenseStatus.VOID, reason.strip()[:255]
    audit.record("finance.expense_voided", entity=e, actor=actor, name=e.voucher_number, reason=reason)


# --------------------------------------------------------------------------- #
# Transfers between cash, bank and MoMo
# --------------------------------------------------------------------------- #
def transfer(*, on: date, from_account: Account | None, to_account: Account | None, amount, memo: str, actor,
             fee=None, fee_account: Account | None = None) -> JournalEntry:
    amount = money(amount)
    _require(from_account, AccountType.ASSET, money_only=True, label="source account")
    _require(to_account, AccountType.ASSET, money_only=True, label="destination account")
    if from_account.id == to_account.id:
        raise LedgerError("Choose two different accounts.")
    if amount <= 0:
        raise LedgerError("The amount must be more than zero.")
    fee = money(fee) if fee not in (None, "") else ZERO
    lines = [Line(to_account, debit=amount, memo=memo), Line(from_account, credit=amount + fee, memo=memo)]
    if fee:
        lines.append(Line(fee_account or ledger.account("5090"), debit=fee, memo="Transfer charges"))
    entry = ledger.post(on, f"Transfer: {from_account.name} → {to_account.name}" + (f" – {memo}" if memo else ""),
                        lines, source=EntrySource.TRANSFER, actor=actor)
    audit.record("finance.transfer", entity=entry, actor=actor, name=entry.number, amount=str(amount))
    return entry


# --------------------------------------------------------------------------- #
# Budgets
# --------------------------------------------------------------------------- #
def budget_vs_actual(budget: Budget) -> dict:
    totals = ledger._lines(budget.start_date, budget.end_date)
    dept_actuals = defaultdict(lambda: ZERO)
    for e in db.session.query(Expense).filter(Expense.status.in_((ExpenseStatus.APPROVED, ExpenseStatus.PAID)),
                                              Expense.expense_date >= budget.start_date,
                                              Expense.expense_date <= budget.end_date,
                                              Expense.department_id.isnot(None)):
        dept_actuals[(e.expense_account_id, e.department_id)] += e.amount
    rows = {"income": [], "expense": []}
    for line in sorted(budget.lines, key=lambda l: l.account.code):
        acc = line.account
        if line.department_id:
            actual = dept_actuals[(acc.id, line.department_id)]
        else:
            d, c = totals.get(acc.id, (ZERO, ZERO))
            actual = ledger.signed(acc, d, c)
        variance = (actual - line.amount) if acc.type == AccountType.INCOME else (line.amount - actual)
        pct = int(actual / line.amount * 100) if line.amount else None
        rows["income" if acc.type == AccountType.INCOME else "expense"].append(
            {"line": line, "budget": line.amount, "actual": actual, "variance": variance, "percent": pct})
    summary = {k: {"budget": sum((r["budget"] for r in v), ZERO), "actual": sum((r["actual"] for r in v), ZERO)}
               for k, v in rows.items()}
    return {"rows": rows, "summary": summary}


# --------------------------------------------------------------------------- #
# Bank reconciliation
# --------------------------------------------------------------------------- #
def reconciliation_lines(rec: BankReconciliation) -> list[JournalLine]:
    """Uncleared lines up to the statement date, plus those ticked in this reconciliation."""
    return (db.session.query(JournalLine).join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
            .filter(JournalLine.account_id == rec.account_id, JournalEntry.entry_date <= rec.statement_date,
                    (JournalLine.reconciliation_id.is_(None)) | (JournalLine.reconciliation_id == rec.id))
            .order_by(JournalEntry.entry_date, JournalLine.id).all())


def reconciliation_status(rec: BankReconciliation) -> dict:
    acc = rec.account
    previously_cleared = (db.session.query(func.coalesce(func.sum(JournalLine.debit - JournalLine.credit), 0))
                          .join(BankReconciliation, BankReconciliation.id == JournalLine.reconciliation_id)
                          .filter(JournalLine.account_id == acc.id, BankReconciliation.status == ReconciliationStatus.COMPLETED,
                                  BankReconciliation.id != rec.id).scalar())
    this_cleared = (db.session.query(func.coalesce(func.sum(JournalLine.debit - JournalLine.credit), 0))
                    .filter(JournalLine.reconciliation_id == rec.id).scalar())
    cleared = money(Decimal(str(previously_cleared))) + money(Decimal(str(this_cleared)))
    book = ledger.balance(acc, as_of=rec.statement_date)
    return {"cleared_balance": cleared, "book_balance": book, "statement_balance": rec.statement_balance,
            "difference": rec.statement_balance - cleared}


def set_cleared(rec: BankReconciliation, line_ids: set[int]) -> None:
    if rec.status == ReconciliationStatus.COMPLETED:
        raise LedgerError("This reconciliation is completed.")
    for line in reconciliation_lines(rec):
        line.reconciliation_id = rec.id if line.id in line_ids else None
    db.session.flush()


def complete_reconciliation(rec: BankReconciliation, actor) -> None:
    status = reconciliation_status(rec)
    if status["difference"] != ZERO:
        raise LedgerError(f"The difference must be zero before completing (currently {status['difference']:,.2f}).")
    rec.status, rec.completed_by_id, rec.completed_at = ReconciliationStatus.COMPLETED, actor.id, datetime.now(timezone.utc)
    audit.record("finance.reconciliation_completed", entity=rec, actor=actor, name=rec.account.name,
                 date=rec.statement_date.isoformat())


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
@search.register("receipts", "finance.view")
def _search_receipts(query: str, limit: int):
    q = contributions_query().filter(Contribution.receipt_number.ilike(f"%{query}%"))
    for c in q.limit(limit):
        yield search.SearchResult(title=c.receipt_number, url=url_for("finance.receipt", public_id=c.public_id),
                                  kind="Receipt", subtitle=f"{c.giver} · {c.amount:,.2f}", icon="receipt")
    q = expenses_query().filter((Expense.voucher_number.ilike(f"%{query}%")) | (Expense.payee.ilike(f"%{query}%")))
    for e in q.limit(limit):
        yield search.SearchResult(title=f"{e.voucher_number} · {e.payee}", url=url_for("finance.expense", public_id=e.public_id),
                                  kind="Payment voucher", subtitle=f"{e.amount:,.2f} · {e.status.value}", icon="file-text")
