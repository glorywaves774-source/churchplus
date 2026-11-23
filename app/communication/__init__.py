"""Communication: SMS, WhatsApp and email to members, announcements and reminder settings."""
from __future__ import annotations

from datetime import date, datetime, timezone

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import func

from app.extensions import db, limiter
from app.models import (
    Announcement, Audience, Branch, Channel, Department, Event, EventStatus, Member, Message, MessageKind,
    MessageRecipient, MessageStatus,
)
from app.models.base import utcnow
from app.services import audit, members as member_svc, messaging
from app.services.messaging import MessageError
from app.utils.context import permission_required

bp = Blueprint("communication", __name__)


def _local_to_utc(d: str, t: str) -> datetime | None:
    from zoneinfo import ZoneInfo
    if not d:
        return None
    try:
        local = datetime.fromisoformat(f"{d}T{t or '08:00'}")
        return local.replace(tzinfo=ZoneInfo(g.tenant.timezone or "Africa/Accra")).astimezone(timezone.utc)
    except ValueError:
        return None


def _audience_from_form(f) -> dict:
    kind = f.get("audience", "all")
    if kind not in messaging.AUDIENCES:
        kind = "all"
    aud = {"kind": kind}
    for key in ("branch", "department", "gender", "age", "event", "numbers"):
        if f.get(key):
            aud[key] = f.get(key)[:5000]
    return aud


def _choices():
    return {
        "branches": member_svc.allowed_branches(),
        "departments": db.session.query(Department).filter_by(is_active=True).order_by(Department.name).all(),
        "events": (db.session.query(Event).filter(Event.status == EventStatus.PUBLISHED)
                   .order_by(Event.starts_at.desc()).limit(30).all()),
        "audiences": messaging.AUDIENCES,
    }


@bp.get("/communication")
@permission_required("communication.view")
def index():
    rows = db.session.query(Message).order_by(Message.created_at.desc()).limit(50).all()
    month_start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    sms_month = (db.session.query(func.coalesce(func.sum(Message.sent_count * Message.segments), 0))
                 .filter(Message.channel == Channel.SMS, Message.sent_at >= month_start).scalar())
    reachable = member_svc.members_query().filter(Member.phone.isnot(None), Member.sms_opt_out.is_(False)).count()
    today = date.today()
    announcements = (db.session.query(Announcement).filter(Announcement.starts_on <= today)
                     .filter((Announcement.ends_on.is_(None)) | (Announcement.ends_on >= today))
                     .order_by(Announcement.is_pinned.desc(), Announcement.starts_on.desc()).limit(5).all())
    return render_template("communication/index.html", rows=rows, sms_month=int(sms_month or 0), reachable=reachable,
                           announcements=announcements, cfg=messaging.settings(),
                           providers={c: messaging.channel_status(c) for c in ("sms", "whatsapp", "email")},
                           breadcrumbs=[("Engage", None), ("Communication", None)])


@bp.route("/communication/new", methods=["GET", "POST"])
@permission_required("communication.create")
@limiter.limit("60 per hour", methods=["POST"])
def compose():
    f = request.form
    preview = None
    if request.method == "POST":
        try:
            channel = Channel(f.get("channel", "sms"))
        except ValueError:
            abort(400)
        aud = _audience_from_form(f)
        recips, skipped = messaging.build_recipients(channel, aud, g.tenant)
        if f.get("action") == "preview" or not recips:
            sample = recips[0]["context"] if recips else {"first_name": "Ama", "name": "Ama Mensah", "church": g.tenant.name}
            text = messaging.render(f.get("body", ""), {"church": g.tenant.name, **sample})
            preview = {"count": len(recips), "skipped": skipped, "sample": text,
                       "segments": messaging.sms_segments(text) if channel != Channel.EMAIL else 0,
                       "label": messaging.describe_audience(aud)}
            if not recips and f.get("action") != "preview":
                flash("Nobody to send to. Check the audience, and that people have phone numbers or emails and have not opted out.", "warning")
        elif not messaging.channel_status(channel.value)["ready"]:
            flash(messaging.channel_status(channel.value)["reason"] + " Nothing was sent.", "danger")
        else:
            when = _local_to_utc(f.get("send_date", ""), f.get("send_time", "")) if f.get("when") == "later" else None
            if f.get("when") == "later" and (when is None or when <= utcnow()):
                flash("Choose a date and time in the future.", "danger")
            else:
                try:
                    msg = messaging.create_message(tenant=g.tenant, channel=channel, body=f.get("body", ""),
                                                   subject=f.get("subject"), audience=aud, scheduled_at=when,
                                                   actor=g.user, recipients=recips)
                except MessageError as e:
                    flash(str(e), "danger")
                else:
                    audit.record("message.created", entity=msg, actor=g.user, name=msg.audience_label,
                                 channel=channel.value, recipients=msg.recipients_count)
                    from flask import current_app
                    if when is None and msg.recipients_count <= current_app.config["MESSAGES_SEND_NOW_LIMIT"]:
                        messaging.deliver(msg, g.tenant)
                        flash(f"Sent to {msg.sent_count} of {msg.recipients_count} people.", "success" if not msg.failed_count else "warning")
                    elif when is None:
                        flash(f"Sending to {msg.recipients_count} people in the background. Check back in a few minutes.", "info")
                    else:
                        flash("Message scheduled.", "success")
                    db.session.commit()
                    return redirect(url_for("communication.detail", public_id=msg.public_id))
    initial = {"channel": request.args.get("channel", "sms"), "audience": request.args.get("audience", "all"),
               "event": request.args.get("event", "")}
    return render_template("communication/compose.html", form=f, initial=initial, preview=preview,
                           channels={c: messaging.channel_status(c) for c in ("sms", "whatsapp", "email")},
                           placeholders=messaging.PLACEHOLDERS, today=date.today(), **_choices(),
                           breadcrumbs=[("Communication", url_for("communication.index")), ("New message", None)])


