"""Stage 8: communication, member portal and church website."""
from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy.orm.attributes import flag_modified

from app.cli import seed_plans
from app.models import (
    Announcement, Audience, Channel, Contribution, CounsellingCase, CounsellingSession, Event, EventRegistration,
    EventStatus, Member, Message, MessageRecipient, MessageStatus, PaymentMethod, PrayerRequest, Role, Sermon,
    Tenant, TenantUser, User, Visibility, Visitor,
)
from app.models.base import utcnow
from app.services import finance as fin, ledger, mailer, members as member_svc, messaging
from app.utils.tenancy import tenant_bypass, tenant_context
from tests.conftest import make_user
from tests.test_design_system import assert_csp_clean

PW = "Correct-Horse-9"
TODAY = date.today()


def login(client, email, password=PW):
    return client.post("/login", data={"email": email, "password": password})


def logout(client):
    client.post("/logout")


def add_member(db, tid, first, last="Mensah", phone="0244000001", email=None, **kw):
    with tenant_context(tid):
        t = db.session.get(Tenant, tid)
        m = member_svc.create_member(t, None, first_name=first, last_name=last, phone=phone, email=email, **kw)
        db.session.commit()
        return m.id


def portal_user(db, tid, member_id, email):
    """A member-portal sign-in linked to a member (as the invitation creates it)."""
    with tenant_context(tid):
        role = db.session.query(Role).filter_by(slug="member").one()
        user = make_user(email)
        with tenant_bypass():
            db.session.add(user)
            db.session.flush()
        db.session.add(TenantUser(user_id=user.id, member_id=member_id, roles=[role]))
        db.session.commit()


def enable_site(db, tid, **extra):
    with tenant_context(tid):
        t = db.session.get(Tenant, tid)
        data = dict(t.settings or {})
        data["website"] = {"enabled": True, "about": "We love God and people.", "giving_text": "MoMo 024 000 0000", **extra}
        t.settings = data
        flag_modified(t, "settings")
        db.session.commit()
        return t.slug


@pytest.fixture(autouse=True)
def _plans(db):
    with tenant_bypass():
        seed_plans()
        db.session.commit()
    messaging.outbox.clear()
    mailer.outbox.clear()


@pytest.fixture()
def grace(client, two_churches):
    login(client, "owner@grace.test")
    return two_churches["grace"]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def test_phone_numbers_and_sms_parts():
    assert messaging.normalize_phone("024 412 3456") == "233244123456"
    assert messaging.normalize_phone("+233 20 123 4567") == "233201234567"
    assert messaging.normalize_phone("0803 123 4567", "NG") == "2348031234567"
    assert messaging.normalize_phone("12") is None
    assert messaging.sms_segments("a" * 160) == 1 and messaging.sms_segments("a" * 161) == 2
    assert messaging.sms_segments("Akwaaba 🙏") == 1 and messaging.sms_segments("🙏" * 71) == 2
    assert messaging.render("Hi {first_name} from {church} {secret}", {"first_name": "Ama", "church": "GC"}) == "Hi Ama from GC {secret}"


@pytest.mark.parametrize("path", ["/communication", "/communication/new", "/communication/announcements",
                                  "/communication/announcements/new", "/communication/settings", "/website"])
def test_engage_pages_render_cleanly(client, grace, path):
    res = client.get(path)
    assert res.status_code == 200, path
    assert_csp_clean(res.get_data(as_text=True))


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #
def test_sms_to_all_members_skips_opt_outs_and_duplicates(client, db, grace):
    add_member(db, grace, "Ama", phone="0244000001")
    add_member(db, grace, "Kofi", phone="+233244000001")            # same number: one SMS
    add_member(db, grace, "Yaw", phone="0244000002", sms_opt_out=True)
    add_member(db, grace, "Esi", phone=None)
    res = client.post("/communication/new", data={"channel": "sms", "audience": "all", "action": "send", "when": "now",
                                                  "body": "Hello {first_name}, service is at 9am. – {church}"},
                      follow_redirects=True)
    assert "Sent to 1 of 1" in res.get_data(as_text=True)
    assert [m["to"] for m in messaging.outbox] == ["233244000001"]
    assert messaging.outbox[0]["text"] == "Hello Ama, service is at 9am. – Grace Chapel International"
    with tenant_context(grace):
        msg = db.session.query(Message).one()
        assert msg.status == MessageStatus.SENT and msg.segments == 1 and msg.sent_count == 1


def test_preview_counts_without_sending(client, db, grace):
    add_member(db, grace, "Ama")
    res = client.post("/communication/new", data={"channel": "sms", "audience": "all", "action": "preview",
                                                  "body": "Hi {first_name}"})
    html = res.get_data(as_text=True)
    assert "Hi Ama" in html and "<strong>1</strong>" in html
    assert messaging.outbox == []


