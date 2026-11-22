"""Versioned REST API (``/api/v1``). Module endpoints are added stage by stage."""
from flask import Blueprint, current_app, jsonify

from app.extensions import csrf

bp = Blueprint("api", __name__)
csrf.exempt(bp)  # API uses token auth (Stage 3), not browser cookies with forms.


@bp.get("/")
def index():
    return jsonify(name=current_app.config["APP_NAME"], api_version="v1",
                   version=current_app.config["APP_VERSION"])
