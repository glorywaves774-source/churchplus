"""Public pages, global search and platform health check."""
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, render_template, request
from sqlalchemy import text

from app.extensions import csrf, db, limiter

bp = Blueprint("core", __name__)


@bp.get("/")
def index():
    return render_template("core/index.html")


@bp.get("/search")
@limiter.limit("60 per minute")
def search():
    from app.services import search as search_service
    from app.ui import can

    q = request.args.get("q", "").strip()[:100]
    results = search_service.run(q, can)
    return render_template("core/search.html", q=q, results=results,
                           breadcrumbs=[("Search", None)])


@bp.get("/healthz")
@limiter.exempt
@csrf.exempt
def health():
    """Used by Render's health check. Confirms the app and database respond."""
    db_ok = True
    try:
        db.session.execute(text("SELECT 1"))
    except Exception:  # pragma: no cover - exercised only when the DB is down
        current_app.logger.exception("Health check database failure")
        db_ok = False
        db.session.rollback()
    body = {
        "status": "ok" if db_ok else "degraded",
        "app": current_app.config["APP_NAME"],
        "version": current_app.config["APP_VERSION"],
        "database": "ok" if db_ok else "unavailable",
        "time": datetime.now(timezone.utc).isoformat(),
    }
    return jsonify(body), (200 if db_ok else 503)


# --------------------------------------------------------------------------- #
# Installable app (Progressive Web App)
# --------------------------------------------------------------------------- #
PRECACHE = ["css/core.css", "css/app.css", "css/dark.css", "css/aurora.css", "css/mobile.css", "js/theme-init.js", "js/theme-toggle.js",
            "js/app.js", "vendor/inter/inter-latin-wght-normal.woff2", "images/favicon.svg",
            "images/icons/icon-192.png", "images/icons/icon-512.png"]


@bp.get("/manifest.webmanifest")
def manifest():
    import json
    from flask import Response, url_for
    data = {
        "id": "/?app=churchplus",
        "name": "ChurchPlus",
        "short_name": "ChurchPlus",
        "description": "Church management: members, attendance, giving, events and more.",
        "start_url": "/dashboard?source=app",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#0b1530",
        "theme_color": "#0b1530",
        "lang": "en",
        "categories": ["productivity", "business", "lifestyle"],
        "icons": [
            {"src": url_for("static", filename="images/icons/icon-192.png"), "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": url_for("static", filename="images/icons/icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": url_for("static", filename="images/icons/maskable-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
        "shortcuts": [
            {"name": "Dashboard", "url": "/dashboard?source=shortcut", "icons": [{"src": url_for("static", filename="images/icons/icon-192.png"), "sizes": "192x192"}]},
            {"name": "Search", "url": "/search?source=shortcut", "icons": [{"src": url_for("static", filename="images/icons/icon-192.png"), "sizes": "192x192"}]},
        ],
    }
    res = Response(json.dumps(data), mimetype="application/manifest+json")
    res.headers["Cache-Control"] = "public, max-age=3600"
    return res


@bp.get("/sw.js")
def service_worker():
    import json
    import os
    from flask import Response, url_for
    with open(os.path.join(current_app.static_folder, "js", "sw.js"), encoding="utf-8") as f:
        js = f.read()
    files = [url_for("static", filename=p) for p in PRECACHE]
    js = js.replace("__VERSION__", current_app.config["APP_VERSION"]).replace("__PRECACHE__", json.dumps(files))
    res = Response(js, mimetype="text/javascript")
    res.headers["Cache-Control"] = "no-cache"
    res.headers["Service-Worker-Allowed"] = "/"
    return res


@bp.get("/offline")
def offline():
    return render_template("core/offline.html")
