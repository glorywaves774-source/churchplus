"""Departments and ministries: leaders, members, meetings and attendance."""
from __future__ import annotations

from datetime import date

from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from flask_wtf import FlaskForm
from sqlalchemy import func
from wtforms import BooleanField, SelectField, StringField, TextAreaField
from wtforms.validators import DataRequired, Length, Optional

from app.extensions import db
from app.models import (
    AttendanceRecord, Department, DepartmentKind, DepartmentMembership, Member, Service, ServiceKind,
)
from app.services import audit, members as member_svc, operations as ops
from app.utils.context import permission_required

bp = Blueprint("departments", __name__)


class DepartmentForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=120)])
    kind = SelectField("Type", choices=[(DepartmentKind.MINISTRY.value, "Ministry"),
                                        (DepartmentKind.DEPARTMENT.value, "Department")])
    description = TextAreaField("Purpose", validators=[Optional(), Length(max=2000)])
    leader = StringField("Leader (membership number)", validators=[Optional(), Length(max=30)])
    branch = SelectField("Branch", validators=[Optional()])
    meeting_schedule = StringField("When they meet", validators=[Optional(), Length(max=160)],
                                   description="e.g. Saturdays, 4:00 pm")
    is_active = BooleanField("Active", default=True)


def _get(public_id: str) -> Department:
    return db.session.query(Department).filter_by(public_id=public_id).first_or_404()


def _branch_choices(form):
    form.branch.choices = [("", "Whole church")] + [(b.public_id, b.name) for b in member_svc.allowed_branches()]


def _member_by_number(number: str | None) -> Member | None:
    if not number:
        return None
    return member_svc.members_query().filter(func.upper(Member.membership_number) == number.strip().upper()).first()


@bp.get("/departments")
@permission_required("departments.view")
def index():
    ops.ensure_defaults()
    db.session.commit()
    kind = request.args.get("kind", "")
    q = db.session.query(Department)
    if kind in ("ministry", "department"):
        q = q.filter(Department.kind == DepartmentKind(kind))
    rows = q.order_by(Department.is_active.desc(), Department.name).all()
    return render_template("departments/index.html", rows=rows, sizes=ops.department_sizes(), kind=kind,
                           breadcrumbs=[("Ministry", None), ("Departments", None)])


def _save(form: DepartmentForm, dept: Department) -> bool:
    leader = _member_by_number(form.leader.data)
    if form.leader.data and leader is None:
        form.leader.errors.append("No member with that number.")
        return False
    clash = db.session.query(Department.id).filter(Department.name == form.name.data.strip(), Department.id != (dept.id or 0)).first()
    if clash:
        form.name.errors.append("A department with this name exists.")
        return False
    dept.name, dept.kind = form.name.data.strip(), DepartmentKind(form.kind.data)
    dept.description, dept.meeting_schedule = form.description.data or None, form.meeting_schedule.data or None
    dept.is_active = form.is_active.data
    branch = next((b for b in member_svc.allowed_branches() if b.public_id == form.branch.data), None)
    dept.branch_id = branch.id if branch else None
    dept.leader_member_id = leader.id if leader else None
    return True


@bp.route("/departments/new", methods=["GET", "POST"])
@permission_required("departments.create")
def create():
    form = DepartmentForm()
    _branch_choices(form)
    if form.validate_on_submit():
        dept = Department(name=form.name.data)
        if _save(form, dept):
            db.session.add(dept)
            db.session.flush()
            if dept.leader_member_id:
                db.session.add(DepartmentMembership(department_id=dept.id, member_id=dept.leader_member_id, role="Leader"))
            audit.record("department.created", entity=dept, actor=g.user, name=dept.name)
            db.session.commit()
            flash(f"{dept.name} created.", "success")
            return redirect(url_for("departments.detail", public_id=dept.public_id))
    return render_template("departments/form.html", form=form, dept=None,
                           breadcrumbs=[("Departments", url_for("departments.index")), ("New", None)])


