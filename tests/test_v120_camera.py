"""1.2.0: the ChurchPlus camera, cleaned-up photos, leader photos and Photo day."""
import io
import os
import re

import pytest
from PIL import Image

from app.models import AuditLog, Member, StaffMember
from app.services import photos
from app.services.storage import StorageError
from app.utils.tenancy import tenant_context
from tests.test_design_system import assert_csp_clean
from tests.test_stage4_members import _plans, add_person, grace, login, member, new_member, pid  # noqa: F401
from tests.test_stage5_operations import _add_staff

JSON = {"Accept": "application/json"}


def jpeg(size=(800, 1000), colour=(150, 110, 90), exif=None) -> bytes:
    out = io.BytesIO()
    kw = {"exif": exif} if exif is not None else {}
    Image.new("RGB", size, colour).save(out, "JPEG", **kw)
    return out.getvalue()


def upload(client, url, data_bytes, name="photo.jpg", source="camera", json=True):
    return client.post(url, data={"photo": (io.BytesIO(data_bytes), name), "source": source},
                       content_type="multipart/form-data", headers=JSON if json else {})


def stored_path(app, key):
    return os.path.join(app.config["LOCAL_STORAGE_PATH"], key)


class Snap:
    """Plain copy of a record's columns, safe to use after the database session closes."""
    def __init__(self, obj):
        for col in obj.__table__.columns:
            setattr(self, col.key, getattr(obj, col.key))


def staff(db, tenant_id, **filters):
    db.session.expire_all()
    with tenant_context(tenant_id):
        return Snap(db.session.query(StaffMember).filter_by(**filters).one())


def mem(db, tenant_id, **filters):
    db.session.expire_all()
    with tenant_context(tenant_id):
        return Snap(db.session.query(Member).filter_by(**filters).one())


def grid(html):
    """Names of the people listed on Photo day (pop-up messages elsewhere can mention names too)."""
    return re.findall(r'class="photo-person-name"[^>]*>([^<]*)<', html)


# --------------------------------------------------------------------------- #
# Cleaning photos: right way up, smaller, no hidden location data
# --------------------------------------------------------------------------- #
def test_photos_are_resized_and_saved_as_jpeg():
    out = Image.open(io.BytesIO(photos.normalise(jpeg((3000, 4000)))))
    assert out.format == "JPEG" and max(out.size) == 1200 and out.size == (900, 1200)


def test_location_and_camera_details_are_removed():
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"                     # camera make
    exif[0x0132] = "2026:11:22 09:00:00"            # date taken
    exif[0x8825] = {1: "N", 2: (5.0, 33.0, 0.0), 3: "W", 4: (0.0, 12.0, 0.0)}   # GPS position
    raw = jpeg(exif=exif.tobytes())
    assert Image.open(io.BytesIO(raw)).getexif()            # the original carries details
    clean = Image.open(io.BytesIO(photos.normalise(raw)))
    assert not clean.getexif() and b"PhoneMaker" not in photos.normalise(raw)


def test_sideways_phone_photos_are_turned_upright():
    exif = Image.Exif()
    exif[0x0112] = 6                                # "rotate 90° to display"
    raw = jpeg((400, 300), exif=exif.tobytes())
    assert Image.open(io.BytesIO(photos.normalise(raw))).size == (300, 400)


def test_transparent_png_gets_a_white_background():
    out = io.BytesIO()
    Image.new("RGBA", (40, 50), (0, 0, 0, 0)).save(out, "PNG")
    img = Image.open(io.BytesIO(photos.normalise(out.getvalue())))
    assert img.mode == "RGB" and img.getpixel((20, 25))[0] > 240


@pytest.mark.parametrize("data", [b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, b"\xff\xd8\xff" + b"junk" * 20])
def test_broken_images_are_refused(data):
    with pytest.raises(StorageError):
        photos.normalise(data)


def test_oversized_images_are_refused_before_loading():
    out = io.BytesIO()
    Image.new("1", (8000, 7000)).save(out, "PNG")      # 56 million pixels, tiny file
    with pytest.raises(StorageError, match="too large"):
        photos.normalise(out.getvalue())


