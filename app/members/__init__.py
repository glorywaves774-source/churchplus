"""Membership management pages."""
from __future__ import annotations

import io
import json
from datetime import date

from flask import (
    Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, send_file, session,
    url_for,
)

from app.extensions import db, limiter
from app.members.forms import DocumentForm, FamilyForm, ImportForm, MemberForm, PhotoForm, StatusForm, TransferForm
from app.models import (
    Branch, Family, FamilyRole, Gender, MaritalStatus, Member, MemberDocument, MemberStatus,
)
from app.services import audit, limits, members as svc, security, storage
from app.services.storage import StorageError
from app.utils.context import church_required, permission_required

bp = Blueprint("members", __name__)

ENUM_FIELDS = {"gender": Gender, "marital_status": MaritalStatus}
TEXT_FIELDS = ["title", "first_name", "middle_name", "last_name", "occupation", "employer", "nationality", "hometown",
               "phone", "alt_phone", "email", "address", "city", "region", "digital_address", "emergency_name",
               "emergency_phone", "emergency_relation", "previous_church", "baptism_place", "baptised_by", "notes"]
DATE_FIELDS = ["date_of_birth", "date_joined", "baptism_date"]


def _branch_choices(blank=True):
    opts = [(b.public_id, b.name) for b in svc.allowed_branches()]
    return ([("", "No branch")] if blank and svc.branch_limit_id() is None else []) + opts


def _branch_by_public_id(pid: str | None) -> Branch | None:
    if not pid:
        return None
    return next((b for b in svc.allowed_branches() if b.public_id == pid), None)


def _form_values(form: MemberForm) -> dict:
    values = {}
    for f in TEXT_FIELDS:
        v = getattr(form, f).data
        values[f] = (v.strip() if isinstance(v, str) else v) or None
    if values.get("email"):
        values["email"] = values["email"].lower()
    for f, enum_cls in ENUM_FIELDS.items():
        v = getattr(form, f).data
        if isinstance(v, enum_cls):                 # field absent from the post: keep stored value
            values[f] = v
        else:
            values[f] = enum_cls(v) if v else None
    for f in DATE_FIELDS:
        values[f] = getattr(form, f).data
    values["is_baptised"] = bool(form.is_baptised.data or form.baptism_date.data)
    values["allow_birthday_messages"] = bool(form.allow_birthday_messages.data)
    raw = form.branch.data
    if isinstance(raw, Branch):                     # field absent from the post: keep stored branch
        values["branch_id"] = raw.id
    else:
        branch = _branch_by_public_id(raw)
        values["branch_id"] = branch.id if branch else None
    return values


# --------------------------------------------------------------------------- #
# List
# --------------------------------------------------------------------------- #
@bp.get("/members")
@permission_required("members.view")
def index():
    query, text = svc.filtered(request.args)
    page = query.paginate(page=request.args.get("page", 1, type=int), per_page=25, error_out=False)
    params = {k: v for k, v in request.args.items() if k != "page" and v}
    return render_template("members/index.html", page=page, q=text, params=params, stats=svc.stats(),
                           branches=_branch_choices(blank=False), limit=limits.check("members"),
                           breadcrumbs=[("People", None), ("Members", None)])


# --------------------------------------------------------------------------- #
# Create and edit
# --------------------------------------------------------------------------- #
@bp.route("/members/new", methods=["GET", "POST"])
@permission_required("members.create")
def create():
    limit = limits.check("members")
    if not limit.allowed:
        flash(f"Your plan allows {limit.limit:,} members. Upgrade your plan to register more.", "warning")
        return redirect(url_for("members.index"))
    form = MemberForm()
    form.branch.choices = _branch_choices()
    if request.method == "GET":
        form.date_joined.data = date.today()
        if g.account.branch:
            form.branch.data = g.account.branch.public_id
    if form.validate_on_submit():
        values = _form_values(form)
        member = svc.create_member(g.tenant, g.user, **values)
        if values.get("previous_church"):
            svc.record_transfer_in(member, values["previous_church"], g.user)
        db.session.commit()
        flash(f"{member.display_name} registered as {member.membership_number}.", "success")
        return redirect(url_for("members.profile", public_id=member.public_id))
    return render_template("members/form.html", form=form, member=None,
                           breadcrumbs=[("Members", url_for("members.index")), ("Register member", None)])


