"""Stage 9: reports and analytics with CSV, Excel and PDF downloads."""
import io
from datetime import date, timedelta

import pytest
from openpyxl import load_workbook

from app.cli import seed_plans
from app.models import AuditLog, Member, PaymentMethod, Role, Service, Tenant, TenantUser
from app.services import finance as fin, ledger, members as member_svc, reports as rep
from app.utils.tenancy import tenant_bypass, tenant_context
from tests.conftest import make_user
from tests.test_design_system import assert_csp_clean

TODAY = date.today()


def login(client, email, password="Correct-Horse-9"):
    return client.post("/login", data={"email": email, "password": password})


@pytest.fixture(autouse=True)
def _plans(db):
    with tenant_bypass():
        seed_plans()
        db.session.commit()


@pytest.fixture()
def grace(client, db, two_churches):
    tid = two_churches["grace"]
    with tenant_context(tid):
        t = db.session.get(Tenant, tid)
        ledger.ensure_chart()
        for i, (first, gender) in enumerate((("Ama", "female"), ("Kofi", "male"), ("Esi", "female"))):
            m = member_svc.create_member(t, None, first_name=first, last_name="Owusu", gender=gender, phone=f"02400000{i}",
                                         date_of_birth=TODAY.replace(year=TODAY.year - 20 - i * 10), date_joined=TODAY)
            fin.record_contribution(received_on=TODAY, amount=100 * (i + 1), income_account=ledger.account("4000"),
                                    deposit_account=ledger.account("1000"), method=PaymentMethod.CASH, member=m)
        db.session.add(Service(title="Sunday Service", service_date=TODAY, headcount_men=40, headcount_women=60))
        db.session.commit()
    login(client, "owner@grace.test")
    return tid


@pytest.mark.parametrize("key", list(rep.REGISTRY))
def test_every_report_renders(client, grace, key):
    res = client.get(f"/reports/{key}")
    assert res.status_code == 200, key
    assert_csp_clean(res.get_data(as_text=True))


def test_reports_hub(client, grace):
    html = client.get("/reports").get_data(as_text=True)
    assert "Giving by month" in html and "Membership summary" in html
    assert_csp_clean(html)


def test_giving_report_totals(client, db, grace):
    with tenant_context(grace):
        r = rep.giving(rep.Params(TODAY.replace(day=1), TODAY))
    assert r.totals[-1] == 600 and r.columns[1] == "Tithes"
    html = client.get("/reports/giving?period=this_month").get_data(as_text=True)
    assert "600.00" in html


def test_downloads_csv_excel_pdf(client, db, grace):
    csv = client.get("/reports/membership?format=csv")
    assert csv.status_code == 200 and csv.mimetype == "text/csv" and "Female,2" in csv.get_data(as_text=True)
    x = client.get("/reports/attendance?format=xlsx&period=this_month")
    wb = load_workbook(io.BytesIO(x.data))
    ws = wb.active
    assert ws["A1"].value == "Grace Chapel International"
    assert any(row[1] == "Sunday Service" and row[6] == 100 for row in ws.iter_rows(values_only=True))
    pdf = client.get("/reports/giving?format=pdf")
    assert pdf.status_code == 200 and pdf.data.startswith(b"%PDF")
    with tenant_context(grace):
        assert db.session.query(AuditLog).filter_by(action="report.exported").count() == 3


def test_csv_neutralises_formulas(db, grace):
    with tenant_context(grace):
        r = rep.Report("x", ["Name"], [["=HYPERLINK(\"evil\")"]])
    assert rep.to_csv(r).splitlines()[1].startswith("\"'=") or rep.to_csv(r).splitlines()[1].startswith("'=")


def test_report_permissions(client, db, two_churches, grace):
    with tenant_context(grace):
        role = db.session.query(Role).filter_by(slug="membership_officer").one()
        user = make_user("mo@grace.test")
        with tenant_bypass():
            db.session.add(user)
            db.session.flush()
        db.session.add(TenantUser(user_id=user.id, roles=[role]))
        db.session.commit()
    client.post("/logout")
    login(client, "mo@grace.test")
    hub = client.get("/reports").get_data(as_text=True)
    assert "Membership summary" in hub and "Giving by month" not in hub
    assert client.get("/reports/giving").status_code == 403
    assert client.get("/reports/membership?format=csv").status_code == 403      # no reports.export
    assert client.get("/reports/nothing").status_code == 404


def test_reports_only_count_own_church(client, db, two_churches, grace):
    client.post("/logout")
    login(client, "owner@word.test")
    html = client.get("/reports/giving?period=this_month").get_data(as_text=True)
    assert "600.00" not in html
