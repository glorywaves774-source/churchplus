"""Church messaging: SMS, WhatsApp and email to members (Stage 8).

Providers (chosen by environment variables, credentials never stored in the database):

* SMS – ``console`` (development), ``hubtel`` (Hubtel SMS API) or ``arkesel`` (Arkesel v2).
* WhatsApp – ``console`` or ``meta`` (WhatsApp Business Cloud API). Meta only allows
  free-form text inside a 24-hour conversation window or to people who opted in; members must
  opt in to WhatsApp in their record, and long campaigns should use approved templates.
* Email – the same mail settings as account emails (``MAIL_BACKEND``).

Each person gets their own copy, personalised with ``{first_name}``, ``{name}``, ``{church}``…
Members who opted out of SMS or email are skipped; nobody is messaged twice in one send.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from flask import current_app, g
from sqlalchemy import extract, func
from sqlalchemy.orm.attributes import flag_modified

from app.extensions import db
from app.models import (
    Channel, CounsellingCase, CounsellingSession, DepartmentMembership, Event, EventRegistration, EventStatus,
    Member, MemberStatus, Message, MessageKind, MessageRecipient, MessageStatus, RecipientStatus,
    RegistrationStatus, SessionStatus, Tenant, TenantStatus,
)
from app.models.base import utcnow
from app.services import audit
from app.utils.tenancy import tenant_bypass, tenant_context

outbox: list[dict] = []          # console backends keep messages here (development and tests)

GSM7 = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZ"
           "ÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà^{}\\[~]|€")
PLACEHOLDERS = ("first_name", "last_name", "name", "church", "event", "date", "time", "number")

DEFAULT_SETTINGS = {
    "sms_sender": "",
    "birthday_enabled": False,
    "birthday_channel": "sms",
    "birthday_text": "Happy birthday {first_name}! The family at {church} celebrates you today. God bless you.",
    "event_reminders": True,
    "appointment_reminders": True,
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def settings(tenant: Tenant | None = None) -> dict:
    t = tenant or g.tenant
    return {**DEFAULT_SETTINGS, **((t.settings or {}).get("communication") or {})}


def save_settings(tenant: Tenant, values: dict) -> None:
    data = dict(tenant.settings or {})
    data["communication"] = {**settings(tenant), **values}
    tenant.settings = data
    flag_modified(tenant, "settings")


def sender_id(tenant: Tenant) -> str:
    """SMS sender name: up to 11 letters/digits (network rule)."""
    raw = settings(tenant).get("sms_sender") or tenant.name
    clean = re.sub(r"[^A-Za-z0-9 ]", "", raw).strip()
    return (clean.replace(" ", "")[:11] if len(clean) > 11 else clean) or current_app.config["SMS_DEFAULT_SENDER"]


COUNTRY_CODES = {"GH": "233", "NG": "234", "KE": "254", "ZA": "27", "GB": "44", "US": "1", "CA": "1",
                 "TG": "228", "CI": "225", "LR": "231", "SL": "232", "UG": "256", "TZ": "255", "RW": "250"}


def normalize_phone(phone: str | None, country: str = "GH") -> str | None:
    """Return an international number without '+', e.g. 0244123456 -> 233244123456."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if phone.strip().startswith("+") or phone.strip().startswith("00"):
        digits = digits[2:] if phone.strip().startswith("00") else digits
    elif digits.startswith("0"):
        digits = COUNTRY_CODES.get(country, "233") + digits[1:]
    elif len(digits) <= 9:
        digits = COUNTRY_CODES.get(country, "233") + digits
    return digits if 10 <= len(digits) <= 15 else None


