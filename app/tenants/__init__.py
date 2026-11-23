"""Church workspace: profile and branding, branches, and denomination member churches."""
from __future__ import annotations

from flask import (
    Blueprint, abort, current_app, flash, g, redirect, render_template, request, send_file, url_for,
)
from sqlalchemy import or_

from app.extensions import db, limiter
from app.models import Branch, TenantStatus
from app.services import audit, limits, mailer, storage, tokens
from app.services.denominations import create_member_church, is_headquarters, member_church_summaries
from app.services.storage import StorageError
from app.tenants.forms import BranchForm, ChurchProfileForm, LogoForm, MemberChurchForm
from app.utils.context import church_required, login_required, permission_required

bp = Blueprint("tenants", __name__)


# --------------------------------------------------------------------------- #
# Account status pages
# --------------------------------------------------------------------------- #
@bp.get("/church/pending")
@login_required
def pending():
    if g.tenant is None or g.tenant.status != TenantStatus.PENDING:
        return redirect(url_for("dashboard.index"))
    return render_template("tenants/status.html", title="Awaiting approval", icon_name="clock",
                           message=f"{g.tenant.name} has been registered and is waiting for approval by the "
                                   "ChurchPlus team. You will receive an email as soon as it is approved.")


@bp.get("/church/unavailable")
@login_required
def unavailable():
    if g.tenant is None or g.tenant.is_operational:
        return redirect(url_for("dashboard.index"))
    if (g.tenant.settings or {}).get("suspended_reason") == "billing":
        can_pay = g.account is not None and g.account.can("billing.view")
        return render_template("tenants/status.html", title="Payment needed", icon_name="credit-card",
                               message=f"{g.tenant.name}'s workspace is paused because a ChurchPlus invoice is unpaid. "
                                       + ("Pay it now and everything opens again straight away. No data has been deleted."
                                          if can_pay else "Please ask your church administrator to pay it. No data has been deleted."),
                               action=(url_for("billing.index"), "Pay now") if can_pay else None)
    return render_template("tenants/status.html", title="Workspace unavailable", icon_name="lock",
                           message=f"{g.tenant.name}'s workspace is currently {g.tenant.status.value}. "
                                   "Please contact ChurchPlus support or your church administrator.")


# --------------------------------------------------------------------------- #
# Church profile and branding
# --------------------------------------------------------------------------- #
@bp.route("/church/settings", methods=["GET", "POST"])
@permission_required("church.manage_settings")
def settings():
    tenant = g.tenant
    form = ChurchProfileForm(obj=tenant)
    logo_form = LogoForm()
    if form.validate_on_submit():
        before = {f: getattr(tenant, f) for f in form.data if f != "csrf_token"}
        form.populate_obj(tenant)
        for f in ("primary_color", "accent_color", "legal_name", "tagline", "email", "phone", "website",
                  "address_line1", "address_line2", "region", "digital_address"):
            if getattr(tenant, f) == "":
                setattr(tenant, f, None)
        changed = sorted(f for f, v in before.items() if getattr(tenant, f) != v)
        audit.record("church.profile_updated", entity=tenant, actor=g.user, fields=changed)
        db.session.commit()
        flash("Church profile saved.", "success")
        return redirect(url_for("tenants.settings"))
    return render_template("tenants/settings.html", form=form, logo_form=logo_form,
                           breadcrumbs=[("Administration", None), ("Church settings", None)])


@bp.post("/church/logo")
@permission_required("church.manage_settings")
def upload_logo():
    form = LogoForm()
    if not form.validate_on_submit():
        for e in form.logo.errors:
            flash(e, "danger")
        return redirect(url_for("tenants.settings"))
    f = form.logo.data
    try:
        stored = storage.save(f, filename=f.filename, kind="image", category="branding", content_type=f.mimetype)
    except StorageError as e:
        flash(str(e), "danger")
        return redirect(url_for("tenants.settings"))
    old = g.tenant.logo_key
    g.tenant.logo_key = stored.key
    audit.record("church.logo_updated", entity=g.tenant, actor=g.user)
    db.session.commit()
    if old:
        try:
            storage.delete(old)
        except Exception:  # pragma: no cover - old file already gone
            current_app.logger.warning("Could not delete old logo %s", old)
    flash("Logo updated.", "success")
    return redirect(url_for("tenants.settings"))


