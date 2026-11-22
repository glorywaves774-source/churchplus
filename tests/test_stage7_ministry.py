"""Stage 7: events, sermons and media, prayer, counselling, children's ministry."""
import io
from datetime import date, datetime, timedelta

import pytest

from app.cli import seed_plans
from app.models import (
    AuditLog, ChildCheckIn, ChildClass, ChildGuardian, ChildProfile, ClassTeacher, CounsellingCase, CounsellingSession,
    Event, EventRegistration, Family, FamilyRole, Gender, MediaItem, Member, PrayerRequest, RegistrationStatus, Role,
    SafeguardingIncident, Sermon, Tenant, TenantUser, User,
)
from app.services import members as member_svc
from app.services.permissions import DEFAULT_ROLES, ensure_default_roles
from app.utils.tenancy import tenant_bypass, tenant_context
from tests.conftest import make_user
from tests.test_design_system import assert_csp_clean

PW = "Correct-Horse-9"
TODAY = date.today()
PDF = b"%PDF-1.4\n% test sermon notes\n"


def login(client, email, password=PW):
    return client.post("/login", data={"email": email, "password": password})


def logout(client):
    client.post("/logout")


def add_person(db, tid, email, role_slug, first="Test"):
    with tenant_context(tid):
        role = db.session.query(Role).filter_by(slug=role_slug).one()
        user = make_user(email, first_name=first)
        with tenant_bypass():
            db.session.add(user)
            db.session.flush()
        acc = TenantUser(user_id=user.id, roles=[role])
        db.session.add(acc)
        db.session.commit()
        return acc.public_id


def add_member(db, tid, first, last="Mensah", age=30, **kw):
    with tenant_context(tid):
        t = db.session.get(Tenant, tid)
        dob = TODAY.replace(year=TODAY.year - age) - timedelta(days=10)
        m = member_svc.create_member(t, None, first_name=first, last_name=last, date_of_birth=dob, **kw)
        db.session.commit()
        return m.membership_number


def get(db, tid, model, **f):
    db.session.expire_all()
    with tenant_context(tid):
        obj = db.session.query(model).filter_by(**f).order_by(model.id.desc()).first()
        if obj is not None:
            db.session.expunge(obj)
        return obj


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
# Pages
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", [
    "/events", "/events?view=calendar", "/events?when=past", "/events/new", "/sermons", "/sermons/new", "/media",
    "/prayer", "/prayer/new", "/counselling", "/counselling/new", "/children", "/children/register",
    "/children/classes", "/children/safeguarding",
])
def test_ministry_pages_render_cleanly(client, grace, path):
    res = client.get(path)
    assert res.status_code == 200, path
    assert_csp_clean(res.get_data(as_text=True))


def test_new_churches_get_fifteen_roles_including_counsellor_and_children_leader(db, two_churches):
    assert len(DEFAULT_ROLES) == 15
    with tenant_context(two_churches["grace"]):
        slugs = {r.slug for r in db.session.query(Role)}
    assert {"counsellor", "children_leader"} <= slugs


def test_existing_church_receives_new_default_roles(db, two_churches):
    with tenant_context(two_churches["word"]):
        db.session.query(Role).filter_by(slug="counsellor").one().permissions = []
        db.session.delete(db.session.query(Role).filter_by(slug="counsellor").one())
        db.session.commit()
        assert ensure_default_roles() == 1
        db.session.commit()
        assert ensure_default_roles() == 0
        role = db.session.query(Role).filter_by(slug="counsellor").one()
        assert role.is_system and any(p.code == "counselling.view" for p in role.permissions)


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #
def _event(client, **kw):
    data = {"title": "Youth Camp", "category": "Conference", "status": "published",
            "start_date": (TODAY + timedelta(days=10)).isoformat(), "start_time": "09:00",
            "requires_registration": "y", "capacity": "2"}
    data.update(kw)
    return client.post("/events/new", data=data, follow_redirects=True)


