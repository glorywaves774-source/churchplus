"""Reports and analytics (Stage 9).

Every report is a function returning a :class:`Report` – columns, rows, optional totals and a
chart – so the same data feeds the page, CSV, Excel and PDF. Each report declares the
permissions it needs on top of ``reports.view`` (e.g. giving needs ``finance.view``), and
all queries go through the usual tenant-scoped helpers.
"""
from __future__ import annotations

import io
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Callable

from flask import g
from sqlalchemy import func

from app.extensions import db
from app.models import (
    AttendanceRecord, Branch, ChildCheckIn, ChildClass, ChildProfile, Contribution, ContributionStatus, Department,
    DepartmentMembership, Event, EventRegistration, Expense, ExpenseStatus, Gender, Member, MemberStatus, Message,
    RegistrationStatus, Service, ServiceStatus, Visitor, VisitorStatus,
)
from app.services import members as member_svc

ZERO = Decimal("0.00")


@dataclass
class Report:
    title: str
    columns: list[str]
    rows: list[list]
    totals: list | None = None
    chart: dict | None = None
    numeric: set[int] = field(default_factory=set)     # column indexes that are numbers
    money: set[int] = field(default_factory=set)       # column indexes that are amounts
    note: str | None = None
    summary: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class ReportDef:
    key: str
    title: str
    description: str
    icon: str
    group: str
    needs: tuple[str, ...]
    build: Callable[["Params"], Report]
    uses_dates: bool = True


@dataclass
class Params:
    start: date
    end: date
    branch: Branch | None = None


REGISTRY: "OrderedDict[str, ReportDef]" = OrderedDict()


def report(key, title, description, icon, group, needs=(), uses_dates=True):
    def deco(fn):
        REGISTRY[key] = ReportDef(key, title, description, icon, group, tuple(needs), fn, uses_dates)
        return fn
    return deco


def available(can) -> list[ReportDef]:
    return [r for r in REGISTRY.values() if can("reports.view") and all(can(n) for n in r.needs)]


def months_between(start: date, end: date) -> list[date]:
    out, d = [], start.replace(day=1)
    while d <= end:
        out.append(d)
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


def _members(p: Params):
    q = member_svc.members_query()
    if p.branch is not None:
        q = q.filter(Member.branch_id == p.branch.id)
    return q


# --------------------------------------------------------------------------- #
# People
# --------------------------------------------------------------------------- #
AGE_BANDS = [("Children (0–12)", 0, 12), ("Teens (13–17)", 13, 17), ("Young adults (18–35)", 18, 35),
             ("Adults (36–59)", 36, 59), ("Seniors (60+)", 60, 200)]


@report("membership", "Membership summary", "Members by status, gender and age, and new members per month.",
        "users", "People", ("members.view",))
def membership(p: Params) -> Report:
    members = _members(p).all()
    rows = []
    for st, n in sorted(Counter(m.status.value for m in members).items()):
        rows.append(["Status", st.replace("_", " ").capitalize(), n])
    for gd, n in sorted(Counter((m.gender.value if m.gender else "not recorded") for m in members).items()):
        rows.append(["Gender", gd.capitalize(), n])
    ages = Counter()
    for m in members:
        a = m.age
        label = next((b[0] for b in AGE_BANDS if a is not None and b[1] <= a <= b[2]), "Age not recorded")
        ages[label] += 1
    for label, *_ in AGE_BANDS + [("Age not recorded",)]:
        if ages.get(label):
            rows.append(["Age", label, ages[label]])
    months = months_between(p.start, p.end)
    joined = Counter((m.date_joined.year, m.date_joined.month) for m in members
                     if m.date_joined and p.start <= m.date_joined <= p.end)
    chart = {"type": "bar", "labels": [d.strftime("%b %y") for d in months],
             "series": [{"label": "New members", "data": [joined.get((d.year, d.month), 0) for d in months]}]}
    active = sum(1 for m in members if m.status == MemberStatus.ACTIVE)
    return Report("Membership summary", ["Group", "Category", "Members"], rows, chart=chart, numeric={2},
                  summary=[("Total members", str(len(members))), ("Active", str(active)),
                           ("Joined in period", str(sum(joined.values())))])


@report("member_list", "Member directory", "Every member with contact details, for printing or Excel.",
        "id-card", "People", ("members.view", "members.export"), uses_dates=False)
