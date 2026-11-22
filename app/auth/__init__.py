"""Sign-in, two-step sign-in, sign-out, registration, password recovery,
email verification, my account, and church switching.

Team users and roles live in ``app/auth/team.py`` on the same blueprint.
"""
from __future__ import annotations

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for
from flask_login import current_user, login_user, logout_user

from app.auth.forms import (
    CURRENCY_BY_COUNTRY, TIMEZONE_BY_COUNTRY, ChangePasswordForm, ConfirmPasswordForm, EnableTwoFactorForm,
    ForgotPasswordForm, LoginForm, ProfileForm, RegisterChurchForm, SetPasswordForm, TwoFactorForm,
)
from app.extensions import db, limiter
from app.models import Plan, Tenant, TenantKind, TenantStatus, TenantUser, User
from app.models.base import utcnow
from app.services import audit, mailer, security, tokens
from app.services.provisioning import provision_church
from app.utils.context import accounts_for, login_required, select_church
from app.utils.tenancy import tenant_bypass

bp = Blueprint("auth", __name__)

PENDING_2FA_KEY = "cp_2fa_user"
_DUMMY_HASH = None


def _dummy_check(password: str) -> None:
    """Spend the same time on unknown emails so timing does not reveal accounts."""
    global _DUMMY_HASH
    from werkzeug.security import check_password_hash, generate_password_hash
    if _DUMMY_HASH is None:
        _DUMMY_HASH = generate_password_hash("not-a-real-password", method="scrypt")
    check_password_hash(_DUMMY_HASH, password)


def _base_url() -> str:
    return current_app.config.get("PUBLIC_BASE_URL") or request.host_url.rstrip("/")


def _safe_next(default: str) -> str:
    nxt = session.pop("cp_next", None)
    if nxt and nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt:
        return nxt
    return default


def _start_session(user: User, remember: bool = False) -> str:
    """Complete sign-in, pick the church and return where to go next."""
    next_url = session.get("cp_next")
    session.clear()                         # prevent session fixation
    if next_url:
        session["cp_next"] = next_url
    login_user(user, remember=remember)
    session.permanent = True
    with tenant_bypass():
        user.last_login_at = utcnow()
        usable = [a.tenant_id for a in accounts_for(user) if not a.is_suspended]
        is_staff = user.is_platform_staff
        security.record_login("success", user)
        audit.record("auth.login", actor=user, tenant_id=None, entity=user)
        db.session.commit()
    if len(usable) == 1:
        select_church(usable[0])
        return _safe_next(url_for("dashboard.index"))
    if not usable and is_staff:
        return url_for("platform.index")
    return url_for("auth.choose_church")


def send_verification(user: User) -> None:
    link = _base_url() + url_for("auth.verify_email", token=tokens.make(user, "verify_email"))
    mailer.send(user.email, "Confirm your email address for ChurchPlus", "verify_email", user=user, link=link)


# --------------------------------------------------------------------------- #
# Sign in / out
# --------------------------------------------------------------------------- #
@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute; 50 per hour", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index") if g.account else url_for("auth.choose_church"))
    form = LoginForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        with tenant_bypass():
            user = db.session.query(User).filter_by(email=email).one_or_none()
            locked = user is not None and security.is_locked(user)
        if user is None:
            _dummy_check(form.password.data)
        if locked:
            with tenant_bypass():
                security.record_login("locked", user)
                db.session.commit()
            flash("Too many unsuccessful attempts. Please wait 15 minutes or reset your password.", "danger")
        elif user is None or not user.check_password(form.password.data):
            with tenant_bypass():
                security.record_login("failed", user, email=email)
                db.session.commit()
            flash("The email or password is not correct.", "danger")
        elif not user.is_active:
            flash("This account has been suspended. Please contact ChurchPlus support.", "danger")
        elif user.two_factor_enabled:
            next_url = session.get("cp_next")
            session.clear()
            if next_url:
                session["cp_next"] = next_url
            session[PENDING_2FA_KEY] = {"u": user.public_id, "r": bool(form.remember.data),
                                        "t": utcnow().timestamp()}
            return redirect(url_for("auth.two_factor"))
        else:
            return redirect(_start_session(user, form.remember.data))
    return render_template("auth/login.html", form=form)