@bp.route("/members/<public_id>/edit", methods=["GET", "POST"])
@permission_required("members.edit")
def edit(public_id: str):
    member = svc.get_member_or_404(public_id)
    initial = {f: getattr(member, f) for f in TEXT_FIELDS + DATE_FIELDS}
    initial.update(gender=member.gender.value if member.gender else "",
                   marital_status=member.marital_status.value if member.marital_status else "",
                   branch=member.branch.public_id if member.branch else "", title=member.title or "",
                   is_baptised=member.is_baptised, allow_birthday_messages=member.allow_birthday_messages)
    form = MemberForm(**initial)
    form.branch.choices = _branch_choices()
    if form.validate_on_submit():
        values = _form_values(form)
        if values["branch_id"] != member.branch_id and member.branch_id is not None:
            # Branch moves are recorded as transfers so history stays complete.
            values.pop("branch_id")
            flash("Use “Transfer” to move a member to another branch.", "info")
        changed = sorted(k for k, v in values.items() if getattr(member, k) != v)
        for k, v in values.items():
            setattr(member, k, v)
        audit.record("member.updated", entity=member, actor=g.user, name=member.display_name, fields=changed)
        db.session.commit()
        flash("Member details saved.", "success")
        return redirect(url_for("members.profile", public_id=member.public_id))
    return render_template("members/form.html", form=form, member=member,
                           breadcrumbs=[("Members", url_for("members.index")),
                                        (member.display_name, url_for("members.profile", public_id=public_id)),
                                        ("Edit", None)])


# --------------------------------------------------------------------------- #
# Profile
# --------------------------------------------------------------------------- #
@bp.get("/members/<public_id>")
@permission_required("members.view")
def profile(public_id: str):
    member = svc.get_member_or_404(public_id)
    families = db.session.query(Family).order_by(Family.name).all()
    family_form = FamilyForm()
    family_form.family.choices = [("", "Choose a family")] + [(f.public_id, f.name) for f in families]
    if member.family_role:
        family_form.role.data = member.family_role.value
    transfer_form = TransferForm()
    transfer_form.branch.choices = [(b.public_id, b.name) for b in svc.allowed_branches() if b.id != member.branch_id]
    from app.models import DepartmentMembership
    from app.services import operations as ops
    attendance = ops.member_attendance_history(member, 12)
    departments = db.session.query(DepartmentMembership).filter_by(member_id=member.id).all()
    from app.models import TenantUser
    portal_account = db.session.query(TenantUser).filter_by(member_id=member.id).first()
    return render_template(
        "members/profile.html", member=member, portal_account=portal_account, photo_form=PhotoForm(), doc_form=DocumentForm(),
        attendance=attendance, departments=departments,
        status_form=StatusForm(status=member.status.value if member.status != MemberStatus.TRANSFERRED else "inactive"),
        transfer_form=transfer_form, family_form=family_form,
        breadcrumbs=[("Members", url_for("members.index")), (member.display_name, None)],
    )


@bp.post("/members/<public_id>/delete")
@permission_required("members.delete")
def delete(public_id: str):
    """Permanent erasure (e.g. a data-protection request). Prefer changing status otherwise."""
    member = svc.get_member_or_404(public_id)
    keys = [d.file_key for d in member.documents] + ([member.photo_key] if member.photo_key else [])
    audit.record("member.deleted", entity=member, actor=g.user, name=member.display_name,
                 number=member.membership_number)
    db.session.delete(member)
    db.session.commit()
    for key in keys:
        try:
            storage.delete(key)
        except Exception:  # pragma: no cover
            current_app.logger.warning("Could not delete file %s", key)
    flash("Member record permanently deleted.", "success")
    return redirect(url_for("members.index"))


# --------------------------------------------------------------------------- #
# Photo
# --------------------------------------------------------------------------- #
@bp.post("/members/<public_id>/photo")
@permission_required("members.edit")
def upload_photo(public_id: str):
    member = svc.get_member_or_404(public_id)
    form = PhotoForm()
    if form.validate_on_submit():
        f = form.photo.data
        try:
            stored = storage.save(f, filename=f.filename, kind="image", category="member-photos", content_type=f.mimetype)
        except StorageError as e:
            flash(str(e), "danger")
        else:
            old, member.photo_key = member.photo_key, stored.key
            audit.record("member.photo_updated", entity=member, actor=g.user, name=member.display_name)
            db.session.commit()
            if old:
                storage.delete(old)
            flash("Photo updated.", "success")
    else:
        for e in form.photo.errors:
            flash(e, "danger")
    return redirect(url_for("members.profile", public_id=public_id))


