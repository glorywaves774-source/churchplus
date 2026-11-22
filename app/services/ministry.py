"""Business rules for events, sermons, prayer, counselling and children's ministry.

Privacy rules
-------------
* Prayer: anyone with ``prayer.view`` sees ordinary requests. Confidential requests are
  visible only to the person they are assigned to and to people with ``prayer.approve``.
* Counselling: a case is visible to its counsellor. Standard cases are also visible to
  people with ``counselling.approve`` (e.g. the senior pastor). Restricted cases are
  visible to the counsellor only. Opening a case is written to the audit trail.
  Platform (SaaS) staff never hold church roles, so they never see counselling data.
* Children: profiles, guardians and check-ins need ``children.view``; safeguarding
  incidents need ``children.approve``.
"""
from __future__ import annotations

import secrets
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from flask import abort, g, url_for
from sqlalchemy import func, or_

from app.extensions import db
from app.models import (
    ChildCheckIn, ChildProfile, CounsellingCase, Event, EventRegistration, EventStatus, Expense, ExpenseStatus,
    Member, PrayerRequest, RegistrationStatus, Sermon,
)
from app.services import audit, search
from app.services import members as member_svc
from app.services.members import next_sequence


def _can(code: str) -> bool:
    return bool(getattr(g, "account", None) and g.account.can(code))


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #
def events_query():
    q = db.session.query(Event)
    limit = member_svc.branch_limit_id()
    if limit is not None:
        q = q.filter(or_(Event.branch_id == limit, Event.branch_id.is_(None)))
    return q


def get_event_or_404(public_id: str) -> Event:
    e = events_query().filter(Event.public_id == public_id).first()
    if e is None:
        abort(404)
    return e


def seats_taken(event: Event) -> int:
    rows = (db.session.query(func.count(EventRegistration.id), func.coalesce(func.sum(EventRegistration.guests), 0))
            .filter(EventRegistration.event_id == event.id, EventRegistration.status == RegistrationStatus.REGISTERED).one())
    return int(rows[0]) + int(rows[1])


def register(event: Event, *, member: Member | None = None, name: str | None = None, phone: str | None = None,
             email: str | None = None, guests: int = 0, actor=None) -> EventRegistration:
    if event.status != EventStatus.PUBLISHED:
        raise ValueError("Registration is only open for published events.")
    if event.registration_closes and datetime.now() > event.registration_closes:
        raise ValueError("Registration has closed.")
    if member is None and not (name or "").strip():
        raise ValueError("Enter a membership number or a name.")
    if member is not None:
        existing = db.session.query(EventRegistration).filter_by(event_id=event.id, member_id=member.id).first()
        if existing and existing.status != RegistrationStatus.CANCELLED:
            raise ValueError(f"{member.display_name} is already registered.")
        if existing:
            db.session.delete(existing)
            db.session.flush()
    guests = max(0, min(int(guests or 0), 20))
    full = event.capacity is not None and seats_taken(event) + 1 + guests > event.capacity
    reg = EventRegistration(event_id=event.id, member_id=getattr(member, "id", None), name=(name or None) if not member else None,
                            phone=phone or None, email=email or None, guests=guests,
                            status=RegistrationStatus.WAITLISTED if full else RegistrationStatus.REGISTERED)
    db.session.add(reg)
    db.session.flush()
    audit.record("event.registration", entity=event, actor=actor, name=event.title, person=reg.display_name,
                 status=reg.status.value)
    return reg


def promote_waitlist(event: Event) -> int:
    """After a cancellation, move waitlisted people into free places (first come, first served)."""
    moved = 0
    for reg in (db.session.query(EventRegistration).filter_by(event_id=event.id, status=RegistrationStatus.WAITLISTED)
                .order_by(EventRegistration.created_at)):
        if event.capacity is not None and seats_taken(event) + 1 + reg.guests > event.capacity:
            break
        reg.status = RegistrationStatus.REGISTERED
        db.session.flush()
        moved += 1
    return moved


def event_spend(event: Event) -> Decimal:
    total = (db.session.query(func.coalesce(func.sum(Expense.amount), 0))
             .filter(Expense.event_id == event.id, Expense.status.in_((ExpenseStatus.APPROVED, ExpenseStatus.PAID))).scalar())
    return Decimal(str(total)).quantize(Decimal("0.01"))


def to_local_dt(d: date, t: time | None) -> datetime:
    return datetime.combine(d, t or time(0, 0))


@search.register("events", "events.view")
def _search_events(query: str, limit: int):
    for e in events_query().filter(Event.title.ilike(f"%{query}%")).order_by(Event.starts_at.desc()).limit(limit):
        yield search.SearchResult(title=e.title, url=url_for("events.detail", public_id=e.public_id), kind="Event",
                                  subtitle=e.starts_at.strftime("%d %b %Y"), icon="calendar-days")


@search.register("sermons", "sermons.view")
def _search_sermons(query: str, limit: int):
    like = f"%{query}%"
    q = db.session.query(Sermon).filter(or_(Sermon.title.ilike(like), Sermon.preacher.ilike(like),
                                            Sermon.scripture.ilike(like), Sermon.series.ilike(like)))
    for s in q.order_by(Sermon.preached_on.desc()).limit(limit):
        yield search.SearchResult(title=s.title, url=url_for("sermons.detail", public_id=s.public_id), kind="Sermon",
                                  subtitle=f"{s.preacher} · {s.preached_on:%d %b %Y}", icon="book-open")


