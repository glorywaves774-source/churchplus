"""Team users and roles for the active church.

Safety rules (no privilege escalation):
* People can only grant roles, or give a role permissions, that they hold themselves
  (the church owner is exempt).
* Nobody can change their own roles, suspend or remove themselves.
* The church owner cannot be suspended or removed, and the Church Owner role is locked.
* Built-in roles can be edited but not deleted; custom roles can be deleted when unused.
"""
from __future__ import annotations

import re

from flask import current_app, flash, g, redirect, render_template, request, url_for
from sqlalchemy import or_

from app.auth import _base_url, bp
from app.auth.forms import EditAccountForm, InviteUserForm, RoleForm
from app.extensions import db, limiter
from app.models import Branch, Permission, Role, TenantUser, User, tenant_user_roles
from app.services import audit, limits, mailer, tokens
from app.services.permissions import ACTIONS, MODULES
from app.utils.context import permission_required
from app.utils.tenancy import tenant_bypass

LOCKED_ROLE = "church_owner"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _my_codes() -> set[str]:
    return g.account.permission_codes()


def _is_owner() -> bool:
    return g.account.is_owner


def grantable(role: Role) -> bool:
    """Can the signed-in person give this role to someone?"""
    if _is_owner():
        return True
    if role.slug == LOCKED_ROLE:
        return False
    return {p.code for p in role.permissions} <= _my_codes()


def _branch_choices():
    branches = db.session.query(Branch).filter_by(is_active=True).order_by(Branch.is_headquarters.desc(), Branch.name).all()
    return [("", "All branches")] + [(b.public_id, b.name) for b in branches]


def _branch_from(public_id: str | None):
    if not public_id:
        return None
    return db.session.query(Branch).filter_by(public_id=public_id).one_or_none()


def _roles_from_form(all_roles: list[Role]) -> list[Role] | None:
    """Selected roles, or None if the request tries to grant something not allowed."""
    wanted = set(request.form.getlist("roles"))
    chosen = [r for r in all_roles if r.public_id in wanted]
    if len(chosen) != len(wanted) or not all(grantable(r) for r in chosen):
        return None
    return chosen


def _get_account(public_id: str) -> TenantUser:
    return db.session.query(TenantUser).filter_by(public_id=public_id).first_or_404()


_OWNER_MSG = {"suspend": "The church owner cannot be suspended.", "remove": "The church owner cannot be removed.",
              "change the roles of": "The church owner's roles cannot be changed."}


def _protected(account: TenantUser, action: str) -> str | None:
    if account.id == g.account.id:
        return f"You cannot {action} your own account."
    if account.is_owner:
        return _OWNER_MSG[action]
    return None


def _invite_link(user: User) -> str:
    if user.password_hash is None:
        return _base_url() + url_for("auth.set_password", token=tokens.make(user, "set_password"))
    return _base_url() + url_for("auth.login")


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
@bp.get("/team")
@permission_required("users.view")
def users():
    q = request.args.get("q", "").strip()
    role_id = request.args.get("role", "")
    status = request.args.get("status", "")
    query = db.session.query(TenantUser).join(User, User.id == TenantUser.user_id)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(User.first_name.ilike(like), User.last_name.ilike(like), User.email.ilike(like)))
    if role_id:
        query = query.join(tenant_user_roles, tenant_user_roles.c.tenant_user_id == TenantUser.id) \
                     .join(Role, Role.id == tenant_user_roles.c.role_id).filter(Role.public_id == role_id)
    if status == "active":
        query = query.filter(TenantUser.is_suspended.is_(False))
    elif status == "suspended":
        query = query.filter(TenantUser.is_suspended.is_(True))
    accounts = query.order_by(TenantUser.is_owner.desc(), User.first_name, User.last_name).all()
    roles = db.session.query(Role).order_by(Role.is_system.desc(), Role.name).all()
    return render_template("auth/users.html", accounts=accounts, role_options=[(r.public_id, r.name) for r in roles],
                           q=q, role_id=role_id, status=status,
                           limit=limits.check("users"),
                           breadcrumbs=[("Administration", None), ("Users & roles", None)])


