"""Stage 4: membership management."""
import io
import json
from datetime import date, timedelta

import pytest

from app.cli import seed_plans
from app.models import (
    Branch, Family, Member, MemberDocument, MemberStatus, MemberTransfer, Plan, Role, TenantUser,
)
from app.services import members as svc
from app.utils.tenancy import tenant_bypass, tenant_context
from tests.conftest import make_user
from tests.test_design_system import assert_csp_clean

PW = "Correct-Horse-9"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def real_png(size=(60, 75), colour=(120, 90, 200)) -> bytes:
    """A genuine image: photos are now decoded and re-saved, so a bare file signature is refused."""
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, "PNG")
    return out.getvalue()
PDF = b"%PDF-1.7\n" + b"0" * 64


def login(client, email, password=PW):
    return client.post("/login", data={"email": email, "password": password})


def new_member(client, **over):
    data = {"title": "Mrs", "first_name": "Abena", "last_name": "Mensah", "gender": "female",
            "date_of_birth": "1990-05-14", "marital_status": "married", "phone": "0241112233",
            "email": "Abena@Example.com", "branch": "", "date_joined": "2020-01-05", "is_baptised": "y",
            "allow_birthday_messages": "y"}
    data.update(over)
    return client.post("/members/new", data=data)


def member(db, tenant_id, **filters):
    db.session.expire_all()
    with tenant_context(tenant_id):
        return db.session.query(Member).filter_by(**filters).one()


def pid(db, tenant_id, **filters):
    return member(db, tenant_id, **filters).public_id


def add_person(db, tenant_id, email, role_slug, branch_code=None):
    with tenant_context(tenant_id):
        role = db.session.query(Role).filter_by(slug=role_slug).one()
        branch = db.session.query(Branch).filter_by(code=branch_code).one() if branch_code else None
        user = make_user(email)
        with tenant_bypass():
            db.session.add(user)
            db.session.flush()
        db.session.add(TenantUser(user_id=user.id, roles=[role], branch=branch))
        db.session.commit()


@pytest.fixture(autouse=True)
def _plans(db):
    with tenant_bypass():
        seed_plans()
        db.session.commit()


@pytest.fixture()
def grace(client, two_churches):
    login(client, "owner@grace.test")
    return two_churches["grace"]


# --------------------------------------------------------------------------- #
# Registration and numbering
# --------------------------------------------------------------------------- #
def test_register_member_assigns_number_and_history(client, db, grace):
    res = new_member(client)
    assert res.status_code == 302 and "/members/" in res.headers["Location"]
    m = member(db, grace, first_name="Abena")
    assert m.membership_number == "GCI-000001"
    assert m.email == "abena@example.com" and m.is_baptised and m.status == MemberStatus.ACTIVE
    with tenant_context(grace):
        assert [h.to_status for h in m.history] == ["active"]
    new_member(client, first_name="Kofi", phone="0209998888")
    assert member(db, grace, first_name="Kofi").membership_number == "GCI-000002"


def test_numbers_are_per_church(client, db, two_churches):
    login(client, "owner@word.test")
    new_member(client)
    assert member(db, two_churches["word"], first_name="Abena").membership_number == "LWA-000001"


def test_custom_prefix(client, db, grace):
    from app.models import Tenant
    with tenant_bypass():
        t = db.session.get(Tenant, grace)
        t.settings = {"member_prefix": "GCHAPEL"}
        db.session.commit()
    new_member(client)
    assert member(db, grace, first_name="Abena").membership_number == "GCHAPEL-000001"


def test_register_validation(client, db, grace):
    future = (date.today() + timedelta(days=5)).isoformat()
    html = new_member(client, date_of_birth=future, email="not-an-email", digital_address="nowhere").get_data(as_text=True)
    assert "This date is in the future" in html and "Invalid email" in html and "GA-123-4567" in html
    html = new_member(client, first_name="").get_data(as_text=True)
    assert "This field is required" in html


