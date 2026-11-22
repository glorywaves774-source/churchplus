"""Stage 5: services, attendance, visitors, departments, clergy and staff."""
from datetime import date, timedelta

import pytest

from app.cli import seed_plans
from app.models import (
    AttendanceRecord, Branch, Department, DepartmentMembership, LeaveRequest, LeaveStatus, Member, MemberStatus,
    Role, Service, ServiceStatus, ServiceType, StaffMember, StaffRecord, TenantUser, Visitor, VisitorFollowUp,
    VisitorStatus,
)
from app.utils.tenancy import tenant_bypass, tenant_context
from tests.conftest import make_user
from tests.test_design_system import assert_csp_clean

PW = "Correct-Horse-9"
TODAY = date.today().isoformat()


def login(client, email, password=PW):
    return client.post("/login", data={"email": email, "password": password})


class q:
    """Run a filtered query inside the church's context (results are read immediately)."""

    def __init__(self, db, tid, model, **f):
        self.db, self.tid, self.model, self.f = db, tid, model, f
        db.session.expire_all()

    def _run(self, fn):
        with tenant_context(self.tid):
            return fn(self.db.session.query(self.model).filter_by(**self.f))

    def _snap(self, obj):
        # Detach a fully loaded copy so later requests (which commit and close the session)
        # cannot expire it under the test.
        if obj is not None:
            self.db.session.expunge(obj)
        return obj

    def one(self):
        return self._run(lambda x: self._snap(x.one()))

    def attached(self):
        return self._run(lambda x: x.one())

    def first(self):
        return self._run(lambda x: self._snap(x.first()))

    def count(self):
        return self._run(lambda x: x.count())

    def __iter__(self):
        return iter(self._run(lambda x: [self._snap(o) for o in x.all()]))


def add_member(client, first, last="Mensah", **kw):
    data = {"first_name": first, "last_name": last, "branch": "", "date_joined": "2020-01-01"}
    data.update(kw)
    client.post("/members/new", data=data, follow_redirects=True)     # follow so the flash message is consumed


def add_person(db, tid, email, role_slug, branch_code=None):
    with tenant_context(tid):
        role = db.session.query(Role).filter_by(slug=role_slug).one()
        branch = db.session.query(Branch).filter_by(code=branch_code).one() if branch_code else None
        user = make_user(email)
        with tenant_bypass():
            db.session.add(user)
            db.session.flush()
        db.session.add(TenantUser(user_id=user.id, roles=[role], branch=branch))
        db.session.commit()


def schedule(client, title="Sunday First Service", when=TODAY, **kw):
    data = {"title": title, "kind": "service", "service_date": when, "status": "scheduled", "branch": "",
            "department": "", "service_type": "", "item_activity": ["Praise", "Sermon"], "item_time": ["07:00", ""],
            "item_led_by": ["Choir", "Rev. Amoah"], "duty_role": ["Usher"], "duty_name": ["Kofi Boateng"]}
    data.update(kw)
    res = client.post("/services/new", data=data)
    client.get("/dashboard")                                           # consume the flash message
    return res


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
# Defaults and services
# --------------------------------------------------------------------------- #
def test_new_church_gets_default_service_types_and_ministries(db, grace):
    assert q(db, grace, ServiceType).count() == 5
    names = {d.name for d in q(db, grace, Department)}
    assert {"Men's Ministry", "Women's Ministry", "Youth Ministry", "Ushering", "Media"} <= names


def test_schedule_service_with_programme_and_duties(client, db, grace):
    res = schedule(client, theme="Walking in Faith", preacher="Rev. Daniel Amoah")
    assert res.status_code == 302
    s = q(db, grace, Service).attached()
    with tenant_context(grace):
        assert [i.activity for i in s.items] == ["Praise", "Sermon"] and s.items[0].start_time.hour == 7
        assert s.assignments[0].role == "Usher" and s.assignments[0].name == "Kofi Boateng"
    html = client.get(f"/services/{s.public_id}").get_data(as_text=True)
    assert "Walking in Faith" in html and "Kofi Boateng" in html
    assert_csp_clean(html)
    assert "Sunday First Service" in client.get("/services").get_data(as_text=True)


