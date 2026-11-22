"""Children and youth ministry: profiles, guardians, classes, secure check-in/out and
safeguarding concerns.

* Children's information needs ``children.view``. Medical notes and guardians are shown
  only on the child's own page; lists show the minimum.
* Check-out needs the pick-up code issued at check-in; wrong codes are refused and logged.
* Safeguarding concerns need ``children.approve`` to see or record.
"""
from __future__ import annotations

from datetime import date

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import func, or_

from app.extensions import db, limiter
from app.models import (
    ChildCheckIn, ChildClass, ChildGuardian, ChildProfile, ClassTeacher, IncidentStatus, Member, SafeguardingIncident,
    Service,
)
from app.services import audit, members as member_svc, ministry as svc
from app.services.ministry import CheckOutRefused
from app.utils.context import permission_required

bp = Blueprint("children", __name__, url_prefix="/children")
RELATIONSHIPS = ["Mother", "Father", "Guardian", "Grandparent", "Aunt", "Uncle", "Sibling (18+)", "Carer", "Other"]
INCIDENT_CATEGORIES = ["Injury or accident", "Behaviour", "Concern about home", "Concern about an adult",
                       "Bullying", "Missing child", "Medical", "Other"]


def _member(number: str | None):
    if not number:
        return None
    return member_svc.members_query().filter(func.upper(Member.membership_number) == number.strip().upper()).first()


def _classes():
    return db.session.query(ChildClass).order_by(ChildClass.is_youth, ChildClass.min_age, ChildClass.name).all()


def _unregistered_query():
    """Members under 18 who have no children's profile yet."""
    today = date.today()
    try:
        cutoff = today.replace(year=today.year - 18)
    except ValueError:                      # 29 February
        cutoff = today.replace(year=today.year - 18, day=28)
    has_profile = db.session.query(ChildProfile.member_id)
    return member_svc.members_query().filter(Member.date_of_birth > cutoff, ~Member.id.in_(has_profile))


def _today_services():
    return db.session.query(Service).filter(Service.service_date == date.today()).order_by(Service.start_time).all()


# --------------------------------------------------------------------------- #
# Overview and children list
# --------------------------------------------------------------------------- #
@bp.get("")
@permission_required("children.view")
def index():
    q = svc.children_query()
    text = request.args.get("q", "").strip()
    if text:
        like = f"%{text}%"
        q = q.filter(or_(Member.first_name.ilike(like), Member.last_name.ilike(like), Member.membership_number.ilike(like)))
    cls = request.args.get("class", "")
    if cls:
        q = q.join(ChildClass, ChildClass.id == ChildProfile.class_id).filter(ChildClass.public_id == cls)
    children = q.order_by(Member.first_name, Member.last_name).limit(500).all()
    today = (db.session.query(ChildCheckIn).filter(ChildCheckIn.check_in_date == date.today())
             .filter(ChildCheckIn.child_id.in_(svc.children_query().with_entities(ChildProfile.id)))
             .order_by(ChildCheckIn.checked_in_at.desc()).all())
    open_ids = {ci.child_id for ci in today if ci.checked_out_at is None}
    classes = _classes()
    uncleared = sum(1 for c in classes for t in c.teachers if not t.is_cleared())
    incidents = (db.session.query(SafeguardingIncident).filter(SafeguardingIncident.status != IncidentStatus.CLOSED).count()
                 if g.account.can("children.approve") else None)
    # Members under 18 without a children's profile yet (to invite registration).
    unregistered = _unregistered_query().count()
    return render_template("children/index.html", children=children, today=today, open_ids=open_ids, classes=classes,
                           uncleared=uncleared, incidents=incidents, unregistered=unregistered, q=text, cls=cls,
                           breadcrumbs=[("Ministry", None), ("Children and youth", None)])


@bp.route("/register", methods=["GET", "POST"])
@permission_required("children.create")
def register():
    if request.method == "POST":
        member = _member(request.form.get("number"))
        if member is None:
            flash("Add the child as a member first, then enter their membership number here.", "danger")
        elif member.age is not None and member.age >= 18:
            flash(f"{member.display_name} is {member.age}. Children's profiles are for under-18s.", "danger")
        elif db.session.query(ChildProfile.id).filter_by(member_id=member.id).first():
            existing = db.session.query(ChildProfile).filter_by(member_id=member.id).first()
            flash("This child already has a profile.", "info")
            return redirect(url_for("children.profile", public_id=existing.public_id))
        else:
            c = ChildProfile(member_id=member.id)
            _save_profile(c, member)
            db.session.add(c)
            db.session.flush()
            _guardians_from_family(c, member)
            audit.record("child.profile_saved", entity=c, actor=g.user, name=member.display_name)
            db.session.commit()
            flash("Child registered. Check the guardians who may collect them.", "success")
            return redirect(url_for("children.profile", public_id=c.public_id))
    suggestions = _unregistered_query().order_by(Member.first_name).limit(30).all()
    return render_template("children/form.html", c=None, form=request.form, classes=_classes(), suggestions=suggestions,
                           breadcrumbs=[("Children", url_for("children.index")), ("Register a child", None)])


