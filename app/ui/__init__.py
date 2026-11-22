"""Design-system helpers available in every template.

* ``icon(name)`` – inline Lucide SVG (no external requests, CSP friendly).
* ``nav_sections()`` – sidebar navigation; an item appears only once its
  module's page exists *and* the person may open it, so menus never link to
  unfinished or forbidden pages.
* ``tab_items()`` – 3–5 main destinations for the phone tab bar.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from flask import current_app, request, url_for
from markupsafe import Markup, escape

_ICON_FILE = Path(__file__).with_name("icons.json")


@lru_cache(maxsize=1)
def _icons() -> dict[str, str]:
    return json.loads(_ICON_FILE.read_text())


def icon(name: str, size: int = 18, cls: str = "", label: str | None = None) -> Markup:
    icons = _icons()
    if name not in icons:
        if current_app and (current_app.debug or current_app.testing):
            raise KeyError(f"Unknown icon '{name}'. Add it to scripts/build_icons.py.")
        name = "help-circle"
    body = icons[name]
    aria = f'role="img" aria-label="{escape(label)}"' if label else 'aria-hidden="true" focusable="false"'
    return Markup(
        f'<svg class="icon {escape(cls)}" width="{size}" height="{size}" viewBox="0 0 24 24" '
        f'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        f'stroke-linejoin="round" {aria}>{body}</svg>'
    )


@dataclass(frozen=True)
class NavItem:
    label: str
    endpoint: str
    icon: str
    permission: str | None = None     # e.g. "members.view"; None = any signed-in user
    tab_label: str | None = None      # short one-word label for the phone tab bar
    match: tuple[str, ...] = ()       # extra endpoint prefixes that mark this item active


@dataclass(frozen=True)
class NavSection:
    title: str
    items: tuple[NavItem, ...] = field(default_factory=tuple)


# The full ChurchPlus map. Items light up automatically as each stage adds its pages.
NAVIGATION: tuple[NavSection, ...] = (
    NavSection("Overview", (
        NavItem("Dashboard", "dashboard.index", "layout-dashboard", "staff", tab_label="Home"),
    )),
    NavSection("My church", (
        NavItem("Portal home", "portal.index", "home", "portal", tab_label="Home"),
        NavItem("My details", "portal.profile", "user-round", "portal"),
        NavItem("My giving", "portal.giving", "hand-coins", "portal", tab_label="Giving"),
        NavItem("Events", "portal.events", "calendar-days", "portal", tab_label="Events", match=("portal.event_",)),
        NavItem("Sermons", "portal.sermons", "book-open", "portal", tab_label="Sermons", match=("portal.sermon", "portal.media")),
        NavItem("Prayer", "portal.prayer", "heart-handshake", "portal"),
        NavItem("My family", "portal.family", "users", "portal"),
    )),
    NavSection("People", (
        NavItem("Members", "members.index", "users", "members.view", tab_label="Members"),
        NavItem("Visitors", "visitors.index", "user-plus", "visitors.view"),
        NavItem("Attendance", "attendance.index", "clipboard-check", "attendance.view", tab_label="Attendance"),
        NavItem("Children & youth", "children.index", "school", "children.view"),
    )),
    NavSection("Ministry", (
        NavItem("Services", "worship.index", "church", "worship.view"),
        NavItem("Departments", "departments.index", "network", "departments.view"),
        NavItem("Events", "events.index", "calendar-days", "events.view", tab_label="Events"),
        NavItem("Sermons & media", "sermons.index", "book-open", "sermons.view"),
        NavItem("Prayer", "prayer.index", "heart-handshake", "prayer.view"),
        NavItem("Counselling", "counselling.index", "shield-check", "counselling.view"),
        NavItem("Clergy & staff", "clergy.index", "briefcase", "clergy.view"),
    )),
    NavSection("Finance", (
        NavItem("Giving & finance", "finance.index", "hand-coins", "finance.view", tab_label="Giving"),
        NavItem("Accounting", "accounting.index", "landmark", "accounting.view"),
    )),
    NavSection("Engage", (
        NavItem("Communication", "communication.index", "megaphone", "communication.view"),
        NavItem("Reports", "reports.index", "bar-chart-3", "reports.view"),
        NavItem("Church website", "website.index", "monitor", "website.view"),
    )),
    NavSection("Administration", (
        NavItem("Member churches", "tenants.member_churches", "church", "church.view", match=("tenants.member_church_",)),
        NavItem("Church settings", "tenants.settings", "settings", "church.manage_settings"),
        NavItem("Branches", "tenants.branches", "building-2", "branches.view", match=("tenants.branch_",)),
        NavItem("Users & roles", "auth.users", "user-cog", "users.view",
                match=("auth.invite_user", "auth.edit_user", "auth.role")),
        NavItem("Subscription", "billing.index", "credit-card", "billing.view"),
        NavItem("Audit trail", "audit.index", "history", "audit.view"),
        NavItem("Design system", "design.index", "sparkles", None),
    )),
    NavSection("ChurchPlus platform", (
        NavItem("Platform overview", "platform.index", "activity", "platform", tab_label="Overview"),
        NavItem("Churches", "platform.churches", "church", "platform", tab_label="Churches", match=("platform.church",)),
        NavItem("Invoices", "platform.invoices", "receipt", "platform", tab_label="Invoices"),
        NavItem("Plans", "platform.plans", "layers", "platform"),
    )),
)


def _endpoint_exists(endpoint: str) -> bool:
    return endpoint in current_app.view_functions


def _allowed(item: NavItem, can) -> bool:
    if item.endpoint == "design.index":
        return current_app.config.get("ENV_NAME") in ("development", "testing")
    if item.endpoint == "tenants.member_churches":
        from flask import g
        from app.services.denominations import is_headquarters
        if not (getattr(g, "ui_preview", False) or is_headquarters(getattr(g, "tenant", None))):
            return False
    if item.permission is None:
        return True
    return can(item.permission)


def _is_active(item: NavItem) -> bool:
    ep = request.endpoint or ""
    if ep == item.endpoint:
        return True
    blueprint = item.endpoint.split(".")[0]
    if blueprint in ("tenants", "auth", "portal", "platform"):     # blueprints holding several menu items
        return ep.startswith(item.endpoint) or any(ep.startswith(m) for m in item.match)
    return ep.startswith(blueprint + ".") or any(ep.startswith(m) for m in item.match)


def nav_sections(can=None) -> list[dict]:
    can = can or (lambda code: True)
    out = []
    for section in NAVIGATION:
        items = [
            {"label": i.label, "url": url_for(i.endpoint), "icon": i.icon, "active": _is_active(i)}
            for i in section.items if _endpoint_exists(i.endpoint) and _allowed(i, can)
        ]
        if items:
            out.append({"title": section.title, "items": items})
    return out


def tab_items(can=None, limit: int = 4) -> list[dict]:
    """Main destinations for the phone tab bar (a Menu tab is always added after these)."""
    can = can or (lambda code: True)
    tabs = []
    staff = can("staff")
    for section in NAVIGATION:
        for i in section.items:
            if staff and i.permission == "portal":       # staff keep their work tabs
                continue
            if i.tab_label and _endpoint_exists(i.endpoint) and _allowed(i, can):
                tabs.append({"label": i.tab_label, "url": url_for(i.endpoint), "icon": i.icon,
                             "active": _is_active(i)})
    return tabs[:limit]


def can(code: str) -> bool:
    """Template permission check for the signed-in church account."""
    from flask import g
    if getattr(g, "ui_preview", False):
        return True
    if code == "platform":
        user = getattr(g, "user", None)
        return bool(user and user.is_platform_staff)
    account = getattr(g, "account", None)
    return bool(account and account.can(code))


def initials(name: str | None) -> str:
    parts = [p for p in (name or "").replace(".", " ").split() if p[:1].isalpha()]
    return ((parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")) if parts else "?").upper()


def money(value, currency: str | None = None, plain: bool = False) -> str:
    """Format an amount: GHS 1,250.50 (negative amounts in brackets, accounting style)."""
    from decimal import Decimal
    from flask import g
    if value is None or value == "":
        return "—"
    d = Decimal(str(value))
    text = f"{abs(d):,.2f}"
    if d < 0:
        text = f"({text})"
    if plain:
        return text
    cur = currency or (getattr(getattr(g, "tenant", None), "currency", None) or "GHS")
    return f"{cur} {text}"


def localtime(value, fmt: str = "%d %b %Y %H:%M") -> str:
    """Show a stored UTC timestamp in the church's own time zone."""
    from datetime import timezone as _tz
    from zoneinfo import ZoneInfo
    from flask import g
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=_tz.utc)
    try:
        zone = ZoneInfo(getattr(getattr(g, "tenant", None), "timezone", None) or "Africa/Accra")
    except Exception:  # unknown zone name
        zone = _tz.utc
    return value.astimezone(zone).strftime(fmt)


def init_app(app) -> None:
    app.jinja_env.globals.update(icon=icon, nav_sections=nav_sections, tab_items=tab_items, can=can,
                                 has_endpoint=_endpoint_exists)
    app.jinja_env.filters["initials"] = initials
    app.jinja_env.filters["money"] = money
    app.jinja_env.filters["localtime"] = localtime
