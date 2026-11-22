"""Ministry tools: events, sermons and media, prayer, counselling, children and youth."""
from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean, Date, DateTime, Enum, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import PublicIdMixin, TenantScopedMixin, TimestampMixin, utcnow


def _enum(e, length):
    return Enum(e, native_enum=False, length=length, values_callable=lambda x: [m.value for m in x])


# =========================================================================== #
# Events and programmes
# =========================================================================== #
class EventStatus(str, enum.Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class Event(PublicIdMixin, TimestampMixin, TenantScopedMixin, db.Model):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_tenant_start", "tenant_id", "starts_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    category: Mapped[str] = mapped_column(String(60), nullable=False, default="Programme")
    description: Mapped[str | None] = mapped_column(Text)
    starts_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)       # local church time
    ends_at: Mapped[datetime | None] = mapped_column(DateTime)
    venue: Mapped[str | None] = mapped_column(String(200))
    livestream_url: Mapped[str | None] = mapped_column(String(255))
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id", ondelete="SET NULL"), index=True)
    department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id", ondelete="SET NULL"))
    requires_registration: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    capacity: Mapped[int | None] = mapped_column(Integer)
    registration_closes: Mapped[datetime | None] = mapped_column(DateTime)
    budget_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)     # shown on church website
    status: Mapped[EventStatus] = mapped_column(_enum(EventStatus, 12), default=EventStatus.DRAFT, nullable=False)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    branch = relationship("Branch")
    department = relationship("Department")
    registrations: Mapped[list["EventRegistration"]] = relationship(back_populates="event", cascade="all, delete-orphan")
    committee: Mapped[list["EventCommitteeMember"]] = relationship(back_populates="event", cascade="all, delete-orphan")


class RegistrationStatus(str, enum.Enum):
    REGISTERED = "registered"
    WAITLISTED = "waitlisted"
    CANCELLED = "cancelled"


class EventRegistration(PublicIdMixin, TenantScopedMixin, db.Model):
    __tablename__ = "event_registrations"
    __table_args__ = (UniqueConstraint("event_id", "member_id", name="uq_event_registration_member"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"))
    name: Mapped[str | None] = mapped_column(String(160))
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(255))
    guests: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[RegistrationStatus] = mapped_column(_enum(RegistrationStatus, 12), default=RegistrationStatus.REGISTERED, nullable=False)
    attended: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    event: Mapped[Event] = relationship(back_populates="registrations")
    member = relationship("Member")

    @property
    def display_name(self) -> str:
        return self.member.display_name if self.member else (self.name or "—")


class EventCommitteeMember(PublicIdMixin, TenantScopedMixin, db.Model):
    __tablename__ = "event_committee_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id", ondelete="SET NULL"))
    name: Mapped[str | None] = mapped_column(String(160))
    role: Mapped[str] = mapped_column(String(80), nullable=False)

    event: Mapped[Event] = relationship(back_populates="committee")
    member = relationship("Member")

    @property
    def display_name(self) -> str:
        return self.member.display_name if self.member else (self.name or "—")


# =========================================================================== #
# Sermons and media
# =========================================================================== #
class Visibility(str, enum.Enum):
    STAFF = "staff"          # people with sermons.view
    MEMBERS = "members"      # member portal (Stage 8)
    PUBLIC = "public"        # church website (Stage 8)


