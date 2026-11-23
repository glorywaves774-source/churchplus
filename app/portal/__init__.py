"""Member portal: members sign in to see their own giving, events, sermons and to send
prayer requests. A portal account is a church account linked to one member record and
holding no staff permissions, so every staff page stays closed to it (403).
"""
from __future__ import annotations

from datetime import date, datetime, time
from functools import wraps

from flask import Blueprint, abort, flash, g, redirect, render_template, request, send_file, url_for
from sqlalchemy import or_

from app.extensions import db, limiter
from app.models import (
    Announcement, Audience, Event, EventRegistration, EventStatus, MediaItem, Member, Pledge, PrayerRequest,
    RegistrationStatus, Role, Sermon, TenantUser, User, Visibility,
)
from app.services import audit, mailer, members as member_svc, ministry, storage
from app.services import finance as fin
from app.utils.context import church_required, permission_required
from app.utils.tenancy import tenant_bypass

bp = Blueprint("portal", __name__, url_prefix="/portal")
MEMBER_VIS = (Visibility.MEMBERS, Visibility.PUBLIC)


def portal_required(view):
    @wraps(view)
    @church_required
    def wrapped(*args, **kwargs):
        if not g.account.can("portal") or g.account.member is None:
            abort(403)
        g.me = g.account.member
        return view(*args, **kwargs)
    return wrapped


def current_announcements(audiences):
    today = date.today()
    return (db.session.query(Announcement).filter(Announcement.audience.in_(audiences), Announcement.starts_on <= today)
            .filter(or_(Announcement.ends_on.is_(None), Announcement.ends_on >= today))
            .order_by(Announcement.is_pinned.desc(), Announcement.starts_on.desc()).limit(10).all())


def _upcoming_events(limit=20):
    return (db.session.query(Event).filter(Event.status == EventStatus.PUBLISHED,
                                           Event.starts_at >= datetime.combine(date.today(), time.min))
            .order_by(Event.starts_at).limit(limit).all())


@bp.get("")
@portal_required
def index():
    sermons = (db.session.query(Sermon).filter(Sermon.visibility.in_(MEMBER_VIS))
               .order_by(Sermon.preached_on.desc()).limit(3).all())
    mine = {r.event_id for r in db.session.query(EventRegistration).filter_by(member_id=g.me.id)
            .filter(EventRegistration.status != RegistrationStatus.CANCELLED)}
    year = date.today().year
    giving = fin.member_statement(g.me, date(year, 1, 1), date(year, 12, 31))
    return render_template("portal/index.html", me=g.me, announcements=current_announcements([Audience.MEMBERS, Audience.PUBLIC]),
                           events=_upcoming_events(4), mine=mine, sermons=sermons, giving_total=giving["total"], year=year,
                           breadcrumbs=[("My church", None), ("Home", None)])


PROFILE_FIELDS = ("phone", "alt_phone", "email", "address", "city", "region", "digital_address", "occupation",
                  "emergency_name", "emergency_phone", "emergency_relation")


@bp.route("/profile", methods=["GET", "POST"])
@portal_required
def profile():
    m = g.me
    if request.method == "POST":
        f = request.form
        email = (f.get("email") or "").strip().lower()
        if email and ("@" not in email or "." not in email.split("@")[-1]):
            flash("Enter a valid email address.", "danger")
        else:
            changed = []
            for k in PROFILE_FIELDS:
                v = (f.get(k) or "").strip()[:255] or None
                if getattr(m, k) != v:
                    setattr(m, k, v)
                    changed.append(k)
            m.sms_opt_out = f.get("sms") != "y"
            m.email_opt_out = f.get("email_ok") != "y"
            m.whatsapp_opt_in = f.get("whatsapp") == "y"
            m.allow_birthday_messages = f.get("birthday") == "y"
            audit.record("member.self_updated", entity=m, actor=g.user, name=m.display_name, fields=changed)
            db.session.commit()
            flash("Your details have been updated.", "success")
            return redirect(url_for("portal.profile"))
    return render_template("portal/profile.html", m=m, breadcrumbs=[("My church", url_for("portal.index")), ("My details", None)])


