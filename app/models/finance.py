"""Church finance and double-entry accounting.

Every money movement is a balanced journal entry. Posted entries are never
edited or deleted: corrections are made with reversing entries, so the books
always show what happened and when.

Amounts are stored as fixed-point decimals (2 places) in the church's currency.
"""
from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, Enum, ForeignKey, Index, Integer, Numeric, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.base import PublicIdMixin, TenantScopedMixin, TimestampMixin, utcnow

MONEY = Numeric(14, 2)
ZERO = Decimal("0.00")


def _enum(e, length):
    return Enum(e, native_enum=False, length=length, values_callable=lambda x: [m.value for m in x])


# =========================================================================== #
# Chart of accounts and journals
# =========================================================================== #
class AccountType(str, enum.Enum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"          # funds / net assets
    INCOME = "income"
    EXPENSE = "expense"

    @property
    def debit_normal(self) -> bool:
        return self in (AccountType.ASSET, AccountType.EXPENSE)


class AccountSubtype(str, enum.Enum):
    CASH = "cash"
    BANK = "bank"
    MOBILE_MONEY = "mobile_money"
    RECEIVABLE = "receivable"
    PAYABLE = "payable"
    FUND = "fund"
    GENERAL = "general"

    @property
    def is_money(self) -> bool:
        return self in (AccountSubtype.CASH, AccountSubtype.BANK, AccountSubtype.MOBILE_MONEY)


class Account(PublicIdMixin, TimestampMixin, TenantScopedMixin, db.Model):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_accounts_tenant_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    type: Mapped[AccountType] = mapped_column(_enum(AccountType, 12), nullable=False, index=True)
    subtype: Mapped[AccountSubtype] = mapped_column(_enum(AccountSubtype, 15), default=AccountSubtype.GENERAL, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    bank_name: Mapped[str | None] = mapped_column(String(120))
    account_number: Mapped[str | None] = mapped_column(String(40))
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id", ondelete="SET NULL"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)   # required by ChurchPlus

    branch = relationship("Branch")

    @property
    def label(self) -> str:
        return f"{self.code} · {self.name}"


class EntryStatus(str, enum.Enum):
    POSTED = "posted"
    VOID = "void"          # reversed; the reversing entry is linked


class EntrySource(str, enum.Enum):
    MANUAL = "manual"
    OPENING = "opening"
    CONTRIBUTION = "contribution"
    EXPENSE = "expense"
    PAYMENT = "payment"
    TRANSFER = "transfer"
    REVERSAL = "reversal"


class JournalEntry(PublicIdMixin, TenantScopedMixin, db.Model):
    __tablename__ = "journal_entries"
    __table_args__ = (
        UniqueConstraint("tenant_id", "number", name="uq_journal_entries_tenant_number"),
        Index("ix_journal_entries_tenant_date", "tenant_id", "entry_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    number: Mapped[str] = mapped_column(String(20), nullable=False)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(80))
    source: Mapped[EntrySource] = mapped_column(_enum(EntrySource, 15), default=EntrySource.MANUAL, nullable=False)
    status: Mapped[EntryStatus] = mapped_column(_enum(EntryStatus, 10), default=EntryStatus.POSTED, nullable=False)
    reversal_of_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id", ondelete="SET NULL"))
    reversed_by_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id", ondelete="SET NULL"))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    lines: Mapped[list["JournalLine"]] = relationship(back_populates="entry", cascade="all, delete-orphan",
                                                      order_by="JournalLine.id")

    @property
    def total(self) -> Decimal:
        return sum((l.debit for l in self.lines), ZERO)


class JournalLine(TenantScopedMixin, db.Model):
    __tablename__ = "journal_lines"
    __table_args__ = (
        CheckConstraint("debit >= 0 AND credit >= 0", name="non_negative"),
        CheckConstraint("(debit = 0) OR (credit = 0)", name="one_side"),
        Index("ix_journal_lines_account", "tenant_id", "account_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("journal_entries.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False)
    debit: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=ZERO)
    credit: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=ZERO)
    memo: Mapped[str | None] = mapped_column(String(255))
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id", ondelete="SET NULL"), index=True)
    reconciliation_id: Mapped[int | None] = mapped_column(ForeignKey("bank_reconciliations.id", ondelete="SET NULL"), index=True)

    entry: Mapped[JournalEntry] = relationship(back_populates="lines")
    account: Mapped[Account] = relationship()
    branch = relationship("Branch")


# =========================================================================== #
# Giving
# =========================================================================== #
class PaymentMethod(str, enum.Enum):
    CASH = "cash"
    MOBILE_MONEY = "mobile_money"
    BANK_TRANSFER = "bank_transfer"
    CHEQUE = "cheque"
    CARD = "card"
    ONLINE = "online"


class Campaign(PublicIdMixin, TimestampMixin, TenantScopedMixin, db.Model):
    """A fundraising drive or project (building fund, harvest, welfare drive…)."""

    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    income_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False)
    target_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    income_account: Mapped[Account] = relationship()


class PledgeStatus(str, enum.Enum):
    OPEN = "open"
    FULFILLED = "fulfilled"
    CANCELLED = "cancelled"


class Pledge(PublicIdMixin, TimestampMixin, TenantScopedMixin, db.Model):
    """A promise to give. Never income: only payments recorded against it are."""

    __tablename__ = "pledges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id", ondelete="SET NULL"), index=True)
    pledger_name: Mapped[str | None] = mapped_column(String(160))
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id", ondelete="SET NULL"), index=True)
    income_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    pledge_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[PledgeStatus] = mapped_column(_enum(PledgeStatus, 12), default=PledgeStatus.OPEN, nullable=False)
    notes: Mapped[str | None] = mapped_column(String(255))

    member = relationship("Member")
    campaign: Mapped[Campaign | None] = relationship()
    income_account: Mapped[Account] = relationship()
    payments: Mapped[list["Contribution"]] = relationship(back_populates="pledge")

    @property
    def name(self) -> str:
        return self.member.display_name if self.member else (self.pledger_name or "Anonymous")