def test_event_registration_waitlist_and_promotion(client, db, grace):
    assert _event(client).status_code == 200
    e = get(db, grace, Event, title="Youth Camp")
    for name in ("Ama", "Kofi", "Yaw"):
        client.post(f"/events/{e.public_id}/register", data={"name": name, "phone": "0244000000"})
    with tenant_context(grace):
        regs = {r.name: r.status for r in db.session.query(EventRegistration)}
    assert regs == {"Ama": RegistrationStatus.REGISTERED, "Kofi": RegistrationStatus.REGISTERED,
                    "Yaw": RegistrationStatus.WAITLISTED}
    ama = get(db, grace, EventRegistration, name="Ama")
    client.post(f"/events/{e.public_id}/registrations/{ama.public_id}/cancel")
    assert get(db, grace, EventRegistration, name="Yaw").status == RegistrationStatus.REGISTERED
    page = client.get(f"/events/{e.public_id}").get_data(as_text=True)
    assert "Yaw" in page and "Youth Camp" in page
    assert_csp_clean(page)


def test_draft_event_refuses_registration(client, db, grace):
    _event(client, status="draft", title="Planning")
    e = get(db, grace, Event, title="Planning")
    res = client.post(f"/events/{e.public_id}/register", data={"name": "Esi"}, follow_redirects=True)
    assert "only open for published" in res.get_data(as_text=True)
    assert get(db, grace, EventRegistration, name="Esi") is None


def test_event_calendar_shows_event(client, db, grace):
    when = TODAY + timedelta(days=3)
    _event(client, title="Prayer Retreat", start_date=when.isoformat())
    html = client.get(f"/events?view=calendar&month={when:%Y-%m}").get_data(as_text=True)
    assert "Prayer Retreat" in html


def test_events_are_private_to_each_church(client, db, two_churches):
    login(client, "owner@grace.test")
    _event(client, title="Grace Only")
    e = get(db, two_churches["grace"], Event, title="Grace Only")
    logout(client)
    login(client, "owner@word.test")
    assert client.get(f"/events/{e.public_id}").status_code == 404
    assert "Grace Only" not in client.get("/events").get_data(as_text=True)


# --------------------------------------------------------------------------- #
# Sermons and media
# --------------------------------------------------------------------------- #
def test_sermon_with_uploaded_notes(client, db, grace):
    res = client.post("/sermons/new", data={"title": "Faith that Works", "preacher": "Rev. Asante",
                                            "preached_on": TODAY.isoformat(), "scripture": "James 2:14-26",
                                            "series": "James", "visibility": "public",
                                            "video_url": "https://youtube.com/watch?v=x"}, follow_redirects=True)
    assert res.status_code == 200 and "Faith that Works" in res.get_data(as_text=True)
    s = get(db, grace, Sermon, title="Faith that Works")
    client.post("/media", data={"sermon": s.public_id, "title": "Notes", "visibility": "members",
                                "file": (io.BytesIO(PDF), "notes.pdf", "application/pdf")},
                content_type="multipart/form-data")
    m = get(db, grace, MediaItem, title="Notes")
    assert m.sermon_id == s.id and m.kind.value == "document" and m.file_key.startswith(f"tenants/{grace}/")
    dl = client.get(f"/media/{m.public_id}/download")
    assert dl.status_code == 200 and dl.data == PDF
    assert "Faith that Works" in client.get("/search?q=faith").get_data(as_text=True)
    assert "James 2:14-26" in client.get("/sermons?q=James").get_data(as_text=True)


def test_sermon_rejects_unsafe_links_and_files(client, db, grace):
    res = client.post("/sermons/new", data={"title": "X", "preacher": "Y", "preached_on": TODAY.isoformat(),
                                            "video_url": "javascript:alert(1)"}, follow_redirects=True)
    assert "must start with https" in res.get_data(as_text=True)
    assert get(db, grace, Sermon, title="X") is None
    client.post("/media", data={"title": "Bad", "file": (io.BytesIO(b"MZ\x90\x00"), "run.exe")},
                content_type="multipart/form-data")
    client.post("/media", data={"title": "Fake", "file": (io.BytesIO(b"MZ\x90\x00"), "fake.pdf")},
                content_type="multipart/form-data")
    assert get(db, grace, MediaItem, title="Bad") is None and get(db, grace, MediaItem, title="Fake") is None


def test_media_officer_cannot_open_counselling_or_children(client, db, grace):
    add_person(db, grace, "media@grace.test", "media_officer")
    logout(client)
    login(client, "media@grace.test")
    assert client.get("/sermons").status_code == 200
    assert client.get("/counselling").status_code == 403
    assert client.get("/children").status_code == 403
    assert client.get("/prayer").status_code == 403