def test_member_limit_from_plan(client, db, grace):
    with tenant_bypass():
        db.session.query(Plan).filter_by(code="growth").one().max_members = 1
        db.session.commit()
    new_member(client)
    res = client.get("/members/new", follow_redirects=True)
    assert "Your plan allows 1 members" in res.get_data(as_text=True)


# --------------------------------------------------------------------------- #
# Listing, search, filters
# --------------------------------------------------------------------------- #
def test_list_search_and_filters(client, db, grace):
    new_member(client)
    new_member(client, first_name="Kwame", last_name="Asante", gender="male", phone="0507776666", email="", is_baptised="")
    html = client.get("/members").get_data(as_text=True)
    assert "Abena Mensah" in html and "Kwame Asante" in html
    assert "Kwame" not in client.get("/members?q=abena").get_data(as_text=True)
    assert "Abena" not in client.get("/members?q=0507776666").get_data(as_text=True)
    assert "Abena" in client.get("/members?q=GCI-000001").get_data(as_text=True)
    assert "Abena" in client.get("/members?q=abena+mensah").get_data(as_text=True)
    assert "Abena" not in client.get("/members?gender=male").get_data(as_text=True)
    assert "Kwame" not in client.get("/members?baptised=yes").get_data(as_text=True)
    assert_csp_clean(html)


def test_global_search_finds_members(client, db, grace):
    new_member(client)
    html = client.get("/search?q=mensah").get_data(as_text=True)
    assert "Abena Mensah" in html and "GCI-000001" in html


# --------------------------------------------------------------------------- #
# Profile, editing, status, transfers
# --------------------------------------------------------------------------- #
def test_profile_and_edit(client, db, grace):
    new_member(client)
    p = pid(db, grace, first_name="Abena")
    html = client.get(f"/members/{p}").get_data(as_text=True)
    assert "Mrs Abena Mensah" in html and "GCI-000001" in html
    assert_csp_clean(html)
    client.post(f"/members/{p}/edit", data={"first_name": "Abena", "last_name": "Mensah-Owusu", "title": "Mrs",
                                            "occupation": "Nurse", "branch": ""})
    m = member(db, grace, public_id=p)
    assert m.last_name == "Mensah-Owusu" and m.occupation == "Nurse"


def test_status_change_records_history(client, db, grace):
    new_member(client)
    p = pid(db, grace, first_name="Abena")
    client.post(f"/members/{p}/status", data={"status": "inactive", "effective_date": date.today().isoformat(),
                                              "reason": "Relocated abroad"})
    m = member(db, grace, public_id=p)
    with tenant_context(grace):
        assert m.status == MemberStatus.INACTIVE
        assert m.history[0].from_status == "active" and m.history[0].reason == "Relocated abroad"


def test_branch_transfer_and_transfer_out(client, db, grace):
    client.post("/branches/new", data={"name": "Tema", "code": "TEMA", "country": "GH"})
    with tenant_context(grace):
        hq = db.session.query(Branch).filter_by(code="HQ").one().public_id
        tema = db.session.query(Branch).filter_by(code="TEMA").one()
        tema_pid, tema_id = tema.public_id, tema.id
    new_member(client, branch=hq)
    p = pid(db, grace, first_name="Abena")
    # Changing branch through "Edit" is refused; transfers are recorded instead.
    client.post(f"/members/{p}/edit", data={"first_name": "Abena", "last_name": "Mensah", "branch": tema_pid})
    assert member(db, grace, public_id=p).branch_id != tema_id
    client.post(f"/members/{p}/transfer", data={"kind": "branch", "branch": tema_pid,
                                                "effective_date": date.today().isoformat()})
    assert member(db, grace, public_id=p).branch_id == tema_id
    client.post(f"/members/{p}/transfer", data={"kind": "out", "other_church": "Calvary Charismatic, Kumasi",
                                                "effective_date": date.today().isoformat(), "reason": "Moved"})
    m = member(db, grace, public_id=p)
    with tenant_context(grace):
        assert m.status == MemberStatus.TRANSFERRED
        kinds = sorted(t.kind.value for t in db.session.query(MemberTransfer))
    assert kinds == ["branch", "out"]


