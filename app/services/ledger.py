"""Double-entry ledger: chart of accounts, posting, reversal, balances and statements.

Rules enforced here (and by database constraints):
* every entry has at least two lines and total debits equal total credits;
* each line is either a debit or a credit, never negative, rounded to cents;
* posted entries are never changed or deleted; mistakes are corrected with a
  reversing entry, which marks the original as void;
* reports include voided entries together with their reversals, so every
  period shows exactly what was recorded in it.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy import func

from app.extensions import db
from app.models import (
    Account, AccountSubtype, AccountType, EntrySource, EntryStatus, JournalEntry, JournalLine,
)
from app.services import audit
from app.services.members import next_sequence
from app.utils.tenancy import require_tenant_id

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

# Codes ChurchPlus relies on (cannot be deleted or retyped).
CASH, BANK, MOMO = "1000", "1010", "1020"
PAYABLE = "2000"
GENERAL_FUND = "3000"

DEFAULT_CHART = [
    # code, name, type, subtype, system
    (CASH, "Cash on hand", AccountType.ASSET, AccountSubtype.CASH, True),
    (BANK, "Main bank account", AccountType.ASSET, AccountSubtype.BANK, True),
    (MOMO, "Mobile money wallet", AccountType.ASSET, AccountSubtype.MOBILE_MONEY, True),
    ("1100", "Receivables", AccountType.ASSET, AccountSubtype.RECEIVABLE, False),
    ("1500", "Land, buildings and equipment", AccountType.ASSET, AccountSubtype.GENERAL, False),
    (PAYABLE, "Accounts payable", AccountType.LIABILITY, AccountSubtype.PAYABLE, True),
    ("2100", "Taxes and SSNIT payable", AccountType.LIABILITY, AccountSubtype.PAYABLE, False),
    (GENERAL_FUND, "General fund", AccountType.EQUITY, AccountSubtype.FUND, True),
    ("3100", "Building fund", AccountType.EQUITY, AccountSubtype.FUND, False),
    ("4000", "Tithes", AccountType.INCOME, AccountSubtype.GENERAL, False),
    ("4010", "Offerings", AccountType.INCOME, AccountSubtype.GENERAL, False),
    ("4020", "Special offerings", AccountType.INCOME, AccountSubtype.GENERAL, False),
    ("4030", "Donations", AccountType.INCOME, AccountSubtype.GENERAL, False),
    ("4040", "Welfare contributions", AccountType.INCOME, AccountSubtype.GENERAL, False),
    ("4050", "Project and building contributions", AccountType.INCOME, AccountSubtype.GENERAL, False),
    ("4060", "Fundraising income", AccountType.INCOME, AccountSubtype.GENERAL, False),
    ("4900", "Other income", AccountType.INCOME, AccountSubtype.GENERAL, False),
    ("5000", "Salaries and allowances", AccountType.EXPENSE, AccountSubtype.GENERAL, False),
    ("5010", "Utilities (ECG, water, internet)", AccountType.EXPENSE, AccountSubtype.GENERAL, False),
    ("5020", "Rent", AccountType.EXPENSE, AccountSubtype.GENERAL, False),
    ("5030", "Repairs and maintenance", AccountType.EXPENSE, AccountSubtype.GENERAL, False),
    ("5040", "Ministry and programme expenses", AccountType.EXPENSE, AccountSubtype.GENERAL, False),
    ("5050", "Welfare and benevolence", AccountType.EXPENSE, AccountSubtype.GENERAL, False),
    ("5060", "Missions and evangelism", AccountType.EXPENSE, AccountSubtype.GENERAL, False),
    ("5070", "Transport and fuel", AccountType.EXPENSE, AccountSubtype.GENERAL, False),
    ("5080", "Office and administration", AccountType.EXPENSE, AccountSubtype.GENERAL, False),
    ("5090", "Bank and MoMo charges", AccountType.EXPENSE, AccountSubtype.GENERAL, False),
    ("5100", "Media, music and IT", AccountType.EXPENSE, AccountSubtype.GENERAL, False),
    ("5110", "Denominational remittances", AccountType.EXPENSE, AccountSubtype.GENERAL, False),
    ("5900", "Other expenses", AccountType.EXPENSE, AccountSubtype.GENERAL, False),
]


class LedgerError(ValueError):
    pass


def money(value) -> Decimal:
    """Parse an amount ("1,250.5", 1250.5, Decimal) to cents. Raises LedgerError."""
    if isinstance(value, Decimal):
        d = value
    else:
        text = str(value if value is not None else "").replace(",", "").replace(" ", "").strip()
        if not text:
            raise LedgerError("Enter an amount.")
        try:
            d = Decimal(text)
        except InvalidOperation:
            raise LedgerError(f"'{value}' is not an amount.") from None
    if not d.is_finite():
        raise LedgerError("Enter a valid amount.")
    if abs(d) > Decimal("999999999999.99"):
        raise LedgerError("The amount is too large.")
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- #
# Chart of accounts
# --------------------------------------------------------------------------- #
def ensure_chart() -> None:
    require_tenant_id()
    existing = {c for (c,) in db.session.query(Account.code)}
    for code, name, typ, sub, system in DEFAULT_CHART:
        if code not in existing:
            db.session.add(Account(code=code, name=name, type=typ, subtype=sub, is_system=system))
    db.session.flush()


def account(code: str) -> Account:
    acc = db.session.query(Account).filter_by(code=code).one_or_none()
    if acc is None:
        ensure_chart()
        acc = db.session.query(Account).filter_by(code=code).one()
    return acc


def accounts(*types: AccountType, active=True, money_only=False) -> list[Account]:
    q = db.session.query(Account)
    if types:
        q = q.filter(Account.type.in_(types))
    if active:
        q = q.filter(Account.is_active.is_(True))
    rows = q.order_by(Account.code).all()
    return [a for a in rows if a.subtype.is_money] if money_only else rows


def money_accounts() -> list[Account]:
    return accounts(AccountType.ASSET, money_only=True)


# --------------------------------------------------------------------------- #
# Posting
# --------------------------------------------------------------------------- #
@dataclass
class Line:
    account: Account
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    memo: str | None = None
    branch_id: int | None = None


def post(entry_date: date, description: str, lines: list[Line], *, source: EntrySource = EntrySource.MANUAL,
         reference: str | None = None, actor=None, reversal_of: JournalEntry | None = None) -> JournalEntry:
    require_tenant_id()
    if not description or not description.strip():
        raise LedgerError("Enter a description.")
    if len(lines) < 2:
        raise LedgerError("An entry needs at least two lines.")
    clean = []
    for ln in lines:
        d, c = money(ln.debit or ZERO), money(ln.credit or ZERO)
        if d < 0 or c < 0:
            raise LedgerError("Amounts cannot be negative.")
        if d and c:
            raise LedgerError("A line is either a debit or a credit.")
        if not d and not c:
            continue
        if ln.account is None or ln.account.id is None:
            raise LedgerError("Every line needs an account.")
        if not ln.account.is_active and reversal_of is None:
            raise LedgerError(f"{ln.account.label} is closed.")
        clean.append((ln, d, c))
    total_d = sum((d for _, d, _ in clean), ZERO)
    total_c = sum((c for _, _, c in clean), ZERO)
    if len(clean) < 2 or total_d == ZERO:
        raise LedgerError("The entry has no amounts.")
    if total_d != total_c:
        raise LedgerError(f"Debits ({total_d:,.2f}) and credits ({total_c:,.2f}) must be equal.")
    entry = JournalEntry(number=f"JV-{next_sequence('journal'):06d}", entry_date=entry_date,
                         description=description.strip()[:255], reference=(reference or None),
                         source=source, created_by_id=getattr(actor, "id", None),
                         reversal_of_id=reversal_of.id if reversal_of else None)
    for ln, d, c in clean:
        entry.lines.append(JournalLine(account_id=ln.account.id, debit=d, credit=c,
                                       memo=(ln.memo or None), branch_id=ln.branch_id))
    db.session.add(entry)
    db.session.flush()
    return entry


def reverse(entry: JournalEntry, on: date, reason: str, actor=None) -> JournalEntry:
    if entry.status == EntryStatus.VOID or entry.reversed_by_id:
        raise LedgerError(f"{entry.number} has already been reversed.")
    if entry.reversal_of_id:
        raise LedgerError("A reversing entry cannot itself be reversed.")
    lines = [Line(account=l.account, debit=l.credit, credit=l.debit, memo=l.memo, branch_id=l.branch_id)
             for l in entry.lines]
    rev = post(on, f"Reversal of {entry.number}: {reason}"[:255], lines, source=EntrySource.REVERSAL,
               reference=entry.number, actor=actor, reversal_of=entry)
    entry.status = EntryStatus.VOID
    entry.reversed_by_id = rev.id
    audit.record("journal.reversed", entity=entry, actor=actor, name=entry.number, reason=reason)
    return rev


# --------------------------------------------------------------------------- #
# Balances and statements
# --------------------------------------------------------------------------- #
def _lines(start: date | None = None, end: date | None = None, branch_id: int | None = None):
    q = (db.session.query(JournalLine.account_id, func.coalesce(func.sum(JournalLine.debit), 0),
                          func.coalesce(func.sum(JournalLine.credit), 0))
         .join(JournalEntry, JournalEntry.id == JournalLine.entry_id))
    if start:
        q = q.filter(JournalEntry.entry_date >= start)
    if end:
        q = q.filter(JournalEntry.entry_date <= end)
    if branch_id:
        q = q.filter(JournalLine.branch_id == branch_id)
    def dec(v):
        # SQLite sums decimals as floats; convert via str and round to cents.
        return Decimal(str(v)).quantize(CENT, rounding=ROUND_HALF_UP)
    return {aid: (dec(d), dec(c)) for aid, d, c in q.group_by(JournalLine.account_id).all()}


def signed(acc: Account, debit: Decimal, credit: Decimal) -> Decimal:
    """Balance in the account's normal direction (assets/expenses: debit − credit)."""
    return (debit - credit) if acc.type.debit_normal else (credit - debit)


