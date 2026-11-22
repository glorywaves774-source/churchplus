"""Public church website (``/c/<church>``) and its settings page.

Only content marked for everyone is shown: public events, sermons and media marked
"Everyone", and announcements for everyone. Visitors can send a prayer request, tell the
church they are new, and register for public events. Forms use CSRF, a hidden honeypot
field and rate limits.
"""
from __future__ import annotations

from datetime import date, datetime, time

from flask import Blueprint, abort, flash, g, redirect, render_template, request, send_file, url_for
from sqlalchemy import or_
from sqlalchemy.orm.attributes import flag_modified

from app.extensions import db, limiter
from app.models import (
    Announcement, Audience, Event, EventStatus, MediaItem, PrayerRequest, RegistrationStatus, Sermon, ServiceType,
    Tenant, TenantStatus, Visibility, Visitor,
)
from app.services import audit, ministry, storage
from app.utils.context import permission_required
from app.utils.tenancy import set_current_tenant, tenant_bypass

bp = Blueprint("website", __name__)
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DEFAULTS = {"enabled": False, "headline": "", "about": "", "giving_text": "", "show_events": True,
            "show_sermons": True, "show_service_times": True, "visitor_form": True, "prayer_form": True}


def site_settings(t: Tenant) -> dict:
    return {**DEFAULTS, **((t.settings or {}).get("website") or {})}


# --------------------------------------------------------------------------- #
# Public site
# --------------------------------------------------------------------------- #
@bp.url_value_preprocessor
def _pop_slug(endpoint, values):
    if values and "slug" in values:
        request.cp_site_slug = values.pop("slug")


@bp.before_request
def _load_site():
    """Runs after the app-wide request setup, so the site's church wins for this request."""
    slug = getattr(request, "cp_site_slug", None)
    if slug is None:
        return
    with tenant_bypass():
        t = db.session.query(Tenant).filter_by(slug=slug).first()
    if t is None or t.status not in (TenantStatus.ACTIVE, TenantStatus.TRIAL) or not site_settings(t)["enabled"]:
        abort(404)
    g.site, g.site_cfg = t, site_settings(t)
    set_current_tenant(t.id)       # every query below is limited to this church


@bp.url_defaults
def _add_slug(endpoint, values):
    if endpoint.startswith("website.site_") and "slug" not in values and getattr(g, "site", None):
        values["slug"] = g.site.slug


def _public_events(limit=None):
    q = (db.session.query(Event).filter(Event.status == EventStatus.PUBLISHED, Event.is_public.is_(True),
                                        Event.starts_at >= datetime.combine(date.today(), time.min))
         .order_by(Event.starts_at))
    return q.limit(limit).all() if limit else q.all()


def _honeypot_tripped() -> bool:
    return bool(request.form.get("website_url"))       # hidden field only bots fill in


@bp.get("/c/<slug>")
def site_home(**_):
    today = date.today()
    ann = (db.session.query(Announcement).filter(Announcement.audience == Audience.PUBLIC, Announcement.starts_on <= today)
           .filter(or_(Announcement.ends_on.is_(None), Announcement.ends_on >= today))
           .order_by(Announcement.is_pinned.desc(), Announcement.starts_on.desc()).limit(4).all())
    times = (db.session.query(ServiceType).filter(ServiceType.is_active.is_(True), ServiceType.weekday.isnot(None))
             .order_by(ServiceType.weekday, ServiceType.start_time).all()) if g.site_cfg["show_service_times"] else []
    sermons = (db.session.query(Sermon).filter(Sermon.visibility == Visibility.PUBLIC).order_by(Sermon.preached_on.desc())
               .limit(3).all()) if g.site_cfg["show_sermons"] else []
    return render_template("website/home.html", ann=ann, times=times, days=DAYS, sermons=sermons,
                           events=_public_events(3) if g.site_cfg["show_events"] else [])


@bp.get("/c/<slug>/events")
def site_events(**_):
    if not g.site_cfg["show_events"]:
        abort(404)
    return render_template("website/events.html", rows=_public_events())


