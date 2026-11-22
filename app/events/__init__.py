"""Events and programmes: calendar, registration, attendance, committees, budget and spend."""
from __future__ import annotations

import calendar as cal
from datetime import date, datetime, time, timedelta

from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from sqlalchemy import func

from app.extensions import db
from app.models import (
    Department, Event, EventCommitteeMember, EventRegistration, EventStatus, Expense, ExpenseStatus, Member,
    RegistrationStatus,
)
from app.services import audit, ledger, members as member_svc, ministry as svc
from app.services.ledger import LedgerError
from app.utils.context import permission_required

bp = Blueprint("events", __name__)
CATEGORIES = ["Programme", "Conference", "Crusade / outreach", "Retreat / camp", "Fellowship", "Fundraising",
              "Wedding", "Funeral / memorial", "Thanksgiving", "Training", "Meeting", "Other"]


def _dt(d: str, t: str) -> datetime | None:
    try:
        return datetime.combine(date.fromisoformat(d), datetime.strptime(t, "%H:%M").time() if t else time(0, 0))
    except (ValueError, TypeError):
        return None


def _member(number):
    if not number:
        return None
    return member_svc.members_query().filter(func.upper(Member.membership_number) == number.strip().upper()).first()


@bp.get("/events")
@permission_required("events.view")
def index():
    view = request.args.get("view", "list")
    today = date.today()
    if view == "calendar":
        try:
            month = date.fromisoformat(request.args.get("month", "") + "-01") if request.args.get("month") else today.replace(day=1)
        except ValueError:
            month = today.replace(day=1)
        first = month - timedelta(days=month.weekday())
        last_day = month.replace(day=cal.monthrange(month.year, month.month)[1])
        last = last_day + timedelta(days=6 - last_day.weekday())
        events = (svc.events_query().filter(Event.starts_at >= datetime.combine(first, time.min),
                                            Event.starts_at <= datetime.combine(last, time.max))
                  .order_by(Event.starts_at).all())
        by_day = {}
        for e in events:
            by_day.setdefault(e.starts_at.date(), []).append(e)
        days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        prev_m = (month - timedelta(days=1)).replace(day=1)
        next_m = (last_day + timedelta(days=1))
        return render_template("events/calendar.html", days=days, by_day=by_day, month=month, today=today,
                               prev_m=prev_m, next_m=next_m, breadcrumbs=[("Ministry", None), ("Events", None)])
    past = request.args.get("when") == "past"
    q = svc.events_query()
    q = q.filter(Event.starts_at < datetime.combine(today, time.min)) if past else \
        q.filter(Event.starts_at >= datetime.combine(today, time.min))
    rows = q.order_by(Event.starts_at.desc() if past else Event.starts_at).limit(100).all()
    taken = {e.id: svc.seats_taken(e) for e in rows}
    return render_template("events/index.html", rows=rows, taken=taken, past=past,
                           breadcrumbs=[("Ministry", None), ("Events", None)])


def _save(e: Event) -> str | None:
    f = request.form
    starts = _dt(f.get("start_date", ""), f.get("start_time", ""))
    ends = _dt(f.get("end_date") or f.get("start_date", ""), f.get("end_time", "")) if f.get("end_time") or f.get("end_date") else None
    if not f.get("title", "").strip():
        return "Enter a title."
    if starts is None:
        return "Enter the start date."
    if ends and ends < starts:
        return "The event ends before it starts."
    try:
        capacity = int(f["capacity"]) if f.get("capacity") else None
        budget = ledger.money(f["budget"]) if f.get("budget") else None
    except (ValueError, LedgerError):
        return "Capacity and budget must be numbers."
    if capacity is not None and capacity < 1:
        return "Capacity must be at least 1."
    e.title, e.category = f["title"].strip()[:160], (f.get("category") or "Programme")[:60]
    e.description, e.venue = f.get("description") or None, (f.get("venue") or "").strip() or None
    e.livestream_url = (f.get("livestream_url") or "").strip() or None
    if e.livestream_url and not e.livestream_url.startswith(("https://", "http://")):
        return "The livestream link must start with https://"
    e.starts_at, e.ends_at, e.capacity, e.budget_amount = starts, ends, capacity, budget
    e.requires_registration = f.get("requires_registration") == "y"
    e.registration_closes = _dt(f.get("closes_date", ""), f.get("closes_time", "")) if f.get("closes_date") else None
    e.is_public = f.get("is_public") == "y"
    e.status = EventStatus(f.get("status", "draft"))
    branch = next((b for b in member_svc.allowed_branches() if b.public_id == f.get("branch")), None)
    limit = member_svc.branch_limit_id()
    e.branch_id = branch.id if branch else (limit if limit not in (None, -1) else None)
    dept = db.session.query(Department).filter_by(public_id=f.get("department")).first() if f.get("department") else None
    e.department_id = dept.id if dept else None
    return None


def _form_ctx():
    return {"categories": CATEGORIES, "branches": member_svc.allowed_branches(),
            "departments": db.session.query(Department).filter_by(is_active=True).order_by(Department.name).all(),
            "statuses": list(EventStatus)}