@bp.route("/team/invite", methods=["GET", "POST"])
@permission_required("users.create")
@limiter.limit("30 per hour", methods=["POST"])
def invite_user():
    limit = limits.check("users")
    if not limit.allowed:
        flash(f"Your plan allows {limit.limit} users. Upgrade your plan to add more.", "warning")
        return redirect(url_for("auth.users"))
    form = InviteUserForm()
    form.branch.choices = _branch_choices()
    all_roles = db.session.query(Role).order_by(Role.is_system.desc(), Role.name).all()
    selected = set(request.form.getlist("roles")) if request.method == "POST" else set()
    if form.validate_on_submit():
        roles = _roles_from_form(all_roles)
        email = form.email.data.strip().lower()
        if roles is None:
            flash("You can only give roles whose permissions you have yourself.", "danger")
        elif not roles:
            flash("Choose at least one role.", "warning")
        else:
            with tenant_bypass():
                user = db.session.query(User).filter_by(email=email).one_or_none()
            if user and db.session.query(TenantUser.id).filter_by(user_id=user.id).first():
                form.email.errors.append("This person already has an account in your church.")
            else:
                is_new = user is None
                if is_new:
                    user = User(email=email, first_name=form.first_name.data.strip(),
                                last_name=form.last_name.data.strip())
                    with tenant_bypass():
                        db.session.add(user)
                        db.session.flush()
                account = TenantUser(user_id=user.id, branch=_branch_from(form.branch.data), roles=roles)
                db.session.add(account)
                db.session.flush()
                audit.record("user.invited", entity=account, actor=g.user, name=user.full_name,
                             roles=[r.slug for r in roles])
                db.session.commit()
                mailer.send(user.email, f"You have been invited to {g.tenant.name} on ChurchPlus", "user_invite",
                            user=user, church=g.tenant, inviter=g.user, link=_invite_link(user),
                            is_new=user.password_hash is None, roles=roles)
                flash(f"Invitation sent to {user.email}.", "success")
                return redirect(url_for("auth.users"))
    return render_template("auth/user_form.html", form=form, roles=all_roles, selected=selected, account=None,
                           grantable=grantable,
                           breadcrumbs=[("Users & roles", url_for("auth.users")), ("Invite", None)])


@bp.route("/team/<public_id>", methods=["GET", "POST"])
@permission_required("users.edit")
def edit_user(public_id: str):
    account = _get_account(public_id)
    form = EditAccountForm(branch=account.branch.public_id if account.branch else "")
    form.branch.choices = _branch_choices()
    all_roles = db.session.query(Role).order_by(Role.is_system.desc(), Role.name).all()
    selected = ({r.public_id for r in account.roles} if request.method == "GET"
                else set(request.form.getlist("roles")))
    if form.validate_on_submit():
        reason = _protected(account, "change the roles of")
        roles = _roles_from_form(all_roles)
        # Roles the editor cannot grant must not be removed by them either.
        kept_locked = [r for r in account.roles if not grantable(r)]
        if reason:
            flash(reason, "warning")
        elif roles is None:
            flash("You can only give roles whose permissions you have yourself.", "danger")
        elif not roles and not kept_locked:
            flash("Choose at least one role.", "warning")
        else:
            before = sorted(r.slug for r in account.roles)
            account.roles = list({r.id: r for r in roles + kept_locked}.values())
            account.branch = _branch_from(form.branch.data)
            audit.record("user.updated", entity=account, actor=g.user, name=account.user.full_name,
                         roles_before=before, roles_after=sorted(r.slug for r in account.roles))
            db.session.commit()
            flash("Changes saved.", "success")
            return redirect(url_for("auth.users"))
    return render_template("auth/user_form.html", form=form, roles=all_roles, selected=selected, account=account,
                           grantable=grantable,
                           breadcrumbs=[("Users & roles", url_for("auth.users")), (account.user.full_name, None)])


@bp.post("/team/<public_id>/suspend")
@permission_required("users.edit")
def toggle_user(public_id: str):
    account = _get_account(public_id)
    reason = _protected(account, "suspend")
    if reason:
        flash(reason, "warning")
        return redirect(url_for("auth.users"))
    if account.is_suspended and not limits.check("users").allowed:
        flash("Your plan's user limit has been reached.", "warning")
        return redirect(url_for("auth.users"))
    if not account.is_suspended and not all(grantable(r) for r in account.roles):
        flash("You cannot suspend someone with more access than you.", "danger")
        return redirect(url_for("auth.users"))
    account.is_suspended = not account.is_suspended
    audit.record("user.suspended" if account.is_suspended else "user.reactivated", entity=account,
                 actor=g.user, name=account.user.full_name)
    db.session.commit()
    flash(f"{account.user.full_name} {'suspended' if account.is_suspended else 'reactivated'}.", "success")
    return redirect(url_for("auth.users"))


@bp.post("/team/<public_id>/remove")
@permission_required("users.delete")
def remove_user(public_id: str):
    account = _get_account(public_id)
    reason = _protected(account, "remove")
    if reason or not all(grantable(r) for r in account.roles):
        flash(reason or "You cannot remove someone with more access than you.", "warning")
        return redirect(url_for("auth.users"))
    name = account.user.full_name
    audit.record("user.removed", entity=account, actor=g.user, name=name)
    db.session.delete(account)
    db.session.commit()
    flash(f"{name} no longer has access to {g.tenant.name}.", "success")
    return redirect(url_for("auth.users"))


@bp.post("/team/<public_id>/resend")
@permission_required("users.create")
@limiter.limit("10 per hour")
def resend_invite(public_id: str):
    account = _get_account(public_id)
    user = account.user
    mailer.send(user.email, f"You have been invited to {g.tenant.name} on ChurchPlus", "user_invite",
                user=user, church=g.tenant, inviter=g.user, link=_invite_link(user),
                is_new=user.password_hash is None, roles=account.roles)
    flash(f"Invitation sent again to {user.email}.", "success")
    return redirect(url_for("auth.users"))


# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #
def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:70] or "role"


def _matrix_context(role: Role | None, chosen: set[str]) -> dict:
    mine = _my_codes()
    return {"modules": MODULES, "actions": ACTIONS, "chosen": chosen,
            "allowed": (lambda code: _is_owner() or code in mine)}


def _save_permissions(role: Role) -> bool:
    wanted = set(request.form.getlist("perm"))
    perms = db.session.query(Permission).filter(Permission.code.in_(wanted)).all() if wanted else []
    if len(perms) != len(wanted):
        return False
    if not _is_owner() and not wanted <= _my_codes():
        return False
    role.permissions = perms
    return True


@bp.get("/roles")
@permission_required("users.view")
def roles():
    rows = db.session.query(Role).order_by(Role.is_system.desc(), Role.name).all()
    counts = dict(db.session.query(tenant_user_roles.c.role_id, db.func.count())
                  .join(TenantUser, TenantUser.id == tenant_user_roles.c.tenant_user_id)
                  .group_by(tenant_user_roles.c.role_id).all())
    return render_template("auth/roles.html", roles=rows, counts=counts, total=len(MODULES) * len(ACTIONS),
                           breadcrumbs=[("Users & roles", url_for("auth.users")), ("Roles", None)])


@bp.route("/roles/new", methods=["GET", "POST"])
@permission_required("users.manage_settings")
def role_create():
    form = RoleForm()
    chosen = set(request.form.getlist("perm"))
    if form.validate_on_submit():
        slug = _slugify(form.name.data)
        if db.session.query(Role.id).filter_by(slug=slug).first():
            form.name.errors.append("A role with this name already exists.")
        else:
            role = Role(name=form.name.data.strip(), slug=slug, description=form.description.data or None,
                        is_branch_scoped=form.is_branch_scoped.data, is_system=False)
            if not _save_permissions(role):
                flash("You can only give permissions you have yourself.", "danger")
            else:
                db.session.add(role)
                db.session.flush()
                audit.record("role.created", entity=role, actor=g.user, name=role.name,
                             permissions=sorted(p.code for p in role.permissions))
                db.session.commit()
                flash(f"Role “{role.name}” created.", "success")
                return redirect(url_for("auth.roles"))
    return render_template("auth/role_form.html", form=form, role=None, **_matrix_context(None, chosen),
                           breadcrumbs=[("Roles", url_for("auth.roles")), ("New role", None)])


@bp.route("/roles/<public_id>", methods=["GET", "POST"])
@permission_required("users.manage_settings")
def role_edit(public_id: str):
    role = db.session.query(Role).filter_by(public_id=public_id).first_or_404()
    if role.slug == LOCKED_ROLE:
        flash("The Church Owner role always has full access and cannot be changed.", "info")
        return redirect(url_for("auth.roles"))
    form = RoleForm(obj=role)
    chosen = ({p.code for p in role.permissions} if request.method == "GET" else set(request.form.getlist("perm")))
    if form.validate_on_submit():
        before = sorted(p.code for p in role.permissions)
        wanted = set(request.form.getlist("perm"))
        valid = {c for (c,) in db.session.query(Permission.code)}
        mine = valid if _is_owner() else _my_codes()
        keep = {c for c in before if c not in mine}      # permissions the editor lacks stay as they were
        if not wanted <= valid or (wanted - keep) - mine:
            flash("You can only give permissions you have yourself.", "danger")
            return redirect(url_for("auth.role_edit", public_id=public_id))
        final = (wanted & mine) | keep
        role.permissions = db.session.query(Permission).filter(Permission.code.in_(final)).all() if final else []
        if not role.is_system:
            role.name = form.name.data.strip()
        role.description = form.description.data or None
        role.is_branch_scoped = form.is_branch_scoped.data
        audit.record("role.updated", entity=role, actor=g.user, name=role.name, permissions_before=before,
                     permissions_after=sorted(p.code for p in role.permissions))
        db.session.commit()
        flash(f"Role “{role.name}” saved.", "success")
        return redirect(url_for("auth.roles"))
    return render_template("auth/role_form.html", form=form, role=role, **_matrix_context(role, chosen),
                           breadcrumbs=[("Roles", url_for("auth.roles")), (role.name, None)])


@bp.post("/roles/<public_id>/delete")
@permission_required("users.manage_settings")
def role_delete(public_id: str):
    role = db.session.query(Role).filter_by(public_id=public_id).first_or_404()
    in_use = db.session.query(tenant_user_roles).filter(tenant_user_roles.c.role_id == role.id).first()
    if role.is_system:
        flash("Built-in roles cannot be deleted.", "warning")
    elif in_use:
        flash("This role is still assigned to people. Change their roles first.", "warning")
    else:
        audit.record("role.deleted", entity=role, actor=g.user, name=role.name)
        db.session.delete(role)
        db.session.commit()
        flash("Role deleted.", "success")
    return redirect(url_for("auth.roles"))