@bp.route("/login/verify", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def two_factor():
    pending = session.get(PENDING_2FA_KEY)
    if not pending or utcnow().timestamp() - pending.get("t", 0) > 300:
        session.pop(PENDING_2FA_KEY, None)
        flash("Please sign in again.", "info")
        return redirect(url_for("auth.login"))
    with tenant_bypass():
        user = db.session.query(User).filter_by(public_id=pending["u"]).one_or_none()
    if user is None or not user.is_active:
        session.clear()
        return redirect(url_for("auth.login"))
    form = TwoFactorForm()
    if form.validate_on_submit():
        code = form.code.data.strip()
        with tenant_bypass():
            ok = security.verify_totp(user.totp_secret, code) or security.use_recovery_code(user, code)
            if not ok:
                security.record_login("two_factor_failed", user)
                locked = security.is_locked(user)
            db.session.commit()
        if ok:
            used_recovery = not security.verify_totp(user.totp_secret, code)
            session.pop(PENDING_2FA_KEY, None)
            target = _start_session(user, pending.get("r", False))
            if used_recovery:
                left = len(user.recovery_codes or [])
                flash(f"Recovery code used. {left} left. Consider generating new codes in My account.", "warning")
            return redirect(target)
        if locked:
            session.clear()
            flash("Too many unsuccessful attempts. Please wait 15 minutes.", "danger")
            return redirect(url_for("auth.login"))
        form.code.errors.append("That code is not correct. Check your authenticator app and try again.")
    return render_template("auth/two_factor.html", form=form)


@bp.post("/logout")
def logout():
    if current_user.is_authenticated:
        with tenant_bypass():
            security.record_login("logout", current_user)
            audit.record("auth.logout", actor=current_user, tenant_id=None, entity=current_user)
            db.session.commit()
    logout_user()
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
@bp.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))
    form = RegisterChurchForm()
    with tenant_bypass():
        plans = db.session.query(Plan).filter_by(is_public=True, is_active=True).order_by(Plan.sort_order).all()
    form.plan.choices = [(p.code, f"{p.name} – {p.currency} {p.monthly_price:,.0f}/month" if p.monthly_price else p.name)
                         for p in plans] or [("starter", "Starter")]
    if request.method == "GET" and request.args.get("plan") in {c for c, _ in form.plan.choices}:
        form.plan.data = request.args["plan"]

    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        with tenant_bypass():
            exists = db.session.query(User.id).filter_by(email=email).first()
        if exists:
            form.email.errors.append("An account with this email already exists. Sign in instead.")
        else:
            country = form.country.data
            needs_approval = current_app.config.get("REGISTRATION_REQUIRES_APPROVAL", False)
            owner = User(email=email, first_name=form.first_name.data.strip(), last_name=form.last_name.data.strip())
            owner.set_password(form.password.data)
            with tenant_bypass():
                tenant = provision_church(
                    name=form.church_name.data.strip(), owner=owner, plan_code=form.plan.data,
                    country=country, currency=CURRENCY_BY_COUNTRY.get(country, "USD"),
                    timezone=TIMEZONE_BY_COUNTRY.get(country, "UTC"),
                    city=form.city.data.strip(), phone=(form.church_phone.data or "").strip() or None,
                    email=email, kind=TenantKind(form.kind.data),
                    status=TenantStatus.PENDING if needs_approval else TenantStatus.TRIAL,
                )
                if tenant.kind == TenantKind.DENOMINATION_HQ:
                    from app.services.denominations import ensure_denomination
                    ensure_denomination(tenant)
                db.session.commit()
                tenant_id = tenant.id
                tenant_name = tenant.name
            _start_session(owner)
            select_church(tenant_id)
            send_verification(owner)
            flash(f"Welcome to ChurchPlus! {tenant_name} is ready. We've sent a link to confirm your email.", "success")
            return redirect(url_for("dashboard.index"))
    return render_template("auth/register.html", form=form)


