"""Membership business logic (kept out of the route handlers)."""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from flask import abort, g, url_for
from sqlalchemy import extract, func, or_

from app.extensions import db
from app.models import (
    Branch, FamilyRole, Gender, MaritalStatus, Member, MemberStatus, MemberStatusChange, MemberTransfer,
    NumberSequence, TransferKind,
)
from app.services import audit, limits, search
from app.utils.tenancy import require_tenant_id

# --------------------------------------------------------------------------- #
# Membership numbers
# --------------------------------------------------------------------------- #
def member_prefix(tenant) -> str:
    custom = (tenant.settings or {}).get("member_prefix")
    if custom:
        return custom
    words = [w for w in re.split(r"[^A-Za-z]+", tenant.name) if w and w.lower() not in {"the", "of", "and"}]
    return ("".join(w[0] for w in words[:4]) or "CP").upper()


def next_sequence(name: str) -> int:
    """Atomically take the next number of a per-church counter."""
    require_tenant_id()
    seq = (db.session.query(NumberSequence).filter_by(name=name).with_for_update().one_or_none())
    if seq is None:
        seq = NumberSequence(name=name, next_value=1)
        db.session.add(seq)
        db.session.flush()
        seq = db.session.query(NumberSequence).filter_by(name=name).with_for_update().one()
    value = seq.next_value
    seq.next_value = value + 1
    db.session.flush()
    return value


def new_membership_number(tenant) -> str:
    prefix = member_prefix(tenant)
    while True:
        number = f"{prefix}-{next_sequence('member'):06d}"
        if not db.session.query(Member.id).filter_by(membership_number=number).first():
            return number


# --------------------------------------------------------------------------- #
# Branch scoping
# --------------------------------------------------------------------------- #
def branch_limit_id() -> int | None:
    """Branch the signed-in person is limited to, or None for church-wide access."""
    account = getattr(g, "account", None)
    if account is None or account.is_owner or not account.roles:
        return None
    if all(r.is_branch_scoped for r in account.roles):
        return account.branch_id or -1        # branch-limited role with no branch sees nothing
    return None


def members_query():
    q = db.session.query(Member)
    limit = branch_limit_id()
    if limit is not None:
        q = q.filter(Member.branch_id == limit)
    return q


def get_member_or_404(public_id: str) -> Member:
    member = members_query().filter(Member.public_id == public_id).first()
    if member is None:
        abort(404)
    return member


def allowed_branches() -> list[Branch]:
    q = db.session.query(Branch).filter_by(is_active=True)
    limit = branch_limit_id()
    if limit is not None:
        q = q.filter(Branch.id == limit)
    return q.order_by(Branch.is_headquarters.desc(), Branch.name).all()


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #
def filtered(args) -> tuple:
    q = members_query()
    text = (args.get("q") or "").strip()
    if text:
        like = f"%{text}%"
        parts = text.split()
        conds = [Member.membership_number.ilike(like), Member.phone.ilike(like), Member.email.ilike(like),
                 Member.first_name.ilike(like), Member.last_name.ilike(like)]
        if len(parts) > 1:
            conds.append(Member.first_name.ilike(f"%{parts[0]}%") & Member.last_name.ilike(f"%{parts[-1]}%"))
        q = q.filter(or_(*conds))
    if args.get("status") in {s.value for s in MemberStatus}:
        q = q.filter(Member.status == MemberStatus(args["status"]))
    if args.get("gender") in {s.value for s in Gender}:
        q = q.filter(Member.gender == Gender(args["gender"]))
    if args.get("baptised") in ("yes", "no"):
        q = q.filter(Member.is_baptised.is_(args["baptised"] == "yes"))
    if args.get("branch"):
        q = q.join(Branch, Branch.id == Member.branch_id).filter(Branch.public_id == args["branch"])
    sort = args.get("sort", "name")
    order = {"name": (Member.last_name, Member.first_name), "number": (Member.membership_number,),
             "newest": (Member.created_at.desc(),), "joined": (Member.date_joined.desc(),)}.get(sort, (Member.last_name,))
    return q.order_by(*order), text


def stats() -> dict:
    q = members_query()
    today = date.today()
    first = today.replace(day=1)
    return {
        "total": q.count(),
        "active": q.filter(Member.status == MemberStatus.ACTIVE).count(),
        "new_this_month": q.filter(Member.created_at >= datetime(first.year, first.month, 1)).count(),
        "birthdays_this_month": q.filter(Member.status == MemberStatus.ACTIVE,
                                         extract("month", Member.date_of_birth) == today.month).count(),
    }