def member_list(p: Params) -> Report:
    rows = [[m.membership_number, m.display_name, m.gender.value.capitalize() if m.gender else "", m.phone or "",
             m.email or "", m.status.value.capitalize(), m.branch.name if getattr(m, "branch", None) else "",
             m.date_joined.isoformat() if m.date_joined else ""]
            for m in _members(p).order_by(Member.last_name, Member.first_name)]
    return Report("Member directory", ["Number", "Name", "Gender", "Phone", "Email", "Status", "Branch", "Joined"], rows,
                  note="Contains personal data. Share only with people who need it (Data Protection Act, 2012).")


@report("birthdays", "Birthdays", "Members' birthdays in the chosen period (by day and month).", "cake", "People",
        ("members.view",))
def birthdays(p: Params) -> Report:
    span = {(d.month, d.day) for d in (p.start + timedelta(days=i) for i in range(min((p.end - p.start).days + 1, 366)))}
    rows = sorted(([m.date_of_birth.strftime("%m-%d"), m.date_of_birth.strftime("%d %B"), m.display_name, m.phone or "",
                    (m.age + (1 if (m.date_of_birth.month, m.date_of_birth.day) > (date.today().month, date.today().day) else 0))
                    if m.age is not None else ""]
                   for m in _members(p).filter(Member.date_of_birth.isnot(None), Member.status == MemberStatus.ACTIVE)
                   if (m.date_of_birth.month, m.date_of_birth.day) in span), key=lambda r: r[0])
    return Report("Birthdays", ["Date", "Name", "Phone", "Turning"], [r[1:] for r in rows], numeric={3})


@report("visitors", "First-timers and follow-up", "New visitors per month, how many came back and how many joined.",
        "user-plus", "People", ("visitors.view",))
def visitors(p: Params) -> Report:
    q = db.session.query(Visitor).filter(Visitor.first_visit_date >= p.start, Visitor.first_visit_date <= p.end)
    if p.branch is not None:
        q = q.filter(Visitor.branch_id == p.branch.id)
    vs = q.all()
    visits = Counter()
    if vs:
        for vid, n in (db.session.query(AttendanceRecord.visitor_id, func.count())
                       .filter(AttendanceRecord.visitor_id.in_([v.id for v in vs])).group_by(AttendanceRecord.visitor_id)):
            visits[vid] = n
    months = months_between(p.start, p.end)
    by = defaultdict(lambda: [0, 0, 0])
    for v in vs:
        k = (v.first_visit_date.year, v.first_visit_date.month)
        by[k][0] += 1
        by[k][1] += 1 if visits[v.id] > 1 or v.status in (VisitorStatus.REGULAR, VisitorStatus.CONVERTED) else 0
        by[k][2] += 1 if v.status == VisitorStatus.CONVERTED else 0
    rows = [[d.strftime("%B %Y"), *by[(d.year, d.month)]] for d in months]
    tot = [sum(r[i] for r in rows) for i in (1, 2, 3)]
    chart = {"type": "bar", "labels": [d.strftime("%b %y") for d in months],
             "series": [{"label": "First-timers", "data": [r[1] for r in rows]}, {"label": "Became members", "data": [r[3] for r in rows]}]}
    rate = f"{(tot[2] / tot[0] * 100):.0f}%" if tot[0] else "—"
    return Report("First-timers and follow-up", ["Month", "First-timers", "Came back", "Became members"], rows,
                  totals=["Total", *tot], chart=chart, numeric={1, 2, 3},
                  summary=[("First-timers", str(tot[0])), ("Conversion", rate)])


@report("departments", "Departments and ministries", "How many members serve in each department.", "network",
        "People", ("departments.view",), uses_dates=False)
def departments(p: Params) -> Report:
    counts = dict(db.session.query(DepartmentMembership.department_id, func.count(DepartmentMembership.id))
                  .group_by(DepartmentMembership.department_id).all())
    rows = [[d.name, d.kind.value.capitalize(), counts.get(d.id, 0)]
            for d in db.session.query(Department).filter_by(is_active=True).order_by(Department.name)]
    rows.sort(key=lambda r: -r[2])
    chart = {"type": "bar", "labels": [r[0] for r in rows[:12]], "series": [{"label": "Members", "data": [r[2] for r in rows[:12]]}]}
    return Report("Departments and ministries", ["Department", "Type", "Members"], rows, chart=chart, numeric={2})


