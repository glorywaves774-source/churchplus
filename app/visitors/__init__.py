"""Visitors and first-timers: registration, follow-up, conversion to membership, retention."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from flask_wtf import FlaskForm
from sqlalchemy import or_
from wtforms import BooleanField, DateField, EmailField, SelectField, StringField, TextAreaField
from wtforms.validators import DataRequired, Email, Length, Optional

from app.extensions import db
from app.models import (
    AttendanceRecord, CheckInMethod, FollowUpMethod, Member, Service, ServiceStatus, TenantUser, User, Visitor,
    VisitorFollowUp, VisitorStatus,
)
from app.services import audit, limits, members as member_svc, operations as ops
from app.utils.context import permission_required

bp = Blueprint("visitors", __name__)

HOW_HEARD = ["", "Invited by a member", "Family", "Friend", "Social media", "Radio / TV", "Flyer or banner",
             "Walked in", "Online search", "Crusade / outreach", "Other"]


class VisitorForm(FlaskForm):
    first_name = StringField("First name", validators=[DataRequired(), Length(max=100)])
    last_name = StringField("Surname", validators=[DataRequired(), Length(max=100)])
    gender = SelectField("Gender", choices=[("", "Not specified"), ("male", "Male"), ("female", "Female")], validators=[Optional()])
    age_group = SelectField("Age group", choices=[("", "Not specified"), ("child", "Child"), ("youth", "Youth"),
                                                  ("adult", "Adult"), ("senior", "Senior")], validators=[Optional()])
    phone = StringField("Phone", validators=[Optional(), Length(max=40)])
    email = EmailField("Email", validators=[Optional(), Email(check_deliverability=False), Length(max=255)])
    address = StringField("Area / address", validators=[Optional(), Length(max=255)])
    city = StringField("City or town", validators=[Optional(), Length(max=120)])
    first_visit_date = DateField("Date of first visit", validators=[DataRequired()], default=date.today)
    service = SelectField("Service attended", validators=[Optional()])
    branch = SelectField("Branch", validators=[Optional()])
    how_heard = SelectField("How did they hear about us?", choices=[(h, h or "Not specified") for h in HOW_HEARD],
                            validators=[Optional()])
    invited_by = StringField("Invited by (membership number)", validators=[Optional(), Length(max=30)])
    current_church = StringField("Current church, if any", validators=[Optional(), Length(max=200)])
    wants_visit = BooleanField("Would welcome a visit or call")
    wants_membership = BooleanField("Interested in becoming a member")
    prayer_request = TextAreaField("Prayer request", validators=[Optional(), Length(max=2000)])
    notes = TextAreaField("Notes", validators=[Optional(), Length(max=2000)])
    assigned_to = SelectField("Follow-up by", validators=[Optional()])


class FollowUpForm(FlaskForm):
    due_date = DateField("Due", validators=[DataRequired()], default=lambda: date.today() + timedelta(days=2))
    method = SelectField("How", choices=[(m.value, m.value.capitalize().replace("Sms", "SMS").replace("Whatsapp", "WhatsApp"))
                                         for m in FollowUpMethod])
    assigned_to = SelectField("Assigned to", validators=[Optional()])


class OutcomeForm(FlaskForm):
    outcome = TextAreaField("What happened?", validators=[DataRequired(), Length(max=2000)])
    new_status = SelectField("Visitor status", choices=[("", "No change")] + [
        (s.value, s.value.replace("_", " ").capitalize()) for s in VisitorStatus if s != VisitorStatus.CONVERTED])


def _team_choices():
    rows = (db.session.query(TenantUser).join(User, User.id == TenantUser.user_id)
            .filter(TenantUser.is_suspended.is_(False)).order_by(User.first_name).all())
    return [("", "Unassigned")] + [(a.public_id, a.user.full_name) for a in rows]


def _account(pid):
    return db.session.query(TenantUser).filter_by(public_id=pid).first() if pid else None


def _setup(form: VisitorForm):
    recent = (ops.services_query().filter(Service.service_date >= date.today() - timedelta(days=21),
                                          Service.service_date <= date.today() + timedelta(days=1),
                                          Service.status != ServiceStatus.CANCELLED)
              .order_by(Service.service_date.desc()).all())
    form.service.choices = [("", "Not recorded")] + [(s.public_id, f"{s.title} – {s.service_date:%a %d %b}") for s in recent]
    form.branch.choices = ([("", "Not specified")] if member_svc.branch_limit_id() is None else []) + \
                          [(b.public_id, b.name) for b in member_svc.allowed_branches()]
    form.assigned_to.choices = _team_choices()


@bp.get("/visitors")
@permission_required("visitors.view")
def index():
    q = ops.visitors_query()
    text = request.args.get("q", "").strip()
    if text:
        like = f"%{text}%"
        q = q.filter(or_(Visitor.first_name.ilike(like), Visitor.last_name.ilike(like), Visitor.phone.ilike(like)))
    status = request.args.get("status", "")
    if status in {s.value for s in VisitorStatus}:
        q = q.filter(Visitor.status == VisitorStatus(status))
    if request.args.get("mine") == "1":
        q = q.filter(Visitor.assigned_to_id == g.account.id)
    page = q.order_by(Visitor.first_visit_date.desc(), Visitor.created_at.desc()).paginate(
        page=request.args.get("page", 1, type=int), per_page=25, error_out=False)
    today = date.today()
    due = (db.session.query(VisitorFollowUp).join(Visitor, Visitor.id == VisitorFollowUp.visitor_id)
           .filter(VisitorFollowUp.completed_at.is_(None), VisitorFollowUp.due_date <= today + timedelta(days=3)))
    limit = member_svc.branch_limit_id()
    if limit is not None:
        due = due.filter(Visitor.branch_id == limit)
    due = due.order_by(VisitorFollowUp.due_date).limit(10).all()
    month_start = today.replace(day=1)
    vq = ops.visitors_query()
    stats = {"this_month": vq.filter(Visitor.first_visit_date >= month_start).count(),
             "following_up": vq.filter(Visitor.status.in_((VisitorStatus.NEW, VisitorStatus.FOLLOWING_UP))).count(),
             "converted": vq.filter(Visitor.status == VisitorStatus.CONVERTED).count(),
             "overdue": sum(1 for f in due if f.due_date < today)}
    params = {k: v for k, v in request.args.items() if k != "page" and v}
    return render_template("visitors/index.html", page=page, q=text, status=status, params=params, due=due,
                           stats=stats, today=today, breadcrumbs=[("People", None), ("Visitors", None)])


@bp.route("/visitors/new", methods=["GET", "POST"])
@permission_required("visitors.create")
def create():
    form = VisitorForm()
    _setup(form)
    if request.method == "GET":
        if request.args.get("service"):
            form.service.data = request.args["service"]
            svc = ops.services_query().filter_by(public_id=request.args["service"]).first()
            if svc:
                form.first_visit_date.data = svc.service_date
                form.branch.data = svc.branch.public_id if svc.branch else ""
        form.assigned_to.data = ""
    if form.validate_on_submit():
        service = ops.services_query().filter_by(public_id=form.service.data).first() if form.service.data else None
        branch = next((b for b in member_svc.allowed_branches() if b.public_id == form.branch.data), None)
        inviter = (member_svc.members_query().filter(Member.membership_number == form.invited_by.data.strip().upper()).first()
                   if form.invited_by.data else None)
        assignee = _account(form.assigned_to.data)
        v = Visitor(
            first_name=form.first_name.data.strip(), last_name=form.last_name.data.strip(),
            gender=form.gender.data or None, age_group=form.age_group.data or None,
            phone=(form.phone.data or "").strip() or None, email=(form.email.data or "").strip().lower() or None,
            address=form.address.data or None, city=form.city.data or None,
            first_visit_date=form.first_visit_date.data, service_id=service.id if service else None,
            branch_id=branch.id if branch else (service.branch_id if service else None),
            how_heard=form.how_heard.data or None, invited_by_member_id=inviter.id if inviter else None,
            current_church=form.current_church.data or None, wants_visit=form.wants_visit.data,
            wants_membership=form.wants_membership.data, prayer_request=form.prayer_request.data or None,
            notes=form.notes.data or None, assigned_to_id=assignee.id if assignee else None,
        )
        db.session.add(v)
        db.session.flush()
        if service:
            db.session.add(AttendanceRecord(service_id=service.id, visitor_id=v.id, method=CheckInMethod.MANUAL,
                                            recorded_by_id=g.user.id))
        ops.schedule_first_follow_up(v, v.assigned_to_id)
        audit.record("visitor.created", entity=v, actor=g.user, name=v.display_name)
        db.session.commit()
        flash(f"{v.display_name} registered. A follow-up is scheduled for {v.first_visit_date + timedelta(days=2):%a %d %b}.", "success")
        if request.form.get("again") == "1":
            return redirect(url_for("visitors.create", service=form.service.data or None))
        return redirect(url_for("visitors.profile", public_id=v.public_id))
    return render_template("visitors/form.html", form=form, visitor=None,
                           breadcrumbs=[("Visitors", url_for("visitors.index")), ("Register first-timer", None)])


@bp.route("/visitors/<public_id>/edit", methods=["GET", "POST"])
@permission_required("visitors.edit")
def edit(public_id: str):
    v = ops.get_visitor_or_404(public_id)
    initial = {f: getattr(v, f) for f in ("first_name", "last_name", "phone", "email", "address", "city",
                                         "first_visit_date", "current_church", "wants_visit", "wants_membership",
                                         "prayer_request", "notes")}
    initial.update(gender=v.gender or "", age_group=v.age_group or "", how_heard=v.how_heard or "",
                   service=v.service.public_id if v.service else "", branch=v.branch.public_id if v.branch else "",
                   invited_by=v.invited_by.membership_number if v.invited_by else "",
                   assigned_to=v.assigned_to.public_id if v.assigned_to else "")
    form = VisitorForm(**initial)
    _setup(form)
    if form.validate_on_submit():
        for f in ("first_name", "last_name", "address", "city", "current_church", "prayer_request", "notes"):
            setattr(v, f, (getattr(form, f).data or "").strip() or None)
        v.phone = (form.phone.data or "").strip() or None
        v.email = (form.email.data or "").strip().lower() or None
        v.gender, v.age_group, v.how_heard = form.gender.data or None, form.age_group.data or None, form.how_heard.data or None
        v.first_visit_date, v.wants_visit, v.wants_membership = form.first_visit_date.data, form.wants_visit.data, form.wants_membership.data
        branch = next((b for b in member_svc.allowed_branches() if b.public_id == form.branch.data), None)
        v.branch_id = branch.id if branch else v.branch_id
        assignee = _account(form.assigned_to.data)
        v.assigned_to_id = assignee.id if assignee else None
        audit.record("visitor.updated", entity=v, actor=g.user, name=v.display_name)
        db.session.commit()
        flash("Visitor saved.", "success")
        return redirect(url_for("visitors.profile", public_id=public_id))
    return render_template("visitors/form.html", form=form, visitor=v,
                           breadcrumbs=[("Visitors", url_for("visitors.index")),
                                        (v.display_name, url_for("visitors.profile", public_id=public_id)), ("Edit", None)])


@bp.get("/visitors/<public_id>")
@permission_required("visitors.view")
def profile(public_id: str):
    v = ops.get_visitor_or_404(public_id)
    fu_form = FollowUpForm()
    fu_form.assigned_to.choices = _team_choices()
    fu_form.assigned_to.data = v.assigned_to.public_id if v.assigned_to else ""
    visits = (db.session.query(AttendanceRecord).join(Service, Service.id == AttendanceRecord.service_id)
              .filter(AttendanceRecord.visitor_id == v.id).order_by(Service.service_date.desc()).all())
    return render_template("visitors/profile.html", visitor=v, fu_form=fu_form, outcome_form=OutcomeForm(),
                           visits=visits, today=date.today(), member_room=limits.check("members").allowed,
                           breadcrumbs=[("Visitors", url_for("visitors.index")), (v.display_name, None)])


@bp.post("/visitors/<public_id>/follow-ups")
@permission_required("visitors.edit")
def add_follow_up(public_id: str):
    v = ops.get_visitor_or_404(public_id)
    form = FollowUpForm()
    form.assigned_to.choices = _team_choices()
    if form.validate_on_submit():
        assignee = _account(form.assigned_to.data)
        db.session.add(VisitorFollowUp(visitor_id=v.id, due_date=form.due_date.data,
                                       method=FollowUpMethod(form.method.data),
                                       assigned_to_id=assignee.id if assignee else None))
        if v.status == VisitorStatus.NEW:
            v.status = VisitorStatus.FOLLOWING_UP
        audit.record("visitor.follow_up_scheduled", entity=v, actor=g.user, name=v.display_name)
        db.session.commit()
        flash("Follow-up scheduled.", "success")
    else:
        flash("Please check the follow-up form.", "danger")
    return redirect(url_for("visitors.profile", public_id=public_id))


@bp.post("/visitors/<public_id>/follow-ups/<fu_id>/complete")
@permission_required("visitors.edit")
def complete_follow_up(public_id: str, fu_id: str):
    v = ops.get_visitor_or_404(public_id)
    fu = db.session.query(VisitorFollowUp).filter_by(public_id=fu_id, visitor_id=v.id).first_or_404()
    form = OutcomeForm()
    if form.validate_on_submit():
        fu.outcome = form.outcome.data.strip()
        fu.completed_at = datetime.now(timezone.utc)
        if form.new_status.data:
            v.status = VisitorStatus(form.new_status.data)
        elif v.status == VisitorStatus.NEW:
            v.status = VisitorStatus.FOLLOWING_UP
        audit.record("visitor.follow_up_done", entity=v, actor=g.user, name=v.display_name)
        db.session.commit()
        flash("Follow-up recorded.", "success")
    else:
        flash("Describe what happened before marking it done.", "danger")
    return redirect(url_for("visitors.profile", public_id=public_id))


@bp.post("/visitors/<public_id>/convert")
@permission_required("visitors.edit", "members.create")
def convert(public_id: str):
    v = ops.get_visitor_or_404(public_id)
    if v.status == VisitorStatus.CONVERTED:
        flash("Already a member.", "info")
        return redirect(url_for("visitors.profile", public_id=public_id))
    if not limits.check("members").allowed:
        flash("Your plan's member limit has been reached.", "warning")
        return redirect(url_for("visitors.profile", public_id=public_id))
    member = ops.convert_visitor(v, g.tenant, g.user)
    db.session.commit()
    flash(f"{v.display_name} is now member {member.membership_number}. Please complete their record.", "success")
    return redirect(url_for("members.edit", public_id=member.public_id))


@bp.post("/visitors/<public_id>/delete")
@permission_required("visitors.delete")
def delete(public_id: str):
    v = ops.get_visitor_or_404(public_id)
    audit.record("visitor.deleted", entity=v, actor=g.user, name=v.display_name)
    db.session.delete(v)
    db.session.commit()
    flash("Visitor record deleted.", "success")
    return redirect(url_for("visitors.index"))


@bp.get("/visitors/retention")
@permission_required("visitors.view")
def retention():
    rows = ops.visitor_retention(6)
    chart = {"type": "bar", "labels": [r["month"] for r in rows],
             "series": [{"label": "First-timers", "data": [r["first_timers"] for r in rows]},
                        {"label": "Came back", "data": [r["returned"] for r in rows]},
                        {"label": "Became members", "data": [r["converted"] for r in rows]}]}
    return render_template("visitors/retention.html", rows=rows, chart=chart,
                           breadcrumbs=[("Visitors", url_for("visitors.index")), ("Retention", None)])