@bp.get("/giving")
@portal_required
def giving():
    year = request.args.get("year", date.today().year, type=int)
    year = min(max(year, 2000), date.today().year)
    st = fin.member_statement(g.me, date(year, 1, 1), date(year, 12, 31))
    pledges = db.session.query(Pledge).filter_by(member_id=g.me.id).order_by(Pledge.pledge_date.desc()).all()
    return render_template("portal/giving.html", st=st, year=year, pledges=pledges, paid=fin.pledge_paid, now_year=date.today().year,
                           breadcrumbs=[("My church", url_for("portal.index")), ("My giving", None)])


@bp.get("/events")
@portal_required
def events():
    regs = {r.event_id: r for r in db.session.query(EventRegistration).filter_by(member_id=g.me.id)}
    return render_template("portal/events.html", rows=_upcoming_events(), regs=regs,
                           breadcrumbs=[("My church", url_for("portal.index")), ("Events", None)])


@bp.post("/events/<public_id>/<action>")
@portal_required
def event_action(public_id: str, action: str):
    e = db.session.query(Event).filter_by(public_id=public_id, status=EventStatus.PUBLISHED).first_or_404()
    reg = db.session.query(EventRegistration).filter_by(event_id=e.id, member_id=g.me.id).first()
    if action == "register":
        try:
            r = ministry.register(e, member=g.me, guests=request.form.get("guests", 0, type=int), actor=g.user)
            flash("You're registered." if r.status == RegistrationStatus.REGISTERED else "The event is full: you're on the waiting list.",
                  "success" if r.status == RegistrationStatus.REGISTERED else "info")
        except ValueError as err:
            flash(str(err), "warning")
    elif action == "cancel" and reg and reg.status != RegistrationStatus.CANCELLED:
        reg.status = RegistrationStatus.CANCELLED
        ministry.promote_waitlist(e)
        audit.record("event.registration_cancelled", entity=e, actor=g.user, name=e.title, person=g.me.display_name)
        flash("Your registration is cancelled.", "success")
    else:
        abort(400)
    db.session.commit()
    return redirect(url_for("portal.events"))


@bp.get("/sermons")
@portal_required
def sermons():
    q = db.session.query(Sermon).filter(Sermon.visibility.in_(MEMBER_VIS))
    text = request.args.get("q", "").strip()
    if text:
        q = q.filter(or_(Sermon.title.ilike(f"%{text}%"), Sermon.scripture.ilike(f"%{text}%"), Sermon.series.ilike(f"%{text}%")))
    page = q.order_by(Sermon.preached_on.desc()).paginate(page=request.args.get("page", 1, type=int), per_page=12, error_out=False)
    return render_template("portal/sermons.html", page=page, q=text,
                           breadcrumbs=[("My church", url_for("portal.index")), ("Sermons", None)])


@bp.get("/sermons/<public_id>")
@portal_required
def sermon(public_id: str):
    s = db.session.query(Sermon).filter(Sermon.public_id == public_id, Sermon.visibility.in_(MEMBER_VIS)).first_or_404()
    media = [m for m in s.media if m.visibility in MEMBER_VIS]
    return render_template("portal/sermon.html", s=s, media=media,
                           breadcrumbs=[("My church", url_for("portal.index")), ("Sermons", url_for("portal.sermons")), (s.title, None)])


@bp.get("/media/<public_id>")
@portal_required
def media(public_id: str):
    item = db.session.query(MediaItem).filter(MediaItem.public_id == public_id, MediaItem.visibility.in_(MEMBER_VIS)).first_or_404()
    if not item.file_key:
        return redirect(item.url) if item.url else abort(404)
    inline = request.args.get("inline") == "1"
    return send_file(storage.open_file(item.file_key), mimetype=item.content_type or "application/octet-stream",
                     as_attachment=not inline, download_name=item.file_name, conditional=True)


