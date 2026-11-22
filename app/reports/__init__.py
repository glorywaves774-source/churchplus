"""Reports hub with on-screen charts and CSV, Excel and PDF downloads."""
from __future__ import annotations

from datetime import date, timedelta

from flask import Blueprint, Response, abort, g, render_template, request, url_for

from app.extensions import db
from app.models import Branch
from app.services import audit, members as member_svc, reports as rep
from app.ui import can
from app.utils.context import permission_required

bp = Blueprint("reports", __name__)
PRESETS = [("this_month", "This month"), ("last_month", "Last month"), ("quarter", "Last 3 months"),
           ("year", "This year"), ("last_year", "Last year"), ("12m", "Last 12 months")]


def _period(args) -> tuple[date, date, str]:
    today = date.today()
    preset = args.get("period", "12m")
    if preset == "custom" and args.get("start") and args.get("end"):
        try:
            s, e = date.fromisoformat(args["start"]), date.fromisoformat(args["end"])
            if s <= e and (e - s).days <= 3700:
                return s, e, "custom"
        except ValueError:
            pass
    first = today.replace(day=1)
    if preset == "this_month":
        return first, today, preset
    if preset == "last_month":
        end = first - timedelta(days=1)
        return end.replace(day=1), end, preset
    if preset == "quarter":
        return (first - timedelta(days=62)).replace(day=1), today, preset
    if preset == "year":
        return date(today.year, 1, 1), today, preset
    if preset == "last_year":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31), preset
    return (first - timedelta(days=335)).replace(day=1), today, "12m"


@bp.get("/reports")
@permission_required("reports.view")
def index():
    groups: dict[str, list] = {}
    for r in rep.available(can):
        groups.setdefault(r.group, []).append(r)
    return render_template("reports/index.html", groups=groups, breadcrumbs=[("Engage", None), ("Reports", None)])


@bp.get("/reports/<key>")
@permission_required("reports.view")
def show(key: str):
    d = rep.REGISTRY.get(key)
    if d is None:
        abort(404)
    if not all(g.account.can(n) for n in d.needs):
        abort(403)
    start, end, preset = _period(request.args)
    branch = None
    if request.args.get("branch"):
        branch = next((b for b in member_svc.allowed_branches() if b.public_id == request.args["branch"]), None)
    limit = member_svc.branch_limit_id()
    if branch is None and limit not in (None, -1):
        branch = db.session.get(Branch, limit)
    report = d.build(rep.Params(start, end, branch))
    period = f"{start:%d %b %Y} – {end:%d %b %Y}" if d.uses_dates else ""
    if branch is not None:
        period = (period + " · " if period else "") + branch.name
    fmt = request.args.get("format")
    if fmt:
        if not g.account.can("reports.export"):
            abort(403)
        audit.record("report.exported", entity_type="Report", entity_id=key, actor=g.user, name=d.title, format=fmt)
        db.session.commit()
        name = f"{key}-{date.today():%Y%m%d}"
        if fmt == "csv":
            return Response(rep.to_csv(report), mimetype="text/csv",
                            headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})
        if fmt == "xlsx":
            return Response(rep.to_xlsx(report, g.tenant.name, period),
                            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'})
        if fmt == "pdf":
            return Response(rep.to_pdf(report, g.tenant.name, period), mimetype="application/pdf",
                            headers={"Content-Disposition": f'attachment; filename="{name}.pdf"'})
        abort(400)
    params = {k: v for k, v in request.args.items() if k in ("period", "start", "end", "branch") and v}
    return render_template("reports/show.html", d=d, r=report, start=start, end=end, preset=preset, presets=PRESETS,
                           branches=member_svc.allowed_branches(), branch=branch, period=period, params=params,
                           breadcrumbs=[("Reports", url_for("reports.index")), (d.title, None)])
