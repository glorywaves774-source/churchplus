"""Transactional email (invitations, password resets, verification).

Backends (MAIL_BACKEND):
* ``console`` – development/testing: logs the message and keeps it in ``outbox``.
* ``smtp``    – any SMTP service (Gmail Workspace, Zoho, Mailgun, SendGrid, Amazon SES SMTP).

Bulk church communication (SMS, WhatsApp, newsletters) is a separate module (Stage 8).
"""
from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage

from flask import current_app, render_template

outbox: list["Mail"] = []   # console backend only


@dataclass
class Mail:
    to: str
    subject: str
    text: str
    html: str | None = None
    meta: dict = field(default_factory=dict)


class MailError(Exception):
    """An email couldn't be sent (email not set up, or the mail server refused or is unreachable)."""


def send(to: str, subject: str, template: str, **context) -> Mail:
    """Render ``templates/email/<template>.txt`` (and ``.html`` if present) and send it.

    Raises MailError with a plain-language reason when the email can't be sent.
    """
    text = render_template(f"email/{template}.txt", **context)
    try:
        html = render_template(f"email/{template}.html", **context)
    except Exception:  # html version is optional
        html = None
    mail = Mail(to=to, subject=subject, text=text, html=html, meta={"template": template})
    backend = current_app.config.get("MAIL_BACKEND", "console")
    if backend == "smtp":
        if not current_app.config.get("MAIL_SMTP_HOST"):
            current_app.logger.error("Email not sent (%s): MAIL_BACKEND is smtp but MAIL_SMTP_HOST is empty", template)
            raise MailError("Email isn't set up for ChurchPlus yet.")
        try:
            _send_smtp(mail)
        except (smtplib.SMTPException, OSError) as exc:
            current_app.logger.exception("Email not sent (%s) via %s", template, current_app.config.get("MAIL_SMTP_HOST"))
            raise MailError("The email server didn't accept the message.") from exc
    else:
        outbox.append(mail)
        current_app.logger.info("Email (console) to=%s subject=%s\n%s", to, subject, text)
    return mail


def try_send(to: str, subject: str, template: str, **context) -> bool:
    """Send an email; return False instead of failing when it can't be sent (the reason is logged)."""
    try:
        send(to, subject, template, **context)
        return True
    except MailError:
        return False


def _send_smtp(mail: Mail) -> None:  # pragma: no cover - needs a mail server
    cfg = current_app.config
    msg = EmailMessage()
    msg["From"] = cfg["MAIL_FROM"]
    msg["To"] = mail.to
    msg["Subject"] = mail.subject
    msg.set_content(mail.text)
    if mail.html:
        msg.add_alternative(mail.html, subtype="html")
    host, port = cfg["MAIL_SMTP_HOST"], int(cfg.get("MAIL_SMTP_PORT", 587))
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        if cfg.get("MAIL_SMTP_STARTTLS", True):
            smtp.starttls(context=ssl.create_default_context())
        if cfg.get("MAIL_SMTP_USERNAME"):
            smtp.login(cfg["MAIL_SMTP_USERNAME"], cfg["MAIL_SMTP_PASSWORD"])
        smtp.send_message(msg)
