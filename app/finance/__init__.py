"""Finance pages: giving and receipts, offerings, pledges, campaigns, expenses and approvals,
transfers, budgets and giving statements."""
from __future__ import annotations

import io
from datetime import date, datetime, timedelta
from decimal import Decimal

from flask import (
    Blueprint, Response, abort, flash, g, redirect, render_template, request, send_file, url_for,
)
from sqlalchemy import func, or_

from app.extensions import db, limiter
from app.models import (
    Account, AccountType, Budget, BudgetLine, Campaign, Contribution, ContributionStatus, Department, Expense,
    ExpenseStatus, Member, PaymentMethod, Pledge, PledgeStatus, Service,
)
from app.services import audit, finance as fin, ledger, members as member_svc, operations as ops, storage
from app.services.ledger import LedgerError, ZERO, money
from app.services.storage import StorageError
from app.utils.context import permission_required

bp = Blueprint("finance", __name__)

METHODS = [(m.value, m.value.replace("_", " ").capitalize().replace("Mobile money", "Mobile money (MoMo)")) for m in PaymentMethod]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _acc(public_id: str | None) -> Account | None:
    return db.session.query(Account).filter_by(public_id=public_id).first() if public_id else None


def _date(name: str, default: date | None = None) -> date | None:
    v = request.values.get(name, "")
    try:
        return date.fromisoformat(v) if v else default
    except ValueError:
        return default


def _member(number: str | None) -> Member | None:
    if not number:
        return None
    return member_svc.members_query().filter(func.upper(Member.membership_number) == number.strip().upper()).first()


def _branch_id(pid: str | None) -> int | None:
    b = next((b for b in member_svc.allowed_branches() if b.public_id == pid), None) if pid else None
    if b:
        return b.id
    limit = member_svc.branch_limit_id()
    return limit if limit not in (None, -1) else None


def _period():
    today = date.today()
    start = _date("from", today.replace(day=1))
    end = _date("to", today)
    if start > end:
        start, end = end, start
    return start, end


def _common_options():
    return {
        "income_accounts": ledger.accounts(AccountType.INCOME),
        "expense_accounts": ledger.accounts(AccountType.EXPENSE),
        "money_accounts": ledger.money_accounts(),
        "methods": METHODS,
        "branches": member_svc.allowed_branches(),
        "campaigns": db.session.query(Campaign).filter_by(is_active=True).order_by(Campaign.name).all(),
        "today": date.today(),
    }


def _next(default: str) -> str:
    nxt = request.form.get("next") or ""
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else default


# --------------------------------------------------------------------------- #
# Overview
# --------------------------------------------------------------------------- #
@bp.get("/finance")
@permission_required("finance.view")
def index():
    ledger.ensure_chart()
    db.session.commit()
    today = date.today()
    month_start = today.replace(day=1)
    giving = fin.giving_summary(month_start, today)
    stmt = ledger.income_statement(month_start, today, member_svc.branch_limit_id())
    labels, inc, exp = [], [], []
    cursor = (month_start - timedelta(days=150)).replace(day=1)
    while cursor <= today:
        nxt = (cursor + timedelta(days=32)).replace(day=1)
        s = ledger.income_statement(cursor, min(nxt - timedelta(days=1), today), member_svc.branch_limit_id())
        labels.append(cursor.strftime("%b"))
        inc.append(float(s["total_income"]))
        exp.append(float(s["total_expenses"]))
        cursor = nxt
    balances = [(a, ledger.balance(a, as_of=today)) for a in ledger.money_accounts()]
    pending = fin.expenses_query().filter(Expense.status == ExpenseStatus.SUBMITTED).count()
    unpaid = fin.expenses_query().filter(Expense.status == ExpenseStatus.APPROVED).count()
    recent = (fin.contributions_query().order_by(Contribution.created_at.desc()).limit(8).all())
    return render_template(
        "finance/index.html", giving=giving, stmt=stmt, balances=balances, pending=pending, unpaid=unpaid,
        recent=recent, cash_total=sum((b for _, b in balances), ZERO),
        trend={"type": "bar", "currency": g.tenant.currency, "labels": labels,
               "series": [{"label": "Income", "data": inc}, {"label": "Expenses", "data": exp}]},
        mix={"type": "doughnut", "currency": g.tenant.currency, "labels": [k for k, _ in giving["by_type"]],
             "series": [{"label": "Giving", "data": [float(v) for _, v in giving["by_type"]]}]},
        breadcrumbs=[("Finance", None), ("Overview", None)],
    )


