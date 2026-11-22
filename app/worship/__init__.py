"""Service management: scheduling, service types, order of service, who serves, service reports."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from flask_wtf import FlaskForm
from wtforms import DateField, SelectField, StringField, TextAreaField, TimeField
from wtforms.validators import DataRequired, Length, Optional

from app.extensions import db
from app.models import (
    Department, Service, ServiceAssignment, ServiceItem, ServiceKind, ServiceStatus, ServiceType,
)
from app.services import audit, members as member_svc, operations as ops
from app.utils.context import permission_required

bp = Blueprint("worship", __name__)


class ServiceForm(FlaskForm):
    service_type = SelectField("Service type", validators=[Optional()])
    kind = SelectField("Kind", choices=[(ServiceKind.SERVICE.value, "Church service"),
                                        (ServiceKind.SPECIAL.value, "Special programme"),
                                        (ServiceKind.DEPARTMENT.value, "Departmental meeting")])
    title = StringField("Title", validators=[DataRequired(), Length(max=160)])
    service_date = DateField("Date", validators=[DataRequired()], default=date.today)
    start_time = TimeField("Starts", validators=[Optional()])
    end_time = TimeField("Ends", validators=[Optional()])
    branch = SelectField("Branch", validators=[Optional()])
    department = SelectField("Department (for departmental meetings)", validators=[Optional()])
    venue = StringField("Venue", validators=[Optional(), Length(max=160)])
    theme = StringField("Theme", validators=[Optional(), Length(max=255)])
    preacher = StringField("Preacher", validators=[Optional(), Length(max=160)])
    worship_leader = StringField("Worship leader", validators=[Optional(), Length(max=160)])
    scripture_readings = StringField("Scripture readings", validators=[Optional(), Length(max=255)],
                                     description="e.g. Psalm 23; John 14:1-6")
    sermon_title = StringField("Sermon title", validators=[Optional(), Length(max=200)])
    status = SelectField("Status", choices=[(s.value, s.value.capitalize()) for s in ServiceStatus])
    notes = TextAreaField("Notes", validators=[Optional(), Length(max=4000)])


class ServiceTypeForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=100)])
    kind = SelectField("Kind", choices=[(k.value, k.value.capitalize()) for k in ServiceKind])
    weekday = SelectField("Usual day", choices=[("", "Varies")] + [(str(i), d) for i, d in enumerate(
        ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])])
    start_time = TimeField("Usual start", validators=[Optional()])


def _choices(form: ServiceForm) -> None:
    types = db.session.query(ServiceType).filter_by(is_active=True).order_by(ServiceType.name).all()
    form.service_type.choices = [("", "None")] + [(t.public_id, t.name) for t in types]
    branches = member_svc.allowed_branches()
    form.branch.choices = ([("", "Whole church")] if member_svc.branch_limit_id() is None else []) + \
                          [(b.public_id, b.name) for b in branches]
    depts = db.session.query(Department).filter_by(is_active=True).order_by(Department.name).all()
    form.department.choices = [("", "None")] + [(d.public_id, d.name) for d in depts]


def _by_public_id(model, pid):
    return db.session.query(model).filter_by(public_id=pid).first() if pid else None


def _apply(form: ServiceForm, service: Service) -> None:
    st = _by_public_id(ServiceType, form.service_type.data)
    service.service_type_id = st.id if st else None
    service.kind = ServiceKind(form.kind.data)
    for f in ("title", "venue", "theme", "preacher", "worship_leader", "scripture_readings", "sermon_title", "notes"):
        setattr(service, f, (getattr(form, f).data or "").strip() or None)
    service.title = service.title or (st.name if st else "Service")
    service.service_date, service.start_time, service.end_time = form.service_date.data, form.start_time.data, form.end_time.data
    branch = next((b for b in member_svc.allowed_branches() if b.public_id == form.branch.data), None)
    service.branch_id = branch.id if branch else (member_svc.branch_limit_id() if member_svc.branch_limit_id() not in (None, -1) else None)
    dept = _by_public_id(Department, form.department.data)
    service.department_id = dept.id if dept else None
    service.status = ServiceStatus(form.status.data)


def _save_rows(service: Service) -> None:
    """Order of service and duty rota from the repeated rows in the form."""
    service.items.clear()
    times, acts, leads = (request.form.getlist(k) for k in ("item_time", "item_activity", "item_led_by"))
    for i, act in enumerate(acts):
        if act.strip():
            t = None
            try:
                t = datetime.strptime(times[i], "%H:%M").time() if i < len(times) and times[i] else None
            except ValueError:
                pass
            service.items.append(ServiceItem(position=i, activity=act.strip()[:160], start_time=t,
                                             led_by=(leads[i].strip()[:160] if i < len(leads) else None) or None))
    service.assignments.clear()
    roles, names = request.form.getlist("duty_role"), request.form.getlist("duty_name")
    for i, role in enumerate(roles):
        name = names[i].strip() if i < len(names) else ""
        if role.strip() and name:
            member = member_svc.members_query().filter_by(membership_number=name.upper()).first()
            service.assignments.append(ServiceAssignment(role=role.strip()[:80], member_id=member.id if member else None,
                                                         name=None if member else name[:160]))


# --------------------------------------------------------------------------- #
@bp.get("/services")
@permission_required("worship.view")
def index():
    ops.ensure_defaults()
    db.session.commit()
    view = request.args.get("view", "upcoming")
    q = ops.services_query()
    today = date.today()
    if view == "past":
        q = q.filter(Service.service_date < today).order_by(Service.service_date.desc(), Service.start_time.desc())
    else:
        q = q.filter(Service.service_date >= today).order_by(Service.service_date, Service.start_time)
    if request.args.get("type"):
        q = q.join(ServiceType, ServiceType.id == Service.service_type_id).filter(ServiceType.public_id == request.args["type"])
    page = q.paginate(page=request.args.get("page", 1, type=int), per_page=20, error_out=False)
    counts = ops.attendance_counts([s.id for s in page.items])
    types = db.session.query(ServiceType).order_by(ServiceType.name).all()
    params = {k: v for k, v in request.args.items() if k != "page" and v}
    return render_template("worship/index.html", page=page, counts=counts, view=view, types=types, params=params,
                           total=lambda s: ops.total_attendance(s, counts.get(s.id, {"members": 0, "visitors": 0})),
                           breadcrumbs=[("Ministry", None), ("Services", None)])


@bp.route("/services/new", methods=["GET", "POST"])
@permission_required("worship.create")
def create():
    form = ServiceForm()
    _choices(form)
    if request.method == "GET":
        st = _by_public_id(ServiceType, request.args.get("type"))
        if st:
            form.service_type.data, form.title.data, form.kind.data = st.public_id, st.name, st.kind.value
            form.start_time.data = st.start_time
        if g.account.branch:
            form.branch.data = g.account.branch.public_id
        dept = _by_public_id(Department, request.args.get("department"))
        if dept:
            form.department.data, form.kind.data = dept.public_id, ServiceKind.DEPARTMENT.value
            form.title.data = f"{dept.name} meeting"
    if form.validate_on_submit():
        service = Service(title=form.title.data)
        _apply(form, service)
        db.session.add(service)
        _save_rows(service)
        db.session.flush()
        audit.record("service.created", entity=service, actor=g.user, name=f"{service.title}, {service.service_date:%d %b %Y}")
        db.session.commit()
        flash("Service scheduled.", "success")
        return redirect(url_for("worship.detail", public_id=service.public_id))
    return render_template("worship/form.html", form=form, service=None, items=[], duties=[],
                           breadcrumbs=[("Services", url_for("worship.index")), ("Schedule service", None)])


@bp.route("/services/<public_id>/edit", methods=["GET", "POST"])
@permission_required("worship.edit")
def edit(public_id: str):
    service = ops.get_service_or_404(public_id)
    initial = {f: getattr(service, f) for f in ("title", "service_date", "start_time", "end_time", "venue", "theme",
                                                "preacher", "worship_leader", "scripture_readings", "sermon_title", "notes")}
    initial.update(service_type=service.service_type.public_id if service.service_type else "",
                   kind=service.kind.value, status=service.status.value,
                   branch=service.branch.public_id if service.branch else "",
                   department=service.department.public_id if service.department else "")
    form = ServiceForm(**initial)
    _choices(form)
    if form.validate_on_submit():
        _apply(form, service)
        _save_rows(service)
        audit.record("service.updated", entity=service, actor=g.user, name=f"{service.title}, {service.service_date:%d %b %Y}")
        db.session.commit()
        flash("Service saved.", "success")
        return redirect(url_for("worship.detail", public_id=public_id))
    return render_template("worship/form.html", form=form, service=service, items=service.items,
                           duties=service.assignments,
                           breadcrumbs=[("Services", url_for("worship.index")),
                                        (service.title, url_for("worship.detail", public_id=public_id)), ("Edit", None)])


@bp.get("/services/<public_id>")
@permission_required("worship.view")
def detail(public_id: str):
    service = ops.get_service_or_404(public_id)
    counts = ops.attendance_counts([service.id]).get(service.id, {"members": 0, "visitors": 0})
    return render_template("worship/detail.html", service=service, counts=counts,
                           total=ops.total_attendance(service, counts),
                           breadcrumbs=[("Services", url_for("worship.index")), (service.title, None)])


@bp.post("/services/<public_id>/delete")
@permission_required("worship.delete")
def delete(public_id: str):
    service = ops.get_service_or_404(public_id)
    audit.record("service.deleted", entity=service, actor=g.user, name=f"{service.title}, {service.service_date:%d %b %Y}")
    db.session.delete(service)
    db.session.commit()
    flash("Service deleted, including its attendance.", "success")
    return redirect(url_for("worship.index"))


@bp.route("/services/types", methods=["GET", "POST"])
@permission_required("worship.manage_settings")
def types():
    form = ServiceTypeForm()
    if form.validate_on_submit():
        name = form.name.data.strip()
        if db.session.query(ServiceType.id).filter_by(name=name).first():
            form.name.errors.append("A service type with this name exists.")
        else:
            db.session.add(ServiceType(name=name, kind=ServiceKind(form.kind.data),
                                       weekday=int(form.weekday.data) if form.weekday.data else None,
                                       start_time=form.start_time.data))
            audit.record("service_type.created", actor=g.user, name=name)
            db.session.commit()
            flash("Service type added.", "success")
            return redirect(url_for("worship.types"))
    rows = db.session.query(ServiceType).order_by(ServiceType.is_active.desc(), ServiceType.name).all()
    return render_template("worship/types.html", form=form, types=rows,
                           breadcrumbs=[("Services", url_for("worship.index")), ("Service types", None)])


@bp.post("/services/types/<public_id>/toggle")
@permission_required("worship.manage_settings")
def toggle_type(public_id: str):
    st = db.session.query(ServiceType).filter_by(public_id=public_id).first_or_404()
    st.is_active = not st.is_active
    db.session.commit()
    flash(f"{st.name} {'enabled' if st.is_active else 'hidden'}.", "success")
    return redirect(url_for("worship.types"))
