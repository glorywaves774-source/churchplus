"""Stage 10: ChurchPlus's own billing – invoices and payments from churches.

Invoices belong to a church (tenant-scoped) so church owners see only their own; the
platform console reads them across churches with ``tenant_bypass()``. No card data is ever
stored: payment providers (Paystack, Hubtel) handle cards and mobile money and send us a
reference that we verify with them.
"""
from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Integer, JSON, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import PublicIdMixin, TenantScopedMixin, TimestampMixin


def _enum(e, length):
    return Enum(e, native_enum=False, length=length, values_callable=lambda x: [m.value for m in x])


class InvoiceStatus(str, enum.Enum):
    OPEN = "open"
    PAID = "paid"
    VOID = "void"


class Invoice(PublicIdMixin, TimestampMixin, TenantScopedMixin, db.Model):
    __tablename__ = "invoices"
    __table_args__ = (UniqueConstraint("number", name="uq_invoices_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    number: Mapped[str | None] = mapped_column(String(30))
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id", ondelete="SET NULL"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False)
    billing_cycle: Mapped[str] = mapped_column(String(10), nullable=False, default="monthly")
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="GHS")
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    due_on: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[InvoiceStatus] = mapped_column(_enum(InvoiceStatus, 10), default=InvoiceStatus.OPEN, nullable=False, index=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(String(255))

    plan = relationship("Plan")
    payments: Mapped[list["BillingPayment"]] = relationship(back_populates="invoice", order_by="BillingPayment.id")


class PaymentStatus(str, enum.Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"


class BillingPayment(PublicIdMixin, TimestampMixin, TenantScopedMixin, db.Model):
    __tablename__ = "billing_payments"
    __table_args__ = (UniqueConstraint("provider", "reference", name="uq_billing_payments_provider_ref"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)          # paystack, hubtel, manual, test
    reference: Mapped[str] = mapped_column(String(100), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="GHS")
    status: Mapped[PaymentStatus] = mapped_column(_enum(PaymentStatus, 10), default=PaymentStatus.PENDING, nullable=False)
    channel: Mapped[str | None] = mapped_column(String(30))                    # card, mobile_money, bank…
    details: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)  # non-sensitive provider info only
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    invoice: Mapped[Invoice] = relationship(back_populates="payments")
