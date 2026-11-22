"""Stage 1: design system, app shell and reusable components."""
import re

import pytest
from flask import g, render_template_string

from app.ui import NAVIGATION, can, icon, initials, nav_sections, tab_items

INLINE_HANDLER = re.compile(r"\son[a-z]+\s*=", re.I)
INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.I)
INLINE_STYLE = re.compile(r"\sstyle\s*=", re.I)


def assert_csp_clean(html: str):
    assert not INLINE_SCRIPT.search(html), "inline <script> found"
    assert not INLINE_HANDLER.search(html), "inline event handler found"
    assert not INLINE_STYLE.search(html), "inline style attribute found"


@pytest.mark.parametrize("path", ["/", "/design/", "/search?q=grace", "/missing"])
def test_pages_follow_strict_csp(client, path):
    res = client.get(path)
    html = res.get_data(as_text=True)
    assert_csp_clean(html)
    assert "width=device-width, initial-scale=1" in html
    assert "data-theme-toggle" in html


def test_csp_allows_only_own_origin(client):
    csp = client.get("/").headers["Content-Security-Policy"]
    assert "script-src 'self';" in csp and "style-src 'self';" in csp
    # No third-party CDNs: only the payment pages a church is sent to may appear (form-action).
    parts = {p.split()[0]: p for p in csp.split("; ") if p}
    assert all("http" not in v for k, v in parts.items() if k != "form-action")
    assert parts["form-action"].split()[1:] == ["'self'", "https://checkout.paystack.com", "https://checkout.hubtel.com"]


def test_design_gallery_renders_all_components(client):
    html = client.get("/design/").get_data(as_text=True)
    for marker in ['class="sidebar"', 'class="topbar"', 'class="breadcrumb"', 'role="search"',
                   'notif-menu', 'stat-card', 'data-chart=', 'class="table"', 'data-sortable',
                   'class="pagination"', 'class="form-grid"', 'type="date"', 'dialog class="modal',
                   'id="confirm-dialog"', 'data-confirm=', 'role="tablist"', 'profile-card',
                   'empty-state', 'class="dropdown"', 'data-sidebar-collapse', 'data-drawer-open']:
        assert marker in html, marker


def test_design_gallery_hidden_in_production(app):
    from app import create_app
    from app.config import ProductionConfig
    ProductionConfig.SECRET_KEY, ProductionConfig.SQLALCHEMY_DATABASE_URI = "x" * 40, "postgresql+psycopg://u@h/d"
    try:
        prod = create_app("production")
        assert "design.index" not in prod.view_functions
    finally:
        ProductionConfig.SECRET_KEY = ProductionConfig.SQLALCHEMY_DATABASE_URI = None


def test_assets_are_self_hosted(client):
    for path in ("/static/vendor/chartjs/chart.umd.min.js", "/static/vendor/inter/inter-latin-wght-normal.woff2",
                 "/static/js/app.js", "/static/js/charts.js", "/static/css/app.css", "/static/images/favicon.svg"):
        assert client.get(path).status_code == 200, path


def test_icon_renders_inline_svg(app):
    with app.test_request_context():
        svg = str(icon("users", 20, "x"))
        assert svg.startswith("<svg") and 'width="20"' in svg and 'aria-hidden="true"' in svg
        labelled = str(icon("bell", label="Alerts"))
        assert 'aria-label="Alerts"' in labelled and 'role="img"' in labelled
        with pytest.raises(KeyError):
            icon("not-a-real-icon")


def test_every_navigation_icon_exists(app):
    with app.test_request_context():
        for section in NAVIGATION:
            for item in section.items:
                icon(item.icon)


