"""1.2.2: S3 file storage – clear messages when it isn't set up, and a working storage test."""
import io

import pytest
from PIL import Image

from app.models import AuditLog, PlatformRole, User
from app.services import storage
from app.utils.tenancy import tenant_bypass, tenant_context
from tests.conftest import make_user
from tests.test_stage4_members import _plans, grace, login, new_member, pid  # noqa: F401
from tests.test_v120_camera import JSON, upload

moto = pytest.importorskip("moto")

S3 = {"STORAGE_BACKEND": "s3", "S3_BUCKET": "churchplus-test", "S3_REGION": "us-east-1",
      "S3_ACCESS_KEY_ID": "AKIATESTKEY", "S3_SECRET_ACCESS_KEY": "secret", "S3_ENDPOINT_URL": None}


def jpeg():
    out = io.BytesIO()
    Image.new("RGB", (600, 800), (90, 120, 200)).save(out, "JPEG")
    return out.getvalue()


@pytest.fixture()
def fake_s3(app):
    """A real S3 API (moto) with one bucket, and the app pointed at it."""
    import boto3
    with moto.mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="churchplus-test")
        old = {k: app.config.get(k) for k in S3}
        app.config.update(S3)
        yield
        app.config.update(old)


def platform_admin(db, client, email="boss@kayron.test", role=PlatformRole.SUPER_ADMIN):
    with tenant_bypass():
        u = make_user(email)
        u.platform_role = role
        db.session.add(u)
        db.session.commit()
    client.post("/logout")
    login(client, email)


# --------------------------------------------------------------------------- #
# Settings checks
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cfg,expected", [
    ({"STORAGE_BACKEND": "s3"}, "S3_BUCKET, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY are not set"),
    ({**S3, "S3_SECRET_ACCESS_KEY": None}, "S3_SECRET_ACCESS_KEY is not set"),
    ({**S3, "S3_ENDPOINT_URL": "abc.r2.cloudflarestorage.com"}, "must start with https://"),
    ({**S3, "S3_ENDPOINT_URL": "https://<ACCOUNT_ID>.r2.cloudflarestorage.com"}, "still contains a placeholder"),
    ({**S3, "S3_ENDPOINT_URL": "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com"}, None),
    (S3, None),
])
def test_settings_are_checked_in_plain_words(cfg, expected):
    problem = storage.config_problem(cfg)
    assert problem is None if expected is None else expected in problem


def test_keys_pasted_with_spaces_are_cleaned(monkeypatch):
    import importlib

    import app.config as config
    monkeypatch.setenv("S3_BUCKET", "  churchplus-files \n")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.BaseConfig.S3_BUCKET == "churchplus-files"
    finally:
        monkeypatch.delenv("S3_BUCKET")
        importlib.reload(config)


def test_photo_upload_says_storage_is_not_set_up(app, client, db, grace):
    new_member(client)
    p = pid(db, grace, first_name="Abena")
    app.config.update(STORAGE_BACKEND="s3", S3_BUCKET=None, S3_ACCESS_KEY_ID=None, S3_SECRET_ACCESS_KEY=None)
    try:
        res = upload(client, f"/members/{p}/photo", jpeg(), source="upload")
    finally:
        app.config["STORAGE_BACKEND"] = "local"
    assert res.status_code == 400 and "file storage isn't set up" in res.get_json()["message"]


def test_health_check_reports_storage(app, client):
    assert client.get("/healthz").get_json()["storage"] == "local"
    app.config.update(STORAGE_BACKEND="s3", S3_BUCKET=None)
    try:
        assert client.get("/healthz").get_json()["storage"] == "not set up"
    finally:
        app.config["STORAGE_BACKEND"] = "local"


# --------------------------------------------------------------------------- #
# Working S3
# --------------------------------------------------------------------------- #
def test_photos_are_saved_to_s3_and_served_back(app, client, db, grace, fake_s3):
    new_member(client)
    p = pid(db, grace, first_name="Abena")
    res = upload(client, f"/members/{p}/photo", jpeg(), source="upload")
    assert res.status_code == 200 and res.get_json()["ok"] is True
    import boto3
    keys = [o["Key"] for o in boto3.client("s3", region_name="us-east-1").list_objects_v2(Bucket="churchplus-test")["Contents"]]
    assert len(keys) == 1 and keys[0].startswith(f"tenants/{grace}/member-photos/")
    photo = client.get(res.get_json()["photo_url"])
    assert photo.status_code == 200 and photo.data[:3] == b"\xff\xd8\xff"


def test_storage_check_succeeds_and_leaves_nothing_behind(app, fake_s3):
    with app.app_context():
        ok, message = storage.check_round_trip()
    assert ok and "churchplus-test" in message
    import boto3
    assert boto3.client("s3", region_name="us-east-1").list_objects_v2(Bucket="churchplus-test").get("KeyCount") == 0


def test_storage_check_explains_a_wrong_bucket(app, fake_s3):
    app.config["S3_BUCKET"] = "no-such-bucket"
    with app.app_context():
        ok, message = storage.check_round_trip()
    assert not ok and "bucket was not found" in message


def test_platform_console_shows_and_tests_storage(app, client, db, grace, fake_s3):
    platform_admin(db, client)
    html = client.get("/platform").get_data(as_text=True)
    assert "File storage" in html and "S3 bucket churchplus-test" in html and "Test storage" in html
    res = client.post("/platform/storage-check", follow_redirects=True)
    assert "File storage works" in res.get_data(as_text=True)
    with tenant_bypass():
        assert db.session.query(AuditLog).filter_by(action="platform.storage_checked").count() == 1


def test_platform_console_warns_when_not_set_up(app, client, db, grace):
    app.config.update(STORAGE_BACKEND="s3", S3_BUCKET=None, S3_ACCESS_KEY_ID=None, S3_SECRET_ACCESS_KEY=None)
    try:
        platform_admin(db, client)
        html = client.get("/platform").get_data(as_text=True)
        res = client.post("/platform/storage-check", follow_redirects=True).get_data(as_text=True)
    finally:
        app.config["STORAGE_BACKEND"] = "local"
    assert "Not set up" in html and "S3_BUCKET" in html
    assert "S3_BUCKET" in res and "not set" in res


def test_support_admins_cannot_run_the_test(client, db, grace):
    platform_admin(db, client, "support@kayron.test", PlatformRole.SUPPORT_ADMIN)
    assert "Test storage" not in client.get("/platform").get_data(as_text=True)
    assert client.post("/platform/storage-check").status_code == 403


def test_check_storage_command(app, fake_s3):
    out = app.test_cli_runner().invoke(args=["churchplus", "check-storage"])
    assert out.exit_code == 0 and "File storage works" in out.output
    app.config["S3_SECRET_ACCESS_KEY"] = None
    out = app.test_cli_runner().invoke(args=["churchplus", "check-storage"])
    assert out.exit_code == 1 and "S3_SECRET_ACCESS_KEY" in out.output


def test_bucket_address_pasted_as_endpoint_is_accepted():
    assert storage._endpoint_base("https://abc123.r2.cloudflarestorage.com/churchplus-files/") == "https://abc123.r2.cloudflarestorage.com"
    assert storage.config_problem({**S3, "S3_ENDPOINT_URL": "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com/churchplus-files"}) is None


def test_404_from_r2_is_explained_as_bucket_not_found():
    from botocore.exceptions import ClientError
    err = ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "PutObject")
    assert "bucket was not found" in storage.explain_error(err)