@bp.route("/prayer", methods=["GET", "POST"])
@portal_required
@limiter.limit("20 per hour", methods=["POST"])
def prayer():
    if request.method == "POST":
        text = (request.form.get("request") or "").strip()
        if not text:
            flash("Write your prayer request.", "danger")
        else:
            from app.prayer import CATEGORIES
            cat = request.form.get("category") if request.form.get("category") in CATEGORIES else "General"
            p = PrayerRequest(member_id=g.me.id, request=text[:5000], category=cat, source="portal",
                              is_confidential=request.form.get("is_confidential") == "y",
                              wants_contact=request.form.get("wants_contact") == "y", branch_id=g.me.branch_id)
            db.session.add(p)
            db.session.flush()
            audit.record("prayer.created", entity=p, actor=g.user, confidential=p.is_confidential, source="portal")
            db.session.commit()
            flash("Thank you. Your prayer request has been received.", "success")
            return redirect(url_for("portal.prayer"))
    from app.prayer import CATEGORIES
    mine = (db.session.query(PrayerRequest).filter_by(member_id=g.me.id).order_by(PrayerRequest.created_at.desc()).limit(30).all())
    return render_template("portal/prayer.html", mine=mine, categories=CATEGORIES,
                           breadcrumbs=[("My church", url_for("portal.index")), ("Prayer", None)])


@bp.get("/family")
@portal_required
def family():
    people = [m for m in (g.me.family.members if g.me.family else []) if m.id != g.me.id]
    return render_template("portal/family.html", fam=g.me.family, people=people,
                           breadcrumbs=[("My church", url_for("portal.index")), ("My family", None)])


# --------------------------------------------------------------------------- #
# Staff: invite / remove portal access (from the member's record)
# --------------------------------------------------------------------------- #
@bp.post("/invite/<public_id>")
@permission_required("members.edit")
@limiter.limit("60 per hour")
def invite(public_id: str):
    m = member_svc.members_query().filter(Member.public_id == public_id).first_or_404()
    email = (m.email or "").strip().lower()
    if not email:
        flash("Add an email address first.", "warning")
        return redirect(url_for("members.profile", public_id=public_id))
    if db.session.query(TenantUser.id).filter_by(member_id=m.id).first():
        flash("This member already has portal access.", "info")
        return redirect(url_for("members.profile", public_id=public_id))
    with tenant_bypass():
        user = db.session.query(User).filter_by(email=email).one_or_none()
    if user is not None:
        account = db.session.query(TenantUser).filter_by(user_id=user.id).first()
        if account is not None:
            if account.member_id is None:
                account.member_id = m.id         # an existing staff account gets the portal too
                audit.record("portal.linked", entity=m, actor=g.user, name=m.display_name)
                db.session.commit()
                flash(f"{user.full_name}'s staff sign-in now also opens the member portal.", "success")
            else:
                flash("That email is already used by another member's portal account.", "danger")
            return redirect(url_for("members.profile", public_id=public_id))
    is_new = user is None
    if is_new:
        user = User(email=email, first_name=m.first_name, last_name=m.last_name)
        with tenant_bypass():
            db.session.add(user)
            db.session.flush()
    role = db.session.query(Role).filter_by(slug="member").first()
    account = TenantUser(user_id=user.id, member_id=m.id, branch_id=m.branch_id, roles=[role] if role else [])
    db.session.add(account)
    db.session.flush()
    audit.record("portal.invited", entity=m, actor=g.user, name=m.display_name)
    db.session.commit()
    from app.auth.team import _invite_link, flash_invite_result
    link = _invite_link(user)
    sent = mailer.try_send(email, f"Your {g.tenant.name} member portal", "portal_invite", member=m, church=g.tenant,
                           link=link, is_new=user.password_hash is None)
    flash_invite_result(sent, user, link, f"Invitation sent to {email}.")
    return redirect(url_for("members.profile", public_id=public_id))


@bp.post("/revoke/<public_id>")
@permission_required("members.edit")
def revoke(public_id: str):
    m = member_svc.members_query().filter(Member.public_id == public_id).first_or_404()
    account = db.session.query(TenantUser).filter_by(member_id=m.id).first_or_404()
    if account.permission_codes():
        account.member_id = None                 # keep the staff account, drop the portal link
    else:
        db.session.delete(account)
    audit.record("portal.revoked", entity=m, actor=g.user, name=m.display_name)
    db.session.commit()
    flash("Portal access removed.", "success")
    return redirect(url_for("members.profile", public_id=public_id))