@bp.get("/communication/messages/<public_id>")
@permission_required("communication.view")
def detail(public_id: str):
    msg = db.session.query(Message).filter_by(public_id=public_id).first_or_404()
    recips = (db.session.query(MessageRecipient).filter_by(message_id=msg.id).order_by(MessageRecipient.status.desc(),
                                                                                      MessageRecipient.id).limit(1000).all())
    return render_template("communication/detail.html", m=msg, recips=recips,
                           breadcrumbs=[("Communication", url_for("communication.index")), (msg.audience_label or "Message", None)])


@bp.post("/communication/messages/<public_id>/<action>")
@permission_required("communication.create")
def message_action(public_id: str, action: str):
    msg = db.session.query(Message).filter_by(public_id=public_id).first_or_404()
    if msg.status != MessageStatus.SCHEDULED:
        flash("This message has already been sent.", "info")
    elif action == "cancel":
        msg.status = MessageStatus.CANCELLED
        audit.record("message.cancelled", entity=msg, actor=g.user, name=msg.audience_label)
        flash("Message cancelled.", "success")
    elif action == "send":
        messaging.deliver(msg, g.tenant)
        flash(f"Sent to {msg.sent_count} of {msg.recipients_count} people.", "success")
    else:
        abort(400)
    db.session.commit()
    return redirect(url_for("communication.detail", public_id=public_id))


# --------------------------------------------------------------------------- #
# Announcements
# --------------------------------------------------------------------------- #
@bp.get("/communication/announcements")
@permission_required("communication.view")
def announcements():
    rows = db.session.query(Announcement).order_by(Announcement.is_pinned.desc(), Announcement.starts_on.desc()).limit(200).all()
    return render_template("communication/announcements.html", rows=rows, today=date.today(),
                           breadcrumbs=[("Communication", url_for("communication.index")), ("Announcements", None)])


@bp.route("/communication/announcements/new", methods=["GET", "POST"], endpoint="new_announcement")
@bp.route("/communication/announcements/<public_id>/edit", methods=["GET", "POST"])
@permission_required("communication.create")
def edit_announcement(public_id: str | None = None):
    a = db.session.query(Announcement).filter_by(public_id=public_id).first_or_404() if public_id else None
    if request.method == "POST":
        f = request.form
        try:
            start = date.fromisoformat(f.get("starts_on") or date.today().isoformat())
            end = date.fromisoformat(f["ends_on"]) if f.get("ends_on") else None
            aud = Audience(f.get("audience", "members"))
        except ValueError:
            flash("Check the dates.", "danger")
        else:
            if not f.get("title", "").strip() or not f.get("body", "").strip():
                flash("Enter a title and the announcement.", "danger")
            elif end and end < start:
                flash("The end date is before the start date.", "danger")
            else:
                a = a or Announcement(created_by_id=g.user.id)
                a.title, a.body = f["title"].strip()[:160], f["body"].strip()
                a.audience, a.starts_on, a.ends_on, a.is_pinned = aud, start, end, f.get("is_pinned") == "y"
                db.session.add(a)
                db.session.flush()
                audit.record("announcement.saved", entity=a, actor=g.user, name=a.title)
                db.session.commit()
                flash("Announcement saved.", "success")
                return redirect(url_for("communication.announcements"))
    return render_template("communication/announcement_form.html", a=a, form=request.form, today=date.today(),
                           breadcrumbs=[("Communication", url_for("communication.index")),
                                        ("Announcements", url_for("communication.announcements")), ("Edit" if a else "New", None)])