def sms_segments(text: str) -> int:
    """How many SMS parts a message needs (GSM-7: 160/153 chars, otherwise 70/67)."""
    if not text:
        return 0
    gsm = all(ch in GSM7 for ch in text)
    single, multi = (160, 153) if gsm else (70, 67)
    n = len(text)
    return 1 if n <= single else -(-n // multi)


def render(body: str, ctx: dict) -> str:
    def repl(m):
        key = m.group(1)
        return str(ctx.get(key, "")) if key in PLACEHOLDERS else m.group(0)
    return re.sub(r"\{(\w+)\}", repl, body)


def member_context(member: Member, tenant: Tenant) -> dict:
    return {"first_name": member.first_name, "last_name": member.last_name, "name": member.display_name,
            "church": tenant.name, "number": member.membership_number}


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #
@dataclass
class SendResult:
    ok: bool
    ref: str | None = None
    error: str | None = None


def _http(url: str, *, data: dict | None = None, headers: dict | None = None, method: str = "POST") -> dict:
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Content-Type": "application/json", "Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as res:      # noqa: S310 - fixed provider URLs
        raw = res.read().decode() or "{}"
    return json.loads(raw)


def send_sms(to: str, text: str, sender: str) -> SendResult:
    backend = current_app.config["SMS_BACKEND"]
    try:
        if backend == "hubtel":
            cfg = current_app.config
            q = urllib.parse.urlencode({"clientid": cfg["HUBTEL_SMS_CLIENT_ID"], "clientsecret": cfg["HUBTEL_SMS_CLIENT_SECRET"],
                                        "from": sender, "to": to, "content": text})
            res = _http(f"https://smsc.hubtel.com/v1/messages/send?{q}", method="GET")
            return SendResult(res.get("status") in (0, "0", None) and bool(res.get("messageId")), res.get("messageId"),
                              None if res.get("messageId") else str(res)[:250])
        if backend == "arkesel":
            res = _http("https://sms.arkesel.com/api/v2/sms/send", headers={"api-key": current_app.config["ARKESEL_API_KEY"]},
                        data={"sender": sender, "message": text, "recipients": [to]})
            ok = res.get("status") == "success"
            return SendResult(ok, (res.get("data") or [{}])[0].get("id") if ok and isinstance(res.get("data"), list) else None,
                              None if ok else str(res.get("message") or res)[:250])
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:
        return SendResult(False, error=f"Provider error: {e}"[:250])
    outbox.append({"channel": "sms", "to": to, "text": text, "sender": sender})
    current_app.logger.info("SMS (console) to=%s from=%s: %s", to, sender, text)
    return SendResult(True, f"console-{len(outbox)}")


def send_whatsapp(to: str, text: str) -> SendResult:
    if current_app.config["WHATSAPP_BACKEND"] == "meta":
        cfg = current_app.config
        try:
            res = _http(f"https://graph.facebook.com/v20.0/{cfg['WHATSAPP_PHONE_NUMBER_ID']}/messages",
                        headers={"Authorization": f"Bearer {cfg['WHATSAPP_TOKEN']}"},
                        data={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}})
            msgs = res.get("messages") or []
            return SendResult(bool(msgs), msgs[0].get("id") if msgs else None, None if msgs else str(res)[:250])
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            return SendResult(False, error=f"Provider error: {e}"[:250])
    outbox.append({"channel": "whatsapp", "to": to, "text": text})
    current_app.logger.info("WhatsApp (console) to=%s: %s", to, text)
    return SendResult(True, f"console-{len(outbox)}")


def send_email(to: str, subject: str, text: str, church: Tenant) -> SendResult:
    from app.services import mailer
    try:
        mailer.send(to, subject, "church_message", text=text, church=church)
        return SendResult(True, None)
    except Exception as e:  # pragma: no cover - SMTP failures
        return SendResult(False, error=str(e)[:250])


# --------------------------------------------------------------------------- #
# Audiences
# --------------------------------------------------------------------------- #
AUDIENCES = {
    "all": "All active members",
    "branch": "Members of a branch",
    "department": "Members of a department or ministry",
    "gender": "Men or women",
    "age": "Age group",
    "event": "People registered for an event",
    "birthday_week": "Members with a birthday in the next 7 days",
    "numbers": "Phone numbers or emails I type in",
}
AGE_GROUPS = {"children": (0, 12), "youth": (13, 35), "adults": (18, 200), "seniors": (60, 200)}


def _members_for(aud: dict):
    from app.models import Branch, Department
    from app.services import members as member_svc
    q = member_svc.members_query().filter(Member.status == MemberStatus.ACTIVE)
    kind = aud.get("kind", "all")
    if kind == "branch":
        b = db.session.query(Branch).filter_by(public_id=aud.get("branch")).first()
        q = q.filter(Member.branch_id == getattr(b, "id", -1))
    elif kind == "department":
        d = db.session.query(Department).filter_by(public_id=aud.get("department")).first()
        ids = db.session.query(DepartmentMembership.member_id).filter(DepartmentMembership.department_id == getattr(d, "id", -1))
        q = q.filter(Member.id.in_(ids))
    elif kind == "gender":
        q = q.filter(Member.gender == aud.get("gender"))
    elif kind == "age":
        lo, hi = AGE_GROUPS.get(aud.get("age"), (0, 200))
        today = date.today()
        youngest = date(today.year - lo, today.month, min(today.day, 28))
        oldest = date(today.year - hi - 1, today.month, min(today.day, 28))
        q = q.filter(Member.date_of_birth.isnot(None), Member.date_of_birth <= youngest, Member.date_of_birth > oldest)
    elif kind == "birthday_week":
        today = date.today()
        days = [today + timedelta(days=i) for i in range(7)]
        cond = None
        for d in days:
            c = (extract("month", Member.date_of_birth) == d.month) & (extract("day", Member.date_of_birth) == d.day)
            cond = c if cond is None else (cond | c)
        q = q.filter(cond)
    elif kind == "event":
        e = db.session.query(Event).filter_by(public_id=aud.get("event")).first()
        return [], e
    return q.order_by(Member.first_name, Member.last_name).all(), None