def test_whatsapp_only_to_people_who_opted_in(client, db, grace):
    add_member(db, grace, "Ama", phone="0244000001")
    add_member(db, grace, "Kojo", phone="0244000009", whatsapp_opt_in=True)
    client.post("/communication/new", data={"channel": "whatsapp", "audience": "all", "action": "send", "when": "now",
                                            "body": "Hi"})
    assert [m["to"] for m in messaging.outbox] == ["233244000009"]


def test_email_needs_subject_and_uses_mailer(client, db, grace):
    add_member(db, grace, "Ama", email="ama@example.test")
    res = client.post("/communication/new", data={"channel": "email", "audience": "all", "action": "send", "when": "now",
                                                  "body": "Newsletter"}, follow_redirects=True)
    assert "Emails need a subject" in res.get_data(as_text=True)
    client.post("/communication/new", data={"channel": "email", "audience": "all", "action": "send", "when": "now",
                                            "subject": "News for {first_name}", "body": "Newsletter"})
    assert mailer.outbox[-1].to == "ama@example.test" and mailer.outbox[-1].subject == "News for Ama"


def test_scheduled_message_is_sent_by_background_job(client, db, grace):
    add_member(db, grace, "Ama")
    later = TODAY + timedelta(days=2)
    client.post("/communication/new", data={"channel": "sms", "audience": "numbers", "numbers": "0201112222, 0201112222",
                                            "action": "send", "when": "later", "send_date": later.isoformat(),
                                            "send_time": "07:30", "body": "Fasting begins Monday"})
    with tenant_context(grace):
        msg = db.session.query(Message).one()
        assert msg.status == MessageStatus.SCHEDULED and msg.recipients_count == 1
    assert messaging.process_due() == 0 and messaging.outbox == []
    assert messaging.process_due(utcnow() + timedelta(days=3)) == 1
    assert messaging.outbox[0]["to"] == "233201112222"


def test_messages_are_private_to_each_church(client, db, two_churches):
    add_member(db, two_churches["grace"], "Ama")
    login(client, "owner@grace.test")
    client.post("/communication/new", data={"channel": "sms", "audience": "all", "action": "send", "when": "now", "body": "x"})
    with tenant_context(two_churches["grace"]):
        pid = db.session.query(Message).one().public_id
    logout(client)
    login(client, "owner@word.test")
    assert client.get(f"/communication/messages/{pid}").status_code == 404


def test_birthday_and_appointment_reminders_once_per_day(db, two_churches):
    tid = two_churches["grace"]
    dob = TODAY.replace(year=TODAY.year - 30) if not (TODAY.month == 2 and TODAY.day == 29) else date(1996, 2, 29)
    mid = add_member(db, tid, "Ama", phone="0244000001", date_of_birth=dob)
    add_member(db, tid, "NoGreeting", phone="0244000003", date_of_birth=dob, allow_birthday_messages=False)
    with tenant_context(tid):
        t = db.session.get(Tenant, tid)
        messaging.save_settings(t, {"birthday_enabled": True})
        owner = db.session.query(TenantUser).first()
        case = CounsellingCase(reference="CC-00001", member_id=mid, category="Marriage", counsellor_id=owner.id,
                               opened_on=TODAY)
        db.session.add(case)
        db.session.flush()
        db.session.add(CounsellingSession(case_id=case.id, scheduled_at=datetime.combine(TODAY + timedelta(days=1), time(10))))
        db.session.commit()
    with tenant_context(tid):
        made = messaging.queue_reminders(db.session.get(Tenant, tid), TODAY)
        db.session.commit()
        assert {m.kind.value for m in made} == {"birthday", "appointment_reminder"}
        again = messaging.queue_reminders(db.session.get(Tenant, tid), TODAY)
        assert again == []
    messaging.process_due(utcnow() + timedelta(minutes=1))
    texts = {m["to"]: m["text"] for m in messaging.outbox}
    assert set(texts) == {"233244000001"} or len(messaging.outbox) == 2
    assert any("Happy birthday Ama" in m["text"] for m in messaging.outbox)
    appt = [m["text"] for m in messaging.outbox if "appointment" in m["text"]][0]
    assert "Marriage" not in appt and "10:00" in appt


def test_contact_preferences_saved_from_member_record(client, db, grace):
    mid = add_member(db, grace, "Ama")
    with tenant_context(grace):
        pid = db.session.get(Member, mid).public_id
    client.post(f"/members/{pid}/preferences", data={"email": "y", "whatsapp": "y"})
    with tenant_context(grace):
        m = db.session.get(Member, mid)
        assert m.sms_opt_out and not m.email_opt_out and m.whatsapp_opt_in and not m.allow_birthday_messages


