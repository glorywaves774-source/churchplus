"""Business logic for services, attendance, visitors, departments and staff."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta

from flask import url_for
from sqlalchemy import func

from app.extensions import db
from app.models import (
    AttendanceRecord, CheckInMethod, Department, DepartmentKind, DepartmentMembership, Gender, LeaveRequest, LeaveStatus,
    Member, MemberStatus, Service, ServiceKind, ServiceStatus, ServiceType, StaffMember, Visitor, VisitorFollowUp,
    VisitorStatus,
)
from app.services import audit, search
from app.services import members as member_svc
from app.utils.tenancy import require_tenant_id

# --------------------------------------------------------------------------- #
# Defaults for a new church
# --------------------------------------------------------------------------- #
DEFAULT_SERVICE_TYPES = [
    ("Sunday First Service", ServiceKind.SERVICE, 6, time(7, 0)),
    ("Sunday Second Service", ServiceKind.SERVICE, 6, time(9, 30)),
    ("Midweek Service", ServiceKind.SERVICE, 2, time(18, 30)),
    ("Prayer Meeting", ServiceKind.SERVICE, 4, time(18, 30)),
    ("Special Programme", ServiceKind.SPECIAL, None, None),
]
DEFAULT_MINISTRIES = [
    ("Men's Ministry", DepartmentKind.MINISTRY), ("Women's Ministry", DepartmentKind.MINISTRY),
    ("Youth Ministry", DepartmentKind.MINISTRY), ("Children's Ministry", DepartmentKind.MINISTRY),
    ("Music Ministry", DepartmentKind.MINISTRY), ("Prayer Ministry", DepartmentKind.MINISTRY),
    ("Evangelism", DepartmentKind.MINISTRY), ("Ushering", DepartmentKind.DEPARTMENT),
    ("Protocol", DepartmentKind.DEPARTMENT), ("Media", DepartmentKind.DEPARTMENT),
    ("Welfare", DepartmentKind.DEPARTMENT),
]


def ensure_defaults() -> None:
    """Create default service types and ministries once per church (idempotent)."""
    require_tenant_id()
    if not db.session.query(ServiceType.id).first():
        for name, kind, weekday, start in DEFAULT_SERVICE_TYPES:
            db.session.add(ServiceType(name=name, kind=kind, weekday=weekday, start_time=start))
    if not db.session.query(Department.id).first():
        for name, kind in DEFAULT_MINISTRIES:
            db.session.add(Department(name=name, kind=kind))
    db.session.flush()


# --------------------------------------------------------------------------- #
# Services
# --------------------------------------------------------------------------- #
def services_query():
    q = db.session.query(Service)
    limit = member_svc.branch_limit_id()
    if limit is not None:
        q = q.filter((Service.branch_id == limit) | (Service.branch_id.is_(None)))
    return q


def get_service_or_404(public_id: str) -> Service:
    s = services_query().filter(Service.public_id == public_id).first()
    if s is None:
        from flask import abort
        abort(404)
    return s


def attendance_counts(service_ids: list[int]) -> dict[int, dict]:
    if not service_ids:
        return {}
    rows = (db.session.query(AttendanceRecord.service_id,
                             func.count(AttendanceRecord.member_id), func.count(AttendanceRecord.visitor_id))
            .filter(AttendanceRecord.service_id.in_(service_ids)).group_by(AttendanceRecord.service_id).all())
    return {sid: {"members": m, "visitors": v} for sid, m, v in rows}


def total_attendance(service: Service, counts: dict | None = None) -> int:
    """Headcount when recorded, otherwise names marked present (+ counted visitors)."""
    c = counts or attendance_counts([service.id]).get(service.id, {"members": 0, "visitors": 0})
    if service.headcount_total is not None:
        return service.headcount_total + (service.headcount_visitors or 0)
    return c["members"] + max(c["visitors"], service.headcount_visitors or 0)


def attendance_trend(weeks: int = 12, kind: ServiceKind = ServiceKind.SERVICE) -> dict:
    since = date.today() - timedelta(weeks=weeks)
    services = (services_query().filter(Service.kind == kind, Service.service_date >= since,
                                        Service.status != ServiceStatus.CANCELLED)
                .order_by(Service.service_date).all())
    counts = attendance_counts([s.id for s in services])
    by_week: dict[date, int] = defaultdict(int)
    for s in services:
        week = s.service_date - timedelta(days=s.service_date.weekday())
        by_week[week] += total_attendance(s, counts.get(s.id, {"members": 0, "visitors": 0}))
    labels = sorted(by_week)
    return {"labels": [d.strftime("%d %b") for d in labels], "values": [by_week[d] for d in labels]}


def mark_present(service: Service, member: Member, method: CheckInMethod, actor) -> bool:
    """Returns False if already marked."""
    if db.session.query(AttendanceRecord.id).filter_by(service_id=service.id, member_id=member.id).first():
        return False
    db.session.add(AttendanceRecord(service_id=service.id, member_id=member.id, method=method,
                                    recorded_by_id=getattr(actor, "id", None)))
    return True


def set_register(service: Service, member_ids: set[int], visible_ids: set[int], actor) -> tuple[int, int]:
    """Save the tick-list register for the members shown on the page."""
    existing = {r.member_id: r for r in db.session.query(AttendanceRecord)
                .filter(AttendanceRecord.service_id == service.id, AttendanceRecord.member_id.isnot(None))}
    added = removed = 0
    for mid in member_ids & visible_ids:
        if mid not in existing:
            db.session.add(AttendanceRecord(service_id=service.id, member_id=mid, method=CheckInMethod.MANUAL,
                                            recorded_by_id=actor.id))
            added += 1
    for mid, rec in existing.items():
        if mid in visible_ids and mid not in member_ids:
            db.session.delete(rec)
            removed += 1
    return added, removed


def find_member_for_checkin(code: str) -> tuple[Member | None, CheckInMethod]:
    """Accepts a scanned QR link, a QR token or a membership number."""
    code = (code or "").strip()
    if not code:
        return None, CheckInMethod.NUMBER
    q = member_svc.members_query()
    if "/m/" in code:
        token = code.rstrip("/").rsplit("/m/", 1)[-1].split("?")[0]
        return q.filter(Member.qr_token == token).first(), CheckInMethod.QR
    by_number = q.filter(func.upper(Member.membership_number) == code.upper()).first()
    if by_number:
        return by_number, CheckInMethod.NUMBER
    return q.filter(Member.qr_token == code).first(), CheckInMethod.QR


def absentees(weeks: int = 4, min_services: int = 2) -> list[dict]:
    """Active members not present at any of the main services in the last N weeks."""
    since = date.today() - timedelta(weeks=weeks)
    services = services_query().filter(Service.kind == ServiceKind.SERVICE, Service.service_date >= since,
                                       Service.service_date <= date.today(),
                                       Service.status != ServiceStatus.CANCELLED).all()
    marked = [s.id for s in services if db.session.query(AttendanceRecord.id).filter_by(service_id=s.id).first()]
    if len(marked) < min_services:
        return []
    present = {mid for (mid,) in db.session.query(AttendanceRecord.member_id)
               .filter(AttendanceRecord.service_id.in_(marked), AttendanceRecord.member_id.isnot(None)).distinct()}
    last_seen = dict(db.session.query(AttendanceRecord.member_id, func.max(Service.service_date))
                     .join(Service, Service.id == AttendanceRecord.service_id)
                     .filter(AttendanceRecord.member_id.isnot(None)).group_by(AttendanceRecord.member_id).all())
    rows = []
    for m in member_svc.members_query().filter(Member.status == MemberStatus.ACTIVE).order_by(Member.last_name):
        if m.id not in present:
            rows.append({"member": m, "last_seen": last_seen.get(m.id)})
    return rows


def member_attendance_history(member: Member, limit: int = 20) -> list[AttendanceRecord]:
    return (db.session.query(AttendanceRecord).join(Service, Service.id == AttendanceRecord.service_id)
            .filter(AttendanceRecord.member_id == member.id).order_by(Service.service_date.desc()).limit(limit).all())


# --------------------------------------------------------------------------- #
# Visitors
# --------------------------------------------------------------------------- #
def visitors_query():
    q = db.session.query(Visitor)
    limit = member_svc.branch_limit_id()
    if limit is not None:
        q = q.filter(Visitor.branch_id == limit)
    return q


def get_visitor_or_404(public_id: str) -> Visitor:
    v = visitors_query().filter(Visitor.public_id == public_id).first()
    if v is None:
        from flask import abort
        abort(404)
    return v


def schedule_first_follow_up(visitor: Visitor, assigned_to_id: int | None) -> VisitorFollowUp:
    fu = VisitorFollowUp(visitor_id=visitor.id, due_date=visitor.first_visit_date + timedelta(days=2),
                         assigned_to_id=assigned_to_id)
    db.session.add(fu)
    return fu


def convert_visitor(visitor: Visitor, tenant, actor) -> Member:
    """Register a visitor as a member, carrying their details across."""
    member = member_svc.create_member(
        tenant, actor, first_name=visitor.first_name, last_name=visitor.last_name, phone=visitor.phone,
        email=visitor.email, address=visitor.address, city=visitor.city, branch_id=visitor.branch_id,
        previous_church=visitor.current_church, date_joined=date.today(),
        gender=Gender(visitor.gender) if visitor.gender in ("male", "female") else None,
    )
    visitor.status = VisitorStatus.CONVERTED
    visitor.converted_member_id = member.id
    visitor.converted_at = date.today()
    # Their past attendance as a visitor now counts for the member.
    for rec in db.session.query(AttendanceRecord).filter_by(visitor_id=visitor.id):
        if not db.session.query(AttendanceRecord.id).filter_by(service_id=rec.service_id, member_id=member.id).first():
            rec.member_id, rec.visitor_id = member.id, None
    audit.record("visitor.converted", entity=visitor, actor=actor, name=visitor.display_name,
                 number=member.membership_number)
    return member


def visitor_retention(months: int = 6) -> list[dict]:
    """Per month of first visit: how many came, returned at least once more, and became members."""
    start = (date.today().replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)
    visitors = visitors_query().filter(Visitor.first_visit_date >= start).all()
    visits = defaultdict(int)
    if visitors:
        for vid, n in (db.session.query(AttendanceRecord.visitor_id, func.count())
                       .filter(AttendanceRecord.visitor_id.in_([v.id for v in visitors]))
                       .group_by(AttendanceRecord.visitor_id)):
            visits[vid] = n
    out = defaultdict(lambda: {"first_timers": 0, "returned": 0, "converted": 0})
    for v in visitors:
        key = v.first_visit_date.strftime("%Y-%m")
        out[key]["first_timers"] += 1
        if visits[v.id] > 1 or v.status in (VisitorStatus.REGULAR, VisitorStatus.CONVERTED):
            out[key]["returned"] += 1
        if v.status == VisitorStatus.CONVERTED:
            out[key]["converted"] += 1
    rows = []
    cursor = start
    while cursor <= date.today():
        key = cursor.strftime("%Y-%m")
        r = out[key]
        rows.append({"month": cursor.strftime("%b %Y"), **r,
                     "conversion_rate": round(100 * r["converted"] / r["first_timers"]) if r["first_timers"] else 0})
        cursor = (cursor + timedelta(days=32)).replace(day=1)
    return rows


@search.register("visitors", "visitors.view")
def _search_visitors(query: str, limit: int):
    like = f"%{query}%"
    q = visitors_query().filter((Visitor.first_name.ilike(like)) | (Visitor.last_name.ilike(like)) |
                                (Visitor.phone.ilike(like)))
    for v in q.limit(limit):
        yield search.SearchResult(title=v.display_name, url=url_for("visitors.profile", public_id=v.public_id),
                                  kind="Visitor", subtitle=f"First visit {v.first_visit_date:%d %b %Y}", icon="user-plus")


# --------------------------------------------------------------------------- #
# Departments
# --------------------------------------------------------------------------- #
def department_sizes() -> dict[int, int]:
    return dict(db.session.query(DepartmentMembership.department_id, func.count())
                .group_by(DepartmentMembership.department_id).all())


# --------------------------------------------------------------------------- #
# Staff
# --------------------------------------------------------------------------- #
def new_staff_number(tenant) -> str:
    prefix = member_svc.member_prefix(tenant)
    while True:
        number = f"{prefix}-S{member_svc.next_sequence('staff'):04d}"
        if not db.session.query(StaffMember.id).filter_by(staff_number=number).first():
            return number


def working_days(start: date, end: date) -> int:
    days, d = 0, start
    while d <= end:
        if d.weekday() < 5:
            days += 1
        d += timedelta(days=1)
    return days


def leave_taken(staff: StaffMember, year: int | None = None) -> int:
    year = year or date.today().year
    return sum(r.days for r in staff.leave if r.status == LeaveStatus.APPROVED and r.leave_type.value == "annual"
               and r.start_date.year == year)


@search.register("staff", "clergy.view")
def _search_staff(query: str, limit: int):
    like = f"%{query}%"
    q = db.session.query(StaffMember).filter((StaffMember.first_name.ilike(like)) | (StaffMember.last_name.ilike(like)) |
                                             (StaffMember.staff_number.ilike(like)))
    for s in q.limit(limit):
        yield search.SearchResult(title=s.display_name, url=url_for("clergy.profile", public_id=s.public_id),
                                  kind="Staff", subtitle=f"{s.position} · {s.staff_number}", icon="briefcase")