def test_edit_service_keeps_values(client, db, grace):
    schedule(client)
    pid = q(db, grace, Service).one().public_id
    client.post(f"/services/{pid}/edit", data={"title": "Thanksgiving Service", "kind": "special", "service_date": TODAY,
                                               "status": "scheduled", "branch": "", "department": "", "service_type": "",
                                               "item_activity": ["Thanksgiving"], "item_time": [""], "item_led_by": [""]})
    s = q(db, grace, Service).attached()
    with tenant_context(grace):
        assert s.title == "Thanksgiving Service" and s.kind.value == "special" and [i.activity for i in s.items] == ["Thanksgiving"]
    assert "Thanksgiving Service" in client.get(f"/services/{pid}/edit").get_data(as_text=True)


def test_service_types_manage(client, db, grace):
    client.post("/services/types", data={"name": "Youth Service", "kind": "service", "weekday": "5", "start_time": "16:00"})
    assert q(db, grace, ServiceType, name="Youth Service").count() == 1


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
def test_register_headcount_checkin_and_trend(client, db, grace):
    for n in ("Abena", "Kwame", "Esi"):
        add_member(client, n)
    schedule(client)
    svc = q(db, grace, Service).one()
    ids = {m.first_name: m.id for m in q(db, grace, Member)}
    html = client.get(f"/attendance/{svc.public_id}").get_data(as_text=True)
    assert "Abena Mensah" in html
    assert_csp_clean(html)

    client.post(f"/attendance/{svc.public_id}", data={"shown": list(map(str, ids.values())),
                                                      "present": [str(ids["Abena"]), str(ids["Kwame"])]})
    assert q(db, grace, AttendanceRecord).count() == 2
    assert q(db, grace, Service).one().status == ServiceStatus.COMPLETED

    # Unticking removes; ids not shown on the page are left alone.
    client.post(f"/attendance/{svc.public_id}", data={"shown": [str(ids["Abena"])], "present": []})
    assert {r.member_id for r in q(db, grace, AttendanceRecord)} == {ids["Kwame"]}

    esi = q(db, grace, Member, first_name="Esi").one()
    res = client.post(f"/attendance/{svc.public_id}/check-in", data={"code": esi.membership_number},
                      headers={"Accept": "application/json"})
    assert res.get_json()["ok"] and "Esi Mensah checked in" in res.get_json()["message"]
    res = client.post(f"/attendance/{svc.public_id}/check-in", data={"code": f"https://x.app/m/{esi.qr_token}"},
                      headers={"Accept": "application/json"})
    assert "already checked in" in res.get_json()["message"]
    res = client.post(f"/attendance/{svc.public_id}/check-in", data={"code": "NOPE-1"}, headers={"Accept": "application/json"})
    assert res.status_code == 404

    client.post(f"/attendance/{svc.public_id}/headcount", data={"men": "40", "women": "55", "children": "20", "visitors": "6"})
    s = q(db, grace, Service).one()
    assert s.headcount_total == 115
    page = client.get("/attendance").get_data(as_text=True)
    assert "121" in page                                       # headcount + visitors
    assert_csp_clean(page)


def test_inactive_member_cannot_check_in(client, db, grace):
    add_member(client, "Yaw")
    schedule(client)
    svc = q(db, grace, Service).one()
    m = q(db, grace, Member).one()
    client.post(f"/members/{m.public_id}/status", data={"status": "inactive", "effective_date": TODAY})
    res = client.post(f"/attendance/{svc.public_id}/check-in", data={"code": m.membership_number},
                      headers={"Accept": "application/json"})
    assert res.status_code == 404 and "not an active member" in res.get_json()["message"]