# --------------------------------------------------------------------------- #
# Member portal
# --------------------------------------------------------------------------- #
def test_portal_invite_sends_email_and_does_not_use_a_staff_seat(client, db, grace):
    mid = add_member(db, grace, "Ama", email="ama@example.test")
    with tenant_context(grace):
        pid = db.session.get(Member, mid).public_id
    client.post(f"/portal/invite/{pid}")
    assert mailer.outbox[-1].to == "ama@example.test" and "/set-password/" in mailer.outbox[-1].text
    with tenant_context(grace):
        acc = db.session.query(TenantUser).filter_by(member_id=mid).one()
        assert acc.is_portal_only
        from app.services import limits
        assert limits.check("users").used == 1          # only the owner
    client.post(f"/portal/revoke/{pid}")
    with tenant_context(grace):
        assert db.session.query(TenantUser).filter_by(member_id=mid).count() == 0


def _portal_setup(db, tid):
    mid = add_member(db, tid, "Ama", email="ama@example.test")
    other = add_member(db, tid, "Kofi", phone="0244000005")
    portal_user(db, tid, mid, "ama@example.test")
    with tenant_context(tid):
        ledger.ensure_chart()
        for member_id, amt in ((mid, 150), (other, 999)):
            fin.record_contribution(received_on=TODAY, amount=amt, income_account=ledger.account("4000"),
                                    deposit_account=ledger.account("1000"), method=PaymentMethod.CASH,
                                    member=db.session.get(Member, member_id))
        db.session.add_all([
            Sermon(title="Members Sermon", preacher="Rev", preached_on=TODAY, visibility=Visibility.MEMBERS),
            Sermon(title="Staff Only Notes", preacher="Rev", preached_on=TODAY, visibility=Visibility.STAFF),
            Event(title="Youth Camp", starts_at=datetime.combine(TODAY + timedelta(days=5), time(9)),
                  status=EventStatus.PUBLISHED, requires_registration=True, capacity=10),
            Announcement(title="Fasting week", body="Join us", audience=Audience.MEMBERS, starts_on=TODAY),
            Announcement(title="Staff meeting", body="Leaders only", audience=Audience.STAFF, starts_on=TODAY),
        ])
        db.session.commit()
    return mid


def test_portal_member_sees_only_their_own_things(client, db, two_churches):
    tid = two_churches["grace"]
    mid = _portal_setup(db, tid)
    login(client, "ama@example.test")
    assert client.get("/dashboard").headers["Location"].endswith("/portal")
    home = client.get("/portal").get_data(as_text=True)
    assert "Welcome, Ama" in home and "Fasting week" in home and "Staff meeting" not in home
    assert_csp_clean(home)
    giving = client.get("/portal/giving").get_data(as_text=True)
    assert "150.00" in giving and "999.00" not in giving
    sermons = client.get("/portal/sermons").get_data(as_text=True)
    assert "Members Sermon" in sermons and "Staff Only Notes" not in sermons
    with tenant_context(tid):
        staff_sermon = db.session.query(Sermon).filter_by(title="Staff Only Notes").one().public_id
    assert client.get(f"/portal/sermons/{staff_sermon}").status_code == 404
    for staff_page in ("/members", "/finance", "/prayer", "/communication", "/counselling"):
        assert client.get(staff_page).status_code == 403, staff_page
    for page in ("/portal/profile", "/portal/events", "/portal/prayer", "/portal/family"):
        res = client.get(page)
        assert res.status_code == 200 and assert_csp_clean(res.get_data(as_text=True)) is None


def test_portal_actions(client, db, two_churches):
    tid = two_churches["grace"]
    mid = _portal_setup(db, tid)
    login(client, "ama@example.test")
    client.post("/portal/profile", data={"phone": "0209998888", "city": "Tema", "sms": "y", "email_ok": "y"})
    with tenant_context(tid):
        e = db.session.query(Event).filter_by(title="Youth Camp").one()
        m = db.session.get(Member, mid)
        assert m.phone == "0209998888" and m.city == "Tema" and not m.whatsapp_opt_in
        epid = e.public_id
    client.post(f"/portal/events/{epid}/register", data={"guests": "1"})
    client.post("/portal/prayer", data={"request": "Exams next week", "category": "Exams and studies"})
    with tenant_context(tid):
        reg = db.session.query(EventRegistration).one()
        assert reg.member_id == mid and reg.guests == 1
        p = db.session.query(PrayerRequest).one()
        assert p.member_id == mid and p.source == "portal"
    client.post(f"/portal/events/{epid}/cancel")
    with tenant_context(tid):
        assert db.session.query(EventRegistration).one().status.value == "cancelled"