def upcoming_birthdays(days: int = 14, limit: int = 8) -> list[Member]:
    today = date.today()
    rows = members_query().filter(Member.status == MemberStatus.ACTIVE, Member.date_of_birth.isnot(None)).all()

    def next_bd(m):
        d = m.date_of_birth
        try:
            nb = d.replace(year=today.year)
        except ValueError:          # 29 February
            nb = date(today.year, 3, 1)
        if nb < today:
            try:
                nb = d.replace(year=today.year + 1)
            except ValueError:
                nb = date(today.year + 1, 3, 1)
        return nb

    soon = [(next_bd(m), m) for m in rows if (next_bd(m) - today).days <= days]
    return [m for _, m in sorted(soon, key=lambda x: x[0])][:limit]


# --------------------------------------------------------------------------- #
# Changes
# --------------------------------------------------------------------------- #
def create_member(tenant, actor, **values) -> Member:
    member = Member(membership_number=new_membership_number(tenant), created_by_id=getattr(actor, "id", None),
                    **values)
    if member.date_joined is None:
        member.date_joined = date.today()
    db.session.add(member)
    db.session.flush()
    db.session.add(MemberStatusChange(member_id=member.id, from_status=None, to_status=member.status.value,
                                      effective_date=member.date_joined, reason="Registered",
                                      changed_by_id=getattr(actor, "id", None)))
    audit.record("member.created", entity=member, actor=actor, name=member.display_name,
                 number=member.membership_number)
    db.session.flush()          # stamp everything with the active church now
    return member


def change_status(member: Member, to: MemberStatus, effective: date, reason: str | None, actor) -> None:
    if member.status == to:
        return
    db.session.add(MemberStatusChange(member_id=member.id, from_status=member.status.value, to_status=to.value,
                                      effective_date=effective, reason=reason, changed_by_id=actor.id))
    audit.record("member.status_changed", entity=member, actor=actor, name=member.display_name,
                 before=member.status.value, after=to.value)
    member.status = to


def transfer_to_branch(member: Member, branch: Branch, effective: date, reason: str | None, actor) -> None:
    if branch.id == member.branch_id:
        raise ValueError("The member is already in that branch.")
    db.session.add(MemberTransfer(member_id=member.id, kind=TransferKind.BRANCH, from_branch_id=member.branch_id,
                                  to_branch_id=branch.id, effective_date=effective, reason=reason,
                                  recorded_by_id=actor.id))
    audit.record("member.transferred_branch", entity=member, actor=actor, name=member.display_name,
                 to=branch.name)
    member.branch_id = branch.id


def transfer_out(member: Member, other_church: str, effective: date, reason: str | None, actor) -> None:
    db.session.add(MemberTransfer(member_id=member.id, kind=TransferKind.OUT, from_branch_id=member.branch_id,
                                  other_church=other_church, effective_date=effective, reason=reason,
                                  recorded_by_id=actor.id))
    change_status(member, MemberStatus.TRANSFERRED, effective, f"Transferred to {other_church}", actor)


def record_transfer_in(member: Member, from_church: str, actor) -> None:
    db.session.add(MemberTransfer(member_id=member.id, kind=TransferKind.IN, to_branch_id=member.branch_id,
                                  other_church=from_church, effective_date=member.date_joined or date.today(),
                                  reason="Received by transfer", recorded_by_id=actor.id))


# --------------------------------------------------------------------------- #
# Import and export
# --------------------------------------------------------------------------- #
COLUMNS = [
    ("membership_number", "Membership number"), ("title", "Title"), ("first_name", "First name"),
    ("middle_name", "Middle name"), ("last_name", "Surname"), ("gender", "Gender"),
    ("date_of_birth", "Date of birth"), ("marital_status", "Marital status"), ("phone", "Phone"),
    ("alt_phone", "Other phone"), ("email", "Email"), ("address", "Address"), ("city", "City"),
    ("region", "Region"), ("digital_address", "GPS address"), ("occupation", "Occupation"),
    ("branch", "Branch code"), ("status", "Status"), ("date_joined", "Date joined"),
    ("is_baptised", "Baptised"), ("baptism_date", "Baptism date"), ("emergency_name", "Emergency contact"),
    ("emergency_phone", "Emergency phone"),
]
IMPORT_FIELDS = [c for c in COLUMNS if c[0] != "membership_number"]
_HEADER_ALIASES = {label.lower(): key for key, label in COLUMNS} | {key: key for key, _ in COLUMNS} | {
    "firstname": "first_name", "last name": "last_name", "lastname": "last_name", "dob": "date_of_birth",
    "mobile": "phone", "telephone": "phone", "sex": "gender", "branch": "branch",
}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_IMPORT_ROWS = 5000


