"""Church operations: services, attendance, visitors, departments, staff."""
from __future__ import annotations

import enum
from datetime import date, datetime, time

from sqlalchemy import (
    Boolean, Date, DateTime, Enum, ForeignKey, Index, Integer, String, Text, Time, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import PublicIdMixin, TenantScopedMixin, TimestampMixin, utcnow


def _enum(e, length):
    return Enum(e, native_enum=False, length=length, values_callable=lambda x: [m.value for m in x])


# =========================================================================== #
# Services and attendance
# =========================================================================== #
class ServiceKind(str, enum.Enum):
    SERVICE = "service"                  # Sunday / midweek worship
    SPECIAL = "special"                  # special programme, crusade, convention
    DEPARTMENT = "department"            # departmental meeting


class ServiceStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ServiceType(PublicIdMixin, TenantScopedMixin, db.Model):
    __tablename__ = "service_types"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_service_types_tenant_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[ServiceKind] = mapped_column(_enum(ServiceKind, 12), default=ServiceKind.SERVICE, nullable=False)
    weekday: Mapped[int | None] = mapped_column(Integer)          # 0 = Monday … 6 = Sunday
    start_time: Mapped[time | None] = mapped_column(Time)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Service(PublicIdMixin, TimestampMixin, TenantScopedMixin, db.Model):
    """One gathering on one date: a service, special programme or departmental meeting."""

    __tablename__ = "services"
    __table_args__ = (Index("ix_services_tenant_date", "tenant_id", "service_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_type_id: Mapped[int | None] = mapped_column(ForeignKey("service_types.id", ondelete="SET NULL"), index=True)
    kind: Mapped[ServiceKind] = mapped_column(_enum(ServiceKind, 12), default=ServiceKind.SERVICE, nullable=False)
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id", ondelete="SET NULL"), index=True)
    department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id", ondelete="SET NULL"), index=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    theme: Mapped[str | None] = mapped_column(String(255))
    service_date: Mapped[date] = mapped_column(Date, nullable=False)
    start_time: Mapped[time | None] = mapped_column(Time)
    end_time: Mapped[time | None] = mapped_column(Time)
    venue: Mapped[str | None] = mapped_column(String(160))
    preacher: Mapped[str | None] = mapped_column(String(160))
    worship_leader: Mapped[str | None] = mapped_column(String(160))
    scripture_readings: Mapped[str | None] = mapped_column(String(255))
    sermon_title: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ServiceStatus] = mapped_column(_enum(ServiceStatus, 12), default=ServiceStatus.SCHEDULED, nullable=False)

    # Headcount (for churches that count rather than mark names)
    headcount_men: Mapped[int | None] = mapped_column(Integer)
    headcount_women: Mapped[int | None] = mapped_column(Integer)
    headcount_children: Mapped[int | None] = mapped_column(Integer)
    headcount_visitors: Mapped[int | None] = mapped_column(Integer)

    service_type = relationship("ServiceType")
    branch = relationship("Branch")
    department = relationship("Department")
    items: Mapped[list["ServiceItem"]] = relationship(back_populates="service", cascade="all, delete-orphan",
                                                      order_by="ServiceItem.position")
    assignments: Mapped[list["ServiceAssignment"]] = relationship(back_populates="service",
                                                                  cascade="all, delete-orphan",
                                                                  order_by="ServiceAssignment.role")
    attendance: Mapped[list["AttendanceRecord"]] = relationship(back_populates="service", cascade="all, delete-orphan")

    @property
    def headcount_total(self) -> int | None:
        parts = [self.headcount_men, self.headcount_women, self.headcount_children]
        return sum(p or 0 for p in parts) if any(p is not None for p in parts) else None


class ServiceItem(TenantScopedMixin, db.Model):
    """A line of the order of service."""

    __tablename__ = "service_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id", ondelete="CASCADE"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    start_time: Mapped[time | None] = mapped_column(Time)
    activity: Mapped[str] = mapped_column(String(160), nullable=False)
    led_by: Mapped[str | None] = mapped_column(String(160))
    duration_minutes: Mapped[int | None] = mapped_column(Integer)

    service: Mapped[Service] = relationship(back_populates="items")


class ServiceAssignment(TenantScopedMixin, db.Model):
    """Who serves: ushers, readers, protocol, media, …"""

    __tablename__ = "service_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(80), nullable=False)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id", ondelete="SET NULL"))
    name: Mapped[str | None] = mapped_column(String(160))       # when not a registered member

    service: Mapped[Service] = relationship(back_populates="assignments")
    member = relationship("Member")

    @property
    def display(self) -> str:
        return self.member.display_name if self.member else (self.name or "")


class CheckInMethod(str, enum.Enum):
    MANUAL = "manual"
    QR = "qr"
    NUMBER = "number"


