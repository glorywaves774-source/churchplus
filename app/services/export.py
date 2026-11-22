"""Whole-church data export (a ZIP of CSV files) for portability, audits and backups.

Kept out on purpose: counselling cases and notes, safeguarding concerns, confidential prayer
requests and children's medical notes. Those stay inside ChurchPlus where access is
controlled and logged; ask ChurchPlus support if a lawful request needs them.
"""
from __future__ import annotations

import csv
import io
import zipfile
from datetime import date, datetime
from decimal import Decimal

from app.extensions import db
from app.models import (
    Account, AttendanceRecord, Branch, Contribution, Department, DepartmentMembership, Event, EventRegistration,
    Expense, Family, JournalEntry, JournalLine, Member, Pledge, PrayerRequest, Service, Sermon, Visitor,
)
from app.services.members import safe_cell


def _val(v):
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    if hasattr(v, "value"):
        return v.value
    return safe_cell(v) if isinstance(v, str) else ("" if v is None else v)


def _table(model, columns: list[str], query=None) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    for row in (query if query is not None else db.session.query(model).order_by(model.id)):
        w.writerow([_val(getattr(row, c, None)) for c in columns])
    return buf.getvalue()


TABLES = [
    ("branches.csv", Branch, ["id", "name", "code", "city", "is_headquarters", "is_active"]),
    ("families.csv", Family, ["id", "name"]),
    ("members.csv", Member, ["id", "membership_number", "title", "first_name", "middle_name", "last_name", "gender",
                             "date_of_birth", "marital_status", "phone", "alt_phone", "email", "address", "city", "region",
                             "digital_address", "occupation", "status", "date_joined", "is_baptised", "baptism_date",
                             "branch_id", "family_id", "family_role", "sms_opt_out", "email_opt_out", "whatsapp_opt_in"]),
    ("visitors.csv", Visitor, ["id", "first_name", "last_name", "phone", "email", "first_visit_date", "status", "how_heard",
                               "converted_member_id"]),
    ("departments.csv", Department, ["id", "name", "kind", "is_active"]),
    ("department_members.csv", DepartmentMembership, ["id", "department_id", "member_id", "role", "joined_on"]),
    ("services.csv", Service, ["id", "service_date", "title", "kind", "preacher", "theme", "headcount_men", "headcount_women",
                               "headcount_children", "headcount_visitors", "branch_id"]),
    ("attendance.csv", AttendanceRecord, ["id", "service_id", "member_id", "visitor_id", "method"]),
    ("events.csv", Event, ["id", "title", "category", "starts_at", "ends_at", "venue", "status", "capacity", "budget_amount"]),
    ("event_registrations.csv", EventRegistration, ["id", "event_id", "member_id", "name", "phone", "guests", "status", "attended"]),
    ("sermons.csv", Sermon, ["id", "preached_on", "title", "preacher", "series", "scripture", "video_url", "audio_url", "visibility"]),
    ("accounts.csv", Account, ["id", "code", "name", "type", "is_active"]),
    ("journal_entries.csv", JournalEntry, ["id", "number", "entry_date", "description", "status", "source"]),
    ("journal_lines.csv", JournalLine, ["id", "entry_id", "account_id", "debit", "credit", "description", "branch_id"]),
    ("contributions.csv", Contribution, ["id", "receipt_number", "received_on", "member_id", "giver_name", "income_account_id",
                                         "amount", "method", "reference", "status", "branch_id"]),
    ("pledges.csv", Pledge, ["id", "member_id", "pledger_name", "campaign_id", "amount", "pledge_date", "due_date", "status"]),
    ("expenses.csv", Expense, ["id", "voucher_number", "expense_date", "payee", "description", "amount", "status",
                               "expense_account_id", "branch_id", "event_id"]),
]


def church_zip(tenant) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, model, cols in TABLES:
            cols = [c for c in cols if hasattr(model, c)]
            z.writestr(name, _table(model, cols))
        z.writestr("prayer_requests.csv", _table(PrayerRequest, ["id", "created_at", "category", "status", "source", "member_id", "name"],
                                                 db.session.query(PrayerRequest).filter(PrayerRequest.is_confidential.is_(False))
                                                 .order_by(PrayerRequest.id)))
        z.writestr("README.txt", (
            f"{tenant.name} – ChurchPlus data export, {datetime.now():%d %B %Y %H:%M}\n\n"
            "Each CSV file is one kind of record. 'id' columns link files together (for example\n"
            "contributions.csv member_id = members.csv id).\n\n"
            "Not included, for privacy: counselling cases and notes, safeguarding concerns, confidential\n"
            "prayer requests (and the text of all prayer requests), and children's medical notes.\n"
            "Keep this file safe: it contains personal data protected by the Data Protection Act, 2012 (Act 843).\n"))
    return buf.getvalue()