@bp.route("/departments/<public_id>/edit", methods=["GET", "POST"])
@permission_required("departments.edit")
def edit(public_id: str):
    dept = _get(public_id)
    form = DepartmentForm(name=dept.name, kind=dept.kind.value, description=dept.description,
                          leader=dept.leader.membership_number if dept.leader else "",
                          branch=dept.branch.public_id if dept.branch else "",
                          meeting_schedule=dept.meeting_schedule, is_active=dept.is_active)
    _branch_choices(form)
    if form.validate_on_submit() and _save(form, dept):
        if dept.leader_member_id and not db.session.query(DepartmentMembership.id).filter_by(
                department_id=dept.id, member_id=dept.leader_member_id).first():
            db.session.add(DepartmentMembership(department_id=dept.id, member_id=dept.leader_member_id, role="Leader"))
        audit.record("department.updated", entity=dept, actor=g.user, name=dept.name)
        db.session.commit()
        flash("Saved.", "success")
        return redirect(url_for("departments.detail", public_id=public_id))
    return render_template("departments/form.html", form=form, dept=dept,
                           breadcrumbs=[("Departments", url_for("departments.index")),
                                        (dept.name, url_for("departments.detail", public_id=public_id)), ("Edit", None)])


@bp.get("/departments/<public_id>")
@permission_required("departments.view")
def detail(public_id: str):
    dept = _get(public_id)
    visible = {m.id for m in member_svc.members_query().with_entities(Member.id)}
    memberships = [dm for dm in sorted(dept.memberships, key=lambda d: (d.role != "Leader", d.member.last_name))
                   if dm.member_id in visible]
    meetings = (ops.services_query().filter(Service.department_id == dept.id)
                .order_by(Service.service_date.desc()).limit(10).all())
    counts = ops.attendance_counts([s.id for s in meetings])
    avg = None
    if meetings and memberships:
        rates = [counts.get(s.id, {}).get("members", 0) / len(memberships) for s in meetings if s.service_date <= date.today()]
        avg = round(100 * sum(rates) / len(rates)) if rates else None
    return render_template("departments/detail.html", dept=dept, memberships=memberships, meetings=meetings,
                           counts=counts, avg=avg,
                           breadcrumbs=[("Departments", url_for("departments.index")), (dept.name, None)])


@bp.post("/departments/<public_id>/members")
@permission_required("departments.edit")
def add_member(public_id: str):
    dept = _get(public_id)
    member = _member_by_number(request.form.get("number"))
    role = (request.form.get("role") or "Member").strip()[:60] or "Member"
    if member is None:
        flash("No member with that number.", "warning")
    elif db.session.query(DepartmentMembership.id).filter_by(department_id=dept.id, member_id=member.id).first():
        flash(f"{member.display_name} is already in {dept.name}.", "info")
    else:
        db.session.add(DepartmentMembership(department_id=dept.id, member_id=member.id, role=role))
        audit.record("department.member_added", entity=dept, actor=g.user, name=dept.name, member=member.display_name)
        db.session.commit()
        flash(f"{member.display_name} added to {dept.name}.", "success")
    return redirect(url_for("departments.detail", public_id=public_id))


@bp.post("/departments/<public_id>/members/<dm_id>/remove")
@permission_required("departments.edit")
def remove_member(public_id: str, dm_id: str):
    dept = _get(public_id)
    dm = db.session.query(DepartmentMembership).filter_by(public_id=dm_id, department_id=dept.id).first_or_404()
    if dept.leader_member_id == dm.member_id:
        dept.leader_member_id = None
    audit.record("department.member_removed", entity=dept, actor=g.user, name=dept.name)
    db.session.delete(dm)
    db.session.commit()
    flash("Removed.", "success")
    return redirect(url_for("departments.detail", public_id=public_id))


@bp.post("/departments/<public_id>/delete")
@permission_required("departments.delete")
def delete(public_id: str):
    dept = _get(public_id)
    audit.record("department.deleted", entity=dept, actor=g.user, name=dept.name)
    db.session.delete(dept)
    db.session.commit()
    flash("Department deleted.", "success")
    return redirect(url_for("departments.index"))