class ContributionStatus(str, enum.Enum):
    POSTED = "posted"
    VOID = "void"


class Contribution(PublicIdMixin, TenantScopedMixin, db.Model):
    """Money received: tithe, offering, donation, welfare, project or pledge payment. Each has a receipt."""

    __tablename__ = "contributions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "receipt_number", name="uq_contributions_tenant_receipt"),
        Index("ix_contributions_tenant_date", "tenant_id", "received_on"),
        CheckConstraint("amount > 0", name="positive_amount"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    receipt_number: Mapped[str] = mapped_column(String(20), nullable=False)
    received_on: Mapped[date] = mapped_column(Date, nullable=False)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id", ondelete="SET NULL"), index=True)
    giver_name: Mapped[str | None] = mapped_column(String(160))           # non-member or loose offering label
    income_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False)
    deposit_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    method: Mapped[PaymentMethod] = mapped_column(_enum(PaymentMethod, 15), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(80))           # MoMo transaction ID, cheque number…
    original_currency: Mapped[str | None] = mapped_column(String(3))
    original_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    pledge_id: Mapped[int | None] = mapped_column(ForeignKey("pledges.id", ondelete="SET NULL"), index=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id", ondelete="SET NULL"), index=True)
    service_id: Mapped[int | None] = mapped_column(ForeignKey("services.id", ondelete="SET NULL"), index=True)
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id", ondelete="SET NULL"), index=True)
    notes: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[ContributionStatus] = mapped_column(_enum(ContributionStatus, 10), default=ContributionStatus.POSTED, nullable=False)
    void_reason: Mapped[str | None] = mapped_column(String(255))
    journal_entry_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id", ondelete="SET NULL"))
    recorded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    member = relationship("Member")
    income_account: Mapped[Account] = relationship(foreign_keys=[income_account_id])
    deposit_account: Mapped[Account] = relationship(foreign_keys=[deposit_account_id])
    pledge: Mapped[Pledge | None] = relationship(back_populates="payments")
    campaign: Mapped[Campaign | None] = relationship()
    service = relationship("Service")
    branch = relationship("Branch")
    journal_entry: Mapped[JournalEntry | None] = relationship()

    @property
    def giver(self) -> str:
        return self.member.display_name if self.member else (self.giver_name or "Anonymous")


# =========================================================================== #
# Spending
# =========================================================================== #
class ExpenseStatus(str, enum.Enum):
    SUBMITTED = "submitted"     # awaiting approval
    APPROVED = "approved"       # owed (accounts payable)
    PAID = "paid"
    REJECTED = "rejected"
    VOID = "void"


class Expense(PublicIdMixin, TimestampMixin, TenantScopedMixin, db.Model):
    """A payment voucher. Approval posts it to accounts payable; payment clears it from a bank, cash or MoMo account."""

    __tablename__ = "expenses"
    __table_args__ = (
        UniqueConstraint("tenant_id", "voucher_number", name="uq_expenses_tenant_voucher"),
        CheckConstraint("amount > 0", name="positive_amount"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    voucher_number: Mapped[str] = mapped_column(String(20), nullable=False)
    expense_date: Mapped[date] = mapped_column(Date, nullable=False)
    payee: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    expense_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id", ondelete="SET NULL"), index=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"), index=True)
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id", ondelete="SET NULL"), index=True)
    status: Mapped[ExpenseStatus] = mapped_column(_enum(ExpenseStatus, 12), default=ExpenseStatus.SUBMITTED, nullable=False, index=True)
    attachment_key: Mapped[str | None] = mapped_column(String(255))
    attachment_name: Mapped[str | None] = mapped_column(String(255))
    requested_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(String(255))
    paid_on: Mapped[date | None] = mapped_column(Date)
    payment_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    payment_method: Mapped[PaymentMethod | None] = mapped_column(_enum(PaymentMethod, 15))
    payment_reference: Mapped[str | None] = mapped_column(String(80))
    accrual_entry_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id", ondelete="SET NULL"))
    payment_entry_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id", ondelete="SET NULL"))

    expense_account: Mapped[Account] = relationship(foreign_keys=[expense_account_id])
    payment_account: Mapped[Account | None] = relationship(foreign_keys=[payment_account_id])
    department = relationship("Department")
    event = relationship("Event")
    branch = relationship("Branch")


class Budget(PublicIdMixin, TimestampMixin, TenantScopedMixin, db.Model):
    __tablename__ = "budgets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    lines: Mapped[list["BudgetLine"]] = relationship(back_populates="budget", cascade="all, delete-orphan")


class BudgetLine(TenantScopedMixin, db.Model):
    __tablename__ = "budget_lines"
    __table_args__ = (UniqueConstraint("budget_id", "account_id", "department_id", name="uq_budget_line"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    budget_id: Mapped[int] = mapped_column(ForeignKey("budgets.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False)
    department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id", ondelete="SET NULL"))
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=ZERO)

    budget: Mapped[Budget] = relationship(back_populates="lines")
    account: Mapped[Account] = relationship()
    department = relationship("Department")


class ReconciliationStatus(str, enum.Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class BankReconciliation(PublicIdMixin, TenantScopedMixin, db.Model):
    __tablename__ = "bank_reconciliations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True)
    statement_date: Mapped[date] = mapped_column(Date, nullable=False)
    statement_balance: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    status: Mapped[ReconciliationStatus] = mapped_column(_enum(ReconciliationStatus, 12), default=ReconciliationStatus.IN_PROGRESS, nullable=False)
    completed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    account: Mapped[Account] = relationship()
