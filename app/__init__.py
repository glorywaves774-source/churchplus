"""ChurchPlus application factory."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

from app.config import BASE_DIR, get_config
from app.extensions import csrf, db, limiter, login_manager, migrate
from app.utils.tenancy import install_tenant_guards, reset_tenant_state


def create_app(config_name: str | None = None, **overrides) -> Flask:
    config = get_config(config_name)
    if hasattr(config, "validate"):
        config.validate()

    app = Flask(
        __name__,
        template_folder=str(BASE_DIR / "templates"),
        static_folder=str(BASE_DIR / "static"),
        instance_path=str(BASE_DIR / "instance"),
        instance_relative_config=True,
    )
    app.config.from_object(config)
    app.config.update(overrides)
    if app.config.get("TESTING") or app.config.get("ENV_NAME") == "development":
        import email_validator  # allow example domains such as name@church.test
        email_validator.TEST_ENVIRONMENT = True
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)

    _configure_logging(app)
    # Render terminates TLS at its proxy; trust one hop for scheme and client IP.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    _init_extensions(app)
    _register_blueprints(app)
    _register_request_hooks(app)
    _register_error_handlers(app)
    _register_template_globals(app)

    from app.cli import register_cli
    register_cli(app)

    app.logger.info("ChurchPlus %s started (%s)", app.config["APP_VERSION"], config.__name__)
    return app


class _RequestIdFilter(logging.Filter):
    """Adds the request reference to every log line so a user's error can be traced."""
    def filter(self, record):
        from flask import has_request_context
        record.request_id = request.environ.get("cp.request_id", "-") if has_request_context() else "-"
        return True


def _configure_logging(app: Flask) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_RequestIdFilter())
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] [req %(request_id)s] %(message)s"
    ))
    app.logger.handlers[:] = [handler]
    app.logger.setLevel(app.config.get("LOG_LEVEL", "INFO"))
    app.logger.propagate = False


def _init_extensions(app: Flask) -> None:
    from app import models as _models  # noqa: F401  - register every table

    db.init_app(app)
    migrate.init_app(app, db, render_as_batch=True, compare_type=True,
                     directory=str(BASE_DIR / "migrations"))
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)
    install_tenant_guards()

    from app.models import User

    @login_manager.user_loader
    def load_user(session_id: str):
        public_id, _, version = (session_id or "").partition(".")
        user = db.session.query(User).filter_by(public_id=public_id).one_or_none()
        if user is None or str(user.session_version or 1) != version:
            return None
        return user