def test_absentees(client, db, grace):
    add_member(client, "Present")
    add_member(client, "Absent")
    for d in (date.today() - timedelta(days=7), date.today()):
        schedule(client, when=d.isoformat())
    present = q(db, grace, Member, first_name="Present").one().id
    for s in q(db, grace, Service):
        client.post(f"/attendance/{s.public_id}", data={"shown": [str(present)], "present": [str(present)]},
                    follow_redirects=True)
    html = client.get("/attendance/absentees").get_data(as_text=True)
    assert "Absent Mensah" in html and "Present Mensah" not in html


def test_member_profile_shows_attendance(client, db, grace):
    add_member(client, "Abena")
    schedule(client, title="Midweek Service")
    m = q(db, grace, Member).one()
    s = q(db, grace, Service).one()
    client.post(f"/attendance/{s.public_id}/check-in", data={"code": m.membership_number})
    assert "Midweek Service" in client.get(f"/members/{m.public_id}").get_data(as_text=True)


# --------------------------------------------------------------------------- #
# Visitors
# --------------------------------------------------------------------------- #
def test_visitor_registration_follow_up_and_conversion(client, db, grace):
    schedule(client)
    svc = q(db, grace, Service).one()
    res = client.post("/visitors/new", data={"first_name": "Kojo", "last_name": "Badu", "gender": "male",
                                             "phone": "0245550000", "first_visit_date": TODAY, "service": svc.public_id,
                                             "branch": "", "how_heard": "Friend", "assigned_to": "",
                                             "wants_membership": "y", "age_group": ""})
    assert res.status_code == 302
    v = q(db, grace, Visitor).one()
    assert q(db, grace, VisitorFollowUp).one().due_date == date.today() + timedelta(days=2)
    assert q(db, grace, AttendanceRecord, visitor_id=v.id).count() == 1
    html = client.get(f"/visitors/{v.public_id}").get_data(as_text=True)
    assert "Kojo Badu" in html
    assert_csp_clean(html)

    fu = q(db, grace, VisitorFollowUp).one()
    client.post(f"/visitors/{v.public_id}/follow-ups/{fu.public_id}/complete",
                data={"outcome": "Called; he will come on Sunday.", "new_status": "regular"})
    assert q(db, grace, Visitor).one().status == VisitorStatus.REGULAR

    res = client.post(f"/visitors/{v.public_id}/convert")
    assert "/members/" in res.headers["Location"]
    v = q(db, grace, Visitor).one()
    m = q(db, grace, Member).one()
    assert v.status == VisitorStatus.CONVERTED and v.converted_member_id == m.id
    assert m.phone == "0245550000" and m.gender.value == "male"
    rec = q(db, grace, AttendanceRecord).one()
    assert rec.member_id == m.id and rec.visitor_id is None        # visit now counts for the member
    assert "Became members" in client.get("/visitors/retention").get_data(as_text=True)


def test_visitor_list_and_due_follow_ups(client, db, grace):
    client.post("/visitors/new", data={"first_name": "Ama", "last_name": "Kusi", "first_visit_date":
                                       (date.today() - timedelta(days=5)).isoformat(), "service": "", "branch": "",
                                       "assigned_to": "", "how_heard": "", "gender": "", "age_group": ""})
    html = client.get("/visitors").get_data(as_text=True)
    assert "Ama Kusi" in html and "Overdue" in html
    assert "Ama Kusi" in client.get("/search?q=kusi").get_data(as_text=True)