# --------------------------------------------------------------------------- #
# Worship and ministry
# --------------------------------------------------------------------------- #
@report("attendance", "Service attendance", "Headcounts and members checked in, per service.", "clipboard-check",
        "Worship and ministry", ("attendance.view",))
def attendance(p: Params) -> Report:
    q = db.session.query(Service).filter(Service.service_date >= p.start, Service.service_date <= p.end,
                                         Service.status != ServiceStatus.CANCELLED)
    if p.branch is not None:
        q = q.filter(Service.branch_id == p.branch.id)
    services = q.order_by(Service.service_date).all()
    checked = dict(db.session.query(AttendanceRecord.service_id, func.count(AttendanceRecord.id))
                   .filter(AttendanceRecord.service_id.in_([s.id for s in services] or [-1]))
                   .group_by(AttendanceRecord.service_id).all())
    rows = []
    for s in services:
        men, women, kids, vis = (s.headcount_men or 0), (s.headcount_women or 0), (s.headcount_children or 0), (s.headcount_visitors or 0)
        rows.append([s.service_date.isoformat(), s.title, men, women, kids, vis, men + women + kids + vis, checked.get(s.id, 0)])
    tot = [sum(r[i] for r in rows) for i in range(2, 8)]
    avg = round(tot[4] / len(rows)) if rows else 0
    chart = {"type": "line", "labels": [r[0][5:] for r in rows], "series": [{"label": "Total attendance", "data": [r[6] for r in rows]},
                                                                           {"label": "Members checked in", "data": [r[7] for r in rows]}]}
    return Report("Service attendance", ["Date", "Service", "Men", "Women", "Children", "Visitors", "Total", "Checked in"], rows,
                  totals=["Total", "", *tot], chart=chart, numeric={2, 3, 4, 5, 6, 7},
                  summary=[("Services", str(len(rows))), ("Average attendance", str(avg))])


@report("events", "Events", "Registrations, attendance and spending per event.", "calendar-days",
        "Worship and ministry", ("events.view",))
def events(p: Params) -> Report:
    evs = (db.session.query(Event).filter(Event.starts_at >= datetime.combine(p.start, time.min),
                                          Event.starts_at <= datetime.combine(p.end, time.max)).order_by(Event.starts_at).all())
    rows = []
    for e in evs:
        regs = [r for r in e.registrations if r.status == RegistrationStatus.REGISTERED]
        spent = (db.session.query(func.coalesce(func.sum(Expense.amount), 0))
                 .filter(Expense.event_id == e.id, Expense.status.in_([ExpenseStatus.APPROVED, ExpenseStatus.PAID])).scalar())
        rows.append([e.starts_at.date().isoformat(), e.title, e.status.value.capitalize(), sum(1 + r.guests for r in regs),
                     sum(1 for r in e.registrations if r.attended), e.budget_amount or ZERO, Decimal(str(spent or 0))])
    return Report("Events", ["Date", "Event", "Status", "Registered", "Attended", "Budget", "Spent"], rows,
                  totals=["Total", "", "", sum(r[3] for r in rows), sum(r[4] for r in rows),
                          sum((r[5] for r in rows), ZERO), sum((r[6] for r in rows), ZERO)],
                  numeric={3, 4}, money={5, 6})


@report("children", "Children's check-ins", "Children checked in per class and week.", "school",
        "Worship and ministry", ("children.view",))
def children(p: Params) -> Report:
    rows_q = (db.session.query(ChildCheckIn).filter(ChildCheckIn.check_in_date >= p.start, ChildCheckIn.check_in_date <= p.end)
              .filter(ChildCheckIn.child_id.in_(db.session.query(ChildProfile.id))).all())
    classes = {c.id: c.name for c in db.session.query(ChildClass)}
    by = defaultdict(Counter)
    for ci in rows_q:
        wk = ci.check_in_date - timedelta(days=ci.check_in_date.weekday())
        by[wk][classes.get(ci.class_id, "No class")] += 1
    names = sorted({n for c in by.values() for n in c})
    weeks = sorted(by)
    rows = [[w.strftime("Week of %d %b %Y"), *[by[w].get(n, 0) for n in names], sum(by[w].values())] for w in weeks]
    chart = {"type": "bar", "labels": [w.strftime("%d %b") for w in weeks],
             "series": [{"label": n, "data": [by[w].get(n, 0) for w in weeks]} for n in names[:6]]}
    return Report("Children's check-ins", ["Week", *names, "Total"], rows,
                  totals=["Total", *[sum(r[i + 1] for r in rows) for i in range(len(names))], sum(r[-1] for r in rows)],
                  chart=chart, numeric=set(range(1, len(names) + 2)))