def test_media_is_private_to_each_church(client, db, two_churches):
    login(client, "owner@grace.test")
    client.post("/media", data={"title": "Grace PDF", "file": (io.BytesIO(PDF), "a.pdf")}, content_type="multipart/form-data")
    m = get(db, two_churches["grace"], MediaItem, title="Grace PDF")
    logout(client)
    login(client, "owner@word.test")
    assert client.get(f"/media/{m.public_id}/download").status_code == 404


# --------------------------------------------------------------------------- #
# Prayer
# --------------------------------------------------------------------------- #
def test_confidential_prayer_hidden_from_team_until_assigned(client, db, grace):
    assoc = add_person(db, grace, "assoc@grace.test", "associate_pastor", first="Assoc")
    client.post("/prayer/new", data={"name": "Akosua", "request": "Private family matter", "category": "Family",
                                     "is_confidential": "y"})
    client.post("/prayer/new", data={"name": "Kwame", "request": "Job interview on Friday", "category": "Work and career"})
    secret = get(db, grace, PrayerRequest, name="Akosua")
    logout(client)
    login(client, "assoc@grace.test")
    listing = client.get("/prayer").get_data(as_text=True)
    assert "Kwame" in listing and "Akosua" not in listing
    assert client.get(f"/prayer/{secret.public_id}").status_code == 404
    logout(client)
    login(client, "owner@grace.test")
    client.post(f"/prayer/{secret.public_id}/update", data={"assigned_to": assoc, "team": "", "is_confidential": "y"})
    logout(client)
    login(client, "assoc@grace.test")
    page = client.get(f"/prayer/{secret.public_id}")
    assert page.status_code == 200 and "Private family matter" in page.get_data(as_text=True)
    client.post(f"/prayer/{secret.public_id}/update", data={"status": "answered", "note": "Reconciled, praise God"})
    db.session.expire_all()
    p = get(db, grace, PrayerRequest, name="Akosua")
    assert p.status.value == "answered"
    with tenant_context(grace):
        assert db.session.query(AuditLog).filter_by(action="prayer.viewed").count() >= 1


def test_confidential_request_by_non_approver_is_assigned_to_them(client, db, grace):
    add_person(db, grace, "assoc@grace.test", "associate_pastor")
    logout(client)
    login(client, "assoc@grace.test")
    client.post("/prayer/new", data={"name": "Esi", "request": "Health", "is_confidential": "y"})
    p = get(db, grace, PrayerRequest, name="Esi")
    assert p.assigned_to_id is not None
    assert client.get(f"/prayer/{p.public_id}").status_code == 200


# --------------------------------------------------------------------------- #
# Counselling
# --------------------------------------------------------------------------- #
def _open_case(client, counsellor_pid, **kw):
    data = {"client_name": "Efua Owusu", "category": "Marriage", "counsellor": counsellor_pid,
            "opened_on": TODAY.isoformat()}
    data.update(kw)
    return client.post("/counselling/new", data=data, follow_redirects=True)


def test_counselling_access_rules(client, db, grace):
    couns = add_person(db, grace, "couns@grace.test", "counsellor", first="Counsellor")
    add_person(db, grace, "assoc@grace.test", "associate_pastor")
    _open_case(client, couns)
    case = get(db, grace, CounsellingCase, client_name="Efua Owusu")
    assert case.reference.startswith("CC-")
    # Counsellor books a session and writes private notes.
    logout(client)
    login(client, "couns@grace.test")
    assert client.get(f"/counselling/{case.public_id}").status_code == 200
    client.post(f"/counselling/{case.public_id}/sessions", data={"date": TODAY.isoformat(), "time": "14:00", "duration": "60"})
    sess = get(db, grace, CounsellingSession, case_id=case.id)
    client.post(f"/counselling/{case.public_id}/sessions/{sess.public_id}",
                data={"status": "held", "notes": "SECRET-NOTE-123", "follow_up": "Meet both spouses"})
    assert "SECRET-NOTE-123" in client.get(f"/counselling/{case.public_id}").get_data(as_text=True)
    # Associate pastor has counselling.view but is not the counsellor and has no oversight.
    logout(client)
    login(client, "assoc@grace.test")
    assert client.get(f"/counselling/{case.public_id}").status_code == 404
    assert "Efua" not in client.get("/counselling").get_data(as_text=True)
    # Church owner (oversight) sees the case but never the notes, and cannot edit them.
    logout(client)
    login(client, "owner@grace.test")
    page = client.get(f"/counselling/{case.public_id}").get_data(as_text=True)
    assert "Efua Owusu" in page and "SECRET-NOTE-123" not in page
    assert client.post(f"/counselling/{case.public_id}/sessions/{sess.public_id}", data={"notes": "x"}).status_code == 403
    with tenant_context(grace):
        assert db.session.query(AuditLog).filter_by(action="counselling.case_viewed").count() >= 3
    # Case views never appear in the dashboard activity feed.
    assert "Counselling case" not in client.get("/dashboard").get_data(as_text=True)


