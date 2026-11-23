"""1.2.6: SMS and WhatsApp say honestly whether they are connected, never fake a send, and can be tested."""
import html as htmllib
import io
import json
import urllib.error

import pytest

from app.models import Message
from app.services import messaging
from tests.test_stage3_security import add_person, login  # noqa: F401
from tests.test_stage8_engage import _plans, add_member, grace  # noqa: F401


@pytest.fixture
def live(app):
    """Behave like the live site: test mode is not allowed to pretend messages were delivered."""
    saved = {k: app.config.get(k) for k in ("ENV_NAME", "SMS_BACKEND", "WHATSAPP_BACKEND", "MAIL_BACKEND",
                                             "HUBTEL_SMS_CLIENT_ID", "HUBTEL_SMS_CLIENT_SECRET", "ARKESEL_API_KEY",
                                             "WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID")}
    app.config.update(ENV_NAME="production", SMS_BACKEND="console", WHATSAPP_BACKEND="console")
    yield app
    app.config.update(saved)


@pytest.fixture(autouse=True)
def _clear():
    messaging.outbox.clear()


def flashes(client):
    with client.session_transaction() as s:
        return " ".join(m for _, m in s.get("_flashes", []))


def test_test_mode_is_fine_on_a_developer_machine(app):
    with app.test_request_context():
        st = messaging.channel_status("sms")
        assert st["ready"] and st["test_mode"] and "Test mode" in st["label"]


def test_live_site_in_test_mode_refuses_instead_of_faking(live):
    with live.test_request_context():
        assert messaging.channel_status("whatsapp")["ready"] is False
        res = messaging.send_whatsapp("233244000001", "Hi")
        assert not res.ok and "isn't connected" in res.error
        assert not messaging.send_sms("233244000001", "Hi", "Grace").ok
        assert messaging.outbox == []                       # nothing pretended to go out


def test_chosen_service_without_its_keys_is_not_connected(live):
    live.config.update(SMS_BACKEND="hubtel", HUBTEL_SMS_CLIENT_ID=None, HUBTEL_SMS_CLIENT_SECRET=None)
    with live.test_request_context():
        st = messaging.channel_status("sms")
        assert not st["ready"] and st["label"] == "Hubtel: not connected"
        assert "HUBTEL_SMS_CLIENT_ID" in st["reason"] and "HUBTEL_SMS_CLIENT_SECRET" in st["reason"]


def test_arkesel_with_key_is_connected_and_sends(live, monkeypatch):
    live.config.update(SMS_BACKEND="arkesel", ARKESEL_API_KEY="test-key")
    calls = []
    def fake_http(url, *, data=None, headers=None, method="POST"):
        calls.append((url, data, headers))
        return {"status": "success", "data": [{"id": "abc123"}]}
    monkeypatch.setattr(messaging, "_http", fake_http)
    with live.test_request_context():
        assert messaging.channel_status("sms") == {"ready": True, "test_mode": False, "label": "Arkesel: connected", "reason": None}
        res = messaging.send_sms("233244000001", "Hello", "GraceChapel")
    assert res.ok and res.ref == "abc123"
    assert calls[0][1] == {"sender": "GraceChapel", "message": "Hello", "recipients": ["233244000001"]}
    assert calls[0][2] == {"api-key": "test-key"}


def test_whatsapp_24_hour_rule_is_explained(live, monkeypatch):
    live.config.update(WHATSAPP_BACKEND="meta", WHATSAPP_TOKEN="t", WHATSAPP_PHONE_NUMBER_ID="123")
    body = json.dumps({"error": {"code": 131047, "message": "Re-engagement message"}}).encode()
    def refuse(*a, **k):
        raise urllib.error.HTTPError("https://graph.facebook.com", 400, "Bad Request", {}, io.BytesIO(body))
    monkeypatch.setattr(messaging, "_http", refuse)
    with live.test_request_context():
        res = messaging.send_whatsapp("233244000001", "Hi")
    assert not res.ok and "last 24 hours" in res.error


def test_expired_token_is_explained(live, monkeypatch):
    live.config.update(WHATSAPP_BACKEND="meta", WHATSAPP_TOKEN="t", WHATSAPP_PHONE_NUMBER_ID="123")
    body = json.dumps({"error": {"code": 190, "message": "Session has expired"}}).encode()
    monkeypatch.setattr(messaging, "_http", lambda *a, **k: (_ for _ in ()).throw(
        urllib.error.HTTPError("https://graph.facebook.com", 401, "Unauthorized", {}, io.BytesIO(body))))
    with live.test_request_context():
        assert "access token" in messaging.send_whatsapp("233244000001", "Hi").error


def test_sending_on_an_unconnected_channel_is_stopped_with_a_clear_message(client, db, grace, live):
    add_member(db, grace, "Ama", whatsapp_opt_in=True)
    page = htmllib.unescape(client.get("/communication/new?channel=whatsapp").get_data(as_text=True))
    assert "WhatsApp isn't connected" in page and "How to connect it" in page
    res = client.post("/communication/new", data={"channel": "whatsapp", "audience": "all", "action": "send",
                                                  "when": "now", "body": "Hi"})
    assert res.status_code == 200 and "Nothing was sent" in htmllib.unescape(res.get_data(as_text=True))
    from app.utils.tenancy import tenant_context
    with tenant_context(grace):
        assert db.session.query(Message).count() == 0


def test_communication_page_shows_real_status(client, grace, live):
    html = client.get("/communication").get_data(as_text=True)
    assert html.count("Not connected") >= 2 and "How to connect SMS and WhatsApp" in html


def test_send_test_message(client, db, grace):
    res = client.post("/communication/test", data={"channel": "sms", "phone": "0244 123 456"})
    assert res.status_code == 302 and "handed to the service for +233244123456" in flashes(client)
    assert messaging.outbox[-1]["to"] == "233244123456" and "is working" in messaging.outbox[-1]["text"]


def test_send_test_reports_why_it_failed(client, grace, live):
    client.post("/communication/test", data={"channel": "whatsapp", "phone": "0244123456"})
    assert "wasn't sent" in flashes(client) and "isn't connected" in flashes(client)
    client.post("/communication/test", data={"channel": "sms", "phone": "12"})
    assert "valid phone number" in flashes(client)


def test_send_test_needs_the_settings_permission(app, db, grace):
    add_person(db, grace, "fin@grace.test", "finance_manager")
    other = app.test_client()
    login(other, "fin@grace.test")
    assert other.post("/communication/test", data={"channel": "sms", "phone": "0244123456"}).status_code == 403
    assert messaging.outbox == []


def test_settings_page_has_connections_and_test_form(client, grace):
    from tests.test_design_system import assert_csp_clean
    html = client.get("/communication/settings").get_data(as_text=True)
    assert_csp_clean(html)
    assert 'id="connections"' in html and 'action="/communication/test"' in html and "Test mode" in html