def _save_profile(c: ChildProfile, member: Member) -> None:
    f = request.form
    chosen = next((k for k in _classes() if k.public_id == f.get("class")), None)
    c.class_id = chosen.id if chosen else getattr(svc.class_for_age(member.age), "id", None)
    for k in ("school", "allergies"):
        setattr(c, k, (f.get(k) or "").strip()[:255] or None)
    for k in ("medical_notes", "special_needs"):
        setattr(c, k, (f.get(k) or "").strip() or None)
    c.photo_consent = f.get("photo_consent") == "y"


def _guardians_from_family(c: ChildProfile, member: Member) -> None:
    """Adults in the child's family become guardians (authorised to collect)."""
    if not member.family:
        return
    for adult in member.family.members:
        if adult.id == member.id or (adult.age is not None and adult.age < 18):
            continue
        gender = getattr(adult.gender, "value", adult.gender)
        rel = {"male": "Father", "female": "Mother"}.get(gender or "", "Guardian")
        db.session.add(ChildGuardian(child_id=c.id, member_id=adult.id, relationship_to_child=rel,
                                     is_authorized_pickup=True, is_primary=False))


@bp.route("/<public_id>/edit", methods=["GET", "POST"])
@permission_required("children.edit")
def edit(public_id: str):
    c = svc.get_child_or_404(public_id)
    if request.method == "POST":
        _save_profile(c, c.member)
        audit.record("child.profile_saved", entity=c, actor=g.user, name=c.member.display_name)
        db.session.commit()
        flash("Saved.", "success")
        return redirect(url_for("children.profile", public_id=public_id))
    return render_template("children/form.html", c=c, form=request.form, classes=_classes(), suggestions=[],
                           breadcrumbs=[("Children", url_for("children.index")), (c.member.display_name, url_for("children.profile", public_id=public_id)), ("Edit", None)])


@bp.get("/<public_id>")
@permission_required("children.view")
def profile(public_id: str):
    c = svc.get_child_or_404(public_id)
    history = (db.session.query(ChildCheckIn).filter_by(child_id=c.id).order_by(ChildCheckIn.checked_in_at.desc()).limit(20).all())
    return render_template("children/profile.html", c=c, active=svc.active_check_in(c), history=history,
                           relationships=RELATIONSHIPS, services=_today_services(),
                           breadcrumbs=[("Children", url_for("children.index")), (c.member.display_name, None)])


@bp.post("/<public_id>/guardians")
@permission_required("children.edit")
def add_guardian(public_id: str):
    c = svc.get_child_or_404(public_id)
    f = request.form
    member = _member(f.get("number"))
    if f.get("number") and member is None:
        flash("No member has that membership number.", "danger")
    elif not member and not (f.get("name") or "").strip():
        flash("Enter a membership number or a name.", "danger")
    elif not member and not (f.get("phone") or "").strip():
        flash("Enter a phone number for a guardian who is not a member.", "danger")
    else:
        rel = f.get("relationship") if f.get("relationship") in RELATIONSHIPS else "Other"
        gd = ChildGuardian(child_id=c.id, member_id=getattr(member, "id", None),
                           name=None if member else f["name"].strip()[:160],
                           phone=None if member else f["phone"].strip()[:40], relationship_to_child=rel,
                           is_authorized_pickup=f.get("authorized") == "y", is_primary=not c.guardians,
                           pickup_restriction=(f.get("restriction") or "").strip()[:255] or None)
        db.session.add(gd)
        audit.record("child.guardian_added", entity=c, actor=g.user, name=c.member.display_name, guardian=gd.display_name)
        db.session.commit()
        flash("Guardian added.", "success")
    return redirect(url_for("children.profile", public_id=public_id) + "#guardians")