def test_restricted_case_is_counsellor_only(client, db, grace):
    couns = add_person(db, grace, "couns@grace.test", "counsellor")
    logout(client)
    login(client, "couns@grace.test")
    _open_case(client, couns, client_name="Restricted Person", is_restricted="y")
    case = get(db, grace, CounsellingCase, client_name="Restricted Person")
    assert client.get(f"/counselling/{case.public_id}").status_code == 200
    logout(client)
    login(client, "owner@grace.test")
    assert client.get(f"/counselling/{case.public_id}").status_code == 404
    assert "Restricted Person" not in client.get("/counselling?status=all").get_data(as_text=True)


def test_counsellor_cannot_open_case_for_someone_else(client, db, grace):
    couns = add_person(db, grace, "couns@grace.test", "counsellor")
    other = add_person(db, grace, "couns2@grace.test", "counsellor")
    logout(client)
    login(client, "couns@grace.test")
    res = _open_case(client, other, client_name="Handed")
    assert "counselling oversight" in res.get_data(as_text=True)
    assert get(db, grace, CounsellingCase, client_name="Handed") is None


def test_handover_moves_case_to_new_counsellor(client, db, grace):
    a = add_person(db, grace, "a@grace.test", "counsellor")
    b = add_person(db, grace, "b@grace.test", "counsellor")
    _open_case(client, a, client_name="Moving")
    case = get(db, grace, CounsellingCase, client_name="Moving")
    client.post(f"/counselling/{case.public_id}/handover", data={"counsellor": b})
    logout(client)
    login(client, "a@grace.test")
    assert client.get(f"/counselling/{case.public_id}").status_code == 404
    logout(client)
    login(client, "b@grace.test")
    assert client.get(f"/counselling/{case.public_id}").status_code == 200


def test_platform_admin_without_church_role_cannot_see_counselling(client, db, two_churches):
    from app.models import PlatformRole
    with tenant_bypass():
        admin = make_user("boss@kayron.test")
        admin.platform_role = PlatformRole.SUPER_ADMIN
        db.session.add(admin)
        db.session.commit()
    login(client, "boss@kayron.test")
    res = client.get("/counselling")
    assert res.status_code in (302, 403)
    assert "/counselling" not in (res.headers.get("Location") or "")


def test_counselling_cases_are_private_to_each_church(client, db, two_churches):
    login(client, "owner@grace.test")
    with tenant_context(two_churches["grace"]):
        me = db.session.query(TenantUser).one().public_id
    _open_case(client, me, client_name="Grace Client")
    case = get(db, two_churches["grace"], CounsellingCase, client_name="Grace Client")
    logout(client)
    login(client, "owner@word.test")
    assert client.get(f"/counselling/{case.public_id}").status_code == 404


# --------------------------------------------------------------------------- #
# Children
# --------------------------------------------------------------------------- #
def _family_child(db, tid):
    with tenant_context(tid):
        fam = Family(name="Boateng family")
        db.session.add(fam)
        db.session.commit()
        fam_id = fam.id
    add_member(db, tid, "Abena", "Boateng", age=38, gender=Gender.FEMALE, family_id=fam_id, family_role=FamilyRole.HEAD)
    return add_member(db, tid, "Kojo", "Boateng", age=7, gender=Gender.MALE, family_id=fam_id, family_role=FamilyRole.CHILD)


def test_register_child_with_family_guardians_and_class(client, db, grace):
    client.post("/children/classes", data={"name": "Beginners", "min_age": "4", "max_age": "8", "room": "Room 2"})
    number = _family_child(db, grace)
    res = client.post("/children/register", data={"number": number, "allergies": "Peanuts", "school": "Achimota"},
                      follow_redirects=True)
    html = res.get_data(as_text=True)
    assert "Peanuts" in html and "Abena Boateng" in html and "Mother" in html
    child = get(db, grace, ChildProfile)
    assert get(db, grace, ChildClass, name="Beginners").id == child.class_id
    assert_csp_clean(html)


