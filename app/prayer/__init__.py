"""Prayer requests: capture, assign to the prayer team, follow up, mark answered.

Confidential requests are visible only to the person they are assigned to and to people
with ``prayer.approve`` (see :mod:`app.services.ministry`).
"""
from __future__ import annotations

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import func, or_

from app.extensions import db
from app.models import Department, Member, PrayerRequest, PrayerStatus, PrayerUpdate, TenantUser, User
from app.services import audit, members as member_svc, ministry as svc
from app.utils.context import permission_required

bp = Blueprint("prayer", __name__)
CATEGORIES = ["General", "Healing", "Family", "Marriage", "Finances", "Work and career", "Travel",
              "Salvation", "Exams and studies", "Thanksgiving", "Bereavement", "Other"]


def team_accounts():
    """Church accounts that can be given prayer requests (they must be able to see prayer)."""
    rows = (db.session.query(TenantUser).join(User, User.id == TenantUser.user_id)
            .filter(TenantUser.is_suspended.is_(False)).order_by(User.first_name, User.last_name).all())
    return [a for a in rows if a.can("prayer.view")]


def _member(number: str | None):
    if not number:
        return None
    return member_svc.members_query().filter(func.upper(Member.membership_number) == number.strip().upper()).first()


@bp.get("/prayer")
@permission_required("prayer.view")
def index():
    q = svc.prayer_query()
    status = request.args.get("status", "active")
    if status == "active":
        q = q.filter(PrayerRequest.status.in_([PrayerStatus.OPEN, PrayerStatus.PRAYING]))
    elif status in {s.value for s in PrayerStatus}:
        q = q.filter(PrayerRequest.status == PrayerStatus(status))
    if request.args.get("mine"):
        q = q.filter(PrayerRequest.assigned_to_id == g.account.id)
    cat = request.args.get("category", "")
    if cat:
        q = q.filter(PrayerRequest.category == cat)
    text = request.args.get("q", "").strip()
    if text:
        q = q.outerjoin(Member, Member.id == PrayerRequest.member_id).filter(
            or_(PrayerRequest.request.ilike(f"%{text}%"), PrayerRequest.name.ilike(f"%{text}%"),
                Member.first_name.ilike(f"%{text}%"), Member.last_name.ilike(f"%{text}%")))
    page = q.order_by(PrayerRequest.created_at.desc()).paginate(page=request.args.get("page", 1, type=int), per_page=25, error_out=False)
    base = svc.prayer_query()
    counts = {
        "open": base.filter(PrayerRequest.status == PrayerStatus.OPEN).count(),
        "praying": base.filter(PrayerRequest.status == PrayerStatus.PRAYING).count(),
        "answered": base.filter(PrayerRequest.status == PrayerStatus.ANSWERED).count(),
        "mine": base.filter(PrayerRequest.assigned_to_id == g.account.id,
                            PrayerRequest.status.in_([PrayerStatus.OPEN, PrayerStatus.PRAYING])).count(),
    }
    params = {k: v for k, v in request.args.items() if k != "page" and v}
    return render_template("prayer/index.html", page=page, status=status, category=cat, q=text, counts=counts,
                           categories=CATEGORIES, params=params, mine=bool(request.args.get("mine")),
                           breadcrumbs=[("Ministry", None), ("Prayer requests", None)])


@bp.route("/prayer/new", methods=["GET", "POST"])
@permission_required("prayer.create")
def create():
    if request.method == "POST":
        f = request.form
        text = (f.get("request") or "").strip()
        member = _member(f.get("number"))
        if f.get("number") and member is None:
            flash("No member has that membership number.", "danger")
        elif not text:
            flash("Write the prayer request.", "danger")
        else:
            p = PrayerRequest(request=text[:5000], member_id=getattr(member, "id", None),
                              name=None if member else ((f.get("name") or "").strip()[:160] or None),
                              phone=None if member else ((f.get("phone") or "").strip()[:40] or None),
                              category=f.get("category") if f.get("category") in CATEGORIES else "General",
                              is_confidential=f.get("is_confidential") == "y", wants_contact=f.get("wants_contact") == "y",
                              source="staff", branch_id=getattr(member, "branch_id", None) or getattr(g.account, "branch_id", None))
            _apply_assignment(p, f)
            # A confidential request with nobody assigned is visible only to approvers; if the
            # person recording it cannot approve, assign it to them so they don't lose it.
            if p.is_confidential and not p.assigned_to_id and not g.account.can("prayer.approve"):
                p.assigned_to_id = g.account.id
            db.session.add(p)
            db.session.flush()
            audit.record("prayer.created", entity=p, actor=g.user, confidential=p.is_confidential)
            db.session.commit()
            flash("Prayer request saved.", "success")
            return redirect(url_for("prayer.detail", public_id=p.public_id))
    return render_template("prayer/form.html", form=request.form, categories=CATEGORIES, accounts=team_accounts(),
                           teams=db.session.query(Department).order_by(Department.name).all(),
                           breadcrumbs=[("Prayer", url_for("prayer.index")), ("New request", None)])


