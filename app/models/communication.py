"""Stage 8: announcements and outgoing messages (SMS, WhatsApp, email)."""
from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import PublicIdMixin, TenantScopedMixin, TimestampMixin, utcnow


def _enum(e, length):
    return Enum(e, native_enum=False, length=length, values_callable=lambda x: [m.value for m in x])


class Audience(str, enum.Enum):
    STAFF = "staff"          # church workspace only
    MEMBERS = "members"      # also the member portal
    PUBLIC = "public"        # also the church website


class Announcement(PublicIdMixin, TimestampMixin, TenantScopedMixin, db.Model):
    __tablename__ = "announcements"
    __table_args__ = (Index("ix_announcements_tenant_dates", "tenant_id", "starts_on"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    audience: Mapped[Audience] = mapped_column(_enum(Audience, 10), default=Audience.MEMBERS, nullable=False)
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[date | None] = mapped_column(Date)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    def is_current(self, today: date) -> bool:
        return self.starts_on <= today and (self.ends_on is None or self.ends_on >= today)


class Channel(str, enum.Enum):
    SMS = "sms"
    WHATSAPP = "whatsapp"
    EMAIL = "email"


class MessageStatus(str, enum.Enum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"      # waiting for its time (or for the background sender)
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MessageKind(str, enum.Enum):
    MANUAL = "manual"
    BIRTHDAY = "birthday"
    EVENT_REMINDER = "event_reminder"
    APPOINTMENT_REMINDER = "appointment_reminder"


class Message(PublicIdMixin, TimestampMixin, TenantScopedMixin, db.Model):
    """One outgoing message to many people. Each person is a :class:`MessageRecipient`."""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "dedupe_key", name="uq_messages_tenant_dedupe"),
        Index("ix_messages_status_scheduled", "status", "scheduled_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[Channel] = mapped_column(_enum(Channel, 10), nullable=False)
    kind: Mapped[MessageKind] = mapped_column(_enum(MessageKind, 24), default=MessageKind.MANUAL, nullable=False)
    subject: Mapped[str | None] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    audience: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)   # how recipients were chosen
    audience_label: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[MessageStatus] = mapped_column(_enum(MessageStatus, 12), default=MessageStatus.DRAFT, nullable=False)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recipients_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sent_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    segments: Mapped[int] = mapped_column(Integer, default=0, nullable=False)       # SMS parts per recipient
    dedupe_key: Mapped[str | None] = mapped_column(String(80))                      # automatic messages only
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    recipients: Mapped[list["MessageRecipient"]] = relationship(back_populates="message", cascade="all, delete-orphan",
                                                                order_by="MessageRecipient.id")


class RecipientStatus(str, enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class MessageRecipient(TenantScopedMixin, db.Model):
    __tablename__ = "message_recipients"
    __table_args__ = (Index("ix_message_recipients_message_status", "message_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), nullable=False)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id", ondelete="SET NULL"))
    name: Mapped[str | None] = mapped_column(String(160))
    address: Mapped[str] = mapped_column(String(255), nullable=False)       # phone (international) or email
    status: Mapped[RecipientStatus] = mapped_column(_enum(RecipientStatus, 10), default=RecipientStatus.PENDING, nullable=False)
    provider_ref: Mapped[str | None] = mapped_column(String(120))
    error: Mapped[str | None] = mapped_column(String(255))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    context: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)   # personalisation values

    message: Mapped[Message] = relationship(back_populates="recipients")
    member = relationship("Member")
