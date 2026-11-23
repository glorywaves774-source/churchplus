"""Clergy, staff and volunteers: records, employment history, documents and leave."""
from __future__ import annotations

from datetime import date, datetime, timezone

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, send_file, url_for
from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField
from sqlalchemy import func, or_
from wtforms import DateField, EmailField, IntegerField, SelectField, StringField, TextAreaField
from wtforms.validators import DataRequired, Email, Length, NumberRange, Optional, ValidationError

from app.extensions import db
from app.models import (
    Department, EmploymentType, LeaveRequest, LeaveStatus, LeaveType, Member, StaffCategory, StaffMember,
    StaffRecord, StaffRecordKind, StaffStatus,
)
from app.members.forms import PhotoForm
from app.services import audit, members as member_svc, operations as ops, photos, storage
from app.services.storage import StorageError
from app.utils.context import permission_required

bp = Blueprint("clergy", __name__)


def _opts(enum_cls):
    return [(e.value, e.value.replace("_", " ").capitalize()) for e in enum_cls]


class StaffForm(FlaskForm):
    category = SelectField("Category", choices=_opts(StaffCategory))
    member_number = StringField("Membership number (if a member)", validators=[Optional(), Length(max=30)])
    title = StringField("Title", validators=[Optional(), Length(max=40)], description="e.g. Rev., Pastor, Mr")
    first_name = StringField("First name", validators=[DataRequired(), Length(max=100)])
    last_name = StringField("Surname", validators=[DataRequired(), Length(max=100)])
    gender = SelectField("Gender", choices=[("", "Not specified"), ("male", "Male"), ("female", "Female")], validators=[Optional()])
    date_of_birth = DateField("Date of birth", validators=[Optional()])
    phone = StringField("Phone", validators=[Optional(), Length(max=40)])
    email = EmailField("Email", validators=[Optional(), Email(check_deliverability=False), Length(max=255)])
    address = StringField("Address", validators=[Optional(), Length(max=255)])
    position = StringField("Position", validators=[DataRequired(), Length(max=120)], description="e.g. Associate Pastor, Accountant, Choir Director")
    department = SelectField("Department", validators=[Optional()])
    branch = SelectField("Branch", validators=[Optional()])
    employment_type = SelectField("Employment", choices=_opts(EmploymentType))
    status = SelectField("Status", choices=_opts(StaffStatus))
    start_date = DateField("Start date", validators=[Optional()])
    end_date = DateField("End date", validators=[Optional()])
    ordination_date = DateField("Ordination date (clergy)", validators=[Optional()])
    ghana_card_number = StringField("Ghana Card number", validators=[Optional(), Length(max=30)])
    ssnit_number = StringField("SSNIT number", validators=[Optional(), Length(max=30)])
    annual_leave_days = IntegerField("Annual leave days", default=21, validators=[Optional(), NumberRange(0, 60)])
    emergency_name = StringField("Emergency contact", validators=[Optional(), Length(max=160)])
    emergency_phone = StringField("Emergency phone", validators=[Optional(), Length(max=40)])
    notes = TextAreaField("Notes", validators=[Optional(), Length(max=4000)])

    def validate_end_date(self, field):
        if field.data and self.start_date.data and field.data < self.start_date.data:
            raise ValidationError("End date is before the start date.")


class RecordForm(FlaskForm):
    kind = SelectField("Type", choices=_opts(StaffRecordKind))
    record_date = DateField("Date", validators=[DataRequired()], default=date.today)
    title = StringField("Title", validators=[DataRequired(), Length(max=160)], description="e.g. Promoted to Senior Associate Pastor")
    details = TextAreaField("Details", validators=[Optional(), Length(max=4000)])
    rating = SelectField("Rating (appraisals)", choices=[("", "—")] + [(str(i), str(i)) for i in range(1, 6)], validators=[Optional()])
    file = FileField("Attachment", validators=[FileAllowed(["pdf", "jpg", "jpeg", "png", "docx", "heic", "heif", "webp"],
                                                                         "PDF, Word or a photo only.")],
                     render_kw={"accept": ".pdf,.docx,image/*,.heic,.heif"})


class LeaveForm(FlaskForm):
    leave_type = SelectField("Type", choices=_opts(LeaveType))
    start_date = DateField("From", validators=[DataRequired()])
    end_date = DateField("To", validators=[DataRequired()])
    reason = StringField("Reason", validators=[Optional(), Length(max=255)])

    def validate_end_date(self, field):
        if self.start_date.data and field.data and field.data < self.start_date.data:
            raise ValidationError("The leave ends before it starts.")