class AttendanceRecord(TenantScopedMixin, db.Model):
    __tablename__ = "attendance_records"
    __table_args__ = (
        UniqueConstraint("service_id", "member_id", name="uq_attendance_service_member"),
        UniqueConstraint("service_id", "visitor_id", name="uq_attendance_service_visitor"),
        Index("ix_attendance_member", "tenant_id", "member_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id", ondelete="CASCADE"), nullable=False, index=True)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"))
    visitor_id: Mapped[int | None] = mapped_column(ForeignKey("visitors.id", ondelete="CASCADE"))
    method: Mapped[CheckInMethod] = mapped_column(_enum(CheckInMethod, 10), default=CheckInMethod.MANUAL, nullable=False)
    checked_in_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    recorded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    service: Mapped[Service] = relationship(back_populates="attendance")
    member = relationship("Member")
    visitor = relationship("Visitor")


# =========================================================================== #
# Visitors and follow-up
# =========================================================================== #
class VisitorStatus(str, enum.Enum):
    NEW = "new"
    FOLLOWING_UP = "following_up"
    REGULAR = "regular"
    CONVERTED = "converted"
    INACTIVE = "inactive"


class Visitor(PublicIdMixin, TimestampMixin, TenantScopedMixin, db.Model):
    __tablename__ = "visitors"
    __table_args__ = (Index("ix_visitors_tenant_first_visit", "tenant_id", "first_visit_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    gender: Mapped[str | None] = mapped_column(String(10))
    age_group: Mapped[str | None] = mapped_column(String(20))   # child, youth, adult, senior
    phone: Mapped[str | None] = mapped_column(String(40), index=True)
    email: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(120))
    photo_key: Mapped[str | None] = mapped_column(String(255))
    first_visit_date: Mapped[date] = mapped_column(Date, nullable=False)
    service_id: Mapped[int | None] = mapped_column(ForeignKey("services.id", ondelete="SET NULL"))
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id", ondelete="SET NULL"), index=True)
    how_heard: Mapped[str | None] = mapped_column(String(80))
    invited_by_member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id", ondelete="SET NULL"))
    current_church: Mapped[str | None] = mapped_column(String(200))
    wants_visit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    wants_membership: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    prayer_request: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[VisitorStatus] = mapped_column(_enum(VisitorStatus, 15), default=VisitorStatus.NEW, nullable=False)
    assigned_to_id: Mapped[int | None] = mapped_column(ForeignKey("tenant_users.id", ondelete="SET NULL"), index=True)
    converted_member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id", ondelete="SET NULL"))
    converted_at: Mapped[date | None] = mapped_column(Date)

    service = relationship("Service", foreign_keys=[service_id])
    branch = relationship("Branch")
    invited_by = relationship("Member", foreign_keys=[invited_by_member_id])
    converted_member = relationship("Member", foreign_keys=[converted_member_id])
    assigned_to = relationship("TenantUser")
    follow_ups: Mapped[list["VisitorFollowUp"]] = relationship(back_populates="visitor", cascade="all, delete-orphan",
                                                               order_by="VisitorFollowUp.due_date")

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


class FollowUpMethod(str, enum.Enum):
    CALL = "call"
    VISIT = "visit"
    SMS = "sms"
    WHATSAPP = "whatsapp"
    EMAIL = "email"


class VisitorFollowUp(PublicIdMixin, TenantScopedMixin, db.Model):
    __tablename__ = "visitor_follow_ups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    visitor_id: Mapped[int] = mapped_column(ForeignKey("visitors.id", ondelete="CASCADE"), nullable=False, index=True)
    due_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    method: Mapped[FollowUpMethod] = mapped_column(_enum(FollowUpMethod, 10), default=FollowUpMethod.CALL, nullable=False)
    assigned_to_id: Mapped[int | None] = mapped_column(ForeignKey("tenant_users.id", ondelete="SET NULL"), index=True)
    outcome: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    visitor: Mapped[Visitor] = relationship(back_populates="follow_ups")
    assigned_to = relationship("TenantUser")


# =========================================================================== #
# Departments and ministries
# =========================================================================== #
class DepartmentKind(str, enum.Enum):
    DEPARTMENT = "department"
    MINISTRY = "ministry"


class Department(PublicIdMixin, TimestampMixin, TenantScopedMixin, db.Model):
    __tablename__ = "departments"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_departments_tenant_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[DepartmentKind] = mapped_column(_enum(DepartmentKind, 12), default=DepartmentKind.MINISTRY, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id", ondelete="SET NULL"))
    leader_member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id", ondelete="SET NULL"))
    meeting_schedule: Mapped[str | None] = mapped_column(String(160))   # e.g. "Saturdays, 4pm"
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    branch = relationship("Branch")
    leader = relationship("Member")
    memberships: Mapped[list["DepartmentMembership"]] = relationship(back_populates="department",
                                                                     cascade="all, delete-orphan")


class DepartmentMembership(PublicIdMixin, TenantScopedMixin, db.Model):
    __tablename__ = "department_memberships"
    __table_args__ = (UniqueConstraint("department_id", "member_id", name="uq_department_member"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id", ondelete="CASCADE"), nullable=False, index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(60), nullable=False, default="Member")
    joined_on: Mapped[date] = mapped_column(Date, nullable=False, default=date.today)

    department: Mapped[Department] = relationship(back_populates="memberships")
    member = relationship("Member")


# =========================================================================== #
# Clergy, staff and volunteers
# =========================================================================== #
class StaffCategory(str, enum.Enum):
    CLERGY = "clergy"
    STAFF = "staff"
    VOLUNTEER = "volunteer"


class EmploymentType(str, enum.Enum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    VOLUNTEER = "volunteer"


class StaffStatus(str, enum.Enum):
    ACTIVE = "active"
    ON_LEAVE = "on_leave"
    SUSPENDED = "suspended"
    EXITED = "exited"


class StaffMember(PublicIdMixin, TimestampMixin, TenantScopedMixin, db.Model):
    __tablename__ = "staff_members"
    __table_args__ = (UniqueConstraint("tenant_id", "staff_number", name="uq_staff_tenant_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    staff_number: Mapped[str] = mapped_column(String(30), nullable=False)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id", ondelete="SET NULL"))
    category: Mapped[StaffCategory] = mapped_column(_enum(StaffCategory, 12), nullable=False)
    title: Mapped[str | None] = mapped_column(String(40))
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    gender: Mapped[str | None] = mapped_column(String(10))
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(String(255))
    photo_key: Mapped[str | None] = mapped_column(String(255))
    position: Mapped[str] = mapped_column(String(120), nullable=False)
    department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id", ondelete="SET NULL"))
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id", ondelete="SET NULL"))
    employment_type: Mapped[EmploymentType] = mapped_column(_enum(EmploymentType, 12), nullable=False)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    ordination_date: Mapped[date | None] = mapped_column(Date)
    ghana_card_number: Mapped[str | None] = mapped_column(String(30))
    ssnit_number: Mapped[str | None] = mapped_column(String(30))
    status: Mapped[StaffStatus] = mapped_column(_enum(StaffStatus, 12), default=StaffStatus.ACTIVE, nullable=False)
    annual_leave_days: Mapped[int] = mapped_column(Integer, default=21, nullable=False)
    emergency_name: Mapped[str | None] = mapped_column(String(160))
    emergency_phone: Mapped[str | None] = mapped_column(String(40))
    notes: Mapped[str | None] = mapped_column(Text)

    member = relationship("Member")
    department = relationship("Department")
    branch = relationship("Branch")
    records: Mapped[list["StaffRecord"]] = relationship(back_populates="staff", cascade="all, delete-orphan",
                                                        order_by="StaffRecord.record_date.desc()")
    leave: Mapped[list["LeaveRequest"]] = relationship(back_populates="staff", cascade="all, delete-orphan",
                                                       order_by="LeaveRequest.start_date.desc()",
                                                       foreign_keys="LeaveRequest.staff_id")

    @property
    def display_name(self) -> str:
        return " ".join(p for p in (self.title, self.first_name, self.last_name) if p)


class StaffRecordKind(str, enum.Enum):
    CONTRACT = "contract"
    PROMOTION = "promotion"
    TRANSFER = "transfer"
    PERFORMANCE = "performance"
    TRAINING = "training"
    DISCIPLINARY = "disciplinary"
    DOCUMENT = "document"
    OTHER = "other"


class StaffRecord(PublicIdMixin, TenantScopedMixin, db.Model):
    """Employment history: contracts, promotions, transfers, appraisals, documents."""

    __tablename__ = "staff_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff_members.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[StaffRecordKind] = mapped_column(_enum(StaffRecordKind, 15), nullable=False)
    record_date: Mapped[date] = mapped_column(Date, nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    details: Mapped[str | None] = mapped_column(Text)
    rating: Mapped[int | None] = mapped_column(Integer)          # 1–5 for performance reviews
    file_key: Mapped[str | None] = mapped_column(String(255))
    file_name: Mapped[str | None] = mapped_column(String(255))
    recorded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    staff: Mapped[StaffMember] = relationship(back_populates="records")


class LeaveType(str, enum.Enum):
    ANNUAL = "annual"
    SICK = "sick"
    MATERNITY = "maternity"
    PATERNITY = "paternity"
    COMPASSIONATE = "compassionate"
    STUDY = "study"
    UNPAID = "unpaid"


class LeaveStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class LeaveRequest(PublicIdMixin, TenantScopedMixin, db.Model):
    __tablename__ = "leave_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff_members.id", ondelete="CASCADE"), nullable=False, index=True)
    leave_type: Mapped[LeaveType] = mapped_column(_enum(LeaveType, 15), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    days: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[LeaveStatus] = mapped_column(_enum(LeaveStatus, 12), default=LeaveStatus.PENDING, nullable=False)
    requested_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    staff: Mapped[StaffMember] = relationship(back_populates="leave", foreign_keys=[staff_id])