# --------------------------------------------------------------------------- #
# Departments
# --------------------------------------------------------------------------- #
def test_departments_members_and_meetings(client, db, grace):
    add_member(client, "Abena")
    num = q(db, grace, Member).one().membership_number
    client.post("/departments/new", data={"name": "Hospitality", "kind": "department", "leader": num, "branch": "",
                                          "is_active": "y"})
    d = q(db, grace, Department, name="Hospitality").one()
    assert d.leader_member_id and q(db, grace, DepartmentMembership, department_id=d.id).one().role == "Leader"
    add_member(client, "Kwame")
    kwame = q(db, grace, Member, first_name="Kwame").one()
    client.post(f"/departments/{d.public_id}/members", data={"number": kwame.membership_number, "role": "Secretary"})
    html = client.get(f"/departments/{d.public_id}").get_data(as_text=True)
    assert "Kwame Mensah" in html and "Secretary" in html
    assert_csp_clean(html)
    # A departmental meeting's register lists only that department's members.
    schedule(client, title="Hospitality meeting", kind="department", department=d.public_id)
    meeting = q(db, grace, Service, title="Hospitality meeting").one()
    add_member(client, "Outsider")
    client.get("/dashboard")
    reg = client.get(f"/attendance/{meeting.public_id}").get_data(as_text=True)
    assert "Kwame Mensah" in reg and "Outsider Mensah" not in reg
    client.post(f"/departments/{d.public_id}/members", data={"number": "NOPE", "role": "Member"})
    assert q(db, grace, DepartmentMembership, department_id=d.id).count() == 2


# --------------------------------------------------------------------------- #
# Clergy and staff
# --------------------------------------------------------------------------- #
def _add_staff(client, **kw):
    data = {"category": "clergy", "title": "Rev.", "first_name": "Daniel", "last_name": "Amoah", "position": "Senior Pastor",
            "employment_type": "full_time", "status": "active", "department": "", "branch": "", "gender": "male",
            "annual_leave_days": "21", "email": "daniel@grace.test"}
    data.update(kw)
    return client.post("/clergy/new", data=data)


def test_staff_records_and_leave_approval(app, client, db, grace):
    _add_staff(client)
    s = q(db, grace, StaffMember).one()
    assert s.staff_number == "GCI-S0001"
    client.post(f"/clergy/{s.public_id}/records", data={"kind": "promotion", "record_date": TODAY,
                                                        "title": "Inducted as Senior Pastor", "rating": ""})
    assert q(db, grace, StaffRecord).one().title == "Inducted as Senior Pastor"

    monday = date.today() + timedelta(days=(7 - date.today().weekday()))
    client.post(f"/clergy/{s.public_id}/leave", data={"leave_type": "annual", "start_date": monday.isoformat(),
                                                      "end_date": (monday + timedelta(days=6)).isoformat()})
    lr = q(db, grace, LeaveRequest).one()
    assert lr.days == 5 and lr.status == LeaveStatus.PENDING
    # Overlapping request refused
    client.post(f"/clergy/{s.public_id}/leave", data={"leave_type": "sick", "start_date": monday.isoformat(),
                                                      "end_date": monday.isoformat()})
    assert q(db, grace, LeaveRequest).count() == 1

    add_person(db, grace, "hr@grace.test", "hr_officer")
    hr = app.test_client()
    login(hr, "hr@grace.test")
    hr.post(f"/clergy/leave/{lr.public_id}/approve")
    assert q(db, grace, LeaveRequest).one().status == LeaveStatus.APPROVED
    html = client.get(f"/clergy/{s.public_id}").get_data(as_text=True)
    assert "16" in html                                              # 21 - 5 days left
    assert_csp_clean(html)


def test_cannot_approve_own_leave(client, db, grace):
    _add_staff(client, email="owner@grace.test", first_name="Owner")
    s = q(db, grace, StaffMember).one()
    monday = date.today() + timedelta(days=(7 - date.today().weekday()))
    client.post(f"/clergy/{s.public_id}/leave", data={"leave_type": "annual", "start_date": monday.isoformat(),
                                                      "end_date": monday.isoformat()})
    lr = q(db, grace, LeaveRequest).one()
    client.post(f"/clergy/leave/{lr.public_id}/approve")
    assert q(db, grace, LeaveRequest).one().status == LeaveStatus.PENDING


