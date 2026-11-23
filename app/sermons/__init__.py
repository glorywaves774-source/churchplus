"""Sermon archive and media library."""
from __future__ import annotations

from datetime import date

from flask import Blueprint, abort, flash, g, redirect, render_template, request, send_file, url_for
from sqlalchemy import or_

from app.extensions import db
from app.models import MediaItem, MediaKind, Sermon, Visibility
from app.services import audit, storage
from app.services.storage import StorageError
from app.utils.context import permission_required

bp = Blueprint("sermons", __name__)
KIND_BY_EXT = {"mp3": MediaKind.AUDIO, "m4a": MediaKind.AUDIO, "wav": MediaKind.AUDIO, "mp4": MediaKind.VIDEO,
               "webm": MediaKind.VIDEO, "pdf": MediaKind.DOCUMENT, "docx": MediaKind.DOCUMENT,
               "jpg": MediaKind.IMAGE, "jpeg": MediaKind.IMAGE, "png": MediaKind.IMAGE, "webp": MediaKind.IMAGE,
               "heic": MediaKind.IMAGE, "heif": MediaKind.IMAGE}
STORAGE_KIND = {MediaKind.AUDIO: "audio", MediaKind.VIDEO: "video", MediaKind.DOCUMENT: "document", MediaKind.IMAGE: "image"}


def _url(value: str | None) -> str | None:
    v = (value or "").strip()
    if v and not v.startswith(("https://", "http://")):
        raise ValueError("Links must start with https://")
    return v or None


def _get(public_id):
    return db.session.query(Sermon).filter_by(public_id=public_id).first_or_404()


@bp.get("/sermons")
@permission_required("sermons.view")
def index():
    q = db.session.query(Sermon)
    text = request.args.get("q", "").strip()
    if text:
        like = f"%{text}%"
        q = q.filter(or_(Sermon.title.ilike(like), Sermon.preacher.ilike(like), Sermon.scripture.ilike(like),
                         Sermon.series.ilike(like), Sermon.summary.ilike(like)))
    series = request.args.get("series", "")
    if series:
        q = q.filter(Sermon.series == series)
    page = q.order_by(Sermon.preached_on.desc()).paginate(page=request.args.get("page", 1, type=int), per_page=20, error_out=False)
    all_series = [s for (s,) in db.session.query(Sermon.series).filter(Sermon.series.isnot(None)).distinct().order_by(Sermon.series)]
    params = {k: v for k, v in request.args.items() if k != "page" and v}
    return render_template("sermons/index.html", page=page, q=text, series=series, all_series=all_series, params=params,
                           breadcrumbs=[("Ministry", None), ("Sermons & media", None)])


def _save(s: Sermon) -> str | None:
    f = request.form
    if not f.get("title", "").strip() or not f.get("preacher", "").strip():
        return "Enter the title and the preacher."
    try:
        s.preached_on = date.fromisoformat(f.get("preached_on", ""))
        s.video_url, s.audio_url = _url(f.get("video_url")), _url(f.get("audio_url"))
        s.visibility = Visibility(f.get("visibility", "members"))
    except ValueError as e:
        return str(e) if "https" in str(e) else "Check the date and links."
    s.title, s.preacher = f["title"].strip()[:200], f["preacher"].strip()[:160]
    for k in ("series", "category", "scripture", "summary", "notes"):
        setattr(s, k, (f.get(k) or "").strip() or None)
    return None


@bp.route("/sermons/new", methods=["GET", "POST"])
@permission_required("sermons.create")
def create():
    if request.method == "POST":
        s = Sermon(title="", preacher="", preached_on=date.today())
        err = _save(s)
        if err:
            flash(err, "danger")
        else:
            db.session.add(s)
            db.session.flush()
            audit.record("sermon.created", entity=s, actor=g.user, name=s.title)
            db.session.commit()
            flash("Sermon added.", "success")
            return redirect(url_for("sermons.detail", public_id=s.public_id))
    return render_template("sermons/form.html", s=None, form=request.form, visibilities=list(Visibility), today=date.today(),
                           breadcrumbs=[("Sermons", url_for("sermons.index")), ("Add sermon", None)])


@bp.route("/sermons/<public_id>/edit", methods=["GET", "POST"])
@permission_required("sermons.edit")
def edit(public_id: str):
    s = _get(public_id)
    if request.method == "POST":
        err = _save(s)
        if err:
            db.session.rollback()
            flash(err, "danger")
        else:
            audit.record("sermon.updated", entity=s, actor=g.user, name=s.title)
            db.session.commit()
            flash("Saved.", "success")
            return redirect(url_for("sermons.detail", public_id=public_id))
    return render_template("sermons/form.html", s=s, form=request.form, visibilities=list(Visibility), today=date.today(),
                           breadcrumbs=[("Sermons", url_for("sermons.index")), (s.title, url_for("sermons.detail", public_id=public_id)), ("Edit", None)])


