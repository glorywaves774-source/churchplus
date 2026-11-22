"""Accounting: chart of accounts, journals, ledgers, financial statements and bank reconciliation."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import func

from app.extensions import db
from app.models import (
    Account, AccountSubtype, AccountType, BankReconciliation, EntrySource, JournalEntry, JournalLine,
    ReconciliationStatus,
)
from app.services import audit, finance as fin, ledger, members as member_svc
from app.services.ledger import LedgerError, Line, ZERO, money
from app.utils.context import permission_required

bp = Blueprint("accounting", __name__)

SUBTYPES_BY_TYPE = {
    AccountType.ASSET: [AccountSubtype.CASH, AccountSubtype.BANK, AccountSubtype.MOBILE_MONEY, AccountSubtype.RECEIVABLE, AccountSubtype.GENERAL],
    AccountType.LIABILITY: [AccountSubtype.PAYABLE, AccountSubtype.GENERAL],
    AccountType.EQUITY: [AccountSubtype.FUND],
    AccountType.INCOME: [AccountSubtype.GENERAL],
    AccountType.EXPENSE: [AccountSubtype.GENERAL],
}


def _date(name, default=None):
    try:
        return date.fromisoformat(request.values.get(name, "")) if request.values.get(name) else default
    except ValueError:
        return default


def _branch_filter():
    """Branch for consolidation filters; branch-limited roles are always held to their branch."""
    limit = member_svc.branch_limit_id()
    if limit is not None:
        return limit
    pid = request.args.get("branch")
    b = next((b for b in member_svc.allowed_branches() if b.public_id == pid), None) if pid else None
    return b.id if b else None


def _report_ctx(**kw):
    return {"branches": member_svc.allowed_branches(), "branch_pid": request.args.get("branch", ""),
            "can_consolidate": member_svc.branch_limit_id() is None, **kw}


def _csv(rows, name):
    return Response(member_svc.to_csv(rows), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{g.tenant.slug}-{name}.csv"'})


# --------------------------------------------------------------------------- #
@bp.get("/accounting")
@permission_required("accounting.view")
def index():
    ledger.ensure_chart()
    db.session.commit()
    today = date.today()
    bs = ledger.balance_sheet(today, _branch_filter())
    tb = ledger.trial_balance(today, _branch_filter())
    recent = db.session.query(JournalEntry).order_by(JournalEntry.created_at.desc()).limit(10).all()
    return render_template("accounting/index.html", bs=bs, tb=tb, recent=recent, today=today,
                           breadcrumbs=[("Finance", None), ("Accounting", None)])


# --------------------------------------------------------------------------- #
# Chart of accounts
# --------------------------------------------------------------------------- #
@bp.route("/accounting/accounts", methods=["GET", "POST"])
@permission_required("accounting.view")
def accounts():
    if request.method == "POST":
        if not g.account.can("accounting.manage_settings"):
            abort(403)
        code, name = request.form.get("code", "").strip(), request.form.get("name", "").strip()
        try:
            typ = AccountType(request.form.get("type"))
            sub = AccountSubtype(request.form.get("subtype") or "general")
        except ValueError:
            typ = sub = None
        if not code or not name or typ is None:
            flash("Enter a code, a name and a type.", "danger")
        elif sub not in SUBTYPES_BY_TYPE[typ]:
            flash("That kind does not fit the account type.", "danger")
        elif db.session.query(Account.id).filter_by(code=code).first():
            flash("Another account already uses that code.", "danger")
        else:
            a = Account(code=code[:20], name=name[:120], type=typ, subtype=sub,
                        bank_name=request.form.get("bank_name") or None, account_number=request.form.get("account_number") or None,
                        description=request.form.get("description") or None)
            db.session.add(a)
            db.session.flush()
            audit.record("accounting.account_created", entity=a, actor=g.user, name=a.label)
            db.session.commit()
            flash(f"Account {a.label} added.", "success")
            return redirect(url_for("accounting.accounts"))
    today = date.today()
    rows = [(a, ledger.balance(a, as_of=today)) for a in ledger.accounts(active=False)]
    return render_template("accounting/accounts.html", rows=rows, types=list(AccountType),
                           subtypes=SUBTYPES_BY_TYPE,
                           breadcrumbs=[("Accounting", url_for("accounting.index")), ("Chart of accounts", None)])


@bp.post("/accounting/accounts/<public_id>")
@permission_required("accounting.manage_settings")
def update_account(public_id: str):
    a = db.session.query(Account).filter_by(public_id=public_id).first_or_404()
    name = request.form.get("name", "").strip()
    if name:
        a.name = name[:120]
    a.bank_name = request.form.get("bank_name") or a.bank_name
    a.account_number = request.form.get("account_number") or a.account_number
    if request.form.get("toggle") == "1":
        if a.is_system and a.is_active:
            flash("ChurchPlus needs this account; it cannot be closed.", "warning")
            return redirect(url_for("accounting.accounts"))
        if a.is_active and ledger.balance(a) != ZERO:
            flash("Only accounts with a zero balance can be closed.", "warning")
            return redirect(url_for("accounting.accounts"))
        a.is_active = not a.is_active
    audit.record("accounting.account_updated", entity=a, actor=g.user, name=a.label)
    db.session.commit()
    flash("Account saved.", "success")
    return redirect(url_for("accounting.accounts"))


# --------------------------------------------------------------------------- #
# Journals
# --------------------------------------------------------------------------- #
@bp.get("/accounting/journals")
@permission_required("accounting.view")
def journals():
    q = db.session.query(JournalEntry)
    src = request.args.get("source", "")
    if src in {s.value for s in EntrySource}:
        q = q.filter(JournalEntry.source == EntrySource(src))
    text = request.args.get("q", "").strip()
    if text:
        q = q.filter((JournalEntry.number.ilike(f"%{text}%")) | (JournalEntry.description.ilike(f"%{text}%")) |
                     (JournalEntry.reference.ilike(f"%{text}%")))
    page = q.order_by(JournalEntry.entry_date.desc(), JournalEntry.id.desc()).paginate(
        page=request.args.get("page", 1, type=int), per_page=30, error_out=False)
    params = {k: v for k, v in request.args.items() if k != "page" and v}
    return render_template("accounting/journals.html", page=page, params=params, sources=list(EntrySource), q=text,
                           breadcrumbs=[("Accounting", url_for("accounting.index")), ("Journals", None)])


@bp.route("/accounting/journals/new", methods=["GET", "POST"])
@permission_required("accounting.create")
def new_journal():
    accs = ledger.accounts()
    by_pid = {a.public_id: a for a in accs}
    form = request.form if request.method == "POST" else {}
    if request.method == "POST":
        try:
            lines = []
            for pid, dr, cr, memo in zip(request.form.getlist("account"), request.form.getlist("debit"),
                                         request.form.getlist("credit"), request.form.getlist("memo")):
                if not (dr.strip() or cr.strip()):
                    continue
                if pid not in by_pid:
                    raise LedgerError("Choose an account on every line with an amount.")
                lines.append(Line(by_pid[pid], debit=money(dr) if dr.strip() else ZERO,
                                  credit=money(cr) if cr.strip() else ZERO, memo=memo.strip() or None))
            src = EntrySource.OPENING if form.get("opening") == "1" else EntrySource.MANUAL
            entry = ledger.post(_date("entry_date", date.today()), form.get("description", ""), lines, source=src,
                                reference=form.get("reference", "").strip(), actor=g.user)
            audit.record("journal.posted", entity=entry, actor=g.user, name=entry.number, amount=str(entry.total))
            db.session.commit()
            flash(f"{entry.number} posted.", "success")
            return redirect(url_for("accounting.journal", public_id=entry.public_id))
        except LedgerError as e:
            db.session.rollback()
            flash(str(e), "danger")
    return render_template("accounting/journal_form.html", accounts=accs, form=form, today=date.today(),
                           rows=list(zip(request.form.getlist("account"), request.form.getlist("debit"),
                                         request.form.getlist("credit"), request.form.getlist("memo"))) if form else [],
                           breadcrumbs=[("Journals", url_for("accounting.journals")), ("New journal entry", None)])


@bp.get("/accounting/journals/<public_id>")
@permission_required("accounting.view")
def journal(public_id: str):
    e = db.session.query(JournalEntry).filter_by(public_id=public_id).first_or_404()
    related = {i: db.session.get(JournalEntry, i) for i in (e.reversal_of_id, e.reversed_by_id) if i}
    return render_template("accounting/journal.html", e=e, related=related,
                           breadcrumbs=[("Journals", url_for("accounting.journals")), (e.number, None)])


@bp.post("/accounting/journals/<public_id>/reverse")
@permission_required("accounting.edit")
def reverse_journal(public_id: str):
    e = db.session.query(JournalEntry).filter_by(public_id=public_id).first_or_404()
    if e.source not in (EntrySource.MANUAL, EntrySource.OPENING, EntrySource.TRANSFER):
        flash("Reverse receipts and vouchers from their own pages, so their status stays correct.", "warning")
        return redirect(url_for("accounting.journal", public_id=public_id))
    try:
        rev = ledger.reverse(e, _date("on", date.today()), request.form.get("reason", "").strip() or "corrected", g.user)
        db.session.commit()
        flash(f"{e.number} reversed by {rev.number}.", "success")
    except LedgerError as ex:
        db.session.rollback()
        flash(str(ex), "danger")
    return redirect(url_for("accounting.journal", public_id=public_id))


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #
def _period():
    today = date.today()
    start, end = _date("from", today.replace(month=1, day=1)), _date("to", today)
    return (end, start) if start > end else (start, end)


@bp.get("/accounting/reports/trial-balance")
@permission_required("accounting.view")
def trial_balance():
    as_of = _date("as_of", date.today())
    tb = ledger.trial_balance(as_of, _branch_filter())
    if request.args.get("format") == "csv":
        rows = [["Code", "Account", "Debit", "Credit"]] + [[r.account.code, r.account.name, f"{r.debit:.2f}", f"{r.credit:.2f}"] for r in tb["rows"]]
        rows.append(["", "Total", f"{tb['total_debit']:.2f}", f"{tb['total_credit']:.2f}"])
        return _csv(rows, f"trial-balance-{as_of}")
    return render_template("accounting/trial_balance.html", tb=tb, as_of=as_of, **_report_ctx(),
                           breadcrumbs=[("Accounting", url_for("accounting.index")), ("Trial balance", None)])


@bp.get("/accounting/reports/income-statement")
@permission_required("accounting.view")
def income_statement():
    start, end = _period()
    st = ledger.income_statement(start, end, _branch_filter())
    if request.args.get("format") == "csv":
        rows = [["Section", "Code", "Account", "Amount"]]
        rows += [["Income", r.account.code, r.account.name, f"{r.amount:.2f}"] for r in st["income"]]
        rows += [["Expenses", r.account.code, r.account.name, f"{r.amount:.2f}"] for r in st["expenses"]]
        rows += [["", "", "Total income", f"{st['total_income']:.2f}"], ["", "", "Total expenses", f"{st['total_expenses']:.2f}"],
                 ["", "", "Surplus / (deficit)", f"{st['surplus']:.2f}"]]
        return _csv(rows, f"income-statement-{start}-{end}")
    return render_template("accounting/income_statement.html", st=st, start=start, end=end, **_report_ctx(),
                           breadcrumbs=[("Accounting", url_for("accounting.index")), ("Income statement", None)])


@bp.get("/accounting/reports/balance-sheet")
@permission_required("accounting.view")
def balance_sheet():
    as_of = _date("as_of", date.today())
    bs = ledger.balance_sheet(as_of, _branch_filter())
    if request.args.get("format") == "csv":
        rows = [["Section", "Code", "Account", "Amount"]]
        for key, label in (("assets", "Assets"), ("liabilities", "Liabilities"), ("equity", "Funds")):
            rows += [[label, r.account.code, r.account.name, f"{r.amount:.2f}"] for r in bs[key]]
        rows += [["Funds", "", "Surplus to date", f"{bs['surplus']:.2f}"],
                 ["", "", "Total assets", f"{bs['total_assets']:.2f}"],
                 ["", "", "Total liabilities and funds", f"{bs['total_liabilities'] + bs['total_equity']:.2f}"]]
        return _csv(rows, f"balance-sheet-{as_of}")
    return render_template("accounting/balance_sheet.html", bs=bs, as_of=as_of, **_report_ctx(),
                           breadcrumbs=[("Accounting", url_for("accounting.index")), ("Balance sheet", None)])


@bp.get("/accounting/reports/cash-flow")
@permission_required("accounting.view")
def cash_flow():
    start, end = _period()
    cf = ledger.cash_flow(start, end, _branch_filter())
    return render_template("accounting/cash_flow.html", cf=cf, start=start, end=end, **_report_ctx(),
                           breadcrumbs=[("Accounting", url_for("accounting.index")), ("Cash flow", None)])


@bp.get("/accounting/ledger")
@permission_required("accounting.view")
def account_ledger():
    accs = ledger.accounts(active=False)
    acc = next((a for a in accs if a.public_id == request.args.get("account")), None) or \
        next((a for a in accs if a.code == ledger.CASH), None)
    start, end = _period()
    data = ledger.ledger(acc, start, end, _branch_filter()) if acc else None
    if acc and request.args.get("format") == "csv":
        rows = [["Date", "Journal", "Description", "Memo", "Debit", "Credit", "Balance"], ["", "", "Opening balance", "", "", "", f"{data['opening']:.2f}"]]
        rows += [[r["entry"].entry_date.isoformat(), r["entry"].number, r["entry"].description, r["line"].memo or "",
                  f"{r['line'].debit:.2f}", f"{r['line'].credit:.2f}", f"{r['balance']:.2f}"] for r in data["rows"]]
        return _csv(rows, f"ledger-{acc.code}-{start}-{end}")
    return render_template("accounting/ledger.html", accounts=accs, acc=acc, data=data, start=start, end=end,
                           **_report_ctx(), breadcrumbs=[("Accounting", url_for("accounting.index")),
                                                         ("Cashbook" if acc and acc.subtype.is_money else "Ledger", None)])


# --------------------------------------------------------------------------- #
# Bank reconciliation
# --------------------------------------------------------------------------- #
@bp.route("/accounting/reconciliations", methods=["GET", "POST"])
@permission_required("accounting.view")
def reconciliations():
    if request.method == "POST":
        if not g.account.can("accounting.create"):
            abort(403)
        acc = db.session.query(Account).filter_by(public_id=request.form.get("account")).first()
        try:
            if acc is None or not acc.subtype.is_money:
                raise LedgerError("Choose a bank, cash or mobile money account.")
            if db.session.query(BankReconciliation.id).filter_by(account_id=acc.id, status=ReconciliationStatus.IN_PROGRESS).first():
                raise LedgerError("Finish the open reconciliation for this account first.")
            rec = BankReconciliation(account_id=acc.id, statement_date=_date("statement_date", date.today()),
                                     statement_balance=money(request.form.get("statement_balance")))
            db.session.add(rec)
            db.session.flush()
            db.session.commit()
            return redirect(url_for("accounting.reconciliation", public_id=rec.public_id))
        except LedgerError as e:
            db.session.rollback()
            flash(str(e), "danger")
    rows = db.session.query(BankReconciliation).order_by(BankReconciliation.statement_date.desc()).all()
    return render_template("accounting/reconciliations.html", rows=rows, accounts=ledger.money_accounts(), today=date.today(),
                           breadcrumbs=[("Accounting", url_for("accounting.index")), ("Bank reconciliation", None)])


@bp.route("/accounting/reconciliations/<public_id>", methods=["GET", "POST"])
@permission_required("accounting.view")
def reconciliation(public_id: str):
    rec = db.session.query(BankReconciliation).filter_by(public_id=public_id).first_or_404()
    if request.method == "POST":
        if not g.account.can("accounting.create"):
            abort(403)
        try:
            fin.set_cleared(rec, {int(x) for x in request.form.getlist("cleared") if x.isdigit()})
            if request.form.get("complete") == "1":
                fin.complete_reconciliation(rec, g.user)
                flash("Reconciliation completed.", "success")
            else:
                flash("Progress saved.", "success")
            db.session.commit()
        except LedgerError as e:
            # Keep the ticked lines when completing fails because the difference is not zero.
            if request.form.get("complete") == "1":
                db.session.commit()
            else:
                db.session.rollback()
            flash(str(e), "danger")
        return redirect(url_for("accounting.reconciliation", public_id=public_id))
    lines = fin.reconciliation_lines(rec) if rec.status == ReconciliationStatus.IN_PROGRESS else \
        db.session.query(JournalLine).filter_by(reconciliation_id=rec.id).all()
    return render_template("accounting/reconciliation.html", rec=rec, lines=lines, st=fin.reconciliation_status(rec),
                           breadcrumbs=[("Bank reconciliation", url_for("accounting.reconciliations")),
                                        (f"{rec.account.name} – {rec.statement_date:%d %b %Y}", None)])