def _register_blueprints(app: Flask) -> None:
    from app.api import bp as api_bp
    from app.core import bp as core_bp

    from app.audit import bp as audit_bp
    from app.auth import bp as auth_bp
    from app.dashboard import bp as dashboard_bp
    from app.accounting import bp as accounting_bp
    from app.attendance import bp as attendance_bp
    from app.finance import bp as finance_bp
    from app.clergy import bp as clergy_bp
    from app.departments import bp as departments_bp
    from app.members import bp as members_bp
    from app.visitors import bp as visitors_bp
    from app.worship import bp as worship_bp
    from app.tenants import bp as tenants_bp
    from app.events import bp as events_bp
    from app.sermons import bp as sermons_bp
    from app.prayer import bp as prayer_bp
    from app.counselling import bp as counselling_bp
    from app.children import bp as children_bp
    from app.communication import bp as communication_bp
    from app.portal import bp as portal_bp
    from app.website import bp as website_bp
    from app.reports import bp as reports_bp
    from app.billing import bp as billing_bp
    from app.platform import bp as platform_bp

    app.register_blueprint(core_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(tenants_bp)
    app.register_blueprint(audit_bp)
    app.register_blueprint(members_bp)
    app.register_blueprint(visitors_bp)
    app.register_blueprint(attendance_bp)
    app.register_blueprint(worship_bp)
    app.register_blueprint(departments_bp)
    app.register_blueprint(clergy_bp)
    app.register_blueprint(finance_bp)
    app.register_blueprint(accounting_bp)
    for bp in (events_bp, sermons_bp, prayer_bp, counselling_bp, children_bp, communication_bp, portal_bp, website_bp, reports_bp, billing_bp, platform_bp):
        app.register_blueprint(bp)
    app.register_blueprint(api_bp, url_prefix="/api/v1")

    if app.config.get("ENV_NAME") in ("development", "testing"):
        from app.design import bp as design_bp
        app.register_blueprint(design_bp, url_prefix="/design")


def _register_request_hooks(app: Flask) -> None:
    import re
    import time
    import uuid
    rid_ok = re.compile(r"^[A-Za-z0-9-]{8,64}$")

    @app.before_request
    def _request_id():
        incoming = request.headers.get("X-Request-ID", "")
        request.environ["cp.request_id"] = incoming if rid_ok.match(incoming) else uuid.uuid4().hex[:16]
        request.environ["cp.started"] = time.perf_counter()

    @app.before_request
    def _start_clean():
        # Never inherit a tenant from a previous request served by the same worker,
        # then activate the signed-in user's selected church.
        reset_tenant_state()
        if request.endpoint == "static" or request.path == "/healthz":
            return
        from app.utils.context import load_request_context
        load_request_context()

    @app.teardown_request
    def _end_clean(exc):
        reset_tenant_state()

    @app.after_request
    def _security_headers(response):
        h = response.headers
        h.setdefault("Content-Security-Policy", app.config["CONTENT_SECURITY_POLICY"])
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        h.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
        if request.is_secure:
            h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        rid = request.environ.get("cp.request_id")
        if rid:
            h["X-Request-ID"] = rid
        started = request.environ.get("cp.started")
        if started is not None:
            took = time.perf_counter() - started
            if took > app.config.get("SLOW_REQUEST_SECONDS", 2.0):
                app.logger.warning("Slow request %s %s took %.2fs", request.method, request.path, took)
        return response


def _wants_json() -> bool:
    return request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json"


def _register_error_handlers(app: Flask) -> None:
    from app.utils.tenancy import TenantIsolationError

    def _render(code: int, title: str, message: str):
        if _wants_json():
            return jsonify(error=title, message=message, status=code), code
        return render_template("errors/error.html", code=code, title=title, message=message), code

    @app.errorhandler(400)
    def bad_request(e):
        return _render(400, "Bad request", "The request could not be understood.")

    @app.errorhandler(403)
    def forbidden(e):
        return _render(403, "Access denied", "You do not have permission to view this page.")

    @app.errorhandler(TenantIsolationError)
    def tenant_violation(e):
        app.logger.warning("Tenant isolation blocked: %s path=%s", e, request.path)
        # Report as not found so record IDs from other churches are not confirmed to exist.
        return _render(404, "Not found", "The page or record you requested does not exist.")

    @app.errorhandler(404)
    def not_found(e):
        return _render(404, "Not found", "The page or record you requested does not exist.")

    @app.errorhandler(413)
    def too_large(e):
        return _render(413, "File too large", "The file is larger than the allowed upload size.")

    @app.errorhandler(429)
    def rate_limited(e):
        return _render(429, "Too many requests", "Please wait a moment and try again.")

    @app.errorhandler(500)
    def server_error(e):
        db.session.rollback()
        original = getattr(e, "original_exception", None) or e
        app.logger.error("Unhandled error on %s %s", request.method, request.path, exc_info=original)
        ref = request.environ.get("cp.request_id", "")
        return _render(500, "Something went wrong",
                       f"The error has been logged. Please try again. If it keeps happening, quote reference {ref} to support.")


def _register_template_globals(app: Flask) -> None:
    from app import ui
    ui.init_app(app)

    @app.context_processor
    def inject_globals():
        from app.utils.context import shell_context
        ctx = {} if request.blueprint == "design" else shell_context()
        return ctx | {
            "APP_NAME": app.config["APP_NAME"],
            "APP_VERSION": app.config["APP_VERSION"],
            "COMPANY_NAME": app.config["COMPANY_NAME"],
        }
