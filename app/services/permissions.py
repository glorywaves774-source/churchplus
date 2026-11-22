"""Permission catalogue and default church roles.

Permission codes are ``<module>.<action>``. The catalogue is synced into the
``permissions`` table with ``flask churchplus sync-permissions`` and on every
church provisioning, so adding a module here is the only step needed.
"""
from __future__ import annotations

from app.extensions import db
from app.models import Permission

ACTIONS = ("view", "create", "edit", "delete", "approve", "export", "manage_settings")

# module code -> (label, is_sensitive)
MODULES: dict[str, tuple[str, bool]] = {
    "church": ("Church profile and settings", False),
    "branches": ("Branches", False),
    "users": ("Users and roles", False),
    "members": ("Membership", False),
    "visitors": ("Visitors and first-timers", False),
    "attendance": ("Attendance", False),
    "worship": ("Service management", False),
    "departments": ("Departments and ministries", False),
    "clergy": ("Clergy and human resources", False),
    "finance": ("Finance", False),
    "accounting": ("Accounting", False),
    "events": ("Events and programmes", False),
    "sermons": ("Sermon and media library", False),
    "prayer": ("Prayer requests", True),
    "counselling": ("Counselling", True),
    "children": ("Children and youth", True),
    "communication": ("Communication", False),
    "reports": ("Reports and analytics", False),
    "website": ("Church website", False),
    "billing": ("ChurchPlus subscription", False),
    "audit": ("Audit trail", False),
}


def all_codes() -> list[str]:
    return [f"{m}.{a}" for m in MODULES for a in ACTIONS]


def _codes(modules, actions=ACTIONS) -> set[str]:
    return {f"{m}.{a}" for m in modules for a in actions}


_ALL = set(all_codes())
_SENSITIVE = {c for c in _ALL if MODULES[c.split(".")[0]][1]}
_READ = ("view", "export")

# slug -> (display name, description, permission codes)
DEFAULT_ROLES: dict[str, tuple[str, str, set[str]]] = {
    "church_owner": ("Church Owner", "Full control of the church workspace.", _ALL),
    "senior_pastor": ("Senior Pastor", "Oversight of all church ministry and records.", _ALL - _codes(["billing"], ("manage_settings",))),
    "associate_pastor": ("Associate Pastor", "Ministry and pastoral care.",
        _codes(["members", "visitors", "attendance", "worship", "departments", "events", "sermons", "prayer", "counselling", "children", "communication"], ("view", "create", "edit"))
        | _codes(["reports"], _READ)),
    "church_admin": ("Church Administrator", "Day-to-day administration.",
        (_ALL - _SENSITIVE - _codes(["billing"], ("manage_settings",)) - _codes(["finance", "accounting"], ("approve",)))),
    "branch_admin": ("Branch Administrator", "Administration of an assigned branch.",
        _codes(["members", "visitors", "attendance", "worship", "departments", "events", "communication"], ("view", "create", "edit", "export"))
        | _codes(["reports", "branches"], ("view",))),
    "finance_manager": ("Finance Manager", "Manages and approves church finances.",
        _codes(["finance", "accounting"]) | _codes(["reports"], _READ) | _codes(["members"], ("view",))),
    "accountant": ("Accountant", "Records transactions and prepares statements.",
        _codes(["finance", "accounting"], ("view", "create", "edit", "export")) | _codes(["reports"], _READ)),
    "membership_officer": ("Membership Officer", "Maintains membership and visitor records.",
        _codes(["members", "visitors", "attendance"], ("view", "create", "edit", "export")) | _codes(["reports"], ("view",))),
    "hr_officer": ("Human Resource Officer", "Clergy, staff and volunteer records.",
        _codes(["clergy"], ("view", "create", "edit", "export", "approve")) | _codes(["departments"], ("view",))),
    "department_head": ("Department Head", "Leads a department or ministry.",
        _codes(["departments", "attendance", "events"], ("view", "create", "edit")) | _codes(["members"], ("view",))),
    "media_officer": ("Media Officer", "Sermons, media and the church website.",
        _codes(["sermons", "website"], ("view", "create", "edit", "delete")) | _codes(["events"], ("view",))),
    "counsellor": ("Counsellor", "Pastoral counselling. Sees only the cases assigned to them.",
        _codes(["counselling"], ("view", "create", "edit")) | _codes(["prayer"], ("view", "create", "edit")) | _codes(["members"], ("view",))),
    "children_leader": ("Children and Youth Leader", "Sunday school, youth, child check-in and safeguarding reports.",
        _codes(["children"], ("view", "create", "edit")) | _codes(["attendance", "events"], ("view", "create")) | _codes(["members"], ("view",))),
    "auditor": ("Auditor", "Read-only access to finance, accounting and audit trail.",
        _codes(["finance", "accounting", "audit", "reports"], _READ)),
    "member": ("Member", "Member portal access.", set()),
}


def ensure_default_roles() -> int:
    """Add default roles that a church is missing (new roles in later releases). Runs in the
    active tenant context. Existing roles are never changed, so church customisations stay."""
    from app.models import Role
    have = {r.slug for r in db.session.query(Role)}
    perms = {p.code: p for p in db.session.query(Permission)}
    added = 0
    for slug, (label, description, codes) in DEFAULT_ROLES.items():
        if slug in have:
            continue
        db.session.add(Role(name=label, slug=slug, description=description, is_system=True,
                            is_branch_scoped=(slug == "branch_admin"),
                            permissions=[perms[c] for c in sorted(codes) if c in perms]))
        added += 1
    return added


def sync_permissions() -> int:
    """Insert any missing permissions. Safe to run repeatedly. Returns number added."""
    existing = {p.code for p in db.session.query(Permission.code)}
    added = 0
    for module, (label, sensitive) in MODULES.items():
        for action in ACTIONS:
            code = f"{module}.{action}"
            if code in existing:
                continue
            db.session.add(Permission(
                code=code, module=module, action=action, is_sensitive=sensitive,
                description=f"{action.replace('_', ' ').capitalize()} - {label}",
            ))
            added += 1
    db.session.flush()
    return added