def test_adult_cannot_be_registered_as_child(client, db, grace):
    number = add_member(db, grace, "Grown", age=25)
    res = client.post("/children/register", data={"number": number}, follow_redirects=True)
    assert "under-18s" in res.get_data(as_text=True)
    assert get(db, grace, ChildProfile) is None


def test_secure_check_in_and_check_out(client, db, grace):
    number = _family_child(db, grace)
    client.post("/children/register", data={"number": number})
    child = get(db, grace, ChildProfile)
    res = client.post(f"/children/{child.public_id}/check-in", data={"dropped_off_by": "Abena"}, follow_redirects=True)
    ci = get(db, grace, ChildCheckIn, child_id=child.id)
    assert ci.pickup_code in res.get_data(as_text=True)
    assert_csp_clean(res.get_data(as_text=True))
    # A second check-in the same day is refused.
    client.post(f"/children/{child.public_id}/check-in", data={})
    with tenant_context(grace):
        assert db.session.query(ChildCheckIn).count() == 1
    mother = get(db, grace, ChildGuardian, child_id=child.id)
    wrong = "0000" if ci.pickup_code != "0000" else "1111"
    res = client.post(f"/children/{child.public_id}/check-out", data={"code": wrong, "guardian": mother.public_id},
                      follow_redirects=True)
    assert "does not match" in res.get_data(as_text=True)
    assert get(db, grace, ChildCheckIn, child_id=child.id).checked_out_at is None
    with tenant_context(grace):
        assert db.session.query(AuditLog).filter_by(action="child.checkout_refused").count() == 1
    # Someone not allowed to collect is refused even with the right code.
    client.post(f"/children/{child.public_id}/guardians", data={"name": "Uncle Kwesi", "phone": "0201111111",
                                                                "relationship": "Uncle"})
    uncle = get(db, grace, ChildGuardian, name="Uncle Kwesi")
    assert uncle.is_authorized_pickup is False
    res = client.post(f"/children/{child.public_id}/check-out", data={"code": ci.pickup_code, "guardian": uncle.public_id},
                      follow_redirects=True)
    assert "not allowed to collect" in res.get_data(as_text=True)
    # Right code + authorised guardian releases the child.
    res = client.post(f"/children/{child.public_id}/check-out", data={"code": ci.pickup_code, "guardian": mother.public_id},
                      follow_redirects=True)
    out = get(db, grace, ChildCheckIn, child_id=child.id)
    assert out.checked_out_at is not None and out.collected_by_name == "Abena Boateng"
    assert "collected by Abena Boateng" in res.get_data(as_text=True)


def test_teacher_clearance_rule():
    t = ClassTeacher(background_check_date=TODAY - timedelta(days=100), safeguarding_training_date=TODAY - timedelta(days=200))
    assert t.is_cleared(TODAY)
    t.safeguarding_training_date = TODAY - timedelta(days=4 * 365)
    assert not t.is_cleared(TODAY)
    t.safeguarding_training_date = None
    assert not t.is_cleared(TODAY)


def test_safeguarding_needs_approval_rights(client, db, grace):
    add_person(db, grace, "kids@grace.test", "children_leader")
    client.post("/children/safeguarding", data={"occurred_on": TODAY.isoformat(), "category": "Injury or accident",
                                                "description": "Fell in the playground, grazed knee", "action_taken": "First aid"})
    inc = get(db, grace, SafeguardingIncident)
    assert inc.reference.startswith("SG-")
    logout(client)
    login(client, "kids@grace.test")
    assert client.get("/children").status_code == 200
    assert client.get("/children/safeguarding").status_code == 403
    assert client.get(f"/children/safeguarding/{inc.public_id}").status_code == 403
    assert "Safeguarding" not in client.get("/children").get_data(as_text=True).split("<main", 1)[-1].split("Children registered")[0]


def test_children_are_private_to_each_church(client, db, two_churches):
    login(client, "owner@grace.test")
    number = _family_child(db, two_churches["grace"])
    client.post("/children/register", data={"number": number})
    child = get(db, two_churches["grace"], ChildProfile)
    logout(client)
    login(client, "owner@word.test")
    assert client.get(f"/children/{child.public_id}").status_code == 404
    assert client.post(f"/children/{child.public_id}/check-in").status_code == 404
