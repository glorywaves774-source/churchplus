"""User identities, church memberships of users, roles and permissions.

Design
------
* ``User`` is a global login identity (one email, one password). A person can
  hold accounts in more than one church, e.g. a denomination officer.
* ``TenantUser`` links a user to a church and carries that church's roles.
* ``Role`` belongs to one church, so each church can edit its own roles and
  create custom ones. Default roles are copied in when a church is provisioned.
* ``Permission`` is the global catalogue (``module.action``).
* Platform staff (SaaS Super Administrator / Support) are flagged on ``User``
  and are *not* given church roles automatically, so they cannot see private
  counselling or confidential member records by default.
"""
from __future__ import annotations

import enum
from datetime import datetime

from flask_login import UserMixin
from sqlalchemy import (
    JSON, Boolean, Column, DateTime, Enum, ForeignKey, Index, Integer, String, Table, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.models.base import PublicIdMixin, TenantScopedMixin, TimestampMixin, utcnow


class PlatformRole(str, enum.Enum):
    SUPER_ADMIN = "super_admin"
    SUPPORT_ADMIN = "support_admin"


role_permissions = Table(
    "role_permissions",
    db.metadata,
    Column("role_id", ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    Column("permission_id", ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True),
)

tenant_user_roles = Table(
    "tenant_user_roles",
    db.metadata,
    Column("tenant_user_id", ForeignKey("tenant_users.id", ondelete="CASCADE"), primary_key=True),
    Column("role_id", ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
)


class User(UserMixin, PublicIdMixin, TimestampMixin, db.Model):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(40))
    is_active_account: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    two_factor_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Two-step sign-in key, encrypted at rest (see app/utils/crypto.py). Use ``totp_secret``.
    totp_secret_stored: Mapped[str | None] = mapped_column("totp_secret", String(255))
    recovery_codes: Mapped[list | None] = mapped_column(JSON)     # hashed, single-use
    session_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False, server_default="1")
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    preferred_theme: Mapped[str | None] = mapped_column(String(10))  # light | dark | None
    platform_role: Mapped[PlatformRole | None] = mapped_column(
        Enum(PlatformRole, native_enum=False, length=20, values_callable=lambda e: [m.value for m in e]),
        nullable=True,
    )

    memberships: Mapped[list["TenantUser"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    # --- Flask-Login -------------------------------------------------------
    def get_id(self) -> str:
        # Includes the session version, so ending sessions (password change, "sign out other
        # devices") invalidates every existing session and "keep me signed in" cookie.
        return f"{self.public_id}.{self.session_version or 1}"

    @property
    def is_active(self) -> bool:  # type: ignore[override]
        return self.is_active_account

    # --- Passwords --------------------------------------------------------
    def set_password(self, raw: str) -> None:
        if not raw or len(raw) < 10:
            raise ValueError("Password must be at least 10 characters.")
        from app.models.base import utcnow
        self.password_hash = generate_password_hash(raw, method="scrypt")
        self.password_changed_at = utcnow()

    def end_other_sessions(self) -> None:
        """Invalidate every signed-in session (the current one is renewed by the caller)."""
        self.session_version = (self.session_version or 1) + 1

    def check_password(self, raw: str) -> bool:
        return bool(self.password_hash) and check_password_hash(self.password_hash, raw)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def totp_secret(self) -> str | None:
        from app.utils.crypto import decrypt
        return decrypt(self.totp_secret_stored)

    @totp_secret.setter
    def totp_secret(self, value: str | None) -> None:
        from app.utils.crypto import encrypt
        self.totp_secret_stored = encrypt(value)

    @property
    def is_platform_staff(self) -> bool:
        return self.platform_role is not None

    def __repr__(self) -> str:
        return f"<User {self.email}>"


class Permission(db.Model):
    """Global permission catalogue, e.g. ``members.view`` or ``finance.approve``."""

    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    module: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    is_sensitive: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    def __repr__(self) -> str:
        return f"<Permission {self.code}>"


class Role(PublicIdMixin, TimestampMixin, TenantScopedMixin, db.Model):
    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("tenant_id", "slug", name="uq_roles_tenant_slug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Branch-scoped roles only see records of the account's own branch (used from Stage 4).
    is_branch_scoped: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="0")

    permissions: Mapped[list[Permission]] = relationship(secondary=role_permissions, lazy="selectin")

    def has_permission(self, code: str) -> bool:
        return any(p.code == code for p in self.permissions)

    def __repr__(self) -> str:
        return f"<Role {self.slug}>"


class TenantUser(PublicIdMixin, TimestampMixin, TenantScopedMixin, db.Model):
    """A user's account inside one church."""

    __tablename__ = "tenant_users"
    __table_args__ = (UniqueConstraint("tenant_id", "user_id", name="uq_tenant_users_tenant_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id", ondelete="SET NULL"), index=True)
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_suspended: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Member-portal link: the church member this sign-in belongs to (Stage 8).
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id", ondelete="SET NULL"), index=True)

    user: Mapped[User] = relationship(back_populates="memberships")
    member = relationship("Member", foreign_keys=[member_id])
    branch = relationship("Branch")
    roles: Mapped[list[Role]] = relationship(secondary=tenant_user_roles, lazy="selectin")

    @property
    def display_name(self) -> str:
        return self.user.full_name if self.user else "—"

    def permission_codes(self) -> set[str]:
        return {p.code for r in self.roles for p in r.permissions}

    def can(self, code: str) -> bool:
        if self.is_suspended:
            return False
        if code == "portal":            # pseudo-permission: has a linked member record
            return self.member_id is not None
        if code == "staff":             # pseudo-permission: holds any real church permission
            return bool(self.permission_codes())
        return code in self.permission_codes()

    @property
    def is_portal_only(self) -> bool:
        return self.member_id is not None and not self.permission_codes()


class LoginEvent(db.Model):
    """Sign-in history (platform level): successes, failures and sign-outs."""

    __tablename__ = "login_events"
    __table_args__ = (Index("ix_login_events_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    email: Mapped[str | None] = mapped_column(String(255))
    event: Mapped[str] = mapped_column(String(30), nullable=False)   # success, failed, two_factor_failed, logout, locked
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 default=utcnow)