def parse_date(value) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"'{text}' is not a date (use YYYY-MM-DD or DD/MM/YYYY)")


def _choice(enum_cls, value, aliases=None):
    if value in (None, ""):
        return None
    v = str(value).strip().lower()
    v = (aliases or {}).get(v, v)
    try:
        return enum_cls(v)
    except ValueError:
        raise ValueError(f"'{value}' is not one of: {', '.join(e.value for e in enum_cls)}")


def read_table(filename: str, data: bytes) -> list[dict]:
    """Rows from a CSV or XLSX file, keyed by our field names."""
    name = (filename or "").lower()
    if name.endswith(".xlsx"):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        header, body = (rows[0] if rows else []), rows[1:]
        raw = [dict(zip(header, r)) for r in body]
    elif name.endswith(".csv"):
        text = data.decode("utf-8-sig", errors="replace")
        raw = list(csv.DictReader(io.StringIO(text)))
    else:
        raise ValueError("Upload a .csv or .xlsx file.")
    out = []
    for r in raw:
        mapped = {}
        for k, v in r.items():
            key = _HEADER_ALIASES.get(str(k or "").strip().lower())
            if key:
                mapped[key] = v.strip() if isinstance(v, str) else v
        if any(v not in (None, "") for v in mapped.values()):
            out.append(mapped)
    if len(out) > MAX_IMPORT_ROWS:
        raise ValueError(f"Import up to {MAX_IMPORT_ROWS} rows at a time.")
    return out


@dataclass
class ImportResult:
    created: list = field(default_factory=list)
    errors: list = field(default_factory=list)      # (row number, message)
    warnings: list = field(default_factory=list)


def validate_row(row: dict, branches_by_code: dict) -> tuple[dict, list[str], list[str]]:
    errors, warnings, values = [], [], {}
    for key in ("first_name", "last_name"):
        if not row.get(key):
            errors.append(f"{dict(COLUMNS)[key]} is required")
        else:
            values[key] = str(row[key])[:100]
    for key in ("title", "middle_name", "phone", "alt_phone", "address", "city", "region", "digital_address",
                "occupation", "emergency_name", "emergency_phone"):
        if row.get(key) not in (None, ""):
            values[key] = str(row[key]).strip()[:255]
    if row.get("email"):
        email = str(row["email"]).strip().lower()
        if _EMAIL.match(email):
            values["email"] = email
        else:
            errors.append(f"'{row['email']}' is not a valid email")
    for key in ("date_of_birth", "date_joined", "baptism_date"):
        try:
            d = parse_date(row.get(key))
            if d and d > date.today():
                errors.append(f"{dict(COLUMNS)[key]} is in the future")
            elif d:
                values[key] = d
        except ValueError as e:
            errors.append(str(e))
    try:
        values["gender"] = _choice(Gender, row.get("gender"), {"m": "male", "f": "female"})
        values["marital_status"] = _choice(MaritalStatus, row.get("marital_status"))
        status = _choice(MemberStatus, row.get("status"))
        values["status"] = status or MemberStatus.ACTIVE
    except ValueError as e:
        errors.append(str(e))
    bap = str(row.get("is_baptised") or "").strip().lower()
    values["is_baptised"] = bap in ("yes", "y", "true", "1") or bool(values.get("baptism_date"))
    code = str(row.get("branch") or "").strip().upper()
    if code:
        if code in branches_by_code:
            values["branch_id"] = branches_by_code[code].id
        else:
            errors.append(f"No branch with code '{code}'")
    if values.get("phone") and db.session.query(Member.id).filter_by(phone=values["phone"]).first():
        warnings.append(f"Phone {values['phone']} already belongs to another member")
    return {k: v for k, v in values.items() if v is not None}, errors, warnings