# --------------------------------------------------------------------------- #
# Passwords and email verification
# --------------------------------------------------------------------------- #
@bp.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("5 per 15 minutes", methods=["POST"])
def forgot_password():
    form = ForgotPasswordForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        with tenant_bypass():
            user = db.session.query(User).filter_by(email=email).one_or_none()
        if user is not None and user.is_active:
            link = _base_url() + url_for("auth.reset_password", token=tokens.make(user, "reset_password"))
            mailer.send(user.email, "Reset your ChurchPlus password", "reset_password", user=user, link=link)
            with tenant_bypass():
                audit.record("auth.password_reset_requested", tenant_id=None, entity=user)
                db.session.commit()
        # Same answer whether or not the address exists.
        flash("If that email has a ChurchPlus account, a reset link is on its way. It expires in 1 hour.", "info")
        return redirect(url_for("auth.login"))
    return render_template("auth/forgot_password.html", form=form)


def _password_link(token: str, purpose: str, title: str):
    user = tokens.read(token, purpose)
    if user is None:
        flash("This link has expired or has already been used.", "danger")
        return redirect(url_for("auth.forgot_password" if purpose == "reset_password" else "auth.login"))
    form = SetPasswordForm()
    if form.validate_on_submit():
        with tenant_bypass():
            user.set_password(form.password.data)
            user.end_other_sessions()           # sign out everywhere else
            if user.email_verified_at is None:
                user.email_verified_at = utcnow()   # they proved access to the inbox
            audit.record("auth.password_set" if purpose == "set_password" else "auth.password_reset",
                         actor=user, tenant_id=None, entity=user)
            db.session.commit()
        if user.two_factor_enabled:
            flash("Password saved. Sign in with your new password and authentication code.", "success")
            return redirect(url_for("auth.login"))
        flash("Your password has been saved.", "success")
        return redirect(_start_session(user))
    return render_template("auth/set_password.html", form=form, user=user, title=title)


@bp.route("/set-password/<token>", methods=["GET", "POST"])
@limiter.limit("20 per hour", methods=["POST"])
def set_password(token: str):
    """Invitation link: an invited person chooses their password."""
    return _password_link(token, "set_password", "Choose your password")


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
@limiter.limit("20 per hour", methods=["POST"])
def reset_password(token: str):
    return _password_link(token, "reset_password", "Choose a new password")


@bp.get("/verify-email/<token>")
def verify_email(token: str):
    user = tokens.read(token, "verify_email")
    if user is None:
        flash("This verification link has expired or has already been used.", "danger")
    else:
        with tenant_bypass():
            user.email_verified_at = utcnow()
            audit.record("auth.email_verified", actor=user, tenant_id=None, entity=user)
            db.session.commit()
        flash("Thank you — your email address is confirmed.", "success")
    return redirect(url_for("dashboard.index") if current_user.is_authenticated else url_for("auth.login"))


@bp.post("/verify-email/resend")
@login_required
@limiter.limit("3 per hour")
def resend_verification():
    if g.user.email_verified_at is None:
        send_verification(g.user)
        flash(f"A new confirmation link has been sent to {g.user.email}.", "info")
    return redirect(request.referrer if (request.referrer or "").startswith(request.host_url) else url_for("dashboard.index"))


# --------------------------------------------------------------------------- #
# My account
# --------------------------------------------------------------------------- #
@bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    profile = ProfileForm(obj=g.user, prefix="p")
    password = ChangePasswordForm(prefix="pw")
    if request.method == "POST" and request.form.get("action") == "profile" and profile.validate():
        with tenant_bypass():
            profile.populate_obj(g.user)
            g.user.phone = g.user.phone or None
            audit.record("account.profile_updated", actor=g.user, tenant_id=None, entity=g.user)
            db.session.commit()
        flash("Your details have been saved.", "success")
        return redirect(url_for("auth.account"))
    if request.method == "POST" and request.form.get("action") == "password" and password.validate():
        if not g.user.check_password(password.current.data):
            password.current.errors.append("Your current password is not correct.")
        else:
            with tenant_bypass():
                g.user.set_password(password.password.data)
                g.user.end_other_sessions()
                audit.record("account.password_changed", actor=g.user, tenant_id=None, entity=g.user)
                db.session.commit()
            login_user(g.user)                  # keep this device signed in with the new session id
            flash("Password changed. You have been signed out on your other devices.", "success")
            return redirect(url_for("auth.account"))
    with tenant_bypass():
        logins = security.recent_logins(g.user)
    return render_template("auth/account.html", profile=profile, password=password, logins=logins,
                           describe_agent=security.describe_agent, confirm=ConfirmPasswordForm(),
                           breadcrumbs=[("My account", None)])