@bp.post("/<public_id>/guardians/<guardian_id>")
@permission_required("children.edit")
def guardian_action(public_id: str, guardian_id: str):
    c = svc.get_child_or_404(public_id)
    gd = db.session.query(ChildGuardian).filter_by(public_id=guardian_id, child_id=c.id).first_or_404()
    action = request.form.get("action")
    if action == "remove":
        audit.record("child.guardian_removed", entity=c, actor=g.user, name=c.member.display_name, guardian=gd.display_name)
        db.session.delete(gd)
    elif action == "toggle":
        gd.is_authorized_pickup = not gd.is_authorized_pickup
        audit.record("child.profile_saved", entity=c, actor=g.user, name=c.member.display_name,
                     guardian=gd.display_name, may_collect=gd.is_authorized_pickup)
    else:
        abort(400)
    db.session.commit()
    flash("Saved.", "success")
    return redirect(url_for("children.profile", public_id=public_id) + "#guardians")


# --------------------------------------------------------------------------- #
# Check-in and check-out
# --------------------------------------------------------------------------- #
@bp.post("/<public_id>/check-in")
@permission_required("children.create")
def check_in(public_id: str):
    c = svc.get_child_or_404(public_id)
    service = db.session.query(Service).filter_by(public_id=request.form.get("service")).first() if request.form.get("service") else None
    try:
        ci = svc.check_in(c, actor=g.user, dropped_off_by=request.form.get("dropped_off_by"), service=service)
    except ValueError as e:
        flash(str(e), "warning")
        return redirect(url_for("children.profile", public_id=public_id))
    db.session.commit()
    return redirect(url_for("children.label", checkin_id=ci.public_id))


@bp.get("/labels/<checkin_id>")
@permission_required("children.create")
def label(checkin_id: str):
    ci = db.session.query(ChildCheckIn).filter_by(public_id=checkin_id).first_or_404()
    svc.get_child_or_404(ci.child.public_id)          # branch scope
    if ci.checked_out_at is not None or ci.check_in_date != date.today():
        flash("This check-in is closed. Labels are only shown on the day.", "info")
        return redirect(url_for("children.profile", public_id=ci.child.public_id))
    return render_template("children/label.html", ci=ci, c=ci.child,
                           breadcrumbs=[("Children", url_for("children.index")), (ci.child.member.display_name, url_for("children.profile", public_id=ci.child.public_id)), ("Labels", None)])


@bp.post("/<public_id>/check-out")
@permission_required("children.create")
@limiter.limit("20 per minute")
def check_out(public_id: str):
    c = svc.get_child_or_404(public_id)
    ci = svc.active_check_in(c)
    if ci is None:
        flash("This child is not checked in today.", "warning")
        return redirect(url_for("children.profile", public_id=public_id))
    gid = request.form.get("guardian")
    guardian = db.session.query(ChildGuardian).filter_by(public_id=gid, child_id=c.id).first() if gid else None
    try:
        svc.check_out(ci, code=request.form.get("code", ""), guardian=guardian,
                      collected_by=request.form.get("collected_by"), actor=g.user)
    except CheckOutRefused as e:
        db.session.commit()            # keep the refusal in the audit trail
        flash(str(e), "danger")
        return redirect(url_for("children.profile", public_id=public_id) + "#checkout")
    db.session.commit()
    flash(f"{c.member.display_name} collected by {ci.collected_by_name}.", "success")
    return redirect(request.form.get("next") if (request.form.get("next") or "").startswith("/") else url_for("children.profile", public_id=public_id))


# --------------------------------------------------------------------------- #
# Classes and teachers
# --------------------------------------------------------------------------- #
@bp.route("/classes", methods=["GET", "POST"])
@permission_required("children.view")
def classes():
    if request.method == "POST":
        if not g.account.can("children.edit"):
            abort(403)
        f = request.form
        k = db.session.query(ChildClass).filter_by(public_id=f["id"]).first_or_404() if f.get("id") else ChildClass(name="")
        try:
            lo, hi = int(f.get("min_age") or 0), int(f.get("max_age") or 17)
            if not (0 <= lo <= hi <= 25):
                raise ValueError
        except ValueError:
            flash("Check the age range.", "danger")
            return redirect(url_for("children.classes"))
        name = (f.get("name") or "").strip()
        if not name:
            flash("Enter the class name.", "danger")
            return redirect(url_for("children.classes"))
        k.name, k.min_age, k.max_age = name[:100], lo, hi
        k.room = (f.get("room") or "").strip()[:80] or None
        k.is_youth = f.get("is_youth") == "y"
        db.session.add(k)
        audit.record("child_class.saved", entity=k, actor=g.user, name=k.name)
        db.session.commit()
        flash("Class saved.", "success")
        return redirect(url_for("children.classes"))
    rows = _classes()
    counts = dict(db.session.query(ChildProfile.class_id, func.count(ChildProfile.id)).group_by(ChildProfile.class_id).all())
    return render_template("children/classes.html", rows=rows, counts=counts, today=date.today(),
                           breadcrumbs=[("Children", url_for("children.index")), ("Classes and teachers", None)])


