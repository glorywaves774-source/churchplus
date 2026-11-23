"""Portrait photos of members and leaders.

Every photo, whether taken with the in-app camera or uploaded from a file, is cleaned up before
it is stored:

* turned the right way up (phones often save photos sideways with a rotation note);
* shrunk to at most 1200 pixels on the longest side, so pages stay fast;
* re-saved as a fresh JPEG, which drops all hidden details such as GPS location, the phone
  model and the time it was taken.

Any picture a phone produces is accepted – including iPhone HEIC photos and files without an
extension – because the picture is read by its content, not its name.

One person has one photo: a clergy or staff record that is linked to a member record shares the
member's photo, so the two never drift apart.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

from flask import url_for
from PIL import Image, ImageOps, UnidentifiedImageError

from app.services import images, storage  # images registers the iPhone HEIC/HEIF reader
from app.services.storage import StorageError

MAX_SIDE = 1200
JPEG_QUALITY = 86
MAX_PIXELS = 50_000_000   # refuse "decompression bomb" images far bigger than any phone photo


def normalise(data: bytes) -> bytes:
    """Return a cleaned JPEG version of an uploaded photo, or raise StorageError."""
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.width * probe.height > MAX_PIXELS:
                raise StorageError("That photo is too large. Try a smaller one.")
            probe.verify()
        with Image.open(io.BytesIO(data)) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                flat = Image.new("RGB", im.size, (255, 255, 255))
                flat.paste(im, mask=im.getchannel("A"))
                im = flat
            elif im.mode != "RGB":
                im = im.convert("RGB")
            out = io.BytesIO()
            im.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
            return out.getvalue()
    except StorageError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError) as exc:
        raise StorageError("We couldn't read that photo. Try taking it again or choose another file.") from exc


def store(file_storage, *, category: str) -> storage.StoredFile:
    """Check, clean and store an uploaded photo for the active church."""
    raw = file_storage.read()
    # The picture itself decides whether it is a photo (not its file name), so iPhone HEIC photos
    # and files with no extension are accepted; normalise() refuses anything that isn't a picture.
    if not raw:
        raise StorageError("The file is empty.")
    limit = storage.current_app.config["MAX_CONTENT_LENGTH"]
    if len(raw) > limit:
        raise StorageError(f"That photo is larger than {limit // (1024 * 1024)} MB. Try a smaller one.")
    return storage.save(normalise(raw), filename="photo.jpg", kind="image", category=category,
                        content_type="image/jpeg")


def version(key: str | None) -> str:
    """Short value that changes whenever a photo is replaced, so browsers fetch the new one."""
    if not key:
        return ""
    return key.rsplit("/", 1)[-1].split(".", 1)[0][:10]


# --------------------------------------------------------------------------- #
# Whose photo is it?
# --------------------------------------------------------------------------- #
@dataclass
class PhotoOwner:
    """Where a person's photo lives: on their member record, or on a staff record with no member."""
    record: object            # Member or StaffMember
    is_member: bool

    @property
    def key(self) -> str | None:
        return self.record.photo_key


def owner_for_staff(staff) -> PhotoOwner:
    if staff.member is not None:
        return PhotoOwner(staff.member, True)
    return PhotoOwner(staff, False)


def staff_photo_key(staff) -> str | None:
    return owner_for_staff(staff).key


def member_photo_url(member) -> str | None:
    if not member.photo_key:
        return None
    return url_for("members.photo", public_id=member.public_id, v=version(member.photo_key))


def staff_photo_url(staff) -> str | None:
    key = staff_photo_key(staff)
    if not key:
        return None
    return url_for("clergy.photo", public_id=staff.public_id, v=version(key))


def replace(record, file_storage, *, category: str) -> str | None:
    """Store a new photo on a record. Returns the old key, which the caller deletes after commit."""
    stored = store(file_storage, category=category)
    old, record.photo_key = record.photo_key, stored.key
    return old


def send(key: str):
    """Serve a stored photo. Photo links carry a version, so browsers may keep them for a day."""
    from flask import send_file
    ext = key.rsplit(".", 1)[-1]
    resp = send_file(storage.open_file(key),
                     mimetype={"png": "image/png", "webp": "image/webp"}.get(ext, "image/jpeg"), max_age=86400)
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