def test_version_changes_with_each_new_photo():
    assert photos.version(None) == ""
    a, b = photos.version("tenants/1/member-photos/aaa111.jpg"), photos.version("tenants/1/member-photos/bbb222.jpg")
    assert a and b and a != b


# --------------------------------------------------------------------------- #
# Camera uploads for members
# --------------------------------------------------------------------------- #
def test_camera_saves_member_photo_and_answers_in_json(app, client, db, grace):
    new_member(client)
    p = pid(db, grace, first_name="Abena")
    res = upload(client, f"/members/{p}/photo", jpeg())
    body = res.get_json()
    assert res.status_code == 200 and body["ok"] and "Abena" in body["message"]
    m = member(db, grace, public_id=p)
    assert body["photo_url"] == f"/members/{p}/photo?v={photos.version(m.photo_key)}"
    assert m.photo_key.startswith(f"tenants/{grace}/member-photos/") and m.photo_key.endswith(".jpg")
    pic = client.get(body["photo_url"])
    assert pic.status_code == 200 and pic.mimetype == "image/jpeg" and "private" in pic.headers["Cache-Control"]
    with tenant_context(grace):
        entry = db.session.query(AuditLog).filter_by(action="member.photo_updated").one()
        assert entry.details.get("source") == "camera"


def test_retake_replaces_the_old_file(app, client, db, grace):
    new_member(client)
    p = pid(db, grace, first_name="Abena")
    upload(client, f"/members/{p}/photo", jpeg(colour=(10, 10, 10)))
    first = member(db, grace, public_id=p).photo_key
    upload(client, f"/members/{p}/photo", jpeg(colour=(200, 200, 200)))
    second = member(db, grace, public_id=p).photo_key
    assert first != second
    assert not os.path.exists(stored_path(app, first)) and os.path.exists(stored_path(app, second))


def test_camera_errors_come_back_as_json(client, db, grace):
    new_member(client)
    p = pid(db, grace, first_name="Abena")
    bad = upload(client, f"/members/{p}/photo", b"\xff\xd8\xff" + b"x" * 50)
    assert bad.status_code == 400 and bad.get_json()["ok"] is False and "couldn't read" in bad.get_json()["message"]
    wrong = upload(client, f"/members/{p}/photo", b"MZ not an image", name="photo.exe")
    assert wrong.status_code == 400 and wrong.get_json()["ok"] is False
    empty = client.post(f"/members/{p}/photo", data={}, headers=JSON)
    assert empty.status_code == 400 and empty.get_json()["message"] == "Choose a photo."
    assert member(db, grace, public_id=p).photo_key is None


def test_plain_upload_form_still_works_and_redirects(client, db, grace):
    new_member(client)
    p = pid(db, grace, first_name="Abena")
    res = upload(client, f"/members/{p}/photo", jpeg(), name="passport.jpg", source="", json=False)
    assert res.status_code == 302 and res.headers["Location"].endswith(f"/members/{p}")
    assert member(db, grace, public_id=p).photo_key


def test_remove_photo(app, client, db, grace):
    new_member(client)
    p = pid(db, grace, first_name="Abena")
    upload(client, f"/members/{p}/photo", jpeg())
    key = member(db, grace, public_id=p).photo_key
    client.post(f"/members/{p}/photo/remove")
    assert member(db, grace, public_id=p).photo_key is None and not os.path.exists(stored_path(app, key))
    assert client.get(f"/members/{p}/photo").status_code == 404


def test_next_must_stay_on_this_site(client, db, grace):
    new_member(client)
    p = pid(db, grace, first_name="Abena")
    res = client.post(f"/members/{p}/photo", data={"photo": (io.BytesIO(jpeg()), "a.jpg"), "next": "//evil.example/x"},
                      content_type="multipart/form-data")
    assert res.headers["Location"].endswith(f"/members/{p}")