# --------------------------------------------------------------------------- #
# Church website
# --------------------------------------------------------------------------- #
def test_website_is_off_until_enabled(client, db, two_churches):
    with tenant_context(two_churches["grace"]):
        slug = db.session.get(Tenant, two_churches["grace"]).slug
    assert client.get(f"/c/{slug}").status_code == 404
    enable_site(db, two_churches["grace"])
    res = client.get(f"/c/{slug}")
    assert res.status_code == 200 and "We love God and people." in res.get_data(as_text=True)
    assert_csp_clean(res.get_data(as_text=True))
    assert client.get("/c/no-such-church").status_code == 404


def test_website_shows_only_public_content(client, db, two_churches):
    tid = two_churches["grace"]
    slug = enable_site(db, tid)
    with tenant_context(tid):
        db.session.add_all([
            Event(title="Open Crusade", starts_at=datetime.combine(TODAY + timedelta(days=3), time(18)),
                  status=EventStatus.PUBLISHED, is_public=True, requires_registration=True, capacity=50),
            Event(title="Leaders Retreat", starts_at=datetime.combine(TODAY + timedelta(days=3), time(18)),
                  status=EventStatus.PUBLISHED, is_public=False),
            Sermon(title="Public Word", preacher="Rev", preached_on=TODAY, visibility=Visibility.PUBLIC),
            Sermon(title="Members Word", preacher="Rev", preached_on=TODAY, visibility=Visibility.MEMBERS),
        ])
        db.session.commit()
        private_sermon = db.session.query(Sermon).filter_by(title="Members Word").one().public_id
        crusade = db.session.query(Event).filter_by(title="Open Crusade").one().public_id
    home = client.get(f"/c/{slug}").get_data(as_text=True)
    assert "Open Crusade" in home and "Leaders Retreat" not in home and "Public Word" in home and "Members Word" not in home
    assert client.get(f"/c/{slug}/sermons/{private_sermon}").status_code == 404
    for page in ("events", "sermons", "prayer", "new-here", "give"):
        res = client.get(f"/c/{slug}/{page}")
        assert res.status_code == 200, page
        assert_csp_clean(res.get_data(as_text=True))
    client.post(f"/c/{slug}/events/{crusade}", data={"name": "Visitor Vee", "phone": "0207770000"})
    with tenant_context(tid):
        assert db.session.query(EventRegistration).filter_by(name="Visitor Vee").count() == 1


def test_website_forms_create_prayer_and_visitor_records(client, db, two_churches):
    tid = two_churches["grace"]
    slug = enable_site(db, tid)
    client.post(f"/c/{slug}/prayer", data={"name": "Kwesi", "request": "Healing for my father"})
    client.post(f"/c/{slug}/prayer", data={"name": "Bot", "request": "spam", "website_url": "http://spam"})
    client.post(f"/c/{slug}/new-here", data={"first_name": "Abla", "last_name": "Kumah", "phone": "0241231234",
                                             "wants_visit": "y"})
    with tenant_context(tid):
        assert [p.name for p in db.session.query(PrayerRequest)] == ["Kwesi"]
        assert db.session.query(PrayerRequest).one().source == "website"
        v = db.session.query(Visitor).one()
        assert v.first_name == "Abla" and v.wants_visit and v.how_heard == "Church website"
    with tenant_context(two_churches["word"]):
        assert db.session.query(Visitor).count() == 0


def test_website_shows_its_own_church_even_to_another_churchs_staff(client, db, two_churches):
    slug = enable_site(db, two_churches["grace"])
    with tenant_context(two_churches["grace"]):
        db.session.add(Sermon(title="Grace Public", preacher="Rev", preached_on=TODAY, visibility=Visibility.PUBLIC))
        db.session.commit()
    login(client, "owner@word.test")
    assert "Grace Public" in client.get(f"/c/{slug}/sermons").get_data(as_text=True)
    client.post(f"/c/{slug}/prayer", data={"request": "From a Living Word member"})
    with tenant_context(two_churches["grace"]):
        assert db.session.query(PrayerRequest).count() == 1
    with tenant_context(two_churches["word"]):
        assert db.session.query(PrayerRequest).count() == 0
    # Back in their own workspace, nothing leaked across.
    assert client.get("/dashboard").status_code == 200


def test_website_settings_saved(client, db, grace):
    client.post("/website", data={"enabled": "y", "headline": "Welcome home", "show_events": "y", "prayer_form": "y"})
    with tenant_context(grace):
        cfg = db.session.get(Tenant, grace).settings["website"]
    assert cfg["enabled"] and cfg["headline"] == "Welcome home" and cfg["prayer_form"] and not cfg["visitor_form"]