class Sermon(PublicIdMixin, TimestampMixin, TenantScopedMixin, db.Model):
    __tablename__ = "sermons"
    __table_args__ = (Index("ix_sermons_tenant_date", "tenant_id", "preached_on"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    preacher: Mapped[str] = mapped_column(String(160), nullable=False)
    preached_on: Mapped[date] = mapped_column(Date, nullable=False)
    series: Mapped[str | None] = mapped_column(String(160))
    category: Mapped[str | None] = mapped_column(String(80))
    scripture: Mapped[str | None] = mapped_column(String(255))
    summary: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    service_id: Mapped[int | None] = mapped_column(ForeignKey("services.id", ondelete="SET NULL"))
    video_url: Mapped[str | None] = mapped_column(String(255))       # YouTube, Facebook, Vimeo…
    audio_url: Mapped[str | None] = mapped_column(String(255))
    visibility: Mapped[Visibility] = mapped_column(_enum(Visibility, 10), default=Visibility.MEMBERS, nullable=False)

    service = relationship("Service")
    media: Mapped[list["MediaItem"]] = relationship(back_populates="sermon")


class MediaKind(str, enum.Enum):
    AUDIO = "audio"
    VIDEO = "video"
    DOCUMENT = "document"
    IMAGE = "image"
    LINK = "link"


class MediaItem(PublicIdMixin, TenantScopedMixin, db.Model):
    __tablename__ = "media_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[MediaKind] = mapped_column(_enum(MediaKind, 10), nullable=False)
    category: Mapped[str | None] = mapped_column(String(80))
    file_key: Mapped[str | None] = mapped_column(String(255))
    file_name: Mapped[str | None] = mapped_column(String(255))
    content_type: Mapped[str | None] = mapped_column(String(100))
    size: Mapped[int | None] = mapped_column(Integer)
    url: Mapped[str | None] = mapped_column(String(255))
    sermon_id: Mapped[int | None] = mapped_column(ForeignKey("sermons.id", ondelete="SET NULL"), index=True)
    visibility: Mapped[Visibility] = mapped_column(_enum(Visibility, 10), default=Visibility.MEMBERS, nullable=False)
    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    sermon: Mapped[Sermon | None] = relationship(back_populates="media")


# =========================================================================== #
# Prayer
# =========================================================================== #
class PrayerStatus(str, enum.Enum):
    OPEN = "open"
    PRAYING = "praying"
    ANSWERED = "answered"
    CLOSED = "closed"


class PrayerRequest(PublicIdMixin, TimestampMixin, TenantScopedMixin, db.Model):
    __tablename__ = "prayer_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id", ondelete="SET NULL"), index=True)
    name: Mapped[str | None] = mapped_column(String(160))
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(60), nullable=False, default="General")
    request: Mapped[str] = mapped_column(Text, nullable=False)
    is_confidential: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    wants_contact: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="staff")   # staff, website, portal
    status: Mapped[PrayerStatus] = mapped_column(_enum(PrayerStatus, 10), default=PrayerStatus.OPEN, nullable=False)
    team_department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id", ondelete="SET NULL"))
    assigned_to_id: Mapped[int | None] = mapped_column(ForeignKey("tenant_users.id", ondelete="SET NULL"), index=True)
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id", ondelete="SET NULL"), index=True)

    member = relationship("Member")
    team = relationship("Department")
    assigned_to = relationship("TenantUser")
    updates: Mapped[list["PrayerUpdate"]] = relationship(back_populates="prayer", cascade="all, delete-orphan",
                                                         order_by="PrayerUpdate.created_at")

    @property
    def display_name(self) -> str:
        return self.member.display_name if self.member else (self.name or "Anonymous")


class PrayerUpdate(TenantScopedMixin, db.Model):
    __tablename__ = "prayer_updates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prayer_id: Mapped[int] = mapped_column(ForeignKey("prayer_requests.id", ondelete="CASCADE"), nullable=False, index=True)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    prayer: Mapped[PrayerRequest] = relationship(back_populates="updates")
    author = relationship("User")


# =========================================================================== #
# Counselling (strictest access)
# =========================================================================== #
class CaseStatus(str, enum.Enum):
    OPEN = "open"
    CLOSED = "closed"


class CounsellingCase(PublicIdMixin, TimestampMixin, TenantScopedMixin, db.Model):
    """Visible only to the assigned counsellor, and – for standard cases – to people with
    counselling approval rights. Restricted cases: counsellor only."""

    __tablename__ = "counselling_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reference: Mapped[str] = mapped_column(String(20), nullable=False)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id", ondelete="SET NULL"), index=True)
    client_name: Mapped[str | None] = mapped_column(String(160))
    client_phone: Mapped[str | None] = mapped_column(String(40))
    category: Mapped[str] = mapped_column(String(60), nullable=False)
    counsellor_id: Mapped[int] = mapped_column(ForeignKey("tenant_users.id", ondelete="RESTRICT"), nullable=False, index=True)
    is_restricted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[CaseStatus] = mapped_column(_enum(CaseStatus, 10), default=CaseStatus.OPEN, nullable=False)
    opened_on: Mapped[date] = mapped_column(Date, nullable=False)
    closed_on: Mapped[date | None] = mapped_column(Date)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    member = relationship("Member")
    counsellor = relationship("TenantUser")
    sessions: Mapped[list["CounsellingSession"]] = relationship(back_populates="case", cascade="all, delete-orphan",
                                                                order_by="CounsellingSession.scheduled_at")

    @property
    def client(self) -> str:
        return self.member.display_name if self.member else (self.client_name or "—")


class SessionStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    HELD = "held"
    MISSED = "missed"
    CANCELLED = "cancelled"