# --------------------------------------------------------------------------- #
# Photo day lists
# --------------------------------------------------------------------------- #
def _leader_member_ids():
    """Members who lead a department or hold an active clergy/staff post."""
    from sqlalchemy import select, union
    from app.models import Department, StaffMember, StaffStatus
    return union(
        select(Department.leader_member_id).where(Department.leader_member_id.isnot(None), Department.is_active.is_(True)),
        select(StaffMember.member_id).where(StaffMember.member_id.isnot(None), StaffMember.status != StaffStatus.EXITED),
    )


def photo_day_people(args):
    """Active members for Photo day: still without a photo by default, optionally leaders only."""
    from app.models import Branch, Member, MemberStatus
    from app.services import members as member_svc
    from sqlalchemy import or_
    q = member_svc.members_query().filter(Member.status == MemberStatus.ACTIVE)
    if args.get("show") != "all":
        q = q.filter(Member.photo_key.is_(None))
    if args.get("who") == "leaders":
        q = q.filter(Member.id.in_(_leader_member_ids()))
    if args.get("branch"):
        q = q.join(Branch, Branch.id == Member.branch_id).filter(Branch.public_id == args["branch"])
    text = (args.get("q") or "").strip()
    if text:
        like = f"%{text}%"
        q = q.filter(or_(Member.first_name.ilike(like), Member.last_name.ilike(like),
                         Member.membership_number.ilike(like)))
    return q.order_by(Member.last_name, Member.first_name)


def photo_day_counts() -> dict:
    from sqlalchemy import func
    from app.models import Member, MemberStatus
    from app.services import members as member_svc
    q = member_svc.members_query().filter(Member.status == MemberStatus.ACTIVE)
    total = q.count()
    with_photo = q.filter(Member.photo_key.isnot(None)).count()
    leaders = q.filter(Member.id.in_(_leader_member_ids()))
    return {"total": total, "with_photo": with_photo, "missing": total - with_photo,
            "leaders_missing": leaders.filter(Member.photo_key.is_(None)).with_entities(func.count()).scalar() or 0,
            "percent": round(100 * with_photo / total) if total else 0}


def leader_roles(member_ids: list[int]) -> dict[int, list[str]]:
    """Short labels such as "Choir leader" or "Associate Pastor" for the people shown."""
    from app.models import Department, StaffMember, StaffStatus
    from app.extensions import db
    roles: dict[int, list[str]] = {}
    if not member_ids:
        return roles
    for d in db.session.query(Department).filter(Department.leader_member_id.in_(member_ids), Department.is_active.is_(True)):
        roles.setdefault(d.leader_member_id, []).append(f"{d.name} leader")
    for s in db.session.query(StaffMember).filter(StaffMember.member_id.in_(member_ids), StaffMember.status != StaffStatus.EXITED):
        roles.setdefault(s.member_id, []).insert(0, s.position)
    return roles


def unlinked_staff(args):
    """Clergy and staff with no member record: their photo lives on the staff record itself."""
    from sqlalchemy import or_
    from app.models import Branch, StaffMember, StaffStatus
    from app.extensions import db
    from app.services import members as member_svc
    q = db.session.query(StaffMember).filter(StaffMember.member_id.is_(None), StaffMember.status != StaffStatus.EXITED)
    limit = member_svc.branch_limit_id()
    if limit is not None:
        q = q.filter(StaffMember.branch_id == limit)
    if args.get("show") != "all":
        q = q.filter(StaffMember.photo_key.is_(None))
    if args.get("branch"):
        q = q.join(Branch, Branch.id == StaffMember.branch_id).filter(Branch.public_id == args["branch"])
    text = (args.get("q") or "").strip()
    if text:
        like = f"%{text}%"
        q = q.filter(or_(StaffMember.first_name.ilike(like), StaffMember.last_name.ilike(like),
                         StaffMember.staff_number.ilike(like)))
    return q.order_by(StaffMember.last_name, StaffMember.first_name).all()