# --------------------------------------------------------------------------- #
# Who may take photos
# --------------------------------------------------------------------------- #
def test_people_who_can_only_view_members_cannot_change_photos(app, client, db, grace):
    new_member(client)
    p = pid(db, grace, first_name="Abena")
    add_person(db, grace, "viewer@grace.test", "finance_manager")       # members.view only
    viewer = app.test_client()
    login(viewer, "viewer@grace.test")
    assert upload(viewer, f"/members/{p}/photo", jpeg()).status_code == 403
    assert viewer.post(f"/members/{p}/photo/remove").status_code == 403
    assert viewer.get("/members/photos").status_code == 403
    html = viewer.get(f"/members/{p}").get_data(as_text=True)
    assert "data-camera-dialog" not in html and "camera.js" not in html


def test_other_church_cannot_reach_photos(app, client, db, grace, two_churches):
    new_member(client)
    p = pid(db, grace, first_name="Abena")
    upload(client, f"/members/{p}/photo", jpeg())
    other = app.test_client()
    login(other, "owner@word.test")
    assert other.get(f"/members/{p}/photo").status_code == 404
    assert upload(other, f"/members/{p}/photo", jpeg()).status_code == 404
    assert other.post(f"/members/{p}/photo/remove").status_code == 404
    assert "Abena" not in other.get("/members/photos").get_data(as_text=True)


# --------------------------------------------------------------------------- #
# The camera on pages
# --------------------------------------------------------------------------- #
def test_profile_has_camera_without_inline_scripts(client, db, grace):
    new_member(client)
    p = pid(db, grace, first_name="Abena")
    html = client.get(f"/members/{p}").get_data(as_text=True)
    assert_csp_clean(html)
    assert "data-camera-dialog" in html and "/static/js/camera.js" in html
    assert re.search(r'data-camera-open data-upload="/members/' + p + r'/photo"', html)
    # Camera buttons stay hidden until the script runs, so the plain upload form is the fallback.
    assert re.search(r"<button[^>]*data-camera-open[^>]*\bhidden>", html)
    assert 'name="photo"' in html


def test_camera_permission_is_allowed_for_this_site_only(client):
    assert "camera=(self)" in client.get("/login").headers["Permissions-Policy"]


# --------------------------------------------------------------------------- #
# Leaders: clergy and staff photos
# --------------------------------------------------------------------------- #
def test_staff_without_member_record_keeps_own_photo(app, client, db, grace):
    _add_staff(client)
    s = staff(db, grace, first_name="Daniel")
    res = upload(client, f"/clergy/{s.public_id}/photo", jpeg())
    assert res.get_json()["ok"]
    s = staff(db, grace, first_name="Daniel")
    assert s.photo_key.startswith(f"tenants/{grace}/staff-photos/")
    assert client.get(f"/clergy/{s.public_id}/photo").mimetype == "image/jpeg"
    html = client.get("/clergy").get_data(as_text=True)
    assert f"/clergy/{s.public_id}/photo?v=" in html
    client.post(f"/clergy/{s.public_id}/photo/remove")
    assert staff(db, grace, first_name="Daniel").photo_key is None


def test_staff_linked_to_member_shares_one_photo(client, db, grace):
    new_member(client)
    m = mem(db, grace, first_name="Abena")
    _add_staff(client, first_name="Abena", last_name="Mensah", member_number=m.membership_number, position="Choir Director")
    s = staff(db, grace, first_name="Abena")
    assert s.member_id == m.id
    upload(client, f"/clergy/{s.public_id}/photo", jpeg())
    m, s = mem(db, grace, public_id=m.public_id), staff(db, grace, first_name="Abena")
    assert m.photo_key and s.photo_key is None                  # stored once, on the member record
    profile = client.get(f"/clergy/{s.public_id}").get_data(as_text=True)
    assert "shared with the linked member record" in profile
    assert client.get(f"/clergy/{s.public_id}/photo").status_code == 200