@bp.post("/church/logo/remove")
@permission_required("church.manage_settings")
def remove_logo():
    if g.tenant.logo_key:
        storage.delete(g.tenant.logo_key)
        g.tenant.logo_key = None
        audit.record("church.logo_removed", entity=g.tenant, actor=g.user)
        db.session.commit()
        flash("Logo removed.", "info")
    return redirect(url_for("tenants.settings"))


@bp.get("/church/logo")
@church_required
def logo():
    """The signed-in church's own logo (private; never another church's)."""
    if not g.tenant.logo_key:
        abort(404)
    ext = g.tenant.logo_key.rsplit(".", 1)[-1]
    mimetype = {"png": "image/png", "webp": "image/webp"}.get(ext, "image/jpeg")
    resp = send_file(storage.open_file(g.tenant.logo_key), mimetype=mimetype, max_age=300)
    resp.headers["Cache-Control"] = "private, max-age=300"
    return resp


# --------------------------------------------------------------------------- #
# Branches
# --------------------------------------------------------------------------- #
def _get_branch(public_id: str) -> Branch:
    # Tenant isolation means another church's branch simply is not found.
    return db.session.query(Branch).filter_by(public_id=public_id).first_or_404()


@bp.get("/branches")
@permission_required("branches.view")
def branches():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "")
    query = db.session.query(Branch)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Branch.name.ilike(like), Branch.code.ilike(like), Branch.city.ilike(like)))
    if status == "active":
        query = query.filter_by(is_active=True)
    elif status == "inactive":
        query = query.filter_by(is_active=False)
    rows = query.order_by(Branch.is_headquarters.desc(), Branch.name).all()
    return render_template("tenants/branches.html", branches=rows, q=q, status=status,
                           limit=limits.check("branches"),
                           breadcrumbs=[("Administration", None), ("Branches", None)])


@bp.route("/branches/new", methods=["GET", "POST"])
@permission_required("branches.create")
def branch_create():
    limit = limits.check("branches")
    form = BranchForm(country=g.tenant.country)
    if not limit.allowed:
        flash(f"Your plan allows {limit.limit} active branch(es). Upgrade your plan to add more.", "warning")
        return redirect(url_for("tenants.branches"))
    if form.validate_on_submit():
        code = form.code.data.strip().upper()
        if db.session.query(Branch.id).filter_by(code=code).first():
            form.code.errors.append("Another branch already uses this code.")
        else:
            branch = Branch(code=code)
            form.populate_obj(branch)
            branch.code = code
            db.session.add(branch)
            db.session.flush()
            audit.record("branch.created", entity=branch, actor=g.user, name=branch.name)
            db.session.commit()
            flash(f"{branch.name} added.", "success")
            return redirect(url_for("tenants.branches"))
    return render_template("tenants/branch_form.html", form=form, branch=None,
                           breadcrumbs=[("Branches", url_for("tenants.branches")), ("New branch", None)])


@bp.route("/branches/<public_id>/edit", methods=["GET", "POST"])
@permission_required("branches.edit")
def branch_edit(public_id: str):
    branch = _get_branch(public_id)
    form = BranchForm(obj=branch)
    if form.validate_on_submit():
        code = form.code.data.strip().upper()
        clash = db.session.query(Branch.id).filter(Branch.code == code, Branch.id != branch.id).first()
        if clash:
            form.code.errors.append("Another branch already uses this code.")
        else:
            form.populate_obj(branch)
            branch.code = code
            audit.record("branch.updated", entity=branch, actor=g.user)
            db.session.commit()
            flash("Branch saved.", "success")
            return redirect(url_for("tenants.branches"))
    return render_template("tenants/branch_form.html", form=form, branch=branch,
                           breadcrumbs=[("Branches", url_for("tenants.branches")), (branch.name, None)])