@bp.post("/account/sign-out-others")
@login_required
def sign_out_others():
    with tenant_bypass():
        g.user.end_other_sessions()
        audit.record("account.sessions_ended", actor=g.user, tenant_id=None, entity=g.user)
        db.session.commit()
    login_user(g.user)
    flash("You have been signed out on all other devices.", "success")
    return redirect(url_for("auth.account"))


@bp.route("/account/two-step", methods=["GET", "POST"])
@login_required
def two_factor_setup():
    if g.user.two_factor_enabled:
        return redirect(url_for("auth.account"))
    secret = session.get("cp_totp_setup") or security.new_totp_secret()
    session["cp_totp_setup"] = secret
    form = EnableTwoFactorForm()
    if form.validate_on_submit():
        if security.verify_totp(secret, form.code.data):
            with tenant_bypass():
                codes = security.enable_two_factor(g.user, secret)
                audit.record("account.two_factor_enabled", actor=g.user, tenant_id=None, entity=g.user)
                db.session.commit()
            session.pop("cp_totp_setup", None)
            flash("Two-step sign-in is on.", "success")
            return render_template("auth/recovery_codes.html", codes=codes, breadcrumbs=[("My account", url_for("auth.account")), ("Recovery codes", None)])
        form.code.errors.append("That code is not correct. Make sure your phone's clock is set automatically.")
    uri = security.provisioning_uri(g.user, secret)
    return render_template("auth/two_factor_setup.html", form=form, secret=secret, qr=security.qr_svg(uri),
                           breadcrumbs=[("My account", url_for("auth.account")), ("Two-step sign-in", None)])


@bp.post("/account/two-step/disable")
@login_required
def two_factor_disable():
    form = ConfirmPasswordForm()
    if form.validate_on_submit() and g.user.check_password(form.password.data):
        with tenant_bypass():
            security.disable_two_factor(g.user)
            audit.record("account.two_factor_disabled", actor=g.user, tenant_id=None, entity=g.user)
            db.session.commit()
        flash("Two-step sign-in is off.", "warning")
    else:
        flash("Your password was not correct, so two-step sign-in is still on.", "danger")
    return redirect(url_for("auth.account"))


@bp.post("/account/two-step/recovery-codes")
@login_required
def regenerate_recovery_codes():
    form = ConfirmPasswordForm()
    if not (g.user.two_factor_enabled and form.validate_on_submit() and g.user.check_password(form.password.data)):
        flash("Your password was not correct.", "danger")
        return redirect(url_for("auth.account"))
    codes = security.new_recovery_codes()
    with tenant_bypass():
        g.user.recovery_codes = security.hash_codes(codes)
        audit.record("account.recovery_codes_regenerated", actor=g.user, tenant_id=None, entity=g.user)
        db.session.commit()
    return render_template("auth/recovery_codes.html", codes=codes,
                           breadcrumbs=[("My account", url_for("auth.account")), ("Recovery codes", None)])


# --------------------------------------------------------------------------- #
# Choosing a church
# --------------------------------------------------------------------------- #
@bp.get("/churches")
@login_required
def choose_church():
    accounts = accounts_for(g.user)
    return render_template("auth/choose_church.html", accounts=accounts)


@bp.post("/churches/<public_id>")
@login_required
def switch_church(public_id: str):
    with tenant_bypass():
        tenant = db.session.query(Tenant).filter_by(public_id=public_id).one_or_none()
        account = (db.session.query(TenantUser).filter_by(tenant_id=tenant.id, user_id=g.user.id).one_or_none()
                   if tenant else None)
        suspended = account.is_suspended if account else None
        tenant_id = tenant.id if tenant else None
    if account is None:
        flash("You do not have access to that church.", "danger")
        return redirect(url_for("auth.choose_church"))
    if suspended:
        flash("Your account in that church has been suspended.", "danger")
        return redirect(url_for("auth.choose_church"))
    select_church(tenant_id)
    return redirect(url_for("dashboard.index"))


from app.auth import team  # noqa: E402,F401  (registers team routes on this blueprint)
