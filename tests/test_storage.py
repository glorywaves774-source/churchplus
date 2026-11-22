"""Private file storage: validation and per-church access control."""
import pytest

from app.services import storage
from app.services.storage import StorageError
from app.utils.tenancy import TenantContextMissing, TenantIsolationError, tenant_context

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"0" * 64


def test_save_and_read_back(app, two_churches):
    with tenant_context(two_churches["grace"]):
        f = storage.save(PNG, filename="passport.PNG", kind="image", category="member-photos")
        assert f.key.startswith(f"tenants/{two_churches['grace']}/member-photos/")
        assert f.key.endswith(".png") and "passport" not in f.key
        assert storage.open_file(f.key).read() == PNG


def test_files_are_not_in_public_static_folder(app, two_churches):
    with tenant_context(two_churches["grace"]):
        f = storage.save(PDF, filename="baptism.pdf", kind="document", category="docs")
    assert "static" not in app.config["LOCAL_STORAGE_PATH"]
    client = app.test_client()
    assert client.get("/static/" + f.key).status_code == 404


def test_other_church_cannot_read_or_delete(app, two_churches):
    with tenant_context(two_churches["grace"]):
        f = storage.save(PDF, filename="contract.pdf", kind="document", category="hr")
    with tenant_context(two_churches["word"]):
        with pytest.raises(TenantIsolationError):
            storage.open_file(f.key)
        with pytest.raises(TenantIsolationError):
            storage.delete(f.key)


def test_requires_church_context(app):
    with pytest.raises(TenantContextMissing):
        storage.save(PNG, filename="a.png", kind="image", category="x")


@pytest.mark.parametrize("name,data,kind", [
    ("virus.exe", b"MZ" + b"0" * 10, "document"),
    ("photo.png", b"MZ not really a png", "image"),   # renamed file
    ("notes.pdf", b"", "document"),                    # empty
    ("song.mp3", b"ID3....", "image"),                 # wrong kind
])
def test_rejects_bad_files(app, two_churches, name, data, kind):
    with tenant_context(two_churches["grace"]):
        with pytest.raises(StorageError):
            storage.save(data, filename=name, kind=kind, category="x")


def test_rejects_oversized_files(app, two_churches):
    app.config["MAX_CONTENT_LENGTH"] = 1024
    with tenant_context(two_churches["grace"]):
        with pytest.raises(StorageError, match="larger"):
            storage.save(PNG + b"0" * 2048, filename="big.png", kind="image", category="x")


def test_path_traversal_blocked(app, two_churches):
    with tenant_context(two_churches["grace"]):
        with pytest.raises(StorageError):
            storage.open_file(f"tenants/{two_churches['grace']}/../../secret.txt")