@bp.get("/members/<public_id>/photo")
@permission_required("members.view")
def photo(public_id: str):
    member = svc.get_member_or_404(public_id)
    if not member.photo_key:
        abort(404)
    ext = member.photo_key.rsplit(".", 1)[-1]
    resp = send_file(storage.open_file(member.photo_key),
                     mimetype={"png": "image/png", "webp": "image/webp"}.get(ext, "image/jpeg"), max_age=300)
    resp.headers["Cache-Control"] = "private, max-age=300"
    return resp


# --------------------------------------------------------------------------- #
# Status and transfers
# --------------------------------------------------------------------------- #
@bp.post("/members/<public_id>/status")
@permission_required("members.edit")
def change_status(public_id: str):
    member = svc.get_member_or_404(public_id)
    form = StatusForm()
    if form.validate_on_submit():
        svc.change_status(member, MemberStatus(form.status.data), form.effective_date.data, form.reason.data or None, g.user)
        db.session.commit()
        flash(f"Status changed to {form.status.data}.", "success")
    else:
        flash("Please check the status form.", "danger")
    return redirect(url_for("members.profile", public_id=public_id))


@bp.post("/members/<public_id>/transfer")
@permission_required("members.edit")
def transfer(public_id: str):
    member = svc.get_member_or_404(public_id)
    form = TransferForm()
    form.branch.choices = [(b.public_id, b.name) for b in svc.allowed_branches()]
    if not form.validate_on_submit():
        flash("Please check the transfer form.", "danger")
    elif form.kind.data == "branch":
        branch = _branch_by_public_id(form.branch.data)
        if branch is None:
            flash("Choose the new branch.", "danger")
        else:
            try:
                svc.transfer_to_branch(member, branch, form.effective_date.data, form.reason.data or None, g.user)
                db.session.commit()
                flash(f"{member.display_name} transferred to {branch.name}.", "success")
            except ValueError as e:
                flash(str(e), "warning")
    else:
        if not (form.other_church.data or "").strip():
            flash("Enter the receiving church.", "danger")
        else:
            svc.transfer_out(member, form.other_church.data.strip(), form.effective_date.data,
                             form.reason.data or None, g.user)
            db.session.commit()
            flash(f"{member.display_name} recorded as transferred to {form.other_church.data.strip()}.", "success")
    return redirect(url_for("members.profile", public_id=public_id))


# --------------------------------------------------------------------------- #
# Family
# --------------------------------------------------------------------------- #
@bp.post("/members/<public_id>/family")
@permission_required("members.edit")
def set_family(public_id: str):
    member = svc.get_member_or_404(public_id)
    form = FamilyForm()
    form.family.choices = [("", "")] + [(f.public_id, f.name) for f in db.session.query(Family)]
    if not form.validate_on_submit():
        flash("Please check the family form.", "danger")
        return redirect(url_for("members.profile", public_id=public_id))
    if form.new_family_name.data:
        family = Family(name=form.new_family_name.data.strip(), phone=member.phone, address=member.address)
        db.session.add(family)
        db.session.flush()
        audit.record("family.created", entity=family, actor=g.user, name=family.name)
    else:
        family = db.session.query(Family).filter_by(public_id=form.family.data).first()
        if family is None:
            flash("Choose a family or enter a new family name.", "warning")
            return redirect(url_for("members.profile", public_id=public_id))
    member.family_id = family.id
    member.family_role = FamilyRole(form.role.data)
    audit.record("member.family_set", entity=member, actor=g.user, name=member.display_name, family=family.name)
    db.session.commit()
    flash(f"{member.display_name} linked to {family.name}.", "success")
    return redirect(url_for("members.profile", public_id=public_id))