# --------------------------------------------------------------------------- #
# Finance
# --------------------------------------------------------------------------- #
@report("giving", "Giving by month", "Tithes, offerings and other giving per month.", "hand-coins", "Finance",
        ("finance.view",))
def giving(p: Params) -> Report:
    q = db.session.query(Contribution).filter(Contribution.status == ContributionStatus.POSTED,
                                              Contribution.received_on >= p.start, Contribution.received_on <= p.end)
    if p.branch is not None:
        q = q.filter(Contribution.branch_id == p.branch.id)
    months = months_between(p.start, p.end)
    by = defaultdict(lambda: defaultdict(lambda: ZERO))
    cats: Counter = Counter()
    members_giving = set()
    for c in q.all():
        name = c.income_account.name
        by[(c.received_on.year, c.received_on.month)][name] += c.amount
        cats[name] += 1
        if c.member_id:
            members_giving.add(c.member_id)
    names = [n for n, _ in cats.most_common()]
    rows = [[d.strftime("%B %Y"), *[by[(d.year, d.month)][n] for n in names],
             sum(by[(d.year, d.month)].values(), ZERO)] for d in months]
    totals = ["Total", *[sum((r[i + 1] for r in rows), ZERO) for i in range(len(names))], sum((r[-1] for r in rows), ZERO)]
    chart = {"type": "bar", "labels": [d.strftime("%b %y") for d in months],
             "series": [{"label": n, "data": [float(by[(d.year, d.month)][n]) for d in months]} for n in names[:5]], "stacked": True}
    return Report("Giving by month", ["Month", *names, "Total"], rows, totals=totals, chart=chart,
                  money=set(range(1, len(names) + 2)),
                  summary=[("Total giving", f"{totals[-1]:,.2f}"), ("Members who gave", str(len(members_giving)))])


@report("expenses", "Spending by category", "Approved and paid vouchers by expense account.", "receipt", "Finance",
        ("finance.view",))
def expenses(p: Params) -> Report:
    q = db.session.query(Expense).filter(Expense.status.in_([ExpenseStatus.APPROVED, ExpenseStatus.PAID]),
                                         Expense.expense_date >= p.start, Expense.expense_date <= p.end)
    if p.branch is not None:
        q = q.filter(Expense.branch_id == p.branch.id)
    by, count = defaultdict(lambda: ZERO), Counter()
    for e in q.all():
        by[e.expense_account.name] += e.amount
        count[e.expense_account.name] += 1
    rows = sorted(([k, count[k], v] for k, v in by.items()), key=lambda r: -r[2])
    total = sum((r[2] for r in rows), ZERO)
    chart = {"type": "doughnut", "labels": [r[0] for r in rows[:8]], "series": [{"label": "Spent", "data": [float(r[2]) for r in rows[:8]]}]}
    return Report("Spending by category", ["Category", "Vouchers", "Amount"], rows, totals=["Total", sum(count.values()), total],
                  chart=chart, numeric={1}, money={2}, summary=[("Total spent", f"{total:,.2f}")])


# --------------------------------------------------------------------------- #
# Communication
# --------------------------------------------------------------------------- #
@report("messages", "Messages sent", "SMS, WhatsApp and email sent, with SMS parts used.", "send", "Communication",
        ("communication.view",))
def messages(p: Params) -> Report:
    q = (db.session.query(Message).filter(Message.sent_at >= datetime.combine(p.start, time.min),
                                          Message.sent_at <= datetime.combine(p.end, time.max)).order_by(Message.sent_at))
    rows = [[m.sent_at.date().isoformat(), m.audience_label or "", m.channel.value.upper() if m.channel.value == "sms" else m.channel.value.capitalize(),
             m.recipients_count, m.sent_count, m.failed_count, m.segments * m.sent_count if m.channel.value == "sms" else 0] for m in q]
    return Report("Messages sent", ["Date", "To", "Channel", "People", "Sent", "Failed", "SMS parts"], rows,
                  totals=["Total", "", "", *[sum(r[i] for r in rows) for i in (3, 4, 5, 6)]], numeric={3, 4, 5, 6})


