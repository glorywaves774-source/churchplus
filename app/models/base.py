"""Shared model mixins."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, declared_attr, mapped_column, relationship

from app.utils.tenancy import TenantOwned


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_public_id() -> str:
    return str(uuid.uuid4())


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class PublicIdMixin:
    """Non-guessable identifier for URLs and APIs so integer IDs are never exposed."""

    public_id: Mapped[str] = mapped_column(
        String(36), default=new_public_id, unique=True, nullable=False, index=True
    )


class TenantScopedMixin(TenantOwned):
    """Every row belongs to exactly one church (tenant). Automatically isolated."""

    __tenant_required__ = True

    @declared_attr
    def tenant_id(cls) -> Mapped[int]:
        return mapped_column(
            Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
        )

    @declared_attr
    def tenant(cls):
        return relationship("Tenant", lazy="select")


class OptionalTenantMixin(TenantOwned):
    """Rows that are either church-owned or platform-level (tenant_id NULL).

    Inside a church context only that church's rows are visible; platform rows
    are visible only to platform code running under ``tenant_bypass()``.
    """

    __tenant_required__ = False

    @declared_attr
    def tenant_id(cls) -> Mapped[int | None]:
        return mapped_column(
            Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
        )

    @declared_attr
    def tenant(cls):
        return relationship("Tenant", lazy="select")