@bp.post("/members/<public_id>/family/leave")
@permission_required("members.edit")
def leave_family(public_id: str):
    member = svc.get_member_or_404(public_id)
    member.family_id = None
    member.family_role = None
    audit.record("member.family_removed", entity=member, actor=g.user, name=member.display_name)
    db.session.commit()
    flash("Removed from family.", "success")
    return redirect(url_for("members.profile", public_id=public_id))


@bp.get("/families/<public_id>")
@permission_required("members.view")
def family(public_id: str):
    fam = db.session.query(Family).filter_by(public_id=public_id).first_or_404()
    visible = svc.members_query().filter(Member.family_id == fam.id).order_by(Member.family_role).all()
    return render_template("members/family.html", family=fam, members=visible,
                           breadcrumbs=[("Members", url_for("members.index")), (fam.name, None)])


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #
@bp.post("/members/<public_id>/documents")
@permission_required("members.edit")
def upload_document(public_id: str):
    member = svc.get_member_or_404(public_id)
    form = DocumentForm()
    if form.validate_on_submit():
        f = form.file.data
        try:
            stored = storage.save(f, filename=f.filename, kind="document", category="member-documents",
                                  content_type=f.mimetype)
        except StorageError as e:
            flash(str(e), "danger")
        else:
            doc = MemberDocument(member_id=member.id, title=form.title.data.strip(), category=form.category.data,
                                 file_key=stored.key, original_name=f.filename[:255], content_type=f.mimetype,
                                 size=stored.size, uploaded_by_id=g.user.id)
            db.session.add(doc)
            audit.record("member.document_added", entity=member, actor=g.user, name=member.display_name,
                         document=doc.title)
            db.session.commit()
            flash("Document uploaded.", "success")
    else:
        for errs in form.errors.values():
            for e in errs:
                flash(e, "danger")
    return redirect(url_for("members.profile", public_id=public_id) + "#documents")


def _get_document(public_id: str, doc_id: str) -> tuple[Member, MemberDocument]:
    member = svc.get_member_or_404(public_id)
    doc = db.session.query(MemberDocument).filter_by(public_id=doc_id, member_id=member.id).first()
    if doc is None:
        abort(404)
    return member, doc


@bp.get("/members/<public_id>/documents/<doc_id>")
@permission_required("members.view")
def download_document(public_id: str, doc_id: str):
    member, doc = _get_document(public_id, doc_id)
    audit.record("member.document_downloaded", entity=member, actor=g.user, document=doc.title)
    db.session.commit()
    return send_file(storage.open_file(doc.file_key), mimetype=doc.content_type or "application/octet-stream",
                     as_attachment=True, download_name=doc.original_name)


@bp.post("/members/<public_id>/documents/<doc_id>/delete")
@permission_required("members.edit")
def delete_document(public_id: str, doc_id: str):
    member, doc = _get_document(public_id, doc_id)
    key = doc.file_key
    audit.record("member.document_deleted", entity=member, actor=g.user, document=doc.title)
    db.session.delete(doc)
    db.session.commit()
    storage.delete(key)
    flash("Document deleted.", "success")
    return redirect(url_for("members.profile", public_id=public_id) + "#documents")


# --------------------------------------------------------------------------- #
# QR identification
# --------------------------------------------------------------------------- #
def _verify_url(member: Member) -> str:
    base = current_app.config.get("PUBLIC_BASE_URL") or request.host_url.rstrip("/")
    return base + url_for("members.verify", token=member.qr_token)


@bp.get("/members/<public_id>/card")
@permission_required("members.view")
def card(public_id: str):
    member = svc.get_member_or_404(public_id)
    return render_template("members/card.html", member=member, qr=security.qr_svg(_verify_url(member)),
                           breadcrumbs=[("Members", url_for("members.index")),
                                        (member.display_name, url_for("members.profile", public_id=public_id)),
                                        ("ID card", None)])


@bp.get("/m/<token>")
@church_required
def verify(token: str):
    """Scanning a membership QR code: staff of the same church see the member; nobody else learns anything."""
    if not g.account.can("members.view"):
        abort(403)
    member = svc.members_query().filter(Member.qr_token == token).first()
    if member is None:
        abort(404)
    return redirect(url_for("members.profile", public_id=member.public_id))


