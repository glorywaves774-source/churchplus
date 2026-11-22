"""Membership: members, families, documents, status history and transfers."""
from __future__ import annotations

import enum
import secrets
from datetime import date, datetime

from sqlalchemy import (
    Boolean, Date, DateTime, Enum, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import PublicIdMixin, TenantScopedMixin, TimestampMixin, utcnow


def _enum(e, length):
    return Enum(e, native_enum=False, length=length, values_callable=lambda x: [m.value for m in x])


class Gender(str, enum.Enum):
    MALE = "male"
    FEMALE = "female"


class MaritalStatus(str, enum.Enum):
    SINGLE = "single"
    MARRIED = "married"
    WIDOWED = "widowed"
    DIVORCED = "divorced"
    SEPARATED = "separated"


class MemberStatus(str, enum.Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    TRANSFERRED = "transferred"   # moved to another church
    DECEASED = "deceased"


class FamilyRole(str, enum.Enum):
    HEAD = "head"
    SPOUSE = "spouse"
    CHILD = "child"
    DEPENDANT = "dependant"
    OTHER = "other"


class TransferKind(str, enum.Enum):
    BRANCH = "branch"        # between branches of this church
    OUT = "out"              # to another church
    IN = "in"                # received from another church


def new_qr_token() -> str:
    return secrets.token_urlsafe(18)


class NumberSequence(TenantScopedMixin, db.Model):
    """Per-church counters (membership numbers, receipts, …) that never repeat."""

    __tablename__ = "number_sequences"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_number_sequences_tenant_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    next_value: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class Family(PublicIdMixin, TimestampMixin, TenantScopedMixin, db.Model):
    __tablename__ = "families"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)        # e.g. "The Mensah Family"
    phone: Mapped[str | None] = mapped_column(String(40))
    address: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)

    members: Mapped[list["Member"]] = relationship(back_populates="family", order_by="Member.family_role")


class Member(PublicIdMixin, TimestampMixin, TenantScopedMixin, db.Model):
    __tablename__ = "members"
    __table_args__ = (
        UniqueConstraint("tenant_id", "membership_number", name="uq_members_tenant_number"),
        UniqueConstraint("qr_token", name="uq_members_qr_token"),
        Index("ix_members_tenant_name", "tenant_id", "last_name", "first_name"),
        Index("ix_members_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    membership_number: Mapped[str] = mapped_column(String(30), nullable=False)
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id", ondelete="SET NULL"), index=True)

    # Personal
    title: Mapped[str | None] = mapped_column(String(30))
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    middle_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    gender: Mapped[Gender | None] = mapped_column(_enum(Gender, 10))
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    marital_status: Mapped[MaritalStatus | None] = mapped_column(_enum(MaritalStatus, 12))
    occupation: Mapped[str | None] = mapped_column(String(120))
    employer: Mapped[str | None] = mapped_column(String(160))
    nationality: Mapped[str | None] = mapped_column(String(80))
    hometown: Mapped[str | None] = mapped_column(String(120))
    photo_key: Mapped[str | None] = mapped_column(String(255))

    # Contact
    phone: Mapped[str | None] = mapped_column(String(40), index=True)
    alt_phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(255), index=True)
    address: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(120))
    region: Mapped[str | None] = mapped_column(String(120))
    digital_address: Mapped[str | None] = mapped_column(String(30))

    # Emergency contact
    emergency_name: Mapped[str | None] = mapped_column(String(160))
    emergency_phone: Mapped[str | None] = mapped_column(String(40))
    emergency_relation: Mapped[str | None] = mapped_column(String(60))

    # Church life
    status: Mapped[MemberStatus] = mapped_column(_enum(MemberStatus, 15), default=MemberStatus.ACTIVE, nullable=False)
    date_joined: Mapped[date | None] = mapped_column(Date)
    previous_church: Mapped[str | None] = mapped_column(String(200))
    is_baptised: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    baptism_date: Mapped[date | None] = mapped_column(Date)
    baptism_place: Mapped[str | None] = mapped_column(String(160))
    baptised_by: Mapped[str | None] = mapped_column(String(160))
    notes: Mapped[str | None] = mapped_column(Text)
    allow_birthday_messages: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Communication preferences (Stage 8). WhatsApp needs an explicit opt-in (Meta policy).
    sms_opt_out: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    email_opt_out: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    whatsapp_opt_in: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Family and identification
    family_id: Mapped[int | None] = mapped_column(ForeignKey("families.id", ondelete="SET NULL"), index=True)
    family_role: Mapped[FamilyRole | None] = mapped_column(_enum(FamilyRole, 12))
    qr_token: Mapped[str] = mapped_column(String(40), nullable=False, default=new_qr_token)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    branch = relationship("Branch")
    family: Mapped[Family | None] = relationship(back_populates="members")
    documents: Mapped[list["MemberDocument"]] = relationship(back_populates="member", cascade="all, delete-orphan",
                                                             order_by="MemberDocument.created_at.desc()")
    history: Mapped[list["MemberStatusChange"]] = relationship(back_populates="member", cascade="all, delete-orphan",
                                                               order_by="MemberStatusChange.created_at.desc()")
    transfers: Mapped[list["MemberTransfer"]] = relationship(back_populates="member", cascade="all, delete-orphan",
                                                             order_by="MemberTransfer.created_at.desc()")

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.title, self.first_name, self.middle_name, self.last_name) if p)

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @property
    def age(self) -> int | None:
        if not self.date_of_birth:
            return None
        today = date.today()
        dob = self.date_of_birth
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))

    def __repr__(self) -> str:
        return f"<Member {self.membership_number}>"


class MemberDocument(PublicIdMixin, TenantScopedMixin, db.Model):
    __tablename__ = "member_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False, default="other")
    file_key: Mapped[str] = mapped_column(String(255), nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    member: Mapped[Member] = relationship(back_populates="documents")


class MemberStatusChange(TenantScopedMixin, db.Model):
    __tablename__ = "member_status_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), nullable=False, index=True)
    from_status: Mapped[str | None] = mapped_column(String(15))
    to_status: Mapped[str] = mapped_column(String(15), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255))
    changed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    member: Mapped[Member] = relationship(back_populates="history")


class MemberTransfer(PublicIdMixin, TenantScopedMixin, db.Model):
    __tablename__ = "member_transfers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[TransferKind] = mapped_column(_enum(TransferKind, 10), nullable=False)
    from_branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id", ondelete="SET NULL"))
    to_branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id", ondelete="SET NULL"))
    other_church: Mapped[str | None] = mapped_column(String(200))
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255))
    recorded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    member: Mapped[Member] = relationship(back_populates="transfers")
    from_branch = relationship("Branch", foreign_keys=[from_branch_id])
    to_branch = relationship("Branch", foreign_keys=[to_branch_id])
