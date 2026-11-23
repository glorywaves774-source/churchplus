"""Write audit trail entries."""
from __future__ import annotations

from flask import has_request_context, request

from app.extensions import db
from app.models import AuditLog
from app.utils.tenancy import current_tenant_id


def record(action: str, *, entity=None, entity_type: str | None = None, entity_id=None,
           actor=None, tenant_id: int | None = None, **details) -> AuditLog:
    """Add an audit entry to the current session (committed with the caller's transaction)."""
    if entity is not None:
        entity_type = entity_type or type(entity).__name__
        entity_id = entity_id or getattr(entity, "public_id", None) or getattr(entity, "id", None)
    entry = AuditLog(
        tenant_id=tenant_id if tenant_id is not None else current_tenant_id(),
        actor_user_id=getattr(actor, "id", None),
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        details=details or {},
    )
    if has_request_context():
        entry.ip_address = (request.headers.get("X-Forwarded-For", request.remote_addr) or "").split(",")[0].strip()[:45]
        entry.user_agent = (request.user_agent.string or "")[:255]
    db.session.add(entry)
    return entry


# Human-readable activity for dashboards and the audit trail.
LABELS = {
    "tenant.provisioned": ("Church workspace created", "sparkles"),
    "church.profile_updated": ("Church profile updated", "settings"),
    "church.logo_updated": ("Church logo updated", "image"),
    "church.logo_removed": ("Church logo removed", "image"),
    "branch.created": ("Branch added", "building-2"),
    "branch.updated": ("Branch updated", "building-2"),
    "branch.activated": ("Branch reactivated", "building-2"),
    "branch.deactivated": ("Branch deactivated", "building-2"),
    "branch.deleted": ("Branch deleted", "trash-2"),
    "denomination.member_church_added": ("Member church added", "church"),
    "user.invited": ("Team member invited", "user-plus"),
    "user.updated": ("Team member's access changed", "user-cog"),
    "user.suspended": ("Team member suspended", "user-x"),
    "user.reactivated": ("Team member reactivated", "user-check"),
    "user.removed": ("Team member removed", "user-minus"),
    "role.created": ("Role created", "shield"),
    "role.updated": ("Role permissions changed", "shield"),
    "role.deleted": ("Role deleted", "shield"),
    "member.created": ("Member registered", "user-plus"),
    "member.updated": ("Member details updated", "user-round"),
    "member.deleted": ("Member record deleted", "trash-2"),
    "member.status_changed": ("Member status changed", "user-cog"),
    "member.transferred_branch": ("Member moved to another branch", "repeat"),
    "member.photo_updated": ("Member photo updated", "camera"),
    "member.photo_removed": ("Member photo removed", "camera"),
    "member.document_added": ("Member document added", "file-text"),
    "member.document_downloaded": ("Member document downloaded", "download"),
    "member.document_deleted": ("Member document deleted", "file-text"),
    "member.family_set": ("Member linked to family", "users-round"),
    "member.family_removed": ("Member removed from family", "users-round"),
    "family.created": ("Family created", "users-round"),
    "member.card_renewed": ("Membership card reissued", "id-card"),
    "member.imported": ("Members imported", "upload"),
    "member.exported": ("Member list exported", "download"),
    "member.data_exported": ("Member's personal data exported", "download"),
    "service.created": ("Service scheduled", "calendar-plus"),
    "service.updated": ("Service updated", "church"),
    "service.deleted": ("Service deleted", "trash-2"),
    "service_type.created": ("Service type added", "church"),
    "attendance.register_saved": ("Attendance register saved", "clipboard-check"),
    "attendance.headcount_saved": ("Headcount recorded", "clipboard-list"),
    "visitor.created": ("First-timer registered", "user-plus"),
    "visitor.updated": ("Visitor details updated", "user-plus"),
    "visitor.deleted": ("Visitor deleted", "trash-2"),
    "visitor.follow_up_scheduled": ("Visitor follow-up scheduled", "calendar-plus"),
    "visitor.follow_up_done": ("Visitor followed up", "phone"),
    "visitor.converted": ("Visitor became a member", "user-check"),
    "department.created": ("Department created", "network"),
    "department.updated": ("Department updated", "network"),
    "department.deleted": ("Department deleted", "trash-2"),
    "department.member_added": ("Member added to department", "user-plus"),
    "department.member_removed": ("Member removed from department", "user-minus"),
    "staff.created": ("Clergy/staff record added", "briefcase"),
    "staff.updated": ("Clergy/staff record updated", "briefcase"),
    "staff.photo_updated": ("Clergy/staff photo updated", "camera"),
    "staff.photo_removed": ("Clergy/staff photo removed", "camera"),
    "staff.record_added": ("Employment history updated", "file-text"),
    "staff.document_downloaded": ("Staff document downloaded", "download"),
    "staff.leave_requested": ("Leave requested", "calendar-days"),
    "staff.leave_approved": ("Leave approved", "check-circle-2"),
    "staff.leave_rejected": ("Leave rejected", "x-circle"),
    "finance.contribution_recorded": ("Giving received", "hand-coins"),
    "finance.contribution_voided": ("Receipt voided", "x-circle"),
    "finance.giving_exported": ("Giving list exported", "download"),
    "finance.pledge_recorded": ("Pledge recorded", "handshake"),
    "finance.pledge_cancelled": ("Pledge cancelled", "handshake"),
    "finance.campaign_created": ("Campaign created", "target"),
    "finance.expense_submitted": ("Payment voucher submitted", "file-text"),
    "finance.expense_approved": ("Payment voucher approved", "check-circle-2"),
    "finance.expense_rejected": ("Payment voucher rejected", "x-circle"),
    "finance.expense_paid": ("Payment made", "banknote"),
    "finance.expense_voided": ("Payment voucher voided", "x-circle"),
    "finance.transfer": ("Money transferred", "repeat"),
    "finance.budget_created": ("Budget created", "piggy-bank"),
    "finance.budget_updated": ("Budget updated", "piggy-bank"),
    "finance.reconciliation_completed": ("Bank reconciliation completed", "check-circle-2"),
    "journal.posted": ("Journal entry posted", "book-open"),
    "journal.reversed": ("Journal entry reversed", "repeat"),
    "accounting.account_created": ("Account added to chart", "layers"),
    "accounting.account_updated": ("Account updated", "layers"),
    "event.created": ("Event created", "calendar-plus"),
    "event.updated": ("Event updated", "calendar-days"),
    "event.deleted": ("Event deleted", "trash-2"),
    "event.registration": ("Event registration", "user-plus"),
    "sermon.created": ("Sermon added", "book-open"),
    "sermon.updated": ("Sermon updated", "book-open"),
    "sermon.deleted": ("Sermon deleted", "trash-2"),
    "media.added": ("Media added", "upload"),
    "media.deleted": ("Media deleted", "trash-2"),
    "prayer.created": ("Prayer request received", "heart"),
    "prayer.updated": ("Prayer request updated", "heart"),
    "prayer.viewed": ("Confidential prayer request opened", "lock"),
    "prayer.deleted": ("Prayer request deleted", "trash-2"),
    "counselling.case_opened": ("Counselling case opened", "lock"),
    "counselling.case_viewed": ("Counselling case opened for reading", "lock"),
    "counselling.case_closed": ("Counselling case closed", "lock"),
    "counselling.case_reopened": ("Counselling case reopened", "lock"),
    "counselling.case_handed_over": ("Counselling case handed over", "lock"),
    "counselling.session_scheduled": ("Counselling appointment booked", "lock"),
    "counselling.session_updated": ("Counselling session updated", "lock"),
    "counselling.case_deleted": ("Counselling case deleted", "lock"),
    "child.profile_saved": ("Child profile saved", "shield-check"),
    "child.guardian_added": ("Guardian added", "user-plus"),
    "child.guardian_removed": ("Guardian removed", "user-minus"),
    "child.checked_in": ("Child checked in", "log-in"),
    "child.checked_out": ("Child checked out", "log-out"),
    "child.checkout_refused": ("Child pick-up refused: wrong code", "shield-alert"),
    "child_class.saved": ("Children's class saved", "school"),
    "child_class.teacher_added": ("Teacher added to class", "user-check"),
    "safeguarding.reported": ("Safeguarding concern recorded", "shield-alert"),
    "safeguarding.updated": ("Safeguarding concern updated", "shield-alert"),
    "message.created": ("Message prepared", "send"),
    "message.sent": ("Message sent", "send"),
    "message.cancelled": ("Message cancelled", "x-circle"),
    "announcement.saved": ("Announcement saved", "megaphone"),
    "announcement.deleted": ("Announcement deleted", "trash-2"),
    "communication.settings_updated": ("Communication settings updated", "settings"),
    "member.preferences_updated": ("Contact preferences updated", "user-check"),
    "member.self_updated": ("Member updated their details", "user-round"),
    "portal.invited": ("Invited to member portal", "send"),
    "portal.linked": ("Member portal linked", "link"),
    "portal.revoked": ("Member portal access removed", "user-x"),
    "website.updated": ("Church website updated", "monitor"),
    "report.exported": ("Report downloaded", "download"),
    "billing.invoice_created": ("ChurchPlus invoice created", "receipt"),
    "billing.invoice_paid": ("ChurchPlus invoice paid", "check-circle-2"),
    "billing.checkout_started": ("Online payment started", "credit-card"),
    "billing.plan_chosen": ("ChurchPlus plan chosen", "layers"),
    "billing.past_due": ("ChurchPlus payment due", "alert-triangle"),
    "billing.suspended": ("Workspace paused for non-payment", "lock"),
    "platform.approve": ("Church approved by ChurchPlus", "check-circle-2"),
    "platform.suspend": ("Church suspended by ChurchPlus", "lock"),
    "platform.reactivate": ("Church reactivated by ChurchPlus", "refresh-cw"),
    "platform.extend_trial": ("Trial extended by ChurchPlus", "clock"),
    "platform.change_plan": ("Plan changed by ChurchPlus", "layers"),
    "platform.invoice_created": ("Invoice created by ChurchPlus", "receipt"),
    "platform.invoice_voided": ("Invoice voided by ChurchPlus", "x-circle"),
    "platform.plan_updated": ("Plan prices updated", "layers"),
    "church.data_exported": ("Church data downloaded", "download"),
    "auth.login": ("Signed in", "log-in"),
    "auth.logout": ("Signed out", "log-out"),
}

