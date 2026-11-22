"""Tenants (churches), denominations and branches."""
from __future__ import annotations

import enum

from sqlalchemy import JSON, Boolean, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import PublicIdMixin, TenantScopedMixin, TimestampMixin


class TenantStatus(str, enum.Enum):
    PENDING = "pending"        # registered, awaiting approval
    TRIAL = "trial"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"


class TenantKind(str, enum.Enum):
    INDEPENDENT = "independent"            # a single church (with or without branches)
    DENOMINATION_HQ = "denomination_hq"    # headquarters overseeing member churches
    DENOMINATION_MEMBER = "denomination_member"


class Denomination(PublicIdMixin, TimestampMixin, db.Model):
    """A group of churches. Platform-level record; the HQ church is a tenant."""

    __tablename__ = "denominations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    abbreviation: Mapped[str | None] = mapped_column(String(30))
    country: Mapped[str | None] = mapped_column(String(80))
    website: Mapped[str | None] = mapped_column(String(255))

    churches: Mapped[list["Tenant"]] = relationship(back_populates="denomination")

    def __repr__(self) -> str:
        return f"<Denomination {self.name}>"


class Tenant(PublicIdMixin, TimestampMixin, db.Model):
    """A subscribing church: its own secure workspace, branding, users and records."""

    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    legal_name: Mapped[str | None] = mapped_column(String(255))
    kind: Mapped[TenantKind] = mapped_column(
        Enum(TenantKind, native_enum=False, length=30, values_callable=lambda e: [m.value for m in e]),
        default=TenantKind.INDEPENDENT, nullable=False,
    )
    status: Mapped[TenantStatus] = mapped_column(
        Enum(TenantStatus, native_enum=False, length=20, values_callable=lambda e: [m.value for m in e]),
        default=TenantStatus.PENDING, nullable=False, index=True,
    )

    # Contact and address
    email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(40))
    website: Mapped[str | None] = mapped_column(String(255))
    address_line1: Mapped[str | None] = mapped_column(String(255))
    address_line2: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(120))
    region: Mapped[str | None] = mapped_column(String(120))
    country: Mapped[str] = mapped_column(String(2), default="GH", nullable=False)  # ISO 3166-1
    digital_address: Mapped[str | None] = mapped_column(String(30))  # Ghana Post GPS

    # Branding
    logo_key: Mapped[str | None] = mapped_column(String(255))  # private storage key
    primary_color: Mapped[str | None] = mapped_column(String(9))
    accent_color: Mapped[str | None] = mapped_column(String(9))
    tagline: Mapped[str | None] = mapped_column(String(255))

    # Localisation
    currency: Mapped[str] = mapped_column(String(3), default="GHS", nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Africa/Accra", nullable=False)

    settings: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    # Denomination structure
    denomination_id: Mapped[int | None] = mapped_column(
        ForeignKey("denominations.id", ondelete="SET NULL"), index=True
    )
    denomination: Mapped[Denomination | None] = relationship(back_populates="churches")
    parent_tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"), index=True
    )
    parent: Mapped["Tenant | None"] = relationship(remote_side="Tenant.id", backref="children")

    @property
    def is_operational(self) -> bool:
        return self.status in (TenantStatus.TRIAL, TenantStatus.ACTIVE)

    def __repr__(self) -> str:
        return f"<Tenant {self.slug}>"


class Branch(PublicIdMixin, TimestampMixin, TenantScopedMixin, db.Model):
    """A physical location or assembly of a church."""

    __tablename__ = "branches"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_branches_tenant_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    is_headquarters: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(40))
    address: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(120))
    region: Mapped[str | None] = mapped_column(String(120))
    country: Mapped[str | None] = mapped_column(String(2))

    def __repr__(self) -> str:
        return f"<Branch {self.code}>"
