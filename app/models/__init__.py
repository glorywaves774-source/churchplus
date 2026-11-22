"""All models are imported here so Alembic autogenerate sees every table.

Add each new model module to this file. Never define the same table twice.
"""
from app.models.audit import AuditLog
from app.models.member import (
    Family, FamilyRole, Gender, MaritalStatus, Member, MemberDocument, MemberStatus, MemberStatusChange,
    MemberTransfer, NumberSequence, TransferKind,
)
from app.models.operations import (
    AttendanceRecord, CheckInMethod, Department, DepartmentKind, DepartmentMembership, EmploymentType,
    FollowUpMethod, LeaveRequest, LeaveStatus, LeaveType, Service, ServiceAssignment, ServiceItem, ServiceKind,
    ServiceStatus, ServiceType, StaffCategory, StaffMember, StaffRecord, StaffRecordKind, StaffStatus, Visitor,
    VisitorFollowUp, VisitorStatus,
)
from app.models.finance import (
    Account, AccountSubtype, AccountType, BankReconciliation, Budget, BudgetLine, Campaign, Contribution,
    ContributionStatus, EntrySource, EntryStatus, Expense, ExpenseStatus, JournalEntry, JournalLine, PaymentMethod,
    Pledge, PledgeStatus, ReconciliationStatus,
)
from app.models.ministry import (
    CaseStatus, ChildCheckIn, ChildClass, ChildGuardian, ChildProfile, ClassTeacher, CounsellingCase,
    CounsellingSession, Event, EventCommitteeMember, EventRegistration, EventStatus, IncidentStatus, MediaItem,
    MediaKind, PrayerRequest, PrayerStatus, PrayerUpdate, RegistrationStatus, SafeguardingIncident, Sermon,
    SessionStatus, Visibility,
)
from app.models.billing import BillingPayment, Invoice, InvoiceStatus, PaymentStatus
from app.models.communication import (
    Announcement, Audience, Channel, Message, MessageKind, MessageRecipient, MessageStatus, RecipientStatus,
)
from app.models.base import OptionalTenantMixin, PublicIdMixin, TenantScopedMixin, TimestampMixin
from app.models.subscription import BillingCycle, Plan, Subscription, SubscriptionStatus
from app.models.tenant import Branch, Denomination, Tenant, TenantKind, TenantStatus
from app.models.user import (
    LoginEvent, Permission, PlatformRole, Role, TenantUser, User, role_permissions, tenant_user_roles,
)

__all__ = [
    "BillingPayment", "Invoice", "InvoiceStatus", "PaymentStatus", "Announcement", "Audience", "Channel", "Message",
    "MessageKind", "MessageRecipient", "MessageStatus", "RecipientStatus",
    "CaseStatus", "ChildCheckIn", "ChildClass", "ChildGuardian", "ChildProfile", "ClassTeacher", "CounsellingCase",
    "CounsellingSession", "Event", "EventCommitteeMember", "EventRegistration", "EventStatus", "IncidentStatus",
    "MediaItem", "MediaKind", "PrayerRequest", "PrayerStatus", "PrayerUpdate", "RegistrationStatus",
    "SafeguardingIncident", "Sermon", "SessionStatus", "Visibility",
    "Account", "AccountSubtype", "AccountType", "BankReconciliation", "Budget", "BudgetLine", "Campaign",
    "Contribution", "ContributionStatus", "EntrySource", "EntryStatus", "Expense", "ExpenseStatus", "JournalEntry",
    "JournalLine", "PaymentMethod", "Pledge", "PledgeStatus", "ReconciliationStatus",
    "AttendanceRecord", "CheckInMethod", "Department", "DepartmentKind", "DepartmentMembership", "EmploymentType",
    "FollowUpMethod", "LeaveRequest", "LeaveStatus", "LeaveType", "Service", "ServiceAssignment", "ServiceItem",
    "ServiceKind", "ServiceStatus", "ServiceType", "StaffCategory", "StaffMember", "StaffRecord", "StaffRecordKind",
    "StaffStatus", "Visitor", "VisitorFollowUp", "VisitorStatus",
    "Family", "FamilyRole", "Gender", "MaritalStatus", "Member", "MemberDocument", "MemberStatus",
    "MemberStatusChange", "MemberTransfer", "NumberSequence", "TransferKind",
    "AuditLog", "BillingCycle", "LoginEvent", "Branch", "Denomination", "OptionalTenantMixin", "Permission",
    "Plan", "PlatformRole", "PublicIdMixin", "Role", "Subscription", "SubscriptionStatus",
    "Tenant", "TenantKind", "TenantScopedMixin", "TenantStatus", "TenantUser", "TimestampMixin",
    "User", "role_permissions", "tenant_user_roles",
]
