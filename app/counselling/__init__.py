"""Pastoral counselling: cases, appointments and private session notes.

Access rules (strictest in ChurchPlus)
-------------------------------------
* A case is visible to its counsellor. Standard cases are also visible to people with
  ``counselling.approve`` (oversight); restricted cases are visible to the counsellor only.
* Session *notes* are shown only to the counsellor. Oversight sees dates and status only.
* Every case opened, every note added and every hand-over is written to the audit trail.
* Platform (SaaS) staff never hold church roles, so they never see any of this.
"""
from __future__ import annotations

from datetime import date, datetime, time

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import func

from app.extensions import db
from app.models import CaseStatus, CounsellingCase, CounsellingSession, Member, SessionStatus, TenantUser, User
from app.services import audit, members as member_svc, ministry as svc
from app.utils.context import permission_required

bp = Blueprint("counselling", __name__)
CATEGORIES = ["Pre-marital", "Marriage", "Family", "Grief and loss", "Spiritual", "Addiction", "Career and finance",
              "Youth", "Mental wellbeing", "Other"]


def counsellors():
    rows = (db.session.query(TenantUser).join(User, User.id == TenantUser.user_id)
            .filter(TenantUser.is_suspended.is_(False)).order_by(User.first_name, User.last_name).all())
    return [a for a in rows if a.can("counselling.view") and a.can("counselling.edit")]


def _member(number: str | None):
    if not number:
        return None
    return member_svc.members_query().filter(func.upper(Member.membership_number) == number.strip().upper()).first()


def _require_counsellor(c: CounsellingCase) -> None:
    if not svc.is_counsellor(c):
        abort(403)


@bp.get("/counselling")
@permission_required("counselling.view")
def index():
    q = svc.cases_query()
    status = request.args.get("status", "open")
    if status in {s.value for s in CaseStatus}:
        q = q.filter(CounsellingCase.status == CaseStatus(status))
    mine = bool(request.args.get("mine"))
    if mine:
        q = q.filter(CounsellingCase.counsellor_id == g.account.id)
    rows = q.order_by(CounsellingCase.opened_on.desc()).limit(300).all()
    upcoming = (db.session.query(CounsellingSession).join(CounsellingCase)
                .filter(CounsellingCase.counsellor_id == g.account.id, CounsellingSession.status == SessionStatus.SCHEDULED,
                        CounsellingSession.scheduled_at >= datetime.combine(date.today(), time.min))
                .order_by(CounsellingSession.scheduled_at).limit(10).all())
    return render_template("counselling/index.html", rows=rows, status=status, mine=mine, upcoming=upcoming,
                           breadcrumbs=[("Ministry", None), ("Counselling", None)])


@bp.route("/counselling/new", methods=["GET", "POST"])
@permission_required("counselling.create")
def create():
    people = counsellors()
    if request.method == "POST":
        f = request.form
        member = _member(f.get("number"))
        counsellor = next((a for a in people if a.public_id == f.get("counsellor")), None)
        if counsellor is None and g.account in people:
            counsellor = g.account
        if f.get("number") and member is None:
            flash("No member has that membership number.", "danger")
        elif not member and not (f.get("client_name") or "").strip():
            flash("Enter the membership number or the person's name.", "danger")
        elif counsellor is None:
            flash("Choose a counsellor.", "danger")
        elif counsellor.id != g.account.id and not g.account.can("counselling.approve"):
            flash("Only someone with counselling oversight can open a case for another counsellor.", "danger")
        else:
            try:
                opened = date.fromisoformat(f.get("opened_on") or date.today().isoformat())
            except ValueError:
                opened = date.today()
            c = CounsellingCase(reference=svc.new_case_reference(), member_id=getattr(member, "id", None),
                                client_name=None if member else f["client_name"].strip()[:160],
                                client_phone=None if member else ((f.get("client_phone") or "").strip()[:40] or None),
                                category=f.get("category") if f.get("category") in CATEGORIES else "Other",
                                counsellor_id=counsellor.id, is_restricted=f.get("is_restricted") == "y",
                                opened_on=opened, created_by_id=g.user.id)
            db.session.add(c)
            db.session.flush()
            audit.record("counselling.case_opened", entity=c, actor=g.user, name=c.reference)
            db.session.commit()
            flash(f"Case {c.reference} opened.", "success")
            if svc.cases_query().filter(CounsellingCase.id == c.id).first() is None:
                return redirect(url_for("counselling.index"))
            return redirect(url_for("counselling.detail", public_id=c.public_id))
    return render_template("counselling/form.html", form=request.form, categories=CATEGORIES, counsellors=people,
                           today=date.today(), breadcrumbs=[("Counselling", url_for("counselling.index")), ("New case", None)])