@bp.post("/members/<public_id>/card/renew")
@permission_required("members.edit")
def renew_card(public_id: str):
    """Issue a new QR code (old printed cards stop working), e.g. after a card is lost."""
    from app.models.member import new_qr_token
    member = svc.get_member_or_404(public_id)
    member.qr_token = new_qr_token()
    audit.record("member.card_renewed", entity=member, actor=g.user, name=member.display_name)
    db.session.commit()
    flash("A new QR code has been issued. Reprint the membership card.", "success")
    return redirect(url_for("members.card", public_id=public_id))


# --------------------------------------------------------------------------- #
# Import / export
# --------------------------------------------------------------------------- #
IMPORT_KEY = "cp_member_import"


@bp.get("/members/import/template.csv")
@permission_required("members.create")
def import_template():
    rows = [[label for _, label in svc.IMPORT_FIELDS],
            ["Mr", "Kwame", "", "Mensah", "male", "1985-04-12", "married", "0241234567", "", "kwame@example.com",
             "12 Palm Street", "Accra", "Greater Accra", "GA-123-4567", "Teacher", "HQ", "active", "2019-03-10",
             "yes", "2010-08-01", "Ama Mensah", "0207654321"]]
    return Response(svc.to_csv(rows), mimetype="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="churchplus-members-template.csv"'})


@bp.route("/members/import", methods=["GET", "POST"])
@permission_required("members.create")
@limiter.limit("20 per hour", methods=["POST"])
def import_members():
    form = ImportForm()
    form.branch.choices = _branch_choices()
    preview = None
    if request.method == "POST" and request.form.get("confirm") == "1":
        pending = session.get(IMPORT_KEY)
        if not pending:
            flash("Please upload the file again.", "warning")
            return redirect(url_for("members.import_members"))
        rows = svc.read_table(pending["name"], storage.open_file(pending["key"]).read())
        result = svc.import_members(g.tenant, g.user, rows, _branch_by_public_id(pending.get("branch")), commit=True)
        if result.errors:
            db.session.rollback()
            flash("The file has errors. Nothing was imported.", "danger")
            preview = result
        else:
            db.session.commit()
            storage.delete(pending["key"])
            session.pop(IMPORT_KEY, None)
            flash(f"{len(result.created)} members imported.", "success")
            return redirect(url_for("members.index"))
    elif form.validate_on_submit():
        f = form.file.data
        data = f.read()
        try:
            rows = svc.read_table(f.filename, data)
            stored = storage.save(data, filename=f.filename, kind="document", category="imports",
                                  content_type=f.mimetype)
        except (ValueError, StorageError, Exception) as e:   # malformed spreadsheets raise many types
            current_app.logger.info("Import rejected: %s", e)
            flash(str(e) if isinstance(e, (ValueError, StorageError)) else "That file could not be read.", "danger")
            return redirect(url_for("members.import_members"))
        preview = svc.import_members(g.tenant, g.user, rows, _branch_by_public_id(form.branch.data), commit=False)
        session[IMPORT_KEY] = {"key": stored.key, "name": f.filename, "branch": form.branch.data}
    return render_template("members/import.html", form=form, preview=preview,
                           breadcrumbs=[("Members", url_for("members.index")), ("Import", None)])


@bp.get("/members/export.<fmt>")
@permission_required("members.export")
def export(fmt: str):
    if fmt not in ("csv", "xlsx"):
        abort(404)
    query, _ = svc.filtered(request.args)
    rows = svc.export_rows(query.limit(100_000).all())
    audit.record("member.exported", actor=g.user, rows=len(rows) - 1, format=fmt)
    db.session.commit()
    name = f"{g.tenant.slug}-members-{date.today():%Y%m%d}.{fmt}"
    if fmt == "csv":
        return Response(svc.to_csv(rows), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})
    return send_file(io.BytesIO(svc.to_xlsx(rows)), as_attachment=True, download_name=name,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@bp.get("/members/<public_id>/data.json")
@permission_required("members.export")
def personal_data(public_id: str):
    member = svc.get_member_or_404(public_id)
    audit.record("member.data_exported", entity=member, actor=g.user, name=member.display_name)
    db.session.commit()
    body = json.dumps({"church": g.tenant.name, "member": svc.personal_data(member)}, indent=2, ensure_ascii=False)
    return Response(body, mimetype="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{member.membership_number}.json"'})
