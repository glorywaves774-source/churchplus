"""Design system gallery (development and testing only).

Shows every reusable component with sample data so pages can be built and
reviewed consistently. It is never registered in production.
"""
from __future__ import annotations

from math import ceil

from flask import Blueprint, abort, current_app, g, render_template, request

bp = Blueprint("design", __name__)


class SamplePagination:
    """Mimics Flask-SQLAlchemy's Pagination for the gallery."""

    def __init__(self, page: int, per_page: int, total: int):
        self.page, self.per_page, self.total = page, per_page, total
        self.pages = max(1, ceil(total / per_page))
        self.has_prev, self.has_next = page > 1, page < self.pages
        self.prev_num, self.next_num = page - 1, page + 1
        self.first = (page - 1) * per_page + 1
        self.last = min(page * per_page, total)

    def iter_pages(self, left_edge=1, left_current=1, right_current=2, right_edge=1):
        last = 0
        for n in range(1, self.pages + 1):
            if (n <= left_edge or self.page - left_current <= n <= self.page + right_current
                    or n > self.pages - right_edge):
                if last + 1 != n:
                    yield None
                yield n
                last = n


SAMPLE_MEMBERS = [
    ("CP-000124", "Abena Mensah", "Choir", "Accra Central", "active", "024 555 0134", "2019-03-10"),
    ("CP-000125", "Kwame Owusu-Ansah", "Ushering", "Accra Central", "active", "020 555 0199", "2020-07-22"),
    ("CP-000126", "Efua Boateng", "Children's Ministry", "Tema", "pending", "055 555 0147", "2026-10-02"),
    ("CP-000127", "Yaw Asante", "Men's Fellowship", "Kumasi", "inactive", "027 555 0110", "2016-01-15"),
    ("CP-000128", "Akosua Darko", "Prayer", "Tema", "active", "024 555 0162", "2022-11-06"),
    ("CP-000129", "Kofi Adjei", "Media", "Accra Central", "transferred", "050 555 0185", "2018-05-19"),
]


@bp.before_request
def _preview_only():
    if current_app.config.get("ENV_NAME") not in ("development", "testing"):
        abort(404)
    g.ui_preview = True


@bp.get("/")
def index():
    page = max(1, request.args.get("page", 1, type=int))
    months = ["Jun", "Jul", "Aug", "Sep", "Oct", "Nov"]
    return render_template(
        "design/index.html",
        breadcrumbs=[("Administration", None), ("Design system", None)],
        shell_user={"name": "Ama Serwaa Ofori", "email": "ama@gracechapel.test", "role": "Church Administrator",
                    "links": [("My profile", "#", "user-round"), ("Church settings", "#", "settings")]},
        shell_church={"name": "Grace Chapel International", "subtitle": "Accra Central · Growth plan"},
        notifications=[
            {"text": "3 first-timers need follow-up calls", "when": "10 minutes ago", "icon": "user-plus"},
            {"text": "Building fund offering approved", "when": "1 hour ago", "icon": "badge-check"},
        ],
        members=SAMPLE_MEMBERS,
        pager=SamplePagination(page, 6, 1284),
        attendance_chart={"type": "line", "labels": months,
                          "series": [{"label": "Sunday", "data": [812, 845, 790, 902, 938, 961]},
                                     {"label": "Midweek", "data": [310, 296, 325, 341, 330, 362]}]},
        giving_chart={"type": "bar", "currency": "GHS", "labels": months,
                      "series": [{"label": "Tithes", "data": [48200, 51050, 47600, 53900, 55120, 58340]},
                                 {"label": "Offerings", "data": [21400, 19800, 22350, 24010, 23150, 26780]}],
                      "stacked": True},
        ministry_chart={"type": "doughnut", "labels": ["Choir", "Ushering", "Youth", "Children", "Prayer"],
                        "series": [{"label": "Members", "data": [86, 54, 212, 148, 67]}]},
        activity=[
            {"text": "Efua Boateng registered as a new member", "when": "Today, 09:14", "icon": "user-plus"},
            {"text": "Sunday first service attendance recorded: 512", "when": "Today, 08:52", "icon": "clipboard-check"},
            {"text": "GHS 4,250.00 welfare contribution receipted", "when": "Yesterday, 17:30", "icon": "receipt"},
            {"text": "Youth Camp 2026 registration opened", "when": "Yesterday, 11:05", "icon": "calendar-plus"},
        ],
        events=[("Thanksgiving Service", "Sun 23 Nov", "Main auditorium", "Approved"),
                ("Youth Camp 2026", "Fri 12 Dec", "Akosombo", "Pending"),
                ("Carols Night", "Sun 21 Dec", "Main auditorium", "Draft")],
    )