@bp.post("/classes/<class_id>/teachers")
@permission_required("children.edit")
def add_teacher(class_id: str):
    k = db.session.query(ChildClass).filter_by(public_id=class_id).first_or_404()
    f = request.form
    member = _member(f.get("number"))
    if member is None:
        flash("Enter the teacher's membership number.", "danger")
        return redirect(url_for("children.classes"))

    def d(name):
        try:
            v = date.fromisoformat(f.get(name) or "")
            return v if v <= date.today() else None
        except ValueError:
            return None
    t = db.session.query(ClassTeacher).filter_by(class_id=k.id, member_id=member.id).first() or ClassTeacher(class_id=k.id, member_id=member.id)
    t.role = (f.get("role") or "Teacher").strip()[:40]
    t.background_check_date, t.safeguarding_training_date = d("background_check_date"), d("safeguarding_training_date")
    db.session.add(t)
    audit.record("child_class.teacher_added", entity=k, actor=g.user, name=k.name, teacher=member.display_name)
    db.session.commit()
    flash(f"{member.display_name} saved." + ("" if t.is_cleared() else " Not yet cleared to serve alone: record the background check and safeguarding training."),
          "success" if t.is_cleared() else "warning")
    return redirect(url_for("children.classes") + f"#class-{k.public_id}")


@bp.post("/classes/<class_id>/teachers/<teacher_id>/remove")
@permission_required("children.edit")
def remove_teacher(class_id: str, teacher_id: str):
    k = db.session.query(ChildClass).filter_by(public_id=class_id).first_or_404()
    t = db.session.query(ClassTeacher).filter_by(public_id=teacher_id, class_id=k.id).first_or_404()
    db.session.delete(t)
    audit.record("child_class.saved", entity=k, actor=g.user, name=k.name, removed=t.member.display_name)
    db.session.commit()
    flash("Teacher removed.", "success")
    return redirect(url_for("children.classes"))


# --------------------------------------------------------------------------- #
# Safeguarding
# --------------------------------------------------------------------------- #
@bp.route("/safeguarding", methods=["GET", "POST"])
@permission_required("children.approve")
def safeguarding():
    if request.method == "POST":
        f = request.form
        child = None
        if f.get("child"):
            child = svc.children_query().filter(ChildProfile.public_id == f["child"]).first()
        try:
            occurred = date.fromisoformat(f.get("occurred_on") or "")
        except ValueError:
            occurred = None
        desc = (f.get("description") or "").strip()
        if not desc or occurred is None or occurred > date.today():
            flash("Enter the date and describe what happened.", "danger")
        else:
            i = SafeguardingIncident(reference=svc.incident_reference(), child_id=getattr(child, "id", None), occurred_on=occurred,
                                     category=f.get("category") if f.get("category") in INCIDENT_CATEGORIES else "Other",
                                     description=desc, action_taken=(f.get("action_taken") or "").strip() or None,
                                     reported_by_id=g.user.id)
            db.session.add(i)
            db.session.flush()
            audit.record("safeguarding.reported", entity=i, actor=g.user, name=i.reference)
            db.session.commit()
            flash(f"Concern {i.reference} recorded.", "success")
            return redirect(url_for("children.incident", public_id=i.public_id))
    rows = db.session.query(SafeguardingIncident).order_by(SafeguardingIncident.occurred_on.desc()).limit(200).all()
    return render_template("children/safeguarding.html", rows=rows, categories=INCIDENT_CATEGORIES, form=request.form,
                           children=svc.children_query().order_by(Member.first_name).all(), today=date.today(),
                           breadcrumbs=[("Children", url_for("children.index")), ("Safeguarding", None)])


@bp.route("/safeguarding/<public_id>", methods=["GET", "POST"])
@permission_required("children.approve")
def incident(public_id: str):
    i = db.session.query(SafeguardingIncident).filter_by(public_id=public_id).first_or_404()
    if request.method == "POST":
        try:
            i.status = IncidentStatus(request.form.get("status", i.status.value))
        except ValueError:
            abort(400)
        i.action_taken = (request.form.get("action_taken") or "").strip() or None
        i.referred_to = (request.form.get("referred_to") or "").strip()[:200] or None
        audit.record("safeguarding.updated", entity=i, actor=g.user, name=i.reference, status=i.status.value)
        db.session.commit()
        flash("Saved.", "success")
        return redirect(url_for("children.incident", public_id=public_id))
    return render_template("children/incident.html", i=i, statuses=list(IncidentStatus),
                           breadcrumbs=[("Children", url_for("children.index")), ("Safeguarding", url_for("children.safeguarding")), (i.reference, None)])