def test_hr_officer_can_photograph_leaders_but_not_members(app, client, db, grace):
    new_member(client)
    p = pid(db, grace, first_name="Abena")
    _add_staff(client)
    s = staff(db, grace, first_name="Daniel")
    add_person(db, grace, "hr@grace.test", "hr_officer")
    hr = app.test_client()
    login(hr, "hr@grace.test")
    assert upload(hr, f"/clergy/{s.public_id}/photo", jpeg()).get_json()["ok"]
    assert upload(hr, f"/members/{p}/photo", jpeg()).status_code == 403


def test_other_church_cannot_reach_staff_photos(app, client, db, grace):
    _add_staff(client)
    s = staff(db, grace, first_name="Daniel")
    upload(client, f"/clergy/{s.public_id}/photo", jpeg())
    other = app.test_client()
    login(other, "owner@word.test")
    assert other.get(f"/clergy/{s.public_id}/photo").status_code == 404
    assert upload(other, f"/clergy/{s.public_id}/photo", jpeg()).status_code == 404


# --------------------------------------------------------------------------- #
# Photo day
# --------------------------------------------------------------------------- #
def test_photo_day_lists_people_without_photos(client, db, grace):
    new_member(client)
    new_member(client, first_name="Kwame", last_name="Boateng", email="kwame@example.com", phone="0241112299")
    abena = pid(db, grace, first_name="Abena")
    upload(client, f"/members/{abena}/photo", jpeg())
    html = client.get("/members/photos").get_data(as_text=True)
    assert_csp_clean(html)
    assert "Kwame Boateng" in grid(html) and "Abena Mensah" not in grid(html)
    assert "1 of 2" in html and "50%" in html
    page_all = client.get("/members/photos?show=all").get_data(as_text=True)
    assert "Abena Mensah" in grid(page_all) and "Retake" in page_all
    assert "Kwame Boateng" in grid(client.get("/members/photos?q=kwa").get_data(as_text=True))
    assert "Kwame Boateng" not in grid(client.get("/members/photos?q=zzz").get_data(as_text=True))


def test_photo_day_leaders_filter(client, db, grace):
    new_member(client)
    new_member(client, first_name="Kwame", last_name="Boateng", email="kwame@example.com", phone="0241112299")
    kwame = mem(db, grace, first_name="Kwame")
    client.post("/departments/new", data={"name": "Hospitality", "kind": "department", "leader": kwame.membership_number,
                                          "branch": "", "is_active": "y"})
    _add_staff(client)                                        # Rev. Daniel Amoah, no member record
    page = client.get("/members/photos?who=leaders").get_data(as_text=True)
    names = grid(page)
    assert "Kwame Boateng" in names and "Abena Mensah" not in names and "Hospitality leader" in page
    assert "Clergy and staff without a member record" in page and "Rev. Daniel Amoah" in names
    assert re.search(r'data-upload="/clergy/[0-9a-f-]+/photo"', page)


def test_photo_day_is_linked_from_members_and_clergy(client, grace):
    assert "/members/photos" in client.get("/members").get_data(as_text=True)
    assert "/members/photos?who=leaders" in client.get("/clergy").get_data(as_text=True)


def test_branch_admin_photo_day_shows_only_their_branch(app, client, db, grace):
    client.post("/branches/new", data={"name": "Tema", "code": "TEMA", "country": "GH"})
    with tenant_context(grace):
        from app.models import Branch
        tema = db.session.query(Branch).filter_by(code="TEMA").one().public_id
    new_member(client, branch=tema, first_name="Esi", email="esi@example.com", phone="0241112200")
    new_member(client, first_name="Kofi", email="kofi@example.com", phone="0241112201")
    add_person(db, grace, "tema@grace.test", "branch_admin", "TEMA")
    tema_admin = app.test_client()
    login(tema_admin, "tema@grace.test")
    html = grid(tema_admin.get("/members/photos").get_data(as_text=True))
    assert "Esi Mensah" in html and "Kofi Mensah" not in html


def test_pillow_is_a_listed_requirement():
    from pathlib import Path
    reqs = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text()
    assert re.search(r"^Pillow>=", reqs, re.M)