def test_transfer_in_recorded_from_previous_church(client, db, grace):
    new_member(client, previous_church="Bethel Methodist, Cape Coast")
    with tenant_context(grace):
        t = db.session.query(MemberTransfer).one()
        assert t.kind.value == "in" and t.other_church == "Bethel Methodist, Cape Coast"


# --------------------------------------------------------------------------- #
# Families
# --------------------------------------------------------------------------- #
def test_family_create_join_and_leave(client, db, grace):
    new_member(client)
    new_member(client, title="Mr", first_name="Kojo", gender="male", phone="0201110000", email="")
    abena, kojo = pid(db, grace, first_name="Abena"), pid(db, grace, first_name="Kojo")
    client.post(f"/members/{abena}/family", data={"new_family_name": "The Mensah Family", "role": "spouse"})
    with tenant_context(grace):
        fam = db.session.query(Family).one().public_id
    client.post(f"/members/{kojo}/family", data={"family": fam, "role": "head"})
    html = client.get(f"/families/{fam}").get_data(as_text=True)
    assert "Abena Mensah" in html and "Kojo Mensah" in html
    client.post(f"/members/{kojo}/family/leave")
    assert member(db, grace, public_id=kojo).family_id is None


# --------------------------------------------------------------------------- #
# Photos and documents
# --------------------------------------------------------------------------- #
def test_photo_and_documents(client, db, grace):
    new_member(client)
    p = pid(db, grace, first_name="Abena")
    client.post(f"/members/{p}/photo", data={"photo": (io.BytesIO(real_png()), "passport.png")}, content_type="multipart/form-data")
    res = client.get(f"/members/{p}/photo")
    assert res.status_code == 200 and res.data[:3] == b"\xff\xd8\xff" and res.mimetype == "image/jpeg"
    client.post(f"/members/{p}/documents", data={"title": "Baptism certificate", "category": "baptism",
                                                 "file": (io.BytesIO(PDF), "baptism.pdf")}, content_type="multipart/form-data")
    with tenant_context(grace):
        doc = db.session.query(MemberDocument).one()
        doc_id, key = doc.public_id, doc.file_key
    assert key.startswith(f"tenants/{grace}/member-documents/")
    dl = client.get(f"/members/{p}/documents/{doc_id}")
    assert dl.status_code == 200 and dl.data == PDF and "attachment" in dl.headers["Content-Disposition"]
    bad = client.post(f"/members/{p}/documents", data={"title": "x", "category": "other",
                                                       "file": (io.BytesIO(b"MZ fake"), "evil.pdf")},
                      content_type="multipart/form-data", follow_redirects=True)
    assert "do not match its type" in bad.get_data(as_text=True)
    client.post(f"/members/{p}/documents/{doc_id}/delete")
    with tenant_context(grace):
        assert db.session.query(MemberDocument).count() == 0


def test_delete_member_erases_files(app, client, db, grace):
    import os
    new_member(client)
    p = pid(db, grace, first_name="Abena")
    client.post(f"/members/{p}/photo", data={"photo": (io.BytesIO(real_png()), "p.png")}, content_type="multipart/form-data")
    key = member(db, grace, public_id=p).photo_key
    path = os.path.join(app.config["LOCAL_STORAGE_PATH"], key)
    assert os.path.exists(path)
    client.post(f"/members/{p}/delete")
    assert not os.path.exists(path)
    with tenant_context(grace):
        assert db.session.query(Member).count() == 0


