"""Application configuration.

All secrets and environment-specific values come from environment variables.
Nothing sensitive is ever hard-coded here.
"""
from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def normalise_database_url(url: str | None) -> str | None:
    """Render exposes ``postgres://`` URLs. Point them at the psycopg 3 driver explicitly."""
    if not url:
        return url
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


class BaseConfig:
    APP_NAME = "ChurchPlus"
    COMPANY_NAME = "Kayron Global Tech Services"
    APP_VERSION = "1.1.1"  # Aurora UI + fixes

    SECRET_KEY = os.environ.get("SECRET_KEY")

    SQLALCHEMY_DATABASE_URI = normalise_database_url(os.environ.get("DATABASE_URL"))
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # Sessions and cookies
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", False)
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", False)
    PERMANENT_SESSION_LIFETIME = timedelta(hours=int(os.environ.get("SESSION_HOURS", "12")))

    # CSRF
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = 60 * 60 * 4

    # Uploads
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_UPLOAD_MB", "10")) * 1024 * 1024
    STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "local")  # local | s3
    LOCAL_STORAGE_PATH = os.environ.get(
        "LOCAL_STORAGE_PATH", str(BASE_DIR / "instance" / "private_uploads")
    )
    S3_BUCKET = os.environ.get("S3_BUCKET")
    S3_REGION = os.environ.get("S3_REGION")
    S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL")  # e.g. Cloudflare R2 / Backblaze
    S3_ACCESS_KEY_ID = os.environ.get("S3_ACCESS_KEY_ID")
    S3_SECRET_ACCESS_KEY = os.environ.get("S3_SECRET_ACCESS_KEY")

    # Rate limiting
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_DEFAULT = os.environ.get("RATELIMIT_DEFAULT", "300 per minute")
    RATELIMIT_HEADERS_ENABLED = True

    # Localisation defaults
    DEFAULT_CURRENCY = os.environ.get("DEFAULT_CURRENCY", "GHS")
    SUPPORTED_CURRENCIES = ["GHS", "USD", "GBP", "EUR", "NGN", "KES", "ZAR", "XOF", "CAD"]
    DEFAULT_TIMEZONE = os.environ.get("DEFAULT_TIMEZONE", "Africa/Accra")

    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

    # Accounts
    REGISTRATION_REQUIRES_APPROVAL = _bool("REGISTRATION_REQUIRES_APPROVAL", False)
    PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL")  # e.g. https://app.churchplus.com.gh

    # Transactional email
    MAIL_BACKEND = os.environ.get("MAIL_BACKEND", "console")  # console | smtp
    MAIL_FROM = os.environ.get("MAIL_FROM", "ChurchPlus <no-reply@churchplus.app>")
    MAIL_SMTP_HOST = os.environ.get("MAIL_SMTP_HOST")
    MAIL_SMTP_PORT = int(os.environ.get("MAIL_SMTP_PORT", "587"))
    MAIL_SMTP_USERNAME = os.environ.get("MAIL_SMTP_USERNAME")
    MAIL_SMTP_PASSWORD = os.environ.get("MAIL_SMTP_PASSWORD")
    MAIL_SMTP_STARTTLS = _bool("MAIL_SMTP_STARTTLS", True)

    # Church messaging (Stage 8). Credentials only ever come from environment variables.
    SMS_BACKEND = os.environ.get("SMS_BACKEND", "console")            # console | hubtel | arkesel
    SMS_DEFAULT_SENDER = os.environ.get("SMS_DEFAULT_SENDER", "ChurchPlus")
    HUBTEL_SMS_CLIENT_ID = os.environ.get("HUBTEL_SMS_CLIENT_ID")
    HUBTEL_SMS_CLIENT_SECRET = os.environ.get("HUBTEL_SMS_CLIENT_SECRET")
    ARKESEL_API_KEY = os.environ.get("ARKESEL_API_KEY")
    WHATSAPP_BACKEND = os.environ.get("WHATSAPP_BACKEND", "console")  # console | meta
    WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
    WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    MESSAGES_SEND_NOW_LIMIT = int(os.environ.get("MESSAGES_SEND_NOW_LIMIT", "100"))   # larger sends go to the background job

    # ChurchPlus subscription billing (Stage 10). No card data is ever stored.
    BILLING_PROVIDERS = [p for p in os.environ.get("BILLING_PROVIDERS", "paystack,hubtel").split(",") if p]
    PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY")
    HUBTEL_PAY_CLIENT_ID = os.environ.get("HUBTEL_PAY_CLIENT_ID")
    HUBTEL_PAY_CLIENT_SECRET = os.environ.get("HUBTEL_PAY_CLIENT_SECRET")
    HUBTEL_MERCHANT_ACCOUNT = os.environ.get("HUBTEL_MERCHANT_ACCOUNT")
    BILLING_GRACE_DAYS = int(os.environ.get("BILLING_GRACE_DAYS", "7"))
    BILLING_TEST_CHECKOUT = False        # simulated checkout page (development and tests only)

    # Field encryption key for secrets stored in the database (two-step sign-in). Optional:
    # derived from SECRET_KEY when not set. Changing it requires `flask churchplus rotate-key`.
    FIELD_ENCRYPTION_KEY = os.environ.get("FIELD_ENCRYPTION_KEY")

    # Content Security Policy: no inline scripts, external JS only.
    CONTENT_SECURITY_POLICY = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "font-src 'self'; "
        "img-src 'self' data: blob:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self' https://checkout.paystack.com https://checkout.hubtel.com; "
        "manifest-src 'self'; "
        "worker-src 'self'"
    )


class DevelopmentConfig(BaseConfig):
    ENV_NAME = "development"
    DEBUG = True
    BILLING_TEST_CHECKOUT = True
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-not-for-production")
    SQLALCHEMY_DATABASE_URI = BaseConfig.SQLALCHEMY_DATABASE_URI or (
        "sqlite:///" + str(BASE_DIR / "instance" / "churchplus_dev.db")
    )


class TestingConfig(BaseConfig):
    BILLING_TEST_CHECKOUT = True
    ENV_NAME = "testing"
    TESTING = True
    SECRET_KEY = "test-secret-key"
    SQLALCHEMY_DATABASE_URI = os.environ.get("TEST_DATABASE_URL", "sqlite:///:memory:")
    SQLALCHEMY_ENGINE_OPTIONS = {}
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False
    LOCAL_STORAGE_PATH = None  # set per-test to a temp directory
    MAIL_BACKEND = "console"
    REGISTRATION_REQUIRES_APPROVAL = False


class ProductionConfig(BaseConfig):
    ENV_NAME = "production"
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True
    PREFERRED_URL_SCHEME = "https"

    @classmethod
    def validate(cls) -> None:
        missing = [
            name for name in ("SECRET_KEY", "SQLALCHEMY_DATABASE_URI")
            if not getattr(cls, name)
        ]
        if missing:
            raise RuntimeError(
                "Missing required production settings: " + ", ".join(missing)
                + ". Set SECRET_KEY and DATABASE_URL environment variables."
            )
        if cls.SQLALCHEMY_DATABASE_URI.startswith("sqlite"):
            raise RuntimeError("SQLite is not allowed in production. Use PostgreSQL.")


CONFIGS = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}


def get_config(name: str | None = None):
    name = name or os.environ.get("FLASK_ENV_NAME") or os.environ.get("APP_ENV", "development")
    try:
        return CONFIGS[name]
    except KeyError as exc:
        raise RuntimeError(f"Unknown configuration '{name}'. Use one of {list(CONFIGS)}") from exc