# --------------------------------------------------------------------------- #
# Exports
# --------------------------------------------------------------------------- #
def _cell(v, money_col: bool):
    if isinstance(v, Decimal):
        return f"{v:,.2f}" if money_col else str(v)
    return "" if v is None else v


def to_csv(r: Report) -> str:
    from app.services.members import safe_cell, to_csv as rows_csv
    rows = [r.columns] + [[safe_cell(_cell(v, False)) for v in row] for row in r.rows]
    if r.totals:
        rows.append([safe_cell(_cell(v, False)) for v in r.totals])
    return rows_csv(rows)


def to_xlsx(r: Report, church: str, period: str) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from app.services.members import safe_cell
    wb = Workbook()
    ws = wb.active
    ws.title = r.title[:31]
    ws.append([church])
    ws.append([r.title + (f" · {period}" if period else "")])
    ws["A1"].font = Font(bold=True, size=13)
    ws["A2"].font = Font(color="555555")
    ws.append([])
    ws.append(r.columns)
    head = ws.max_row
    for c in ws[head]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="2A1D78")
    for row in r.rows + ([r.totals] if r.totals else []):
        ws.append([float(v) if isinstance(v, Decimal) else (safe_cell(v) if isinstance(v, str) else v) for v in row])
        for i in r.money:
            ws.cell(ws.max_row, i + 1).number_format = "#,##0.00"
    if r.totals:
        for c in ws[ws.max_row]:
            c.font = Font(bold=True)
    for i, col in enumerate(r.columns, start=1):
        width = max([len(str(col))] + [len(str(row[i - 1])) for row in r.rows[:500]]) + 2
        ws.column_dimensions[ws.cell(head, i).column_letter].width = min(max(width, 8), 50)
    ws.freeze_panes = ws.cell(head + 1, 1)
    for row in ws.iter_rows(min_row=head):
        for c in row:
            c.alignment = Alignment(vertical="top")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def to_pdf(r: Report, church: str, period: str) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    page = landscape(A4) if len(r.columns) > 5 else A4
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=14 * mm,
                            bottomMargin=14 * mm, title=r.title, author=church)
    styles = getSampleStyleSheet()
    small = styles["BodyText"].clone("small", fontSize=8.5, leading=10.5)
    story = [Paragraph(escape(church), styles["Title"]),
             Paragraph(escape(r.title + (f" · {period}" if period else "")), styles["Heading2"])]
    if r.summary:
        story.append(Paragraph(" · ".join(f"<b>{escape(k)}:</b> {escape(v)}" for k, v in r.summary), styles["BodyText"]))
    if r.note:
        story.append(Paragraph(f"<i>{escape(r.note)}</i>", small))
    story.append(Spacer(1, 6 * mm))
    right = r.numeric | r.money

    def fmt(i, v):
        if isinstance(v, Decimal):
            return f"{v:,.2f}"
        return Paragraph(escape(str(v)), small) if isinstance(v, str) and len(v) > 18 else ("" if v is None else str(v))
    data = [r.columns] + [[fmt(i, v) for i, v in enumerate(row)] for row in r.rows]
    if r.totals:
        data.append([fmt(i, v) for i, v in enumerate(r.totals)])
    if len(data) == 1:
        data.append(["No data for this period"] + [""] * (len(r.columns) - 1))
    t = Table(data, repeatRows=1)
    style = [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2A1D78")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
             ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 8.5),
             ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F5FA")]),
             ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D9DCE8")), ("VALIGN", (0, 0), (-1, -1), "TOP")]
    for i in right:
        style.append(("ALIGN", (i, 0), (i, -1), "RIGHT"))
    if r.totals:
        style.append(("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"))
    t.setStyle(TableStyle(style))
    story.append(t)
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(f"Generated {datetime.now():%d %b %Y %H:%M} with ChurchPlus", small))
    doc.build(story)
    return buf.getvalue()
