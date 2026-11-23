"""1.2.1: every photo a phone produces can be stored (iPhone HEIC, full-size camera photos,
files with no extension), for members, leaders, documents and the church logo."""
import io

import pytest
from PIL import Image

from app.models import Tenant
from app.services import storage
from app.utils.tenancy import tenant_context
from tests.test_stage4_members import _plans, grace, login, member, new_member, pid  # noqa: F401
from tests.test_stage5_operations import _add_staff
from tests.test_v120_camera import JSON, mem, staff, upload

pytest.importorskip("pillow_heif")


def picture(fmt="JPEG", size=(3024, 4032), noise=False, mode="RGB"):
    if noise:
        im = Image.effect_noise(size, 80).convert("RGB")
    else:
        im = Image.new(mode, size, (200, 120, 60) if mode == "RGB" else (200, 120, 60, 0))
    out = io.BytesIO()
    im.save(out, fmt, **({"quality": 97} if fmt in ("JPEG", "HEIF") else {}))
    return out.getvalue()


@pytest.fixture()
def abena(client, db, grace):
    new_member(client)
    return pid(db, grace, first_name="Abena")


def stored_jpeg(app, db, grace, p):
    m = mem(db, grace, public_id=p)
    assert m.photo_key, "the photo was not stored"
    with tenant_context(grace):
        im = Image.open(storage.open_file(m.photo_key))
    return m.photo_key, im


def test_iphone_heic_photo_is_stored(app, client, db, grace, abena):
    res = upload(client, f"/members/{abena}/photo", picture("HEIF"), name="IMG_4821.HEIC", source="upload")
    assert res.status_code == 200 and res.get_json()["ok"] is True
    key, im = stored_jpeg(app, db, grace, abena)
    assert key.endswith(".jpg") and im.format == "JPEG" and max(im.size) == 1200


def test_heic_sent_by_the_camera_window_under_a_jpg_name_is_stored(app, client, db, grace, abena):
    """When a browser can't preview a HEIC photo, the camera window sends the file as it is."""
    res = upload(client, f"/members/{abena}/photo", picture("HEIF"), name="photo.jpg", source="upload")
    assert res.get_json()["ok"] is True
    assert stored_jpeg(app, db, grace, abena)[1].format == "JPEG"


def test_full_size_camera_photo_is_stored_small(app, client, db, grace, abena):
    big = picture("JPEG", size=(4032, 3024), noise=True)
    assert len(big) > 10 * 1024 * 1024                 # over the old 10 MB limit
    res = upload(client, f"/members/{abena}/photo", big, name="20261123_101500.jpg", source="upload")
    assert res.status_code == 200 and res.get_json()["ok"] is True
    _, im = stored_jpeg(app, db, grace, abena)
    assert im.size == (1200, 900)


def test_photo_without_extension_is_stored(app, client, db, grace, abena):
    res = upload(client, f"/members/{abena}/photo", picture("PNG", (640, 800)), name="image", source="upload")
    assert res.get_json()["ok"] is True
    assert stored_jpeg(app, db, grace, abena)[0].endswith(".jpg")


def test_plain_form_accepts_heic_too(app, client, db, grace, abena):
    res = upload(client, f"/members/{abena}/photo", picture("HEIF"), name="IMG_0001.heic", source="", json=False)
    assert res.status_code == 302
    assert stored_jpeg(app, db, grace, abena)[1].format == "JPEG"


def test_leader_heic_photo_is_stored(app, client, db, grace):
    _add_staff(client, first_name="Kwame", last_name="Asare")
    s = staff(db, grace, first_name="Kwame")
    res = upload(client, f"/clergy/{s.public_id}/photo", picture("HEIF"), name="IMG_7.HEIC", source="upload")
    assert res.status_code == 200 and res.get_json()["ok"] is True
    assert staff(db, grace, first_name="Kwame").photo_key.endswith(".jpg")


def test_things_that_are_not_photos_are_still_refused(client, db, grace, abena):
    for data, name in ((b"MZ\x90\x00 a program", "IMG_1.HEIC"), (b"MZ\x90\x00 a program", "image")):
        res = upload(client, f"/members/{abena}/photo", data, name=name, source="upload")
        assert res.status_code == 400 and res.get_json()["ok"] is False
    assert member(db, grace, public_id=abena).photo_key is None


def test_too_large_upload_explains_itself(app, client, db, grace, abena):
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
    res = client.post(f"/members/{abena}/photo", data={"photo": (io.BytesIO(b"\xff\xd8\xff" + b"0" * 2_000_000), "x.jpg")},
                      content_type="multipart/form-data", headers=JSON)
    assert res.status_code == 413 and res.get_json()["ok"] is False and "1 MB" in res.get_json()["message"]
    plain = client.post(f"/members/{abena}/photo", data={"photo": (io.BytesIO(b"\xff\xd8\xff" + b"0" * 2_000_000), "x.jpg")},
                        content_type="multipart/form-data", headers={"Referer": f"http://localhost/members/{abena}"})
    assert plain.status_code == 302 and plain.headers["Location"].endswith(f"/members/{abena}")


def test_heic_document_and_logo_are_converted(app, client, db, grace, abena):
    with tenant_context(grace):
        doc = storage.save(picture("HEIF"), filename="baptism.heic", kind="document", category="member-documents")
        assert doc.key.endswith(".jpg") and doc.content_type == "image/jpeg" and doc.original_name == "baptism.jpg"
        pdf = storage.save(b"%PDF-1.4 x", filename="letter.pdf", kind="document", category="member-documents")
        assert storage.open_file(pdf.key).getvalue() == b"%PDF-1.4 x"
    res = client.post("/church/logo", data={"logo": (io.BytesIO(picture("HEIF", (900, 900))), "logo.HEIC")},
                      content_type="multipart/form-data")
    assert res.status_code == 302
    with tenant_context(grace):
        key = db.session.get(Tenant, grace).logo_key
        assert key and Image.open(storage.open_file(key)).size == (800, 800)


def test_photo_fields_offer_every_phone_picture(client, db, grace, abena):
    html = client.get(f"/members/{abena}").get_data(as_text=True)
    assert 'accept="image/*,.heic,.heif"' in html


def test_remove_photo_button_is_ready_and_removes(app, client, db, grace, abena):
    """The Remove button is always on the page (hidden until there is a photo) so it can appear
    straight after a camera save, and removing deletes the photo."""
    html = client.get(f"/members/{abena}").get_data(as_text=True)
    assert 'data-photo-remove="#member-photo"' in html and "photo/remove" in html
    upload(client, f"/members/{abena}/photo", picture("JPEG", (800, 1000)), name="p.jpg")
    key = member(db, grace, public_id=abena).photo_key
    html = client.get(f"/members/{abena}").get_data(as_text=True)
    form = html[html.index('data-photo-remove="#member-photo"'):][:400]
    assert " hidden" not in form.split(">")[0]
    res = client.post(f"/members/{abena}/photo/remove")
    assert res.status_code == 302 and member(db, grace, public_id=abena).photo_key is None
    with tenant_context(grace):
        import pytest as _pt
        with _pt.raises(Exception):
            storage.open_file(key)