def test_navigation_only_lists_existing_pages(app):
    with app.test_request_context("/design/"):
        g.ui_preview = True
        sections = nav_sections(lambda c: True)
        items = {i["label"]: i for s in sections for i in s["items"]}
        assert "Communication" in items and "Church website" in items
        assert "Members" in items
        assert {"Dashboard", "Branches", "Church settings", "Design system"} <= set(items)
        assert items["Design system"]["active"] is True
        assert all(i["url"].startswith("/") for i in items.values())


def test_navigation_respects_permissions(app):
    with app.test_request_context("/dashboard"):
        allowed = [i["label"] for s in nav_sections(lambda c: c in ("members.view", "staff")) for i in s["items"]]
        assert "Members" in allowed and "Dashboard" in allowed and "Branches" not in allowed
        denied = [i["label"] for s in nav_sections(lambda c: False) for i in s["items"]]
        assert "Members" not in denied and "Dashboard" not in denied  # portal-only accounts get the portal instead
        assert "Member churches" not in allowed      # only for denomination headquarters
        tabs = tab_items(lambda c: True)
        assert [t["label"] for t in tabs][:3] == ["Home", "Members", "Attendance"]
        assert tabs[0]["active"] is True


def test_tab_bar_limited_to_four_plus_menu(app):
    for ep in ("members.index", "attendance.index", "events.index", "finance.index"):
        if ep not in app.view_functions:
            app.add_url_rule("/_" + ep, endpoint=ep, view_func=lambda: "ok")
    with app.test_request_context("/"):
        assert len(tab_items(lambda c: True)) == 4


def test_template_can_without_account_is_false(app):
    with app.test_request_context("/"):
        assert can("members.view") is False
        g.ui_preview = True
        assert can("members.view") is True


def test_initials():
    assert initials("Ama Serwaa Ofori") == "AO"
    assert initials("Rev. Daniel Amoah") == "RA"
    assert initials("Kwame") == "K"
    assert initials("") == "?"


def test_badge_tones_and_pagination_macros(app):
    from app.design import SamplePagination
    tpl = """{% import "components/_macros.html" as ui with context %}
    {{ ui.badge('active') }}{{ ui.badge('suspended') }}{{ ui.badge('past_due') }}
    {{ ui.pagination(p, 'design.index') }}"""
    with app.test_request_context("/design/"):
        html = render_template_string(tpl, p=SamplePagination(3, 10, 95))
    assert 'badge badge-success">Active' in html
    assert 'badge badge-danger">Suspended' in html
    assert "Past due" in html
    assert "Showing 21–30 of 95" in html and 'aria-current="page">3<' in html


def test_macro_attrs_are_escaped(app):
    tpl = """{% import "components/_macros.html" as ui with context %}{{ ui.button('Go', attrs={'data-x': '"><script>'}) }}"""
    with app.test_request_context("/"):
        html = render_template_string(tpl)
    assert "<script>" not in html and "&#34;&gt;&lt;script&gt;" in html


def test_flash_messages_become_toasts(app, client):
    from flask import flash

    @app.get("/_flash")
    def _flash():
        flash("Member saved.", "success")
        from flask import render_template
        return render_template("core/index.html")

    html = client.get("/_flash").get_data(as_text=True)
    assert 'data-toast="Member saved."' in html and 'data-kind="success"' in html


def test_search_page(client):
    res = client.get("/search?q=a")
    assert res.status_code == 200
    assert "Enter at least two characters" in res.get_data(as_text=True)
    assert "No results" in client.get("/search?q=grace").get_data(as_text=True)


def test_search_providers_respect_permissions():
    from app.services import search
    saved = list(search._PROVIDERS)
    search._PROVIDERS.clear()

    @search.register("_test_provider", "members.view")
    def _provider(q, limit):
        return [search.SearchResult(title=f"Hit {q}", url="/x", kind="Member")]

    try:
        assert search.run("ab", lambda c: False) == []
        assert [r.title for r in search.run("ab", lambda c: True)] == ["Hit ab"]
        assert search.run("a", lambda c: True) == []
    finally:
        search._PROVIDERS[:] = saved