# --------------------------------------------------------------------------- #
# Security
# --------------------------------------------------------------------------- #
def test_other_church_cannot_reach_operations(app, client, db, grace):
    add_member(client, "Abena")
    schedule(client)
    client.post("/visitors/new", data={"first_name": "V", "last_name": "X", "first_visit_date": TODAY, "service": "",
                                       "branch": "", "assigned_to": "", "how_heard": "", "gender": "", "age_group": ""})
    _add_staff(client)
    svc, vis = q(db, grace, Service).one(), q(db, grace, Visitor).one()
    dept, staff = q(db, grace, Department).first(), q(db, grace, StaffMember).one()
    member = q(db, grace, Member).one()
    other = app.test_client()
    login(other, "owner@word.test")
    for path in (f"/services/{svc.public_id}", f"/services/{svc.public_id}/edit", f"/attendance/{svc.public_id}",
                 f"/visitors/{vis.public_id}", f"/departments/{dept.public_id}", f"/clergy/{staff.public_id}"):
        assert other.get(path).status_code == 404, path
    # Check-in into own service with another church's member code finds nothing.
    other.post("/services/new", data={"title": "LW", "kind": "service", "service_date": TODAY, "status": "scheduled",
                                      "branch": "", "department": "", "service_type": ""})
    word_svc = _first_service(db, "LW")
    res = other.post(f"/attendance/{word_svc}/check-in", data={"code": member.membership_number},
                     headers={"Accept": "application/json"})
    assert res.status_code == 404
    res = other.post(f"/attendance/{word_svc}", data={"shown": [str(member.id)], "present": [str(member.id)]})
    with tenant_bypass():
        assert db.session.query(AttendanceRecord).filter_by(member_id=member.id).count() == 0


def _first_service(db, title):
    db.session.expire_all()
    with tenant_bypass():
        return db.session.query(Service).filter_by(title=title).one().public_id


def test_operation_permissions(app, client, db, grace):
    schedule(client)
    svc = q(db, grace, Service).one()
    add_person(db, grace, "usher@grace.test", "department_head")
    add_person(db, grace, "member@grace.test", "member")
    head = app.test_client()
    login(head, "usher@grace.test")
    assert head.get(f"/attendance/{svc.public_id}").status_code == 200
    assert head.get("/clergy").status_code == 403
    assert head.get("/visitors").status_code == 403
    m = app.test_client()
    login(m, "member@grace.test")
    for path in ("/services", "/attendance", "/visitors", "/departments", "/clergy"):
        assert m.get(path).status_code == 403, path


def test_branch_scoping_for_operations(app, client, db, grace):
    client.post("/branches/new", data={"name": "Tema", "code": "TEMA", "country": "GH"})
    with tenant_context(grace):
        hq = db.session.query(Branch).filter_by(code="HQ").one().public_id
        tema = db.session.query(Branch).filter_by(code="TEMA").one().public_id
    schedule(client, title="HQ Service", branch=hq)
    schedule(client, title="Tema Service", branch=tema)
    add_person(db, grace, "tema@grace.test", "branch_admin", "TEMA")
    ba = app.test_client()
    login(ba, "tema@grace.test")
    html = ba.get("/services").get_data(as_text=True)
    assert "Tema Service" in html and "HQ Service" not in html
    assert ba.get(f"/attendance/{_first_service(db, 'HQ Service')}").status_code == 404


def test_operations_pages_render(client, db, grace):
    schedule(client)
    for path in ("/services", "/services/new", "/services/types", "/attendance", "/attendance/absentees", "/visitors",
                 "/visitors/new", "/visitors/retention", "/departments", "/departments/new", "/clergy", "/clergy/new",
                 "/clergy/leave", "/dashboard"):
        res = client.get(path)
        assert res.status_code == 200, path
        assert_csp_clean(res.get_data(as_text=True))