# --------------------------------------------------------------------------- #
# QR identification
# --------------------------------------------------------------------------- #
def test_id_card_and_qr_verification(app, client, db, grace, two_churches):
    new_member(client)
    m = member(db, grace, first_name="Abena")
    p, token = m.public_id, m.qr_token
    html = client.get(f"/members/{p}/card").get_data(as_text=True)
    assert "<svg" in html and "GCI-000001" in html
    assert_csp_clean(html)
    res = client.get(f"/m/{token}")
    assert res.status_code == 302 and res.headers["Location"].endswith(f"/members/{p}")
    anon = app.test_client()
    assert anon.get(f"/m/{token}").headers["Location"].endswith("/login")
    other = app.test_client()
    login(other, "owner@word.test")
    assert other.get(f"/m/{token}").status_code == 404          # another church learns nothing
    client.post(f"/members/{p}/card/renew")
    assert client.get(f"/m/{token}").status_code == 404           # old card stops working


# --------------------------------------------------------------------------- #
# Import and export
# --------------------------------------------------------------------------- #
GOOD_CSV = ("First name,Surname,Gender,Date of birth,Phone,Email,Branch code,Baptised\n"
            "Yaw,Boateng,m,12/03/1988,0244000001,yaw@example.com,HQ,yes\n"
            "Esi,Owusu,female,1995-07-30,0244000002,,,no\n")


def test_import_preview_then_confirm(client, db, grace):
    res = client.post("/members/import", data={"file": (io.BytesIO(GOOD_CSV.encode()), "members.csv"), "branch": ""},
                      content_type="multipart/form-data")
    html = res.get_data(as_text=True)
    assert "Import 2 members" in html
    with tenant_context(grace):
        assert db.session.query(Member).count() == 0              # nothing saved yet
    client.post("/members/import", data={"confirm": "1"})
    with tenant_context(grace):
        rows = {m.first_name: m for m in db.session.query(Member)}
        assert set(rows) == {"Yaw", "Esi"}
        assert rows["Yaw"].date_of_birth == date(1988, 3, 12) and rows["Yaw"].is_baptised
        assert rows["Yaw"].branch.code == "HQ"


def test_import_reports_errors_and_saves_nothing(client, db, grace):
    bad = ("First name,Surname,Gender,Date of birth,Email,Branch code\n"
           "Yaw,,m,1988-01-01,,HQ\n"
           "Esi,Owusu,unknown,31/31/1990,bad-email,NOPE\n")
    html = client.post("/members/import", data={"file": (io.BytesIO(bad.encode()), "m.csv")},
                       content_type="multipart/form-data").get_data(as_text=True)
    assert "Surname is required" in html and "is not a date" in html and "valid email" in html
    assert "No branch with code" in html and "is not one of" in html
    with tenant_context(grace):
        assert db.session.query(Member).count() == 0


def test_import_xlsx(client, db, grace):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["First name", "Surname", "Date of birth"])
    ws.append(["Akosua", "Darko", date(1979, 11, 2)])
    buf = io.BytesIO()
    wb.save(buf)
    client.post("/members/import", data={"file": (io.BytesIO(buf.getvalue()), "m.xlsx")}, content_type="multipart/form-data")
    client.post("/members/import", data={"confirm": "1"})
    assert member(db, grace, first_name="Akosua").date_of_birth == date(1979, 11, 2)


def test_import_rejects_other_file_types(client, grace):
    res = client.post("/members/import", data={"file": (io.BytesIO(b"hello"), "m.txt")},
                      content_type="multipart/form-data")
    assert "Upload a .csv or .xlsx file" in res.get_data(as_text=True)


def test_export_csv_and_xlsx(client, db, grace):
    new_member(client, first_name="=cmd|' /C calc'!A0")
    csv = client.get("/members/export.csv")
    body = csv.get_data(as_text=True)
    assert "GCI-000001" in body and "'=cmd" in body
    xl = client.get("/members/export.xlsx")
    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(xl.data)).active
    assert ws["A2"].value == "GCI-000001" and ws["C2"].value.startswith("'=")
    tpl = client.get("/members/import/template.csv")
    assert "First name" in tpl.get_data(as_text=True)


