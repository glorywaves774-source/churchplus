"""Application factory, configuration, health check, pages and security headers."""
import pytest

from app import create_app
from app.config import ProductionConfig, get_config, normalise_database_url


def test_factory_uses_testing_config(app):
    assert app.config["TESTING"] is True
    assert app.config["APP_NAME"] == "ChurchPlus"


def test_health_check_reports_database(client):
    res = client.get("/healthz")
    assert res.status_code == 200
    body = res.get_json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["version"]


def test_home_page_renders_with_theme_toggle_and_viewport(client):
    res = client.get("/")
    html = res.get_data(as_text=True)
    assert res.status_code == 200
    assert 'name="viewport" content="width=device-width, initial-scale=1' in html
    assert "data-theme-toggle" in html
    assert "theme-init.js" in html
    assert "<script>" not in html  # strict CSP: no inline scripts


def test_static_assets_served(client):
    for path in ("/static/css/core.css", "/static/css/dark.css", "/static/css/mobile.css",
                 "/static/js/theme-init.js", "/static/js/theme-toggle.js"):
        assert client.get(path).status_code == 200, path


def test_security_headers(client):
    res = client.get("/")
    csp = res.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["X-Frame-Options"] == "DENY"


def test_hsts_only_over_https(client):
    assert "Strict-Transport-Security" not in client.get("/").headers
    res = client.get("/", base_url="https://churchplus.test")
    assert "Strict-Transport-Security" in res.headers


def test_404_html_and_json(client):
    assert client.get("/missing").status_code == 404
    res = client.get("/api/v1/missing")
    assert res.status_code == 404 and res.get_json()["error"] == "Not found"


def test_api_index(client):
    body = client.get("/api/v1/").get_json()
    assert body["api_version"] == "v1"


@pytest.mark.parametrize("raw,expected", [
    ("postgres://u:p@h:5432/d", "postgresql+psycopg://u:p@h:5432/d"),
    ("postgresql://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
    ("postgresql+psycopg://u@h/d", "postgresql+psycopg://u@h/d"),
    ("sqlite:///x.db", "sqlite:///x.db"),
    (None, None),
])
def test_database_url_normalised(raw, expected):
    assert normalise_database_url(raw) == expected


def test_unknown_config_rejected():
    with pytest.raises(RuntimeError):
        get_config("staging-typo")


def test_production_refuses_missing_secrets(monkeypatch):
    monkeypatch.setattr(ProductionConfig, "SECRET_KEY", None)
    monkeypatch.setattr(ProductionConfig, "SQLALCHEMY_DATABASE_URI", None)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app("production")


def test_production_refuses_sqlite(monkeypatch):
    monkeypatch.setattr(ProductionConfig, "SECRET_KEY", "x" * 40)
    monkeypatch.setattr(ProductionConfig, "SQLALCHEMY_DATABASE_URI", "sqlite:///prod.db")
    with pytest.raises(RuntimeError, match="SQLite"):
        create_app("production")


def test_production_cookies_secure():
    assert ProductionConfig.SESSION_COOKIE_SECURE is True
    assert ProductionConfig.SESSION_COOKIE_HTTPONLY is True
