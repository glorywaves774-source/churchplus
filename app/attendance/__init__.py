"""Attendance: registers, headcounts, QR / membership-number check-in, trends and absentees."""
from __future__ import annotations

from datetime import date, timedelta

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for
from sqlalchemy import or_

from app.extensions import db, limiter
from app.models import AttendanceRecord, Branch, Member, MemberStatus, Service, ServiceKind, ServiceStatus, Visitor
from app.services import audit, members as member_svc, operations as ops
from app.utils.context import permission_required

bp = Blueprint("attendance", __name__)
REGISTER_LIMIT = 300


@bp.get("/attendance")
@permission_required("attendance.view")
def index():
    today = date.today()
    recent = (ops.services_query().filter(Service.service_date <= today, Service.status != ServiceStatus.CANCELLED)
              .order_by(Service.service_date.desc(), Service.start_time.desc()).limit(12).all())
    todays = (ops.services_query().filter(Service.service_date == today, Service.status != ServiceStatus.CANCELLED)
              .order_by(Service.start_time).all())
    counts = ops.attendance_counts([s.id for s in recent + todays])
    totals = {s.id: ops.total_attendance(s, counts.get(s.id, {"members": 0, "visitors": 0})) for s in recent + todays}
    main = [totals[s.id] for s in recent if s.kind == ServiceKind.SERVICE and totals[s.id]]
    trend = ops.attendance_trend(12)
    return render_template(
        "attendance/index.html", recent=recent, todays=todays, totals=totals, counts=counts,
        last=main[0] if main else None, average=round(sum(main[:4]) / len(main[:4])) if main else None,
        chart={"type": "line", "labels": trend["labels"], "series": [{"label": "Weekly attendance", "data": trend["values"]}]},
        breadcrumbs=[("People", None), ("Attendance", None)],
    )


def _register_members(service: Service, q: str):
    query = member_svc.members_query().filter(Member.status == MemberStatus.ACTIVE)
    if service.branch_id:
        query = query.filter(or_(Member.branch_id == service.branch_id, Member.branch_id.is_(None)))
    if service.department_id:
        from app.models import DepartmentMembership
        query = query.join(DepartmentMembership, DepartmentMembership.member_id == Member.id) \
                     .filter(DepartmentMembership.department_id == service.department_id)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Member.first_name.ilike(like), Member.last_name.ilike(like),
                                 Member.membership_number.ilike(like), Member.phone.ilike(like)))
    return query.order_by(Member.last_name, Member.first_name)


@bp.route("/attendance/<public_id>", methods=["GET", "POST"])
@permission_required("attendance.view")
def register(public_id: str):
    service = ops.get_service_or_404(public_id)
    q = request.values.get("q", "").strip()
    members_q = _register_members(service, q)
    total_matching = members_q.count()
    members = members_q.limit(REGISTER_LIMIT).all()
    if request.method == "POST":
        if not g.account.can("attendance.create"):
            abort(403)
        if service.status == ServiceStatus.CANCELLED:
            flash("This service was cancelled.", "warning")
            return redirect(url_for("attendance.register", public_id=public_id))
        visible = {int(x) for x in request.form.getlist("shown") if x.isdigit()}
        visible &= {m.id for m in members}           # only rows this person could actually see
        present = {int(x) for x in request.form.getlist("present") if x.isdigit()} & visible
        added, removed = ops.set_register(service, present, visible, g.user)
        if service.status == ServiceStatus.SCHEDULED and service.service_date <= date.today():
            service.status = ServiceStatus.COMPLETED
        audit.record("attendance.register_saved", entity=service, actor=g.user, name=service.title,
                     added=added, removed=removed)
        db.session.commit()
        flash(f"Register saved: {added} marked present, {removed} removed.", "success")
        return redirect(url_for("attendance.register", public_id=public_id, q=q or None))
    present_ids = {r.member_id for r in db.session.query(AttendanceRecord.member_id)
                   .filter(AttendanceRecord.service_id == service.id, AttendanceRecord.member_id.isnot(None))}
    recent = (db.session.query(AttendanceRecord).filter_by(service_id=service.id)
              .order_by(AttendanceRecord.checked_in_at.desc()).limit(15).all())
    visitors = db.session.query(Visitor).filter_by(service_id=service.id).all()
    counts = ops.attendance_counts([service.id]).get(service.id, {"members": 0, "visitors": 0})
    return render_template("attendance/register.html", service=service, members=members, present_ids=present_ids,
                           total_matching=total_matching, limit=REGISTER_LIMIT, q=q, recent=recent, visitors=visitors,
                           counts=counts, total=ops.total_attendance(service, counts),
                           breadcrumbs=[("Attendance", url_for("attendance.index")), (service.title, None)])


@bp.post("/attendance/<public_id>/check-in")
@permission_required("attendance.create")
@limiter.limit("240 per minute")
def check_in(public_id: str):
    service = ops.get_service_or_404(public_id)
    member, method = ops.find_member_for_checkin(request.form.get("code", ""))
    wants_json = request.accept_mimetypes.best == "application/json"
    if member is None:
        msg, ok = "No member found for that code.", False
    elif member.status != MemberStatus.ACTIVE:
        msg, ok = f"{member.display_name} is not an active member ({member.status.value}).", False
    elif ops.mark_present(service, member, method, g.user):
        db.session.commit()
        msg, ok = f"{member.display_name} checked in.", True
    else:
        msg, ok = f"{member.display_name} is already checked in.", True
    if wants_json:
        return jsonify(ok=ok, message=msg, name=member.display_name if member else None,
                       number=member.membership_number if member else None), (200 if ok else 404)
    flash(msg, "success" if ok else "warning")
    return redirect(url_for("attendance.register", public_id=public_id) + "#checkin")


@bp.post("/attendance/<public_id>/headcount")
@permission_required("attendance.create")
def headcount(public_id: str):
    service = ops.get_service_or_404(public_id)

    def num(name):
        v = request.form.get(name, "").strip()
        if not v:
            return None
        if not v.isdigit() or int(v) > 200_000:
            raise ValueError(name)
        return int(v)

    try:
        service.headcount_men, service.headcount_women = num("men"), num("women")
        service.headcount_children, service.headcount_visitors = num("children"), num("visitors")
    except ValueError:
        flash("Headcounts must be whole numbers.", "danger")
        return redirect(url_for("attendance.register", public_id=public_id) + "#headcount")
    if service.status == ServiceStatus.SCHEDULED and service.service_date <= date.today():
        service.status = ServiceStatus.COMPLETED
    audit.record("attendance.headcount_saved", entity=service, actor=g.user, name=service.title,
                 total=service.headcount_total)
    db.session.commit()
    flash("Headcount saved.", "success")
    return redirect(url_for("attendance.register", public_id=public_id) + "#headcount")


@bp.post("/attendance/<public_id>/records/<int:record_id>/remove")
@permission_required("attendance.edit")
def remove_record(public_id: str, record_id: int):
    service = ops.get_service_or_404(public_id)
    rec = db.session.query(AttendanceRecord).filter_by(id=record_id, service_id=service.id).first_or_404()
    db.session.delete(rec)
    db.session.commit()
    flash("Check-in removed.", "success")
    return redirect(url_for("attendance.register", public_id=public_id) + "#checkin")


@bp.get("/attendance/absentees")
@permission_required("attendance.view")
def absentees():
    weeks = min(max(request.args.get("weeks", 4, type=int), 2), 26)
    rows = ops.absentees(weeks)
    return render_template("attendance/absentees.html", rows=rows, weeks=weeks,
                           breadcrumbs=[("Attendance", url_for("attendance.index")), ("Absentees", None)])
