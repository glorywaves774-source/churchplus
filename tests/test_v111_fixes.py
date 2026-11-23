"""1.1.1 fixes: negative amounts in exports, the phone calendar list, iPhone zoom on search boxes."""
import csv
import io
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.services import members as member_svc
from app.services import reports
from tests.test_stage7_ministry import TODAY, _event, grace  # noqa: F401  (fixtures)
from tests.test_stage7_ministry import _plans  # noqa: F401

STATIC = Path(__file__).resolve().parent.parent / "static" / "css"


# --------------------------------------------------------------------------- #
# Exports: formulas are neutralised, numbers stay numbers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value, expected", [
    (Decimal("-25.00"), "-25.00"),
    (-3, "-3"),
    (1.5, "1.5"),
    ("-1,250.00", "-1,250.00"),
    ("-40", "-40"),
    ("=HYPERLINK(\"http://x\")", "'=HYPERLINK(\"http://x\")"),
    ("-2+3", "'-2+3"),
    ("-cmd|' /C calc'!A0", "'-cmd|' /C calc'!A0"),
    ("+233245550101", "'+233245550101"),
    ("@SUM(A1)", "'@SUM(A1)"),
    ("\tx", "'\tx"),
    ("Grace", "Grace"),
    (None, ""),
])
def test_safe_cell(value, expected):
    assert member_svc.safe_cell(value) == expected


def test_csv_keeps_negative_amounts_as_numbers():
    rep = reports.Report(title="Income statement", columns=["Line", "Amount"],
                         rows=[["Offerings", Decimal("1200.00")], ["Deficit", Decimal("-350.50")],
                               ["=1+1", Decimal("0")]],
                         totals=["Net", Decimal("-350.50")])
    rows = list(csv.reader(io.StringIO(reports.to_csv(rep).lstrip("﻿"))))
    assert rows[2] == ["Deficit", "-350.50"]
    assert rows[-1] == ["Net", "-350.50"]
    assert rows[3][0] == "'=1+1"
    # The accounting exports pass amounts as formatted text; a deficit must stay a number there too.
    acc = list(csv.reader(io.StringIO(member_svc.to_csv([["Surplus / (deficit)", f"{Decimal('-350.5'):.2f}"]]).lstrip("﻿"))))
    assert acc == [["Surplus / (deficit)", "-350.50"]]


def test_audit_export_uses_the_shared_guard():
    from app import audit
    assert audit.safe_cell is member_svc.safe_cell


# --------------------------------------------------------------------------- #
# Calendar: phones get a readable, tappable list of the month's events
# --------------------------------------------------------------------------- #
def test_calendar_lists_the_months_events_for_phones(client, db, grace):  # noqa: F811
    when = TODAY + timedelta(days=3)
    _event(client, title="Prayer Retreat", start_date=when.isoformat(), start_time="18:30")
    html = client.get(f"/events?view=calendar&month={when:%Y-%m}").get_data(as_text=True)
    agenda = html[html.index('class="card cal-agenda"'):]
    assert "Prayer Retreat" in agenda and "18:30" in agenda
    assert re.search(r'<a class="cal-agenda-item" href="/events/[0-9a-f-]+">', agenda)


def test_calendar_list_says_when_the_month_is_empty(client, db, grace):  # noqa: F811
    far = (TODAY.replace(day=1) + timedelta(days=400)).strftime("%Y-%m")
    html = client.get(f"/events?view=calendar&month={far}").get_data(as_text=True)
    assert "Nothing on the calendar this month." in html


def test_calendar_list_only_shows_on_phones():
    css = (STATIC / "app.css").read_text()
    assert ".cal-agenda { display: none; }" in css
    phone = css[css.index(".cal-agenda { display: none; }"):]
    assert re.search(r"@media \(max-width: 760px\) \{\s*\.cal-agenda \{ display: block;", phone)


# --------------------------------------------------------------------------- #
# iPhone zoom: every text box is at least 16px on phones, tablets and touch screens
# --------------------------------------------------------------------------- #
def test_search_boxes_are_16px_on_touch_screens():
    css = (STATIC / "mobile.css").read_text()
    m = re.search(r"@media \(max-width: 1024px\), \(pointer: coarse\) \{([^}]*)\}", css)
    assert m, "touch-screen rule missing from mobile.css"
    assert ".nav-filter input" in m.group(1) and ".global-search input" in m.group(1)
    assert "font-size: 16px" in m.group(1)