def balance(acc: Account, as_of: date | None = None, start: date | None = None, branch_id: int | None = None) -> Decimal:
    d, c = _lines(start, as_of, branch_id).get(acc.id, (ZERO, ZERO))
    return signed(acc, Decimal(d), Decimal(c)).quantize(CENT)


@dataclass
class Row:
    account: Account
    amount: Decimal
    debit: Decimal = ZERO
    credit: Decimal = ZERO


def trial_balance(as_of: date, branch_id: int | None = None) -> dict:
    totals = _lines(None, as_of, branch_id)
    rows = []
    for acc in accounts(active=False):
        d, c = totals.get(acc.id, (ZERO, ZERO))
        net = Decimal(d) - Decimal(c)
        if net == 0 and not d:
            continue
        rows.append(Row(acc, net, debit=net if net > 0 else ZERO, credit=-net if net < 0 else ZERO))
    td = sum((r.debit for r in rows), ZERO)
    tc = sum((r.credit for r in rows), ZERO)
    return {"rows": rows, "total_debit": td, "total_credit": tc, "balanced": td == tc}


def income_statement(start: date, end: date, branch_id: int | None = None) -> dict:
    totals = _lines(start, end, branch_id)
    income, expenses = [], []
    for acc in accounts(AccountType.INCOME, AccountType.EXPENSE, active=False):
        d, c = totals.get(acc.id, (ZERO, ZERO))
        amt = signed(acc, Decimal(d), Decimal(c))
        if amt:
            (income if acc.type == AccountType.INCOME else expenses).append(Row(acc, amt))
    ti = sum((r.amount for r in income), ZERO)
    te = sum((r.amount for r in expenses), ZERO)
    return {"income": income, "expenses": expenses, "total_income": ti, "total_expenses": te, "surplus": ti - te}