STAFF_FIELDS = ["title", "first_name", "last_name", "phone", "email", "address", "position", "date_of_birth", "start_date",
                "end_date", "ordination_date", "ghana_card_number", "ssnit_number", "emergency_name", "emergency_phone",
                "notes", "annual_leave_days"]


def _get(public_id: str) -> StaffMember:
    q = db.session.query(StaffMember).filter_by(public_id=public_id)
    limit = member_svc.branch_limit_id()
    if limit is not None:
        q = q.filter(StaffMember.branch_id == limit)
    return q.first_or_404()


def _setup(form: StaffForm):
    form.department.choices = [("", "None")] + [(d.public_id, d.name) for d in
                                                db.session.query(Department).filter_by(is_active=True).order_by(Department.name)]
    form.branch.choices = [("", "Whole church")] + [(b.public_id, b.name) for b in member_svc.allowed_branches()]


def _apply(form: StaffForm, s: StaffMember) -> bool:
    member = None
    if form.member_number.data:
        member = member_svc.members_query().filter(func.upper(Member.membership_number) == form.member_number.data.strip().upper()).first()
        if member is None:
            form.member_number.errors.append("No member with that number.")
            return False
    for f in STAFF_FIELDS:
        v = getattr(form, f).data
        setattr(s, f, (v.strip() or None) if isinstance(v, str) else v)
    s.annual_leave_days = form.annual_leave_days.data if form.annual_leave_days.data is not None else 21
    s.email = s.email.lower() if s.email else None
    s.category, s.employment_type, s.status = (StaffCategory(form.category.data), EmploymentType(form.employment_type.data),
                                               StaffStatus(form.status.data))
    s.gender = form.gender.data or None
    s.member_id = member.id if member else None
    dept = db.session.query(Department).filter_by(public_id=form.department.data).first() if form.department.data else None
    s.department_id = dept.id if dept else None
    branch = next((b for b in member_svc.allowed_branches() if b.public_id == form.branch.data), None)
    s.branch_id = branch.id if branch else None
    return True


@bp.get("/clergy")
@permission_required("clergy.view")
def index():
    q = db.session.query(StaffMember)
    limit = member_svc.branch_limit_id()
    if limit is not None:
        q = q.filter(StaffMember.branch_id == limit)
    text = request.args.get("q", "").strip()
    if text:
        like = f"%{text}%"
        q = q.filter(or_(StaffMember.first_name.ilike(like), StaffMember.last_name.ilike(like),
                         StaffMember.position.ilike(like), StaffMember.staff_number.ilike(like)))
    cat = request.args.get("category", "")
    if cat in {c.value for c in StaffCategory}:
        q = q.filter(StaffMember.category == StaffCategory(cat))
    status = request.args.get("status", "active")
    if status in {s.value for s in StaffStatus}:
        q = q.filter(StaffMember.status == StaffStatus(status))
    rows = q.order_by(StaffMember.category, StaffMember.last_name).all()
    counts = {c.value: n for c, n in db.session.query(StaffMember.category, func.count())
              .filter(StaffMember.status != StaffStatus.EXITED).group_by(StaffMember.category).all()}
    pending = db.session.query(LeaveRequest).filter_by(status=LeaveStatus.PENDING).count()
    return render_template("clergy/index.html", rows=rows, q=text, category=cat, status=status, counts=counts,
                           pending=pending, breadcrumbs=[("Ministry", None), ("Clergy & staff", None)])


@bp.route("/clergy/new", methods=["GET", "POST"])
@permission_required("clergy.create")
def create():
    form = StaffForm(category=request.args.get("category", "clergy"))
    _setup(form)
    if form.validate_on_submit():
        s = StaffMember(staff_number=ops.new_staff_number(g.tenant))
        if _apply(form, s):
            db.session.add(s)
            db.session.flush()
            audit.record("staff.created", entity=s, actor=g.user, name=s.display_name)
            db.session.commit()
            flash(f"{s.display_name} added as {s.staff_number}.", "success")
            return redirect(url_for("clergy.profile", public_id=s.public_id))
    return render_template("clergy/form.html", form=form, staff=None,
                           breadcrumbs=[("Clergy & staff", url_for("clergy.index")), ("Add", None)])


