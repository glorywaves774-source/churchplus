"""Audit trail: who did what in the church workspace, with filters and CSV export."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

from flask import Blueprint, Response, g, render_template, request
from sqlalchemy import or_

from app.extensions import db
from app.models import AuditLog, User
from app.services.audit import CATEGORIES, describe
from app.services.members import safe_cell
from app.utils.context import permission_required
from app.utils.tenancy import tenant_bypass

bp = Blueprint("audit", __name__)


def _parse_date(value: str | None):
    try:
        return datetime.strptime(value, "%Y-%m-%d") if value else None
    except ValueError:
        return None


def _filtered():
    category = request.args.get("category", "")
    start = _parse_date(request.args.get("from"))
    end = _parse_date(request.args.get("to"))
    query = db.session.query(AuditLog)
    if category in CATEGORIES:
        query = query.filter(or_(*[AuditLog.action.startswith(p) for p in CATEGORIES[category][1]]))
    if start:
        query = query.filter(AuditLog.created_at >= start)
    if end:
        query = query.filter(AuditLog.created_at < end + timedelta(days=1))
    return query.order_by(AuditLog.created_at.desc()), category


def _actors(entries) -> dict[int, str]:
    ids = {e.actor_user_id for e in entries if e.actor_user_id}
    if not ids:
        return {}
    with tenant_bypass():   # names only; users are global identities
        return {u.id: u.full_name for u in db.session.query(User).filter(User.id.in_(ids))}


@bp.get("/audit")
@permission_required("audit.view")
def index():
    query, category = _filtered()
    page = query.paginate(page=request.args.get("page", 1, type=int), per_page=25, error_out=False)
    names = _actors(page.items)
    rows = [describe(e) | {"actor": names.get(e.actor_user_id, "System"), "ip": e.ip_address,
                           "entity": e.entity_type} for e in page.items]
    params = {k: v for k, v in request.args.items() if k != "page" and v}
    return render_template("audit/index.html", rows=rows, page=page, category=category, categories=CATEGORIES,
                           params=params, breadcrumbs=[("Administration", None), ("Audit trail", None)])




@bp.get("/audit/export.csv")
@permission_required("audit.export")
def export():
    query, _ = _filtered()
    entries = query.limit(50_000).all()
    names = _actors(entries)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Date (UTC)", "Activity", "Action code", "By", "Record type", "Record ID", "IP address"])
    for e in entries:
        writer.writerow([safe_cell(v) for v in (
            e.created_at.strftime("%Y-%m-%d %H:%M:%S"), describe(e)["text"], e.action,
            names.get(e.actor_user_id, "System"), e.entity_type, e.entity_id, e.ip_address)])
    filename = f"{g.tenant.slug}-audit-{datetime.now(timezone.utc):%Y%m%d}.csv"
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