def import_members(tenant, actor, rows: list[dict], default_branch: Branch | None, commit: bool) -> ImportResult:
    branches = {b.code.upper(): b for b in allowed_branches()}
    result = ImportResult()
    limit = limits.check("members")
    room = None if limit.limit is None else max(0, limit.limit - limit.used)
    valid = []
    for i, row in enumerate(rows, start=2):           # row 1 is the header
        values, errs, warns = validate_row(row, branches)
        for e in errs:
            result.errors.append((i, e))
        for w in warns:
            result.warnings.append((i, w))
        if not errs:
            values.setdefault("branch_id", getattr(default_branch, "id", None))
            valid.append(values)
    if room is not None and len(valid) > room:
        result.errors.append((0, f"Your plan has room for {room} more member(s); this file has {len(valid)}."))
    if commit and not result.errors:
        for values in valid:
            result.created.append(create_member(tenant, actor, **values))
        audit.record("member.imported", actor=actor, count=len(valid))
    elif not commit:
        result.created = valid
    return result


def export_rows(members: list[Member]) -> list[list]:
    rows = [[label for _, label in COLUMNS]]
    for m in members:
        rows.append([
            m.membership_number, m.title, m.first_name, m.middle_name, m.last_name,
            m.gender.value if m.gender else "", m.date_of_birth.isoformat() if m.date_of_birth else "",
            m.marital_status.value if m.marital_status else "", m.phone, m.alt_phone, m.email, m.address, m.city,
            m.region, m.digital_address, m.occupation, m.branch.code if m.branch else "", m.status.value,
            m.date_joined.isoformat() if m.date_joined else "", "yes" if m.is_baptised else "no",
            m.baptism_date.isoformat() if m.baptism_date else "", m.emergency_name, m.emergency_phone,
        ])
    return rows


def safe_cell(value) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def to_csv(rows: list[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    for r in rows:
        w.writerow([safe_cell(v) for v in r])
    return "﻿" + buf.getvalue()


def to_xlsx(rows: list[list], title: str = "Members") -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = title[:31]
    for r in rows:
        ws.append([safe_cell(v) for v in r])
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="12204A")
    ws.freeze_panes = "A2"
    for col in ws.columns:
        width = max(len(str(c.value or "")) for c in col[:200])
        ws.column_dimensions[col[0].column_letter].width = min(max(10, width + 2), 40)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def personal_data(member: Member) -> dict:
    """Everything held about one member (data subject access request, Act 843 s.35)."""
    return {
        "membership_number": member.membership_number, "name": member.full_name,
        "gender": member.gender.value if member.gender else None,
        "date_of_birth": member.date_of_birth.isoformat() if member.date_of_birth else None,
        "marital_status": member.marital_status.value if member.marital_status else None,
        "contact": {"phone": member.phone, "other_phone": member.alt_phone, "email": member.email,
                    "address": member.address, "city": member.city, "region": member.region,
                    "gps_address": member.digital_address},
        "occupation": member.occupation, "employer": member.employer, "nationality": member.nationality,
        "hometown": member.hometown,
        "emergency_contact": {"name": member.emergency_name, "phone": member.emergency_phone,
                              "relationship": member.emergency_relation},
        "church": {"branch": member.branch.name if member.branch else None, "status": member.status.value,
                   "date_joined": member.date_joined.isoformat() if member.date_joined else None,
                   "previous_church": member.previous_church, "baptised": member.is_baptised,
                   "baptism_date": member.baptism_date.isoformat() if member.baptism_date else None,
                   "baptism_place": member.baptism_place, "baptised_by": member.baptised_by},
        "family": {"name": member.family.name, "role": member.family_role.value if member.family_role else None}
        if member.family else None,
        "documents": [{"title": d.title, "category": d.category, "uploaded": d.created_at.isoformat()}
                      for d in member.documents],
        "status_history": [{"from": h.from_status, "to": h.to_status, "date": h.effective_date.isoformat(),
                            "reason": h.reason} for h in member.history],
        "transfers": [{"kind": t.kind.value, "other_church": t.other_church,
                       "date": t.effective_date.isoformat(), "reason": t.reason} for t in member.transfers],
        "notes": member.notes,
        "record_created": member.created_at.isoformat(),
    }


# --------------------------------------------------------------------------- #
# Global search
# --------------------------------------------------------------------------- #
@search.register("members", "members.view")
def _search_members(query: str, limit: int):
    q, _ = filtered({"q": query})
    for m in q.limit(limit):
        yield search.SearchResult(title=m.display_name, url=url_for("members.profile", public_id=m.public_id),
                                  kind="Member", subtitle=f"{m.membership_number}" + (f" · {m.phone}" if m.phone else ""),
                                  icon="user-round")