@bp.route("/clergy/<public_id>/edit", methods=["GET", "POST"])
@permission_required("clergy.edit")
def edit(public_id: str):
    s = _get(public_id)
    initial = {f: getattr(s, f) for f in STAFF_FIELDS}
    initial.update(category=s.category.value, employment_type=s.employment_type.value, status=s.status.value,
                   gender=s.gender or "", member_number=s.member.membership_number if s.member else "",
                   department=s.department.public_id if s.department else "", branch=s.branch.public_id if s.branch else "")
    form = StaffForm(**initial)
    _setup(form)
    if form.validate_on_submit() and _apply(form, s):
        audit.record("staff.updated", entity=s, actor=g.user, name=s.display_name)
        db.session.commit()
        flash("Saved.", "success")
        return redirect(url_for("clergy.profile", public_id=public_id))
    return render_template("clergy/form.html", form=form, staff=s,
                           breadcrumbs=[("Clergy & staff", url_for("clergy.index")),
                                        (s.display_name, url_for("clergy.profile", public_id=public_id)), ("Edit", None)])


@bp.get("/clergy/<public_id>")
@permission_required("clergy.view")
def profile(public_id: str):
    s = _get(public_id)
    return render_template("clergy/profile.html", staff=s, record_form=RecordForm(), leave_form=LeaveForm(),
                           photo_form=PhotoForm(), photo_url=photos.staff_photo_url(s),
                           taken=ops.leave_taken(s),
                           breadcrumbs=[("Clergy & staff", url_for("clergy.index")), (s.display_name, None)])


@bp.post("/clergy/<public_id>/photo")
@permission_required("clergy.edit")
def upload_photo(public_id: str):
    s = _get(public_id)
    back = url_for("clergy.profile", public_id=public_id)
    nxt = request.form.get("next") or ""
    if nxt.startswith("/") and not nxt.startswith("//"):
        back = nxt
    wants_json = request.accept_mimetypes.best == "application/json"

    def reply(ok, message, status=200, url=None):
        if wants_json:
            return jsonify(ok=ok, message=message, photo_url=url), status
        flash(message, "success" if ok else "danger")
        return redirect(back)

    form = PhotoForm()
    if not form.validate_on_submit():
        errors = list(form.photo.errors) or [e for errs in form.errors.values() for e in errs] or ["Choose a photo."]
        return reply(False, errors[0], 400)
    owner = photos.owner_for_staff(s)          # a linked member record holds the one photo for this person
    try:
        old = photos.replace(owner.record, form.photo.data, category="member-photos" if owner.is_member else "staff-photos")
    except StorageError as e:
        return reply(False, str(e), 400)
    audit.record("staff.photo_updated", entity=s, actor=g.user, name=s.display_name,
                 source="camera" if request.form.get("source") == "camera" else "upload")
    db.session.commit()
    if old:
        storage.delete(old)
    return reply(True, f"Photo of {s.display_name} saved.", url=photos.staff_photo_url(s))


@bp.post("/clergy/<public_id>/photo/remove")
@permission_required("clergy.edit")
def remove_photo(public_id: str):
    s = _get(public_id)
    owner = photos.owner_for_staff(s)
    old = owner.record.photo_key
    if old:
        owner.record.photo_key = None
        audit.record("staff.photo_removed", entity=s, actor=g.user, name=s.display_name)
        db.session.commit()
        storage.delete(old)
        flash("Photo removed.", "success")
    return redirect(url_for("clergy.profile", public_id=public_id))


@bp.get("/clergy/<public_id>/photo")
@permission_required("clergy.view")
def photo(public_id: str):
    key = photos.staff_photo_key(_get(public_id))
    if not key:
        abort(404)
    return photos.send(key)


@bp.post("/clergy/<public_id>/records")
@permission_required("clergy.edit")
def add_record(public_id: str):
    s = _get(public_id)
    form = RecordForm()
    if not form.validate_on_submit():
        for errs in form.errors.values():
            for e in errs:
                flash(e, "danger")
        return redirect(url_for("clergy.profile", public_id=public_id) + "#records")
    rec = StaffRecord(staff_id=s.id, kind=StaffRecordKind(form.kind.data), record_date=form.record_date.data,
                      title=form.title.data.strip(), details=form.details.data or None,
                      rating=int(form.rating.data) if form.rating.data else None, recorded_by_id=g.user.id)
    f = form.file.data
    if f and getattr(f, "filename", ""):
        try:
            stored = storage.save(f, filename=f.filename, kind="document", category="staff-documents", content_type=f.mimetype)
        except StorageError as e:
            flash(str(e), "danger")
            return redirect(url_for("clergy.profile", public_id=public_id) + "#records")
        rec.file_key, rec.file_name = stored.key, f.filename[:255]
    db.session.add(rec)
    audit.record("staff.record_added", entity=s, actor=g.user, name=s.display_name, record=rec.title)
    db.session.commit()
    flash("Record added.", "success")
    return redirect(url_for("clergy.profile", public_id=public_id) + "#records")