def build_recipients(channel: Channel, aud: dict, tenant: Tenant) -> tuple[list[dict], int]:
    """Return (recipients, skipped). Each recipient: {member_id, name, address, context}."""
    out, seen, skipped = [], set(), 0

    def add(address, name=None, member=None, ctx=None):
        nonlocal skipped
        if not address or address in seen:
            skipped += 1 if not address else 0
            return
        seen.add(address)
        out.append({"member_id": getattr(member, "id", None), "name": name, "address": address,
                    "context": ctx or {"church": tenant.name, "name": name or "", "first_name": (name or "").split(" ")[0]}})

    def member_address(m: Member):
        if channel == Channel.EMAIL:
            return None if m.email_opt_out else (m.email or "").strip().lower() or None
        if channel == Channel.WHATSAPP and not m.whatsapp_opt_in:
            return None
        if channel == Channel.SMS and m.sms_opt_out:
            return None
        return normalize_phone(m.phone, tenant.country)

    kind = aud.get("kind", "all")
    if kind == "numbers":
        for raw in re.split(r"[\s,;]+", aud.get("numbers", "")):
            if not raw:
                continue
            if channel == Channel.EMAIL:
                add(raw.lower() if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", raw) else None)
            else:
                add(normalize_phone(raw, tenant.country))
        return out, skipped
    members, event = _members_for(aud)
    if kind == "event":
        if event is None:
            return [], 0
        regs = (db.session.query(EventRegistration).filter(EventRegistration.event_id == event.id,
                                                           EventRegistration.status != RegistrationStatus.CANCELLED).all())
        for r in regs:
            ctx = {"church": tenant.name, "event": event.title, "date": event.starts_at.strftime("%a %d %b"),
                   "time": event.starts_at.strftime("%H:%M")}
            if r.member:
                ctx.update(member_context(r.member, tenant))
                add(member_address(r.member), r.member.display_name, r.member, ctx)
            else:
                ctx.update({"name": r.name or "", "first_name": (r.name or "").split(" ")[0]})
                add((r.email or "").lower() or None if channel == Channel.EMAIL else normalize_phone(r.phone, tenant.country),
                    r.name, None, ctx)
        return out, skipped
    for m in members:
        add(member_address(m), m.display_name, m, member_context(m, tenant))
    return out, skipped


def describe_audience(aud: dict) -> str:
    from app.models import Branch, Department
    kind = aud.get("kind", "all")
    if kind == "branch":
        b = db.session.query(Branch).filter_by(public_id=aud.get("branch")).first()
        return f"Members of {b.name}" if b else "A branch"
    if kind == "department":
        d = db.session.query(Department).filter_by(public_id=aud.get("department")).first()
        return f"Members of {d.name}" if d else "A department"
    if kind == "gender":
        return {"male": "Men", "female": "Women"}.get(aud.get("gender"), "Members")
    if kind == "age":
        return {"children": "Children (0–12)", "youth": "Youth (13–35)", "adults": "Adults (18+)",
                "seniors": "Seniors (60+)"}.get(aud.get("age"), "Age group")
    if kind == "event":
        e = db.session.query(Event).filter_by(public_id=aud.get("event")).first()
        return f"Registered for {e.title}" if e else "An event"
    return AUDIENCES.get(kind, "Members")


# --------------------------------------------------------------------------- #
# Creating and delivering
# --------------------------------------------------------------------------- #
class MessageError(ValueError):
    pass


def create_message(*, tenant: Tenant, channel: Channel, body: str, audience: dict, subject: str | None = None,
                   scheduled_at: datetime | None = None, kind: MessageKind = MessageKind.MANUAL,
                   dedupe_key: str | None = None, actor=None, recipients: list[dict] | None = None) -> Message:
    body = (body or "").strip()
    if not body:
        raise MessageError("Write the message.")
    if channel == Channel.EMAIL and not (subject or "").strip():
        raise MessageError("Emails need a subject.")
    if channel == Channel.SMS and len(body) > 918:
        raise MessageError("SMS messages can be at most 918 characters (6 parts).")
    if recipients is None:
        recipients, _ = build_recipients(channel, audience, tenant)
    if not recipients:
        raise MessageError("Nobody to send to. Check the audience, and that people have phone numbers or emails "
                           "and have not opted out.")
    msg = Message(channel=channel, kind=kind, subject=(subject or "").strip()[:200] or None, body=body,
                  audience=audience, audience_label=describe_audience(audience)[:200] if kind == MessageKind.MANUAL else audience.get("label"),
                  status=MessageStatus.SCHEDULED, scheduled_at=scheduled_at or utcnow(), recipients_count=len(recipients),
                  segments=sms_segments(render(body, recipients[0]["context"])) if channel != Channel.EMAIL else 0,
                  dedupe_key=dedupe_key, created_by_id=getattr(actor, "id", None))
    db.session.add(msg)
    db.session.flush()
    for r in recipients:
        db.session.add(MessageRecipient(message_id=msg.id, member_id=r.get("member_id"), name=(r.get("name") or "")[:160] or None,
                                        address=r["address"][:255], context=r.get("context") or {}))
    db.session.flush()
    return msg


def deliver(msg: Message, tenant: Tenant) -> Message:
    """Send every pending recipient of a message (inside the church's tenant context)."""
    if msg.status not in (MessageStatus.SCHEDULED, MessageStatus.SENDING):
        return msg
    msg.status = MessageStatus.SENDING
    db.session.flush()
    sender = sender_id(tenant)
    for r in db.session.query(MessageRecipient).filter_by(message_id=msg.id, status=RecipientStatus.PENDING):
        text = render(msg.body, {**{"church": tenant.name}, **(r.context or {})})
        if msg.channel == Channel.SMS:
            res = send_sms(r.address, text, sender)
        elif msg.channel == Channel.WHATSAPP:
            res = send_whatsapp(r.address, text)
        else:
            res = send_email(r.address, render(msg.subject or "", r.context or {}), text, tenant)
        r.status = RecipientStatus.SENT if res.ok else RecipientStatus.FAILED
        r.provider_ref, r.error, r.sent_at = res.ref, res.error, utcnow() if res.ok else None
    db.session.flush()
    counts = dict(db.session.query(MessageRecipient.status, func.count(MessageRecipient.id))
                  .filter_by(message_id=msg.id).group_by(MessageRecipient.status).all())
    msg.sent_count = counts.get(RecipientStatus.SENT, 0)
    msg.failed_count = counts.get(RecipientStatus.FAILED, 0)
    msg.status = MessageStatus.SENT if msg.sent_count else MessageStatus.FAILED
    msg.sent_at = utcnow()
    audit.record("message.sent", entity=msg, actor=None, tenant_id=tenant.id, name=msg.audience_label,
                 channel=msg.channel.value, sent=msg.sent_count, failed=msg.failed_count)
    return msg


def process_due(now: datetime | None = None) -> int:
    """Send scheduled messages whose time has come, for every church. Run by the background job."""
    now = now or utcnow()
    with tenant_bypass():
        due = [(m.id, m.tenant_id) for m in db.session.query(Message).filter(
            Message.status.in_([MessageStatus.SCHEDULED, MessageStatus.SENDING]), Message.scheduled_at <= now)]
        tenants = {t.id: t for t in db.session.query(Tenant).filter(Tenant.id.in_({tid for _, tid in due}))} if due else {}
    sent = 0
    for mid, tid in due:
        t = tenants.get(tid)
        if t is None or t.status not in (TenantStatus.ACTIVE, TenantStatus.TRIAL):
            continue
        with tenant_context(tid):
            msg = db.session.get(Message, mid)
            deliver(msg, t)
            db.session.commit()
            sent += 1
    return sent


# --------------------------------------------------------------------------- #
# Automatic reminders (daily job)
# --------------------------------------------------------------------------- #
def _local_today(tenant: Tenant) -> date:
    from zoneinfo import ZoneInfo
    try:
        return datetime.now(ZoneInfo(tenant.timezone or "Africa/Accra")).date()
    except Exception:
        return datetime.now(timezone.utc).date()


def _auto(tenant: Tenant, *, kind: MessageKind, key: str, channel: Channel, body: str, label: str,
          recipients: list[dict]) -> Message | None:
    if not recipients or db.session.query(Message.id).filter_by(dedupe_key=key).first():
        return None
    return create_message(tenant=tenant, channel=channel, body=body, audience={"kind": kind.value, "label": label},
                          kind=kind, dedupe_key=key, recipients=recipients)


def queue_reminders(tenant: Tenant, today: date | None = None) -> list[Message]:
    """Birthday greetings, tomorrow's event reminders and tomorrow's counselling appointments."""
    today = today or _local_today(tenant)
    cfg, made = settings(tenant), []
    if cfg["birthday_enabled"]:
        channel = Channel(cfg["birthday_channel"])
        people = (db.session.query(Member).filter(Member.status == MemberStatus.ACTIVE, Member.allow_birthday_messages.is_(True),
                                                  extract("month", Member.date_of_birth) == today.month,
                                                  extract("day", Member.date_of_birth) == today.day).all())
        recips, seen = [], set()
        for m in people:
            if channel == Channel.EMAIL:
                addr = None if m.email_opt_out else (m.email or "").lower() or None
            elif channel == Channel.WHATSAPP:
                addr = normalize_phone(m.phone, tenant.country) if m.whatsapp_opt_in else None
            else:
                addr = None if m.sms_opt_out else normalize_phone(m.phone, tenant.country)
            if addr and addr not in seen:
                seen.add(addr)
                recips.append({"member_id": m.id, "name": m.display_name, "address": addr, "context": member_context(m, tenant)})
        m = _auto(tenant, kind=MessageKind.BIRTHDAY, key=f"birthday:{today.isoformat()}", channel=channel,
                  body=cfg["birthday_text"], label=f"Birthdays {today:%d %b}", recipients=recips)
        if channel == Channel.EMAIL and m:
            m.subject = f"Happy birthday from {tenant.name}"
        made.append(m)
    tomorrow = today + timedelta(days=1)
    if cfg["event_reminders"]:
        for e in db.session.query(Event).filter(Event.status == EventStatus.PUBLISHED, Event.requires_registration.is_(True),
                                                Event.starts_at >= datetime.combine(tomorrow, time.min),
                                                Event.starts_at <= datetime.combine(tomorrow, time.max)):
            recips, _ = build_recipients(Channel.SMS, {"kind": "event", "event": e.public_id}, tenant)
            made.append(_auto(tenant, kind=MessageKind.EVENT_REMINDER, key=f"event:{e.id}:{tomorrow.isoformat()}",
                              channel=Channel.SMS, label=f"Reminder: {e.title}"[:200], recipients=recips,
                              body="Hello {first_name}, a reminder that {event} is tomorrow, {date} at {time}. See you there! – {church}"))
    if cfg["appointment_reminders"]:
        rows = (db.session.query(CounsellingSession).join(CounsellingCase)
                .filter(CounsellingSession.status == SessionStatus.SCHEDULED,
                        CounsellingSession.scheduled_at >= datetime.combine(tomorrow, time.min),
                        CounsellingSession.scheduled_at <= datetime.combine(tomorrow, time.max)).all())
        recips = []
        for s in rows:
            c = s.case
            phone = c.client_phone or (c.member.phone if c.member else None)
            addr = normalize_phone(phone, tenant.country)
            if addr and not (c.member and c.member.sms_opt_out):
                # Deliberately vague: no category, counsellor or case details in an SMS.
                recips.append({"member_id": c.member_id, "name": None, "address": addr,
                               "context": {"church": tenant.name, "time": s.scheduled_at.strftime("%H:%M"),
                                           "first_name": c.member.first_name if c.member else ""}})
        made.append(_auto(tenant, kind=MessageKind.APPOINTMENT_REMINDER, key=f"appointments:{tomorrow.isoformat()}",
                          channel=Channel.SMS, label=f"Appointments {tomorrow:%d %b}", recipients=recips,
                          body="Hello {first_name}, a reminder of your appointment at {church} tomorrow at {time}."))
    return [m for m in made if m]


def run_daily_reminders() -> int:
    with tenant_bypass():
        tenants = [t for t in db.session.query(Tenant).filter(Tenant.status.in_([TenantStatus.ACTIVE, TenantStatus.TRIAL]))]
    total = 0
    for t in tenants:
        with tenant_context(t.id):
            total += len(queue_reminders(t))
            db.session.commit()
    return total