CATEGORIES = {
    "church": ("Church settings", ("church.", "tenant.", "denomination.")),
    "branches": ("Branches", ("branch.",)),
    "team": ("Users and roles", ("user.", "role.")),
    "members": ("Membership", ("member.", "family.")),
    "operations": ("Services, attendance and visitors", ("service.", "service_type.", "attendance.", "visitor.")),
    "departments": ("Departments", ("department.",)),
    "staff": ("Clergy and staff", ("staff.",)),
    "finance": ("Finance and accounting", ("finance.", "journal.", "accounting.")),
    "events": ("Events", ("event.",)),
    "sermons": ("Sermons and media", ("sermon.", "media.")),
    "prayer": ("Prayer", ("prayer.",)),
    "counselling": ("Counselling", ("counselling.",)),
    "children": ("Children and safeguarding", ("child.", "child_class.", "safeguarding.")),
    "communication": ("Communication", ("message.", "announcement.", "communication.")),
    "portal": ("Member portal and website", ("portal.", "website.")),
    "billing": ("ChurchPlus subscription", ("billing.", "platform.")),
}

# Actions never shown in dashboard activity feeds (they reveal pastoral activity).
PRIVATE_PREFIXES = ("counselling.", "safeguarding.", "prayer.viewed")


def describe(entry: AuditLog) -> dict:
    label, icon = LABELS.get(entry.action, (entry.action.replace(".", " ").replace("_", " ").capitalize(), "activity"))
    detail = entry.details.get("name") or entry.details.get("church") or ""
    if entry.action == "member.imported":
        detail = f"{entry.details.get('count', 0)} members"
    return {"text": f"{label}{': ' + detail if detail else ''}", "icon": icon, "at": entry.created_at,
            "action": entry.action}