def _apply_assignment(p: PrayerRequest, f) -> None:
    acc = f.get("assigned_to")
    if acc:
        a = next((x for x in team_accounts() if x.public_id == acc), None)
        if a is None:
            abort(400)
        p.assigned_to_id = a.id
    elif "assigned_to" in f:
        p.assigned_to_id = None
    team = f.get("team")
    if team:
        d = db.session.query(Department).filter_by(public_id=team).first()
        p.team_department_id = getattr(d, "id", None)
    elif "team" in f:
        p.team_department_id = None


@bp.get("/prayer/<public_id>")
@permission_required("prayer.view")
def detail(public_id: str):
    p = svc.get_prayer_or_404(public_id)
    if p.is_confidential:
        audit.record("prayer.viewed", entity=p, actor=g.user)
        db.session.commit()
    return render_template("prayer/detail.html", p=p, statuses=list(PrayerStatus), accounts=team_accounts(),
                           teams=db.session.query(Department).order_by(Department.name).all(),
                           can_manage=g.account.can("prayer.edit") or p.assigned_to_id == g.account.id,
                           breadcrumbs=[("Prayer", url_for("prayer.index")), (p.display_name, None)])


@bp.post("/prayer/<public_id>/update")
@permission_required("prayer.view")
def update(public_id: str):
    p = svc.get_prayer_or_404(public_id)
    if not (g.account.can("prayer.edit") or p.assigned_to_id == g.account.id):
        abort(403)
    f = request.form
    note = (f.get("note") or "").strip()
    changed = []
    if f.get("status") and f["status"] != p.status.value:
        try:
            p.status = PrayerStatus(f["status"])
        except ValueError:
            abort(400)
        changed.append(f"status → {p.status.value}")
    if g.account.can("prayer.edit"):
        before = (p.assigned_to_id, p.team_department_id, p.is_confidential)
        _apply_assignment(p, f)
        if "is_confidential" in f or "assigned_to" in f:
            want = f.get("is_confidential") == "y"
            if want and not p.assigned_to_id and not g.account.can("prayer.approve"):
                p.assigned_to_id = g.account.id
            p.is_confidential = want
        if before != (p.assigned_to_id, p.team_department_id, p.is_confidential):
            changed.append("assignment")
    if note:
        db.session.add(PrayerUpdate(prayer_id=p.id, note=note[:4000], author_id=g.user.id))
    if not changed and not note:
        flash("Nothing to save.", "info")
        return redirect(url_for("prayer.detail", public_id=public_id))
    audit.record("prayer.updated", entity=p, actor=g.user, changes=", ".join(changed) or "note")
    db.session.commit()
    flash("Saved.", "success")
    # If the user just un-assigned themselves from a confidential request they may no longer see it.
    if svc.prayer_query().filter(PrayerRequest.id == p.id).first() is None:
        return redirect(url_for("prayer.index"))
    return redirect(url_for("prayer.detail", public_id=public_id))


@bp.post("/prayer/<public_id>/delete")
@permission_required("prayer.delete")
def delete(public_id: str):
    p = svc.get_prayer_or_404(public_id)
    audit.record("prayer.deleted", entity=p, actor=g.user)
    db.session.delete(p)
    db.session.commit()
    flash("Prayer request deleted.", "success")
    return redirect(url_for("prayer.index"))