@bp.route("/events/new", methods=["GET", "POST"])
@permission_required("events.create")
def create():
    if request.method == "POST":
        e = Event(title="", starts_at=datetime.now(), created_by_id=g.user.id)
        err = _save(e)
        if err:
            flash(err, "danger")
        else:
            db.session.add(e)
            db.session.flush()
            audit.record("event.created", entity=e, actor=g.user, name=e.title)
            db.session.commit()
            flash("Event created.", "success")
            return redirect(url_for("events.detail", public_id=e.public_id))
    return render_template("events/form.html", e=None, form=request.form, **_form_ctx(),
                           breadcrumbs=[("Events", url_for("events.index")), ("New event", None)])


@bp.route("/events/<public_id>/edit", methods=["GET", "POST"])
@permission_required("events.edit")
def edit(public_id: str):
    e = svc.get_event_or_404(public_id)
    if request.method == "POST":
        err = _save(e)
        if err:
            db.session.rollback()
            flash(err, "danger")
        else:
            promoted = svc.promote_waitlist(e)
            audit.record("event.updated", entity=e, actor=g.user, name=e.title)
            db.session.commit()
            flash("Event saved." + (f" {promoted} moved off the waiting list." if promoted else ""), "success")
            return redirect(url_for("events.detail", public_id=public_id))
    return render_template("events/form.html", e=e, form=request.form, **_form_ctx(),
                           breadcrumbs=[("Events", url_for("events.index")), (e.title, url_for("events.detail", public_id=public_id)), ("Edit", None)])


@bp.get("/events/<public_id>")
@permission_required("events.view")
def detail(public_id: str):
    e = svc.get_event_or_404(public_id)
    regs = sorted(e.registrations, key=lambda r: (r.status != RegistrationStatus.REGISTERED, r.created_at))
    active = [r for r in regs if r.status == RegistrationStatus.REGISTERED]
    expenses = db.session.query(Expense).filter(Expense.event_id == e.id).order_by(Expense.expense_date).all()
    return render_template("events/detail.html", e=e, regs=regs, taken=svc.seats_taken(e),
                           attended=sum(1 + r.guests for r in active if r.attended),
                           spend=svc.event_spend(e), expenses=expenses,
                           breadcrumbs=[("Events", url_for("events.index")), (e.title, None)])


@bp.post("/events/<public_id>/register")
@permission_required("events.edit")
def add_registration(public_id: str):
    e = svc.get_event_or_404(public_id)
    member = _member(request.form.get("number"))
    if request.form.get("number") and member is None:
        flash("No member with that number.", "danger")
        return redirect(url_for("events.detail", public_id=public_id))
    try:
        reg = svc.register(e, member=member, name=request.form.get("name"), phone=request.form.get("phone"),
                           email=request.form.get("email"), guests=request.form.get("guests", 0, type=int), actor=g.user)
        db.session.commit()
        flash(f"{reg.display_name}: {reg.status.value}.", "success" if reg.status == RegistrationStatus.REGISTERED else "warning")
    except ValueError as ex:
        db.session.rollback()
        flash(str(ex), "danger")
    return redirect(url_for("events.detail", public_id=public_id) + "#registrations")


@bp.post("/events/<public_id>/registrations/<reg_id>/<action>")
@permission_required("events.edit")
def registration_action(public_id: str, reg_id: str, action: str):
    e = svc.get_event_or_404(public_id)
    reg = db.session.query(EventRegistration).filter_by(public_id=reg_id, event_id=e.id).first_or_404()
    if action == "attended":
        reg.attended = not reg.attended
    elif action == "cancel":
        reg.status = RegistrationStatus.CANCELLED
        db.session.flush()
        moved = svc.promote_waitlist(e)
        if moved:
            flash(f"{moved} moved off the waiting list.", "info")
    else:
        return redirect(url_for("events.detail", public_id=public_id))
    db.session.commit()
    return redirect(url_for("events.detail", public_id=public_id) + "#registrations")


@bp.post("/events/<public_id>/committee")
@permission_required("events.edit")
def add_committee(public_id: str):
    e = svc.get_event_or_404(public_id)
    member = _member(request.form.get("number"))
    role, name = (request.form.get("role") or "").strip(), (request.form.get("name") or "").strip()
    if not role or not (member or name):
        flash("Enter a role and a member number or name.", "danger")
    else:
        db.session.add(EventCommitteeMember(event_id=e.id, member_id=getattr(member, "id", None),
                                            name=None if member else name[:160], role=role[:80]))
        db.session.commit()
        flash("Committee member added.", "success")
    return redirect(url_for("events.detail", public_id=public_id) + "#committee")


@bp.post("/events/<public_id>/committee/<cm_id>/remove")
@permission_required("events.edit")
def remove_committee(public_id: str, cm_id: str):
    e = svc.get_event_or_404(public_id)
    cm = db.session.query(EventCommitteeMember).filter_by(public_id=cm_id, event_id=e.id).first_or_404()
    db.session.delete(cm)
    db.session.commit()
    return redirect(url_for("events.detail", public_id=public_id) + "#committee")


@bp.post("/events/<public_id>/delete")
@permission_required("events.delete")
def delete(public_id: str):
    e = svc.get_event_or_404(public_id)
    audit.record("event.deleted", entity=e, actor=g.user, name=e.title)
    db.session.delete(e)
    db.session.commit()
    flash("Event deleted.", "success")
    return redirect(url_for("events.index"))