def balance_sheet(as_of: date, branch_id: int | None = None) -> dict:
    totals = _lines(None, as_of, branch_id)
    groups = {AccountType.ASSET: [], AccountType.LIABILITY: [], AccountType.EQUITY: []}
    surplus = ZERO
    for acc in accounts(active=False):
        d, c = totals.get(acc.id, (ZERO, ZERO))
        amt = signed(acc, Decimal(d), Decimal(c))
        if acc.type in groups:
            if amt:
                groups[acc.type].append(Row(acc, amt))
        elif acc.type == AccountType.INCOME:
            surplus += amt
        else:
            surplus -= amt
    ta = sum((r.amount for r in groups[AccountType.ASSET]), ZERO)
    tl = sum((r.amount for r in groups[AccountType.LIABILITY]), ZERO)
    te = sum((r.amount for r in groups[AccountType.EQUITY]), ZERO) + surplus
    return {"assets": groups[AccountType.ASSET], "liabilities": groups[AccountType.LIABILITY],
            "equity": groups[AccountType.EQUITY], "surplus": surplus, "total_assets": ta,
            "total_liabilities": tl, "total_equity": te, "balanced": ta == tl + te}


def cash_flow(start: date, end: date, branch_id: int | None = None) -> dict:
    """Direct method: money in and out of cash, bank and MoMo, by what it was for."""
    all_money = accounts(AccountType.ASSET, active=False, money_only=True)
    money_ids = {a.id for a in all_money}
    q = (db.session.query(JournalEntry).filter(JournalEntry.entry_date >= start, JournalEntry.entry_date <= end))
    flows = defaultdict(lambda: ZERO)        # net money in (+) or out (−) per purpose
    for entry in q.all():
        cash_lines = [l for l in entry.lines if l.account_id in money_ids and (not branch_id or l.branch_id == branch_id)]
        if not cash_lines:
            continue
        others = [l for l in entry.lines if l.account_id not in money_ids]
        net = sum((l.debit - l.credit for l in cash_lines), ZERO)
        if not others or net == 0:
            continue                           # transfer between our own money accounts
        label_acc = max(others, key=lambda l: l.debit + l.credit).account
        label = label_acc.name if label_acc.type in (AccountType.INCOME, AccountType.EXPENSE) else \
            ("Supplier and staff payments" if label_acc.subtype == AccountSubtype.PAYABLE else label_acc.name)
        flows[label] += net
    receipts = {k: v for k, v in flows.items() if v > 0}
    payments = {k: -v for k, v in flows.items() if v < 0}
    opening = sum((balance(a, as_of=_day_before(start), branch_id=branch_id) for a in all_money), ZERO)
    closing = sum((balance(a, as_of=end, branch_id=branch_id) for a in all_money), ZERO)
    tin, tout = sum(receipts.values(), ZERO), sum(payments.values(), ZERO)
    return {"receipts": sorted(receipts.items(), key=lambda x: -x[1]), "payments": sorted(payments.items(), key=lambda x: -x[1]),
            "total_in": tin, "total_out": tout, "net": tin - tout, "opening": opening, "closing": closing,
            "reconciles": opening + tin - tout == closing}


def _day_before(d: date) -> date:
    from datetime import timedelta
    return d - timedelta(days=1)


def ledger(acc: Account, start: date, end: date, branch_id: int | None = None) -> dict:
    opening = balance(acc, as_of=_day_before(start), branch_id=branch_id)
    q = (db.session.query(JournalLine).join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
         .filter(JournalLine.account_id == acc.id, JournalEntry.entry_date >= start, JournalEntry.entry_date <= end))
    if branch_id:
        q = q.filter(JournalLine.branch_id == branch_id)
    rows, running = [], opening
    for line in q.order_by(JournalEntry.entry_date, JournalEntry.id, JournalLine.id).all():
        running += signed(acc, line.debit, line.credit)
        rows.append({"line": line, "entry": line.entry, "balance": running})
    return {"opening": opening, "rows": rows, "closing": running}