# --------------------------------------------------------------------------- #
# Giving
# --------------------------------------------------------------------------- #
@bp.get("/finance/giving")
@permission_required("finance.view")
def giving():
    start, end = _period()
    q = fin.contributions_query().filter(Contribution.received_on >= start, Contribution.received_on <= end)
    text = request.args.get("q", "").strip()
    if text:
        like = f"%{text}%"
        q = q.outerjoin(Member, Member.id == Contribution.member_id).filter(or_(
            Contribution.receipt_number.ilike(like), Contribution.giver_name.ilike(like), Contribution.reference.ilike(like),
            Member.first_name.ilike(like), Member.last_name.ilike(like), Member.membership_number.ilike(like)))
    acc = _acc(request.args.get("type"))
    if acc:
        q = q.filter(Contribution.income_account_id == acc.id)
    if request.args.get("method") in {m.value for m in PaymentMethod}:
        q = q.filter(Contribution.method == PaymentMethod(request.args["method"]))
    if request.args.get("status") == "void":
        q = q.filter(Contribution.status == ContributionStatus.VOID)
    else:
        q = q.filter(Contribution.status == ContributionStatus.POSTED)
    total = money(Decimal(str(q.with_entities(func.coalesce(func.sum(Contribution.amount), 0)).scalar())))
    if request.args.get("format") == "csv":
        if not g.account.can("finance.export"):
            abort(403)
        rows = [["Receipt", "Date", "Giver", "Membership number", "Type", "Amount", "Method", "Reference", "Deposited to", "Status"]]
        for c in q.order_by(Contribution.received_on, Contribution.id).all():
            rows.append([c.receipt_number, c.received_on.isoformat(), c.giver,
                         c.member.membership_number if c.member else "", c.income_account.name, f"{c.amount:.2f}",
                         c.method.value, c.reference or "", c.deposit_account.name, c.status.value])
        audit.record("finance.giving_exported", actor=g.user, rows=len(rows) - 1)
        db.session.commit()
        return Response(member_svc.to_csv(rows), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="giving-{start}-{end}.csv"'})
    page = q.order_by(Contribution.received_on.desc(), Contribution.id.desc()).paginate(
        page=request.args.get("page", 1, type=int), per_page=30, error_out=False)
    params = {k: v for k, v in request.args.items() if k not in ("page", "format") and v}
    return render_template("finance/giving.html", page=page, total=total, start=start, end=end, q=text, params=params,
                           **_common_options(), breadcrumbs=[("Finance", url_for("finance.index")), ("Giving", None)])


@bp.route("/finance/giving/new", methods=["GET", "POST"])
@permission_required("finance.create")
@limiter.limit("120 per minute", methods=["POST"])
def record_giving():
    opts = _common_options()
    pledge = fin.get_or_404(Pledge, request.values["pledge"]) if request.values.get("pledge") else None
    form = request.form if request.method == "POST" else {}
    if request.method == "POST":
        member = _member(form.get("member_number"))
        if form.get("member_number") and member is None:
            flash("No member with that number.", "danger")
        else:
            service = ops.services_query().filter(Service.public_id == form.get("service")).first() if form.get("service") else None
            try:
                c = fin.record_contribution(
                    received_on=_date("received_on", date.today()), amount=form.get("amount"),
                    income_account=_acc(form.get("income_account")), deposit_account=_acc(form.get("deposit_account")),
                    method=PaymentMethod(form.get("method", "cash")), member=member, giver_name=form.get("giver_name", "").strip(),
                    reference=form.get("reference", "").strip(), pledge=pledge,
                    campaign=db.session.query(Campaign).filter_by(public_id=form.get("campaign")).first() if form.get("campaign") else None,
                    service=service, branch_id=_branch_id(form.get("branch")) if not member else None,
                    notes=form.get("notes", "").strip(), actor=g.user)
            except (LedgerError, ValueError) as e:
                db.session.rollback()
                flash(str(e), "danger")
            else:
                db.session.commit()
                flash(f"{c.receipt_number}: {c.amount:,.2f} from {c.giver} recorded.", "success")
                if form.get("again") == "1":
                    return redirect(url_for("finance.record_giving", type=form.get("income_account"),
                                            account=form.get("deposit_account"), method=form.get("method")))
                return redirect(url_for("finance.receipt", public_id=c.public_id))
    services = (ops.services_query().filter(Service.service_date >= date.today() - timedelta(days=14),
                                                 Service.service_date <= date.today())
                .order_by(Service.service_date.desc()).all())
    return render_template("finance/giving_form.html", form=form, pledge=pledge, services=services, **opts,
                           breadcrumbs=[("Giving", url_for("finance.giving")), ("Record giving", None)])


@bp.route("/finance/giving/offering", methods=["GET", "POST"])
@permission_required("finance.create")
def offering():
    """Count and record a service's offerings (several lines at once, no individual givers)."""
    opts = _common_options()
    services = (ops.services_query().filter(Service.service_date >= date.today() - timedelta(days=30),
                                                 Service.service_date <= date.today())
                .order_by(Service.service_date.desc()).all())
    if request.method == "POST":
        service = ops.services_query().filter(Service.public_id == request.form.get("service")).first()
        types, amounts, accounts, methods = (request.form.getlist(k) for k in ("line_type", "line_amount", "line_account", "line_method"))
        created = []
        try:
            if service is None:
                raise LedgerError("Choose the service.")
            for i, amt in enumerate(amounts):
                if not (amt or "").strip():
                    continue
                created.append(fin.record_contribution(
                    received_on=service.service_date, amount=amt, income_account=_acc(types[i] if i < len(types) else None),
                    deposit_account=_acc(accounts[i] if i < len(accounts) else None),
                    method=PaymentMethod(methods[i] if i < len(methods) and methods[i] else "cash"),
                    giver_name=f"{service.title} offering", service=service, branch_id=service.branch_id, actor=g.user))
            if not created:
                raise LedgerError("Enter at least one amount.")
        except (LedgerError, ValueError) as e:
            db.session.rollback()
            flash(str(e), "danger")
        else:
            db.session.commit()
            flash(f"{len(created)} offering line(s) recorded, total {sum(c.amount for c in created):,.2f}.", "success")
            return redirect(url_for("finance.giving", **{"from": service.service_date.isoformat(), "to": service.service_date.isoformat()}))
    return render_template("finance/offering.html", services=services, **opts,
                           breadcrumbs=[("Giving", url_for("finance.giving")), ("Service offering", None)])


@bp.get("/finance/receipts/<public_id>")
@permission_required("finance.view")
def receipt(public_id: str):
    c = fin.get_or_404(Contribution, public_id)
    return render_template("finance/receipt.html", c=c,
                           breadcrumbs=[("Giving", url_for("finance.giving")), (c.receipt_number, None)])


@bp.post("/finance/receipts/<public_id>/void")
@permission_required("finance.edit")
def void_receipt(public_id: str):
    c = fin.get_or_404(Contribution, public_id)
    try:
        fin.void_contribution(c, request.form.get("reason", ""), g.user)
        db.session.commit()
        flash(f"{c.receipt_number} voided. A reversing entry was posted.", "success")
    except LedgerError as e:
        db.session.rollback()
        flash(str(e), "danger")
    return redirect(url_for("finance.receipt", public_id=public_id))


@bp.get("/finance/statements/<member_id>")
@permission_required("finance.view")
def statement(member_id: str):
    member = member_svc.get_member_or_404(member_id)
    year = request.args.get("year", date.today().year, type=int)
    data = fin.member_statement(member, date(year, 1, 1), date(year, 12, 31))
    return render_template("finance/statement.html", member=member, year=year, data=data,
                           breadcrumbs=[("Members", url_for("members.profile", public_id=member_id)), ("Giving statement", None)])


# --------------------------------------------------------------------------- #
# Pledges and campaigns
# --------------------------------------------------------------------------- #
@bp.route("/finance/pledges", methods=["GET", "POST"])
@permission_required("finance.view")
def pledges():
    opts = _common_options()
    if request.method == "POST":
        if not g.account.can("finance.create"):
            abort(403)
        member = _member(request.form.get("member_number"))
        campaign = db.session.query(Campaign).filter_by(public_id=request.form.get("campaign")).first() if request.form.get("campaign") else None
        income = campaign.income_account if campaign else _acc(request.form.get("income_account"))
        try:
            if request.form.get("member_number") and member is None:
                raise LedgerError("No member with that number.")
            if not member and not request.form.get("pledger_name", "").strip():
                raise LedgerError("Enter the membership number or the name of the person pledging.")
            amount = money(request.form.get("amount"))
            if amount <= 0:
                raise LedgerError("The amount must be more than zero.")
            if income is None or income.type != AccountType.INCOME:
                raise LedgerError("Choose what the pledge is for.")
            p = Pledge(member_id=member.id if member else None, pledger_name=request.form.get("pledger_name", "").strip() or None,
                       campaign_id=campaign.id if campaign else None, income_account_id=income.id, amount=amount,
                       pledge_date=_date("pledge_date", date.today()), due_date=_date("due_date"),
                       notes=request.form.get("notes", "").strip() or None)
            db.session.add(p)
            db.session.flush()
            audit.record("finance.pledge_recorded", entity=p, actor=g.user, name=p.name, amount=str(amount))
            db.session.commit()
            flash(f"Pledge of {amount:,.2f} recorded for {p.name}. It becomes income only as payments are received.", "success")
            return redirect(url_for("finance.pledges"))
        except LedgerError as e:
            db.session.rollback()
            flash(str(e), "danger")
    status = request.args.get("status", "open")
    q = db.session.query(Pledge)
    if status in {s.value for s in PledgeStatus}:
        q = q.filter(Pledge.status == PledgeStatus(status))
    rows = q.order_by(Pledge.pledge_date.desc()).all()
    data = [{"p": p, "paid": fin.pledge_paid(p)} for p in rows]
    totals = {"pledged": sum((d["p"].amount for d in data), ZERO), "paid": sum((d["paid"] for d in data), ZERO)}
    return render_template("finance/pledges.html", rows=data, totals=totals, status=status, **opts,
                           breadcrumbs=[("Finance", url_for("finance.index")), ("Pledges", None)])


@bp.post("/finance/pledges/<public_id>/cancel")
@permission_required("finance.edit")
def cancel_pledge(public_id: str):
    p = fin.get_or_404(Pledge, public_id)
    p.status = PledgeStatus.CANCELLED
    audit.record("finance.pledge_cancelled", entity=p, actor=g.user, name=p.name)
    db.session.commit()
    flash("Pledge cancelled. Payments already received are unaffected.", "success")
    return redirect(url_for("finance.pledges"))


@bp.route("/finance/campaigns", methods=["GET", "POST"])
@permission_required("finance.view")
def campaigns():
    opts = _common_options()
    if request.method == "POST":
        if not g.account.can("finance.manage_settings"):
            abort(403)
        income = _acc(request.form.get("income_account"))
        name = request.form.get("name", "").strip()
        try:
            if not name:
                raise LedgerError("Enter a name.")
            if income is None or income.type != AccountType.INCOME:
                raise LedgerError("Choose the income account for this campaign.")
            target = money(request.form["target"]) if request.form.get("target") else None
            c = Campaign(name=name[:160], description=request.form.get("description") or None, income_account_id=income.id,
                         target_amount=target, start_date=_date("start_date"), end_date=_date("end_date"))
            db.session.add(c)
            db.session.flush()
            audit.record("finance.campaign_created", entity=c, actor=g.user, name=c.name)
            db.session.commit()
            flash(f"Campaign “{c.name}” created.", "success")
            return redirect(url_for("finance.campaigns"))
        except LedgerError as e:
            db.session.rollback()
            flash(str(e), "danger")
    rows = [{"c": c, **fin.campaign_progress(c)} for c in db.session.query(Campaign).order_by(Campaign.is_active.desc(), Campaign.name)]
    return render_template("finance/campaigns.html", rows=rows, **opts,
                           breadcrumbs=[("Finance", url_for("finance.index")), ("Campaigns and projects", None)])


# --------------------------------------------------------------------------- #
# Expenses
# --------------------------------------------------------------------------- #
@bp.get("/finance/expenses")
@permission_required("finance.view")
def expenses():
    status = request.args.get("status", "submitted")
    q = fin.expenses_query()
    if status in {s.value for s in ExpenseStatus}:
        q = q.filter(Expense.status == ExpenseStatus(status))
    rows = q.order_by(Expense.expense_date.desc(), Expense.id.desc()).limit(200).all()
    counts = dict(fin.expenses_query().with_entities(Expense.status, func.count()).group_by(Expense.status).all())
    return render_template("finance/expenses.html", rows=rows, status=status,
                           counts={k.value: v for k, v in counts.items()},
                           breadcrumbs=[("Finance", url_for("finance.index")), ("Expenses", None)])


@bp.route("/finance/expenses/new", methods=["GET", "POST"])
@permission_required("finance.create")
def new_expense():
    opts = _common_options()
    from app.models import Event
    departments = db.session.query(Department).filter_by(is_active=True).order_by(Department.name).all()
    events = (db.session.query(Event).filter(Event.starts_at >= datetime.combine(date.today() - timedelta(days=120), datetime.min.time()))
              .order_by(Event.starts_at).all())
    form = request.form if request.method == "POST" else {"event": request.args.get("event", "")}
    if request.method == "POST":
        dept = db.session.query(Department).filter_by(public_id=form.get("department")).first() if form.get("department") else None
        event = db.session.query(Event).filter_by(public_id=form.get("event")).first() if form.get("event") else None
        try:
            if not form.get("payee", "").strip() or not form.get("description", "").strip():
                raise LedgerError("Enter the payee and what the payment is for.")
            e = fin.create_expense(expense_date=_date("expense_date", date.today()), payee=form["payee"][:160],
                                   description=form["description"][:255], expense_account=_acc(form.get("expense_account")),
                                   amount=form.get("amount"), department=dept, branch_id=_branch_id(form.get("branch")),
                                   actor=g.user)
            e.event_id = event.id if event else None
            f = request.files.get("attachment")
            if f and f.filename:
                stored = storage.save(f, filename=f.filename, kind="document", category="vouchers", content_type=f.mimetype)
                e.attachment_key, e.attachment_name = stored.key, f.filename[:255]
        except (LedgerError, StorageError) as ex:
            db.session.rollback()
            flash(str(ex), "danger")
        else:
            db.session.commit()
            flash(f"{e.voucher_number} submitted for approval.", "success")
            return redirect(url_for("finance.expense", public_id=e.public_id))
    return render_template("finance/expense_form.html", form=form, departments=departments, events=events, **opts,
                           breadcrumbs=[("Expenses", url_for("finance.expenses")), ("New payment voucher", None)])


@bp.get("/finance/expenses/<public_id>")
@permission_required("finance.view")
def expense(public_id: str):
    e = fin.get_or_404(Expense, public_id)
    from app.models import User
    from app.utils.tenancy import tenant_bypass
    with tenant_bypass():
        names = {u.id: u.full_name for u in db.session.query(User).filter(User.id.in_(
            [i for i in (e.requested_by_id, e.approved_by_id) if i]))}
    return render_template("finance/expense.html", e=e, names=names, **_common_options(),
                           breadcrumbs=[("Expenses", url_for("finance.expenses")), (e.voucher_number, None)])


@bp.post("/finance/expenses/<public_id>/<action>")
@permission_required("finance.view")
def expense_action(public_id: str, action: str):
    e = fin.get_or_404(Expense, public_id)
    needed = {"approve": "finance.approve", "reject": "finance.approve", "pay": "finance.edit", "void": "finance.approve"}
    if action not in needed:
        abort(404)
    if not g.account.can(needed[action]):
        abort(403)
    try:
        if action == "approve":
            fin.approve_expense(e, g.user, request.form.get("note"))
        elif action == "reject":
            fin.reject_expense(e, g.user, request.form.get("note", ""))
        elif action == "pay":
            fin.pay_expense(e, paid_on=_date("paid_on", date.today()), payment_account=_acc(request.form.get("payment_account")),
                            method=PaymentMethod(request.form.get("method", "cash")),
                            reference=request.form.get("reference", "").strip(), actor=g.user)
        else:
            fin.void_expense(e, request.form.get("note", ""), g.user)
        db.session.commit()
        flash(f"{e.voucher_number}: {e.status.value}.", "success")
    except (LedgerError, ValueError) as ex:
        db.session.rollback()
        flash(str(ex), "danger")
    return redirect(url_for("finance.expense", public_id=public_id))


@bp.get("/finance/expenses/<public_id>/attachment")
@permission_required("finance.view")
def expense_attachment(public_id: str):
    e = fin.get_or_404(Expense, public_id)
    if not e.attachment_key:
        abort(404)
    return send_file(storage.open_file(e.attachment_key), as_attachment=True, download_name=e.attachment_name or "attachment")


# --------------------------------------------------------------------------- #
# Transfers
# --------------------------------------------------------------------------- #
@bp.route("/finance/transfer", methods=["GET", "POST"])
@permission_required("finance.create")
def transfer():
    opts = _common_options()
    if request.method == "POST":
        try:
            entry = fin.transfer(on=_date("on", date.today()), from_account=_acc(request.form.get("from_account")),
                                 to_account=_acc(request.form.get("to_account")), amount=request.form.get("amount"),
                                 memo=request.form.get("memo", "").strip(), fee=request.form.get("fee"), actor=g.user)
        except LedgerError as e:
            db.session.rollback()
            flash(str(e), "danger")
        else:
            db.session.commit()
            flash(f"Transfer recorded ({entry.number}).", "success")
            return redirect(url_for("finance.index"))
    return render_template("finance/transfer.html", **opts,
                           breadcrumbs=[("Finance", url_for("finance.index")), ("Transfer money", None)])


# --------------------------------------------------------------------------- #
# Budgets
# --------------------------------------------------------------------------- #
@bp.route("/finance/budgets", methods=["GET", "POST"])
@permission_required("finance.view")
def budgets():
    if request.method == "POST":
        if not g.account.can("finance.manage_settings"):
            abort(403)
        start, end = _date("start_date"), _date("end_date")
        name = request.form.get("name", "").strip()
        if not name or not start or not end or end <= start:
            flash("Enter a name and a valid period.", "danger")
        else:
            b = Budget(name=name[:120], start_date=start, end_date=end)
            db.session.add(b)
            db.session.flush()
            audit.record("finance.budget_created", entity=b, actor=g.user, name=b.name)
            db.session.commit()
            return redirect(url_for("finance.budget", public_id=b.public_id))
    year = date.today().year
    rows = db.session.query(Budget).order_by(Budget.start_date.desc()).all()
    return render_template("finance/budgets.html", rows=rows, year=year,
                           breadcrumbs=[("Finance", url_for("finance.index")), ("Budgets", None)])


@bp.route("/finance/budgets/<public_id>", methods=["GET", "POST"])
@permission_required("finance.view")
def budget(public_id: str):
    b = db.session.query(Budget).filter_by(public_id=public_id).first_or_404()
    departments = db.session.query(Department).filter_by(is_active=True).order_by(Department.name).all()
    if request.method == "POST":
        if not g.account.can("finance.manage_settings"):
            abort(403)
        try:
            existing = {(l.account_id, l.department_id): l for l in b.lines}
            seen = set()
            for acc in ledger.accounts(AccountType.INCOME, AccountType.EXPENSE):
                raw = request.form.get(f"amt_{acc.public_id}", "").strip()
                key = (acc.id, None)
                if raw:
                    amt = money(raw)
                    if amt < 0:
                        raise LedgerError("Budget amounts cannot be negative.")
                    line = existing.get(key) or BudgetLine(budget_id=b.id, account_id=acc.id)
                    line.amount = amt
                    db.session.add(line)
                    seen.add(key)
            # Department lines
            for acc_pid, dept_pid, raw in zip(request.form.getlist("dl_account"), request.form.getlist("dl_department"),
                                              request.form.getlist("dl_amount")):
                acc, dept = _acc(acc_pid), db.session.query(Department).filter_by(public_id=dept_pid).first()
                if not (acc and dept and raw.strip()):
                    continue
                key = (acc.id, dept.id)
                line = existing.get(key) or BudgetLine(budget_id=b.id, account_id=acc.id, department_id=dept.id)
                line.amount = money(raw)
                db.session.add(line)
                seen.add(key)
            for key, line in existing.items():
                if key not in seen:
                    db.session.delete(line)
            audit.record("finance.budget_updated", entity=b, actor=g.user, name=b.name)
            db.session.commit()
            flash("Budget saved.", "success")
            return redirect(url_for("finance.budget", public_id=public_id))
        except LedgerError as e:
            db.session.rollback()
            flash(str(e), "danger")
    report = fin.budget_vs_actual(b)
    amounts = {l.account_id: l.amount for l in b.lines if l.department_id is None}
    return render_template("finance/budget.html", b=b, report=report, amounts=amounts, departments=departments,
                           income_accounts=ledger.accounts(AccountType.INCOME),
                           expense_accounts=ledger.accounts(AccountType.EXPENSE),
                           dept_lines=[l for l in b.lines if l.department_id],
                           breadcrumbs=[("Budgets", url_for("finance.budgets")), (b.name, None)])