@bp.get("/sermons/<public_id>")
@permission_required("sermons.view")
def detail(public_id: str):
    s = _get(public_id)
    return render_template("sermons/detail.html", s=s, visibilities=list(Visibility),
                           breadcrumbs=[("Sermons", url_for("sermons.index")), (s.title, None)])


@bp.post("/sermons/<public_id>/delete")
@permission_required("sermons.delete")
def delete(public_id: str):
    s = _get(public_id)
    audit.record("sermon.deleted", entity=s, actor=g.user, name=s.title)
    db.session.delete(s)
    db.session.commit()
    flash("Sermon deleted. Its media stays in the media library.", "success")
    return redirect(url_for("sermons.index"))


# --------------------------------------------------------------------------- #
# Media library
# --------------------------------------------------------------------------- #
@bp.route("/media", methods=["GET", "POST"])
@permission_required("sermons.view")
def media():
    sermon = _get(request.values["sermon"]) if request.values.get("sermon") else None
    if request.method == "POST":
        if not g.account.can("sermons.create"):
            abort(403)
        title = (request.form.get("title") or "").strip()
        f = request.files.get("file")
        try:
            vis = Visibility(request.form.get("visibility", "members"))
            if not title:
                raise ValueError("Enter a title.")
            item = MediaItem(title=title[:200], category=(request.form.get("category") or "").strip() or None,
                             sermon_id=getattr(sermon, "id", None), visibility=vis, uploaded_by_id=g.user.id,
                             kind=MediaKind.LINK)
            if f and f.filename:
                ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
                kind = KIND_BY_EXT.get(ext)
                if kind is None:
                    raise ValueError("Upload MP3, M4A, WAV, MP4, WEBM, PDF, DOCX, JPG or PNG files.")
                stored = storage.save(f, filename=f.filename, kind=STORAGE_KIND[kind], category="media", content_type=f.mimetype)
                item.kind, item.file_key, item.file_name = kind, stored.key, f.filename[:255]
                item.content_type, item.size = f.mimetype, stored.size
            else:
                item.url = _url(request.form.get("url"))
                if not item.url:
                    raise ValueError("Choose a file or paste a link.")
                item.kind = MediaKind(request.form.get("kind") or "link")
            db.session.add(item)
            db.session.flush()
            audit.record("media.added", entity=item, actor=g.user, name=item.title)
            db.session.commit()
            flash("Media added.", "success")
        except (ValueError, StorageError) as e:
            db.session.rollback()
            flash(str(e), "danger")
        return redirect(url_for("sermons.detail", public_id=sermon.public_id) if sermon else url_for("sermons.media"))
    q = db.session.query(MediaItem)
    kind = request.args.get("kind", "")
    if kind in {k.value for k in MediaKind}:
        q = q.filter(MediaItem.kind == MediaKind(kind))
    rows = q.order_by(MediaItem.created_at.desc()).limit(200).all()
    return render_template("sermons/media.html", rows=rows, kind=kind, kinds=list(MediaKind), visibilities=list(Visibility),
                           breadcrumbs=[("Sermons", url_for("sermons.index")), ("Media library", None)])


@bp.get("/media/<public_id>/download")
@permission_required("sermons.view")
def download(public_id: str):
    item = db.session.query(MediaItem).filter_by(public_id=public_id).first_or_404()
    if not item.file_key:
        return redirect(item.url) if item.url else abort(404)
    inline = item.kind in (MediaKind.AUDIO, MediaKind.VIDEO, MediaKind.IMAGE) and request.args.get("inline") == "1"
    return send_file(storage.open_file(item.file_key), mimetype=item.content_type or "application/octet-stream",
                     as_attachment=not inline, download_name=item.file_name, conditional=True)


@bp.post("/media/<public_id>/delete")
@permission_required("sermons.delete")
def delete_media(public_id: str):
    item = db.session.query(MediaItem).filter_by(public_id=public_id).first_or_404()
    key = item.file_key
    audit.record("media.deleted", entity=item, actor=g.user, name=item.title)
    db.session.delete(item)
    db.session.commit()
    if key:
        storage.delete(key)
    flash("Deleted.", "success")
    return redirect(request.form.get("next") if (request.form.get("next") or "").startswith("/") else url_for("sermons.media"))