@bp.route("/c/<slug>/events/<public_id>", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def site_event(public_id: str, **_):
    e = (db.session.query(Event).filter_by(public_id=public_id, is_public=True)
         .filter(Event.status.in_([EventStatus.PUBLISHED, EventStatus.COMPLETED])).first_or_404())
    if request.method == "POST":
        if _honeypot_tripped():
            return redirect(url_for("website.site_event", public_id=public_id))
        name, phone = (request.form.get("name") or "").strip()[:160], (request.form.get("phone") or "").strip()[:40]
        if not e.requires_registration or not name or not phone:
            flash("Enter your name and phone number.", "danger")
        else:
            try:
                r = ministry.register(e, name=name, phone=phone, email=(request.form.get("email") or "").strip()[:255] or None,
                                      guests=request.form.get("guests", 0, type=int))
                db.session.commit()
                flash("You're registered. See you there!" if r.status == RegistrationStatus.REGISTERED
                      else "The event is full: you're on the waiting list and we'll let you know.", "success")
            except ValueError as err:
                db.session.rollback()
                flash(str(err), "warning")
        return redirect(url_for("website.site_event", public_id=public_id))
    return render_template("website/event.html", e=e)


@bp.get("/c/<slug>/sermons")
def site_sermons(**_):
    if not g.site_cfg["show_sermons"]:
        abort(404)
    page = (db.session.query(Sermon).filter(Sermon.visibility == Visibility.PUBLIC).order_by(Sermon.preached_on.desc())
            .paginate(page=request.args.get("page", 1, type=int), per_page=12, error_out=False))
    return render_template("website/sermons.html", page=page)


@bp.get("/c/<slug>/sermons/<public_id>")
def site_sermon(public_id: str, **_):
    s = db.session.query(Sermon).filter_by(public_id=public_id, visibility=Visibility.PUBLIC).first_or_404()
    media = [m for m in s.media if m.visibility == Visibility.PUBLIC]
    return render_template("website/sermon.html", s=s, media=media)


@bp.get("/c/<slug>/media/<public_id>")
def site_media(public_id: str, **_):
    item = db.session.query(MediaItem).filter_by(public_id=public_id, visibility=Visibility.PUBLIC).first_or_404()
    if not item.file_key:
        return redirect(item.url) if item.url else abort(404)
    return send_file(storage.open_file(item.file_key), mimetype=item.content_type or "application/octet-stream",
                     as_attachment=request.args.get("inline") != "1", download_name=item.file_name, conditional=True)


@bp.get("/c/<slug>/logo")
def site_logo(**_):
    if not g.site.logo_key:
        abort(404)
    res = send_file(storage.open_file(g.site.logo_key), mimetype="image/png" if g.site.logo_key.endswith(".png") else "image/jpeg")
    res.headers["Cache-Control"] = "public, max-age=3600"
    return res


@bp.route("/c/<slug>/prayer", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def site_prayer(**_):
    if not g.site_cfg["prayer_form"]:
        abort(404)
    if request.method == "POST":
        text = (request.form.get("request") or "").strip()
        if _honeypot_tripped():
            return redirect(url_for("website.site_home"))
        if not text:
            flash("Write your prayer request.", "danger")
        else:
            p = PrayerRequest(name=(request.form.get("name") or "").strip()[:160] or "Website visitor",
                              phone=(request.form.get("phone") or "").strip()[:40] or None,
                              email=(request.form.get("email") or "").strip()[:255] or None, request=text[:4000],
                              category="General", source="website",
                              is_confidential=request.form.get("is_confidential") == "y",
                              wants_contact=request.form.get("wants_contact") == "y")
            db.session.add(p)
            db.session.flush()
            audit.record("prayer.created", entity=p, actor=None, source="website", confidential=p.is_confidential)
            db.session.commit()
            return render_template("website/thanks.html", title="Thank you",
                                   text="We have received your prayer request. Our prayer team will be praying with you.")
    return render_template("website/prayer.html")


@bp.route("/c/<slug>/new-here", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def site_visitor(**_):
    if not g.site_cfg["visitor_form"]:
        abort(404)
    if request.method == "POST":
        f = request.form
        first, last = (f.get("first_name") or "").strip()[:100], (f.get("last_name") or "").strip()[:100]
        if _honeypot_tripped():
            return redirect(url_for("website.site_home"))
        if not first or not last or not (f.get("phone") or f.get("email")):
            flash("Enter your name and a phone number or email.", "danger")
        else:
            v = Visitor(first_name=first, last_name=last, phone=(f.get("phone") or "").strip()[:40] or None,
                        email=(f.get("email") or "").strip()[:255] or None, first_visit_date=date.today(),
                        how_heard="Church website", wants_visit=f.get("wants_visit") == "y",
                        wants_membership=f.get("wants_membership") == "y",
                        prayer_request=(f.get("prayer_request") or "").strip()[:2000] or None)
            db.session.add(v)
            db.session.flush()
            audit.record("visitor.created", entity=v, actor=None, name=f"{first} {last}", source="website")
            db.session.commit()
            return render_template("website/thanks.html", title="Welcome!",
                                   text=f"Thank you, {first}. Someone from {g.site.name} will be in touch soon.")
    return render_template("website/visitor.html")


@bp.get("/c/<slug>/give")
def site_give(**_):
    if not g.site_cfg["giving_text"]:
        abort(404)
    return render_template("website/give.html")


# --------------------------------------------------------------------------- #
# Settings (signed-in staff)
# --------------------------------------------------------------------------- #
@bp.route("/website", methods=["GET", "POST"])
@permission_required("website.view")
def index():
    cfg = site_settings(g.tenant)
    if request.method == "POST":
        if not g.account.can("website.edit"):
            abort(403)
        f = request.form
        new = {"enabled": f.get("enabled") == "y", "headline": (f.get("headline") or "").strip()[:160],
               "about": (f.get("about") or "").strip()[:4000], "giving_text": (f.get("giving_text") or "").strip()[:2000]}
        for k in ("show_events", "show_sermons", "show_service_times", "visitor_form", "prayer_form"):
            new[k] = f.get(k) == "y"
        data = dict(g.tenant.settings or {})
        data["website"] = {**cfg, **new}
        g.tenant.settings = data
        flag_modified(g.tenant, "settings")
        audit.record("website.updated", entity=g.tenant, actor=g.user, enabled=new["enabled"])
        db.session.commit()
        flash("Website saved." + (" It is live." if new["enabled"] else " It is switched off."), "success")
        return redirect(url_for("website.index"))
    counts = {
        "events": db.session.query(Event).filter(Event.is_public.is_(True), Event.status == EventStatus.PUBLISHED).count(),
        "sermons": db.session.query(Sermon).filter(Sermon.visibility == Visibility.PUBLIC).count(),
        "announcements": db.session.query(Announcement).filter(Announcement.audience == Audience.PUBLIC).count(),
    }
    return render_template("website/settings.html", cfg=cfg, counts=counts,
                           url=url_for("website.site_home", slug=g.tenant.slug, _external=True),
                           breadcrumbs=[("Engage", None), ("Church website", None)])