@bp.post("/communication/announcements/<public_id>/delete")
@permission_required("communication.delete")
def delete_announcement(public_id: str):
    a = db.session.query(Announcement).filter_by(public_id=public_id).first_or_404()
    audit.record("announcement.deleted", entity=a, actor=g.user, name=a.title)
    db.session.delete(a)
    db.session.commit()
    flash("Announcement deleted.", "success")
    return redirect(url_for("communication.announcements"))


# --------------------------------------------------------------------------- #
# Settings and member preferences
# --------------------------------------------------------------------------- #
@bp.route("/communication/settings", methods=["GET", "POST"])
@permission_required("communication.manage_settings")
def comm_settings():
    if request.method == "POST":
        f = request.form
        sender = "".join(ch for ch in (f.get("sms_sender") or "") if ch.isalnum() or ch == " ").strip()[:11]
        channel = f.get("birthday_channel") if f.get("birthday_channel") in ("sms", "whatsapp", "email") else "sms"
        text = (f.get("birthday_text") or "").strip()[:600] or messaging.DEFAULT_SETTINGS["birthday_text"]
        messaging.save_settings(g.tenant, {"sms_sender": sender, "birthday_enabled": f.get("birthday_enabled") == "y",
                                           "birthday_channel": channel, "birthday_text": text,
                                           "event_reminders": f.get("event_reminders") == "y",
                                           "appointment_reminders": f.get("appointment_reminders") == "y"})
        audit.record("communication.settings_updated", entity=g.tenant, actor=g.user)
        db.session.commit()
        flash("Settings saved.", "success")
        return redirect(url_for("communication.comm_settings"))
    return render_template("communication/settings.html", cfg=messaging.settings(), sender=messaging.sender_id(g.tenant),
                           channels={c: messaging.channel_status(c) for c in ("sms", "whatsapp")},
                           breadcrumbs=[("Communication", url_for("communication.index")), ("Settings", None)])


@bp.post("/communication/test")
@permission_required("communication.manage_settings")
@limiter.limit("10 per hour")
def send_test():
    """Send one short test message to a number the admin types, to prove SMS or WhatsApp is connected."""
    channel = request.form.get("channel")
    if channel not in ("sms", "whatsapp"):
        abort(400)
    number = messaging.normalize_phone(request.form.get("phone"), g.tenant.country or "GH")
    if not number:
        flash("Enter a valid phone number, e.g. 0244 123 456.", "danger")
        return redirect(url_for("communication.comm_settings"))
    text = f"Test message from {g.tenant.name} on ChurchPlus. If you received this, {channel.upper() if channel == 'sms' else 'WhatsApp'} is working."
    res = (messaging.send_sms(number, text, messaging.sender_id(g.tenant)) if channel == "sms"
           else messaging.send_whatsapp(number, text))
    audit.record("communication.test_sent", entity=g.tenant, actor=g.user, channel=channel, ok=res.ok)
    db.session.commit()
    label = "SMS" if channel == "sms" else "WhatsApp"
    if res.ok:
        flash(f"Test {label} handed to the service for +{number}. It should arrive within a minute.", "success")
    else:
        flash(f"The test {label} wasn't sent. {res.error or ''}".strip(), "danger")
    return redirect(url_for("communication.comm_settings"))


@bp.post("/members/<public_id>/preferences")
@permission_required("members.edit")
def member_preferences(public_id: str):
    m = member_svc.members_query().filter(Member.public_id == public_id).first_or_404()
    m.sms_opt_out = request.form.get("sms") != "y"
    m.email_opt_out = request.form.get("email") != "y"
    m.whatsapp_opt_in = request.form.get("whatsapp") == "y"
    m.allow_birthday_messages = request.form.get("birthday") == "y"
    audit.record("member.preferences_updated", entity=m, actor=g.user, name=m.display_name)
    db.session.commit()
    flash("Contact preferences saved.", "success")
    return redirect(url_for("members.profile", public_id=public_id))