@bp.get("/clergy/<public_id>/records/<rec_id>/file")
@permission_required("clergy.view")
def record_file(public_id: str, rec_id: str):
    s = _get(public_id)
    rec = db.session.query(StaffRecord).filter_by(public_id=rec_id, staff_id=s.id).first_or_404()
    if not rec.file_key:
        abort(404)
    audit.record("staff.document_downloaded", entity=s, actor=g.user, record=rec.title)
    db.session.commit()
    return send_file(storage.open_file(rec.file_key), as_attachment=True, download_name=rec.file_name or "document")


@bp.post("/clergy/<public_id>/leave")
@permission_required("clergy.edit")
def request_leave(public_id: str):
    s = _get(public_id)
    form = LeaveForm()
    if form.validate_on_submit():
        days = ops.working_days(form.start_date.data, form.end_date.data)
        overlap = db.session.query(LeaveRequest.id).filter(
            LeaveRequest.staff_id == s.id, LeaveRequest.status.in_((LeaveStatus.PENDING, LeaveStatus.APPROVED)),
            LeaveRequest.start_date <= form.end_date.data, LeaveRequest.end_date >= form.start_date.data).first()
        if overlap:
            flash("This overlaps another leave request.", "warning")
        elif days == 0:
            flash("The dates contain no working days.", "warning")
        else:
            db.session.add(LeaveRequest(staff_id=s.id, leave_type=LeaveType(form.leave_type.data),
                                        start_date=form.start_date.data, end_date=form.end_date.data, days=days,
                                        reason=form.reason.data or None, requested_by_id=g.user.id))
            audit.record("staff.leave_requested", entity=s, actor=g.user, name=s.display_name, days=days)
            db.session.commit()
            flash(f"Leave request for {days} working day(s) submitted for approval.", "success")
    else:
        for errs in form.errors.values():
            for e in errs:
                flash(e, "danger")
    return redirect(url_for("clergy.profile", public_id=public_id) + "#leave")


@bp.post("/clergy/leave/<leave_id>/<decision>")
@permission_required("clergy.approve")
def decide_leave(leave_id: str, decision: str):
    if decision not in ("approve", "reject"):
        abort(404)
    lr = db.session.query(LeaveRequest).filter_by(public_id=leave_id).first_or_404()
    _get(lr.staff.public_id)                      # branch limits apply to approvers too
    if lr.status != LeaveStatus.PENDING:
        flash("This request has already been decided.", "info")
    elif lr.staff.email and lr.staff.email == g.user.email:
        flash("You cannot approve your own leave.", "danger")
    else:
        lr.status = LeaveStatus.APPROVED if decision == "approve" else LeaveStatus.REJECTED
        lr.decided_by_id, lr.decided_at = g.user.id, datetime.now(timezone.utc)
        lr.decision_note = (request.form.get("note") or "").strip()[:255] or None
        audit.record(f"staff.leave_{lr.status.value}", entity=lr.staff, actor=g.user, name=lr.staff.display_name,
                     days=lr.days)
        db.session.commit()
        flash(f"Leave {lr.status.value}.", "success")
    return redirect(request.form.get("next") if (request.form.get("next") or "").startswith("/") else url_for("clergy.leave"))


@bp.get("/clergy/leave")
@permission_required("clergy.view")
def leave():
    q = db.session.query(LeaveRequest).join(StaffMember, StaffMember.id == LeaveRequest.staff_id)
    limit = member_svc.branch_limit_id()
    if limit is not None:
        q = q.filter(StaffMember.branch_id == limit)
    pending = q.filter(LeaveRequest.status == LeaveStatus.PENDING).order_by(LeaveRequest.start_date).all()
    today = date.today()
    current = q.filter(LeaveRequest.status == LeaveStatus.APPROVED, LeaveRequest.end_date >= today).order_by(LeaveRequest.start_date).all()
    return render_template("clergy/leave.html", pending=pending, current=current, today=today,
                           breadcrumbs=[("Clergy & staff", url_for("clergy.index")), ("Leave", None)])