# --------------------------------------------------------------------------- #
# Prayer
# --------------------------------------------------------------------------- #
def prayer_query():
    q = db.session.query(PrayerRequest)
    limit = member_svc.branch_limit_id()
    if limit is not None:
        q = q.filter(or_(PrayerRequest.branch_id == limit, PrayerRequest.branch_id.is_(None)))
    if not _can("prayer.approve"):
        q = q.filter(or_(PrayerRequest.is_confidential.is_(False), PrayerRequest.assigned_to_id == g.account.id))
    return q


def get_prayer_or_404(public_id: str) -> PrayerRequest:
    p = prayer_query().filter(PrayerRequest.public_id == public_id).first()
    if p is None:
        abort(404)
    return p


# --------------------------------------------------------------------------- #
# Counselling
# --------------------------------------------------------------------------- #
def cases_query():
    q = db.session.query(CounsellingCase)
    if _can("counselling.approve"):
        return q.filter(or_(CounsellingCase.is_restricted.is_(False), CounsellingCase.counsellor_id == g.account.id))
    return q.filter(CounsellingCase.counsellor_id == g.account.id)


def get_case_or_404(public_id: str) -> CounsellingCase:
    c = cases_query().filter(CounsellingCase.public_id == public_id).first()
    if c is None:
        abort(404)
    audit.record("counselling.case_viewed", entity=c, actor=g.user, name=c.reference)
    return c


def new_case_reference() -> str:
    return f"CC-{next_sequence('counselling'):05d}"


def is_counsellor(c: CounsellingCase) -> bool:
    return c.counsellor_id == g.account.id


# --------------------------------------------------------------------------- #
# Children
# --------------------------------------------------------------------------- #
def children_query():
    q = db.session.query(ChildProfile).join(Member, Member.id == ChildProfile.member_id)
    limit = member_svc.branch_limit_id()
    if limit is not None:
        q = q.filter(Member.branch_id == limit)
    return q


def get_child_or_404(public_id: str) -> ChildProfile:
    c = children_query().filter(ChildProfile.public_id == public_id).first()
    if c is None:
        abort(404)
    return c


def new_pickup_code() -> str:
    """4-digit code printed on the child's and parent's labels."""
    return f"{secrets.randbelow(9000) + 1000}"


def active_check_in(child: ChildProfile, on: date | None = None) -> ChildCheckIn | None:
    return (db.session.query(ChildCheckIn).filter_by(child_id=child.id, check_in_date=on or date.today())
            .filter(ChildCheckIn.checked_out_at.is_(None)).first())


def incident_reference() -> str:
    return f"SG-{next_sequence('safeguarding'):05d}"


class CheckOutRefused(ValueError):
    pass


def class_for_age(age: int | None):
    """The best matching children's class for an age (youngest range first)."""
    from app.models import ChildClass
    if age is None:
        return None
    return (db.session.query(ChildClass).filter(ChildClass.min_age <= age, ChildClass.max_age >= age)
            .order_by(ChildClass.max_age - ChildClass.min_age, ChildClass.min_age).first())


def check_in(child: ChildProfile, *, actor, dropped_off_by: str | None = None, service=None) -> ChildCheckIn:
    """Check a child in for today and issue a pick-up code. Refuses a second open check-in."""
    from app.services import operations
    existing = active_check_in(child)
    if existing:
        raise ValueError(f"{child.member.display_name} is already checked in.")
    ci = ChildCheckIn(child_id=child.id, class_id=child.class_id, service_id=getattr(service, "id", None),
                      check_in_date=date.today(), dropped_off_by=(dropped_off_by or "").strip()[:160] or None,
                      pickup_code=new_pickup_code(), recorded_by_id=getattr(actor, "id", None))
    db.session.add(ci)
    if service is not None:
        from app.models import CheckInMethod
        operations.mark_present(service, child.member, CheckInMethod.MANUAL, actor)
    db.session.flush()
    audit.record("child.checked_in", entity=child, actor=actor, name=child.member.display_name)
    return ci


def check_out(ci: ChildCheckIn, *, code: str, guardian=None, collected_by: str | None = None, actor) -> None:
    """Release a child. The pick-up code must match; a named guardian must be authorised."""
    child = ci.child
    if ci.checked_out_at is not None:
        raise CheckOutRefused("This child has already been collected.")
    if not secrets.compare_digest((code or "").strip(), ci.pickup_code):
        audit.record("child.checkout_refused", entity=child, actor=actor, name=child.member.display_name)
        raise CheckOutRefused("The pick-up code does not match. Do not release the child; call the class leader.")
    if guardian is not None:
        if guardian.child_id != child.id:
            raise CheckOutRefused("That person is not listed for this child.")
        if not guardian.is_authorized_pickup:
            audit.record("child.checkout_refused", entity=child, actor=actor, name=child.member.display_name,
                         reason="not authorised")
            raise CheckOutRefused(f"{guardian.display_name} is not allowed to collect this child.")
    elif not (collected_by or "").strip():
        raise CheckOutRefused("Choose the guardian collecting the child, or write their name.")
    from app.models.base import utcnow
    ci.checked_out_at = utcnow()
    ci.collected_by_guardian_id = getattr(guardian, "id", None)
    ci.collected_by_name = guardian.display_name if guardian else collected_by.strip()[:160]
    audit.record("child.checked_out", entity=child, actor=actor, name=child.member.display_name,
                 collected_by=ci.collected_by_name)