@bp.post("/branches/<public_id>/status")
@permission_required("branches.edit")
def branch_toggle(public_id: str):
    branch = _get_branch(public_id)
    if branch.is_headquarters:
        flash("The headquarters branch cannot be deactivated.", "warning")
        return redirect(url_for("tenants.branches"))
    if not branch.is_active and not limits.check("branches").allowed:
        flash("Your plan's branch limit has been reached. Upgrade to reactivate this branch.", "warning")
        return redirect(url_for("tenants.branches"))
    branch.is_active = not branch.is_active
    audit.record("branch.activated" if branch.is_active else "branch.deactivated", entity=branch, actor=g.user)
    db.session.commit()
    flash(f"{branch.name} {'reactivated' if branch.is_active else 'deactivated'}.", "success")
    return redirect(url_for("tenants.branches"))


@bp.post("/branches/<public_id>/delete")
@permission_required("branches.delete")
def branch_delete(public_id: str):
    branch = _get_branch(public_id)
    if branch.is_headquarters:
        flash("The headquarters branch cannot be deleted.", "warning")
        return redirect(url_for("tenants.branches"))
    from app.models import TenantUser
    if db.session.query(TenantUser.id).filter_by(branch_id=branch.id).first():
        flash("This branch has users assigned. Move them or deactivate the branch instead.", "warning")
        return redirect(url_for("tenants.branches"))
    audit.record("branch.deleted", entity=branch, actor=g.user, name=branch.name)
    db.session.delete(branch)
    db.session.commit()
    flash("Branch deleted.", "success")
    return redirect(url_for("tenants.branches"))


# --------------------------------------------------------------------------- #
# Denomination headquarters: member churches
# --------------------------------------------------------------------------- #
def _require_hq():
    if not is_headquarters(g.tenant):
        abort(404)


@bp.get("/member-churches")
@permission_required("church.view")
def member_churches():
    _require_hq()
    return render_template("tenants/member_churches.html", churches=member_church_summaries(g.tenant),
                           breadcrumbs=[("Denomination", None), ("Member churches", None)])


@bp.route("/member-churches/new", methods=["GET", "POST"])
@permission_required("church.manage_settings")
def member_church_create():
    _require_hq()
    form = MemberChurchForm()
    if form.validate_on_submit():
        church, admin, is_new = create_member_church(
            g.tenant, name=form.name.data, city=form.city.data or None, admin_first=form.admin_first.data,
            admin_last=form.admin_last.data, admin_email=form.admin_email.data, actor=g.user)
        db.session.commit()
        base = current_app.config.get("PUBLIC_BASE_URL") or request.host_url.rstrip("/")
        if is_new:
            link = base + url_for("auth.set_password", token=tokens.make(admin, "set_password"))
        else:
            link = base + url_for("auth.login")
        sent = mailer.try_send(admin.email, f"You have been invited to manage {church.name} on ChurchPlus",
                               "member_church_invite", admin=admin, church=church, hq=g.tenant, link=link, is_new=is_new)
        from app.auth.team import flash_invite_result
        flash_invite_result(sent, admin, link, f"{church.name} added. An invitation has been emailed to {admin.email}.")
        return redirect(url_for("tenants.member_churches"))
    return render_template("tenants/member_church_form.html", form=form,
                           breadcrumbs=[("Member churches", url_for("tenants.member_churches")), ("Add church", None)])


@bp.post("/church/export")
@permission_required("church.manage_settings")
@limiter.limit("5 per hour")
def export_data():
    """Download everything (except counselling, safeguarding and confidential prayer) as CSV files."""
    if not g.account.is_owner:
        abort(403)
    from datetime import date as _date
    from flask import Response
    from app.services.export import church_zip
    data = church_zip(g.tenant)
    audit.record("church.data_exported", entity=g.tenant, actor=g.user, name=g.tenant.name)
    db.session.commit()
    return Response(data, mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{g.tenant.slug}-export-{_date.today():%Y%m%d}.zip"'})

