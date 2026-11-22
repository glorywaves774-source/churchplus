"""SaaS plans and church subscriptions.

These are ChurchPlus's own billing records and are kept fully separate from
church finance (tithes, offerings, accounting).
"""
from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import PublicIdMixin, TenantScopedMixin, TimestampMixin


class SubscriptionStatus(str, enum.Enum):
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"      # payment failed, inside grace period
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"


class BillingCycle(str, enum.Enum):
    MONTHLY = "monthly"
    ANNUAL = "annual"


def _enum(e, length):
    return Enum(e, native_enum=False, length=length, values_callable=lambda x: [m.value for m in x])


class Plan(PublicIdMixin, TimestampMixin, db.Model):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)  # starter, growth...
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    currency: Mapped[str] = mapped_column(String(3), default="GHS", nullable=False)
    monthly_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))  # None = custom pricing
    annual_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    max_users: Mapped[int | None] = mapped_column(Integer)      # None = unlimited
    max_members: Mapped[int | None] = mapped_column(Integer)
    max_branches: Mapped[int | None] = mapped_column(Integer)
    max_storage_mb: Mapped[int | None] = mapped_column(Integer)
    modules: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    trial_days: Mapped[int] = mapped_column(Integer, default=14, nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    def __repr__(self) -> str:
        return f"<Plan {self.code}>"


class Subscription(PublicIdMixin, TimestampMixin, TenantScopedMixin, db.Model):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False, index=True)
    status: Mapped[SubscriptionStatus] = mapped_column(
        _enum(SubscriptionStatus, 20), default=SubscriptionStatus.TRIALING, nullable=False, index=True
    )
    billing_cycle: Mapped[BillingCycle] = mapped_column(
        _enum(BillingCycle, 10), default=BillingCycle.MONTHLY, nullable=False
    )
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    grace_period_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    plan: Mapped[Plan] = relationship()