def test_personal_data_export(client, db, grace):
    new_member(client)
    p = pid(db, grace, first_name="Abena")
    data = json.loads(client.get(f"/members/{p}/data.json").data)
    assert data["member"]["membership_number"] == "GCI-000001"
    assert data["member"]["contact"]["email"] == "abena@example.com"


# --------------------------------------------------------------------------- #
# Access control
# --------------------------------------------------------------------------- #
def test_other_church_cannot_reach_members(app, client, db, grace, two_churches):
    new_member(client)
    p = pid(db, grace, first_name="Abena")
    other = app.test_client()
    login(other, "owner@word.test")
    for path in (f"/members/{p}", f"/members/{p}/edit", f"/members/{p}/photo", f"/members/{p}/card",
                 f"/members/{p}/data.json"):
        assert other.get(path).status_code == 404, path
    for path in (f"/members/{p}/status", f"/members/{p}/transfer", f"/members/{p}/delete", f"/members/{p}/family"):
        assert other.post(path).status_code in (400, 404), path
    assert "Abena" not in other.get("/members").get_data(as_text=True)
    assert "Abena" not in other.get("/members/export.csv").get_data(as_text=True)
    assert "Abena" not in other.get("/search?q=abena").get_data(as_text=True)


def test_branch_admin_sees_only_own_branch(app, client, db, grace):
    client.post("/branches/new", data={"name": "Tema", "code": "TEMA", "country": "GH"})
    with tenant_context(grace):
        hq = db.session.query(Branch).filter_by(code="HQ").one().public_id
        tema = db.session.query(Branch).filter_by(code="TEMA").one().public_id
    new_member(client, branch=hq)
    new_member(client, first_name="Efua", phone="0551234567", email="", branch=tema)
    add_person(db, grace, "tema.admin@grace.test", "branch_admin", "TEMA")
    abena = pid(db, grace, first_name="Abena")
    ba = app.test_client()
    login(ba, "tema.admin@grace.test")
    html = ba.get("/members").get_data(as_text=True)
    assert "Efua" in html and "Abena" not in html
    assert ba.get(f"/members/{abena}").status_code == 404
    form = ba.get("/members/new").get_data(as_text=True)
    assert "Tema" in form and "Headquarters" not in form


def test_member_permissions(app, client, db, grace):
    new_member(client)
    add_person(db, grace, "officer@grace.test", "membership_officer")
    add_person(db, grace, "plain@grace.test", "member")
    officer = app.test_client()
    login(officer, "officer@grace.test")
    assert officer.get("/members").status_code == 200
    assert officer.get("/members/export.csv").status_code == 200
    p = pid(db, grace, first_name="Abena")
    assert officer.post(f"/members/{p}/delete").status_code == 403    # officers cannot erase records
    plain = app.test_client()
    login(plain, "plain@grace.test")
    assert plain.get("/members").status_code == 403
    assert "Members" not in plain.get("/dashboard").get_data(as_text=True).split('class="sidebar-nav"')[1].split("</nav>")[0]


def test_dashboard_shows_member_figures_and_birthdays(client, db, grace):
    soon = date.today() + timedelta(days=3)
    new_member(client, date_of_birth=soon.replace(year=1990).isoformat())
    html = client.get("/dashboard").get_data(as_text=True)
    assert "Upcoming birthdays" in html and "Abena Mensah" in html and "New members" in html


def test_upcoming_birthdays_handles_leap_day(db, two_churches):
    with tenant_context(two_churches["grace"]):
        svc.create_member(type("T", (), {"name": "Grace", "settings": {}})(), None, first_name="Leap",
                          last_name="Day", date_of_birth=date(2000, 2, 29))
        db.session.commit()
        svc.upcoming_birthdays(days=366)       # must not raise