class CounsellingSession(PublicIdMixin, TenantScopedMixin, db.Model):
    __tablename__ = "counselling_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("counselling_cases.id", ondelete="CASCADE"), nullable=False, index=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    location: Mapped[str | None] = mapped_column(String(160))
    status: Mapped[SessionStatus] = mapped_column(_enum(SessionStatus, 10), default=SessionStatus.SCHEDULED, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    follow_up: Mapped[str | None] = mapped_column(String(255))

    case: Mapped[CounsellingCase] = relationship(back_populates="sessions")


# =========================================================================== #
# Children and youth
# =========================================================================== #
class ChildClass(PublicIdMixin, TenantScopedMixin, db.Model):
    """Sunday school class or youth group, by age range."""

    __tablename__ = "child_classes"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_child_classes_tenant_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    min_age: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_age: Mapped[int] = mapped_column(Integer, nullable=False, default=17)
    room: Mapped[str | None] = mapped_column(String(80))
    is_youth: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id", ondelete="SET NULL"))

    teachers: Mapped[list["ClassTeacher"]] = relationship(back_populates="child_class", cascade="all, delete-orphan")


class ClassTeacher(PublicIdMixin, TenantScopedMixin, db.Model):
    """A teacher or helper, with safeguarding clearance dates."""

    __tablename__ = "class_teachers"
    __table_args__ = (UniqueConstraint("class_id", "member_id", name="uq_class_teacher"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    class_id: Mapped[int] = mapped_column(ForeignKey("child_classes.id", ondelete="CASCADE"), nullable=False, index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(40), nullable=False, default="Teacher")
    background_check_date: Mapped[date | None] = mapped_column(Date)
    safeguarding_training_date: Mapped[date | None] = mapped_column(Date)

    child_class: Mapped[ChildClass] = relationship(back_populates="teachers")
    member = relationship("Member")

    def is_cleared(self, today: date | None = None) -> bool:
        """Both checks done within the last 3 years."""
        today = today or date.today()
        ok = lambda d: d is not None and (today - d).days <= 3 * 365
        return ok(self.background_check_date) and ok(self.safeguarding_training_date)


class ChildProfile(PublicIdMixin, TimestampMixin, TenantScopedMixin, db.Model):
    """Extra, restricted information for a child or young person (a member under 18)."""

    __tablename__ = "child_profiles"
    __table_args__ = (UniqueConstraint("member_id", name="uq_child_profiles_member"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), nullable=False)
    class_id: Mapped[int | None] = mapped_column(ForeignKey("child_classes.id", ondelete="SET NULL"), index=True)
    school: Mapped[str | None] = mapped_column(String(160))
    allergies: Mapped[str | None] = mapped_column(String(255))
    medical_notes: Mapped[str | None] = mapped_column(Text)
    special_needs: Mapped[str | None] = mapped_column(Text)
    photo_consent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    member = relationship("Member")
    child_class: Mapped[ChildClass | None] = relationship()
    guardians: Mapped[list["ChildGuardian"]] = relationship(back_populates="child", cascade="all, delete-orphan")


class ChildGuardian(PublicIdMixin, TenantScopedMixin, db.Model):
    __tablename__ = "child_guardians"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    child_id: Mapped[int] = mapped_column(ForeignKey("child_profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id", ondelete="SET NULL"))
    name: Mapped[str | None] = mapped_column(String(160))
    phone: Mapped[str | None] = mapped_column(String(40))
    relationship_to_child: Mapped[str] = mapped_column(String(40), nullable=False)
    is_authorized_pickup: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    pickup_restriction: Mapped[str | None] = mapped_column(String(255))     # court order etc.

    child: Mapped[ChildProfile] = relationship(back_populates="guardians")
    member = relationship("Member")

    @property
    def display_name(self) -> str:
        return self.member.display_name if self.member else (self.name or "—")

    @property
    def contact(self) -> str | None:
        return (self.member.phone if self.member else None) or self.phone


class ChildCheckIn(PublicIdMixin, TenantScopedMixin, db.Model):
    """Secure check-in: a pick-up code is issued at drop-off and must match at collection."""

    __tablename__ = "child_check_ins"
    __table_args__ = (Index("ix_child_check_ins_day", "tenant_id", "check_in_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    child_id: Mapped[int] = mapped_column(ForeignKey("child_profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    class_id: Mapped[int | None] = mapped_column(ForeignKey("child_classes.id", ondelete="SET NULL"))
    service_id: Mapped[int | None] = mapped_column(ForeignKey("services.id", ondelete="SET NULL"))
    check_in_date: Mapped[date] = mapped_column(Date, nullable=False)
    checked_in_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    dropped_off_by: Mapped[str | None] = mapped_column(String(160))
    pickup_code: Mapped[str] = mapped_column(String(8), nullable=False)
    checked_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collected_by_guardian_id: Mapped[int | None] = mapped_column(ForeignKey("child_guardians.id", ondelete="SET NULL"))
    collected_by_name: Mapped[str | None] = mapped_column(String(160))
    recorded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    child: Mapped[ChildProfile] = relationship()
    child_class = relationship("ChildClass")
    collected_by = relationship("ChildGuardian")


class IncidentStatus(str, enum.Enum):
    OPEN = "open"
    REFERRED = "referred"      # to authorities / social welfare
    CLOSED = "closed"


class SafeguardingIncident(PublicIdMixin, TimestampMixin, TenantScopedMixin, db.Model):
    """Safeguarding concerns: visible only to people with children approval rights."""

    __tablename__ = "safeguarding_incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reference: Mapped[str] = mapped_column(String(20), nullable=False)
    child_id: Mapped[int | None] = mapped_column(ForeignKey("child_profiles.id", ondelete="SET NULL"))
    occurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    category: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    action_taken: Mapped[str | None] = mapped_column(Text)
    referred_to: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[IncidentStatus] = mapped_column(_enum(IncidentStatus, 10), default=IncidentStatus.OPEN, nullable=False)
    reported_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    child: Mapped[ChildProfile | None] = relationship()