@bp.get("/counselling/<public_id>")
@permission_required("counselling.view")
def detail(public_id: str):
    c = svc.get_case_or_404(public_id)      # writes "case viewed" to the audit trail
    db.session.commit()
    return render_template("counselling/detail.html", c=c, is_counsellor=svc.is_counsellor(c),
                           statuses=list(SessionStatus), counsellors=counsellors(), now=datetime.now(),
                           breadcrumbs=[("Counselling", url_for("counselling.index")), (c.reference, None)])


@bp.post("/counselling/<public_id>/sessions")
@permission_required("counselling.edit")
def add_session(public_id: str):
    c = svc.get_case_or_404(public_id)
    if not (svc.is_counsellor(c) or g.account.can("counselling.approve")):
        abort(403)
    try:
        d = date.fromisoformat(request.form.get("date", ""))
        t = time.fromisoformat(request.form.get("time") or "10:00")
        minutes = max(15, min(int(request.form.get("duration") or 60), 480))
    except ValueError:
        flash("Check the date, time and length.", "danger")
        return redirect(url_for("counselling.detail", public_id=public_id))
    s = CounsellingSession(case_id=c.id, scheduled_at=datetime.combine(d, t), duration_minutes=minutes,
                           location=(request.form.get("location") or "").strip()[:160] or None)
    db.session.add(s)
    audit.record("counselling.session_scheduled", entity=c, actor=g.user, name=c.reference, when=s.scheduled_at.isoformat())
    db.session.commit()
    flash("Appointment added.", "success")
    return redirect(url_for("counselling.detail", public_id=public_id))


@bp.post("/counselling/<public_id>/sessions/<session_id>")
@permission_required("counselling.edit")
def update_session(public_id: str, session_id: str):
    c = svc.get_case_or_404(public_id)
    _require_counsellor(c)           # notes belong to the counsellor alone
    s = db.session.query(CounsellingSession).filter_by(public_id=session_id, case_id=c.id).first_or_404()
    try:
        s.status = SessionStatus(request.form.get("status", s.status.value))
    except ValueError:
        abort(400)
    if "notes" in request.form:
        s.notes = (request.form.get("notes") or "").strip() or None
    if "follow_up" in request.form:
        s.follow_up = (request.form.get("follow_up") or "").strip()[:255] or None
    audit.record("counselling.session_updated", entity=c, actor=g.user, name=c.reference, status=s.status.value)
    db.session.commit()
    flash("Session saved.", "success")
    return redirect(url_for("counselling.detail", public_id=public_id) + "#sessions")


@bp.post("/counselling/<public_id>/status")
@permission_required("counselling.edit")
def set_status(public_id: str):
    c = svc.get_case_or_404(public_id)
    if not (svc.is_counsellor(c) or g.account.can("counselling.approve")):
        abort(403)
    if request.form.get("action") == "close":
        c.status, c.closed_on = CaseStatus.CLOSED, date.today()
    else:
        c.status, c.closed_on = CaseStatus.OPEN, None
    audit.record("counselling.case_" + ("closed" if c.status == CaseStatus.CLOSED else "reopened"), entity=c, actor=g.user, name=c.reference)
    db.session.commit()
    flash("Case closed." if c.status == CaseStatus.CLOSED else "Case reopened.", "success")
    return redirect(url_for("counselling.detail", public_id=public_id))


@bp.post("/counselling/<public_id>/handover")
@permission_required("counselling.approve")
def handover(public_id: str):
    c = svc.get_case_or_404(public_id)
    new = next((a for a in counsellors() if a.public_id == request.form.get("counsellor")), None)
    if new is None:
        abort(400)
    if c.is_restricted and not svc.is_counsellor(c):
        abort(403)
    old = c.counsellor.display_name if c.counsellor else ""
    c.counsellor_id = new.id
    audit.record("counselling.case_handed_over", entity=c, actor=g.user, name=c.reference, previous=old, to=new.display_name)
    db.session.commit()
    flash(f"Case handed over to {new.display_name}.", "success")
    if svc.cases_query().filter(CounsellingCase.id == c.id).first() is None:
        return redirect(url_for("counselling.index"))
    return redirect(url_for("counselling.detail", public_id=public_id))


@bp.post("/counselling/<public_id>/delete")
@permission_required("counselling.delete")
def delete(public_id: str):
    c = svc.get_case_or_404(public_id)
    audit.record("counselling.case_deleted", entity=c, actor=g.user, name=c.reference)
    db.session.delete(c)
    db.session.commit()
    flash("Case deleted.", "success")
    return redirect(url_for("counselling.index"))
