"""Private file storage for photographs and documents.

Files are never written to a public folder. Every key is prefixed with the
owning church (``tenants/<id>/...``) and every read checks that prefix against
the active tenant, so one church cannot download another church's files even
with a guessed key.

Backends
--------
* ``local`` - a private folder outside ``static/`` (development, tests).
* ``s3``    - any S3-compatible object store (AWS S3, Cloudflare R2, Backblaze
  B2, DigitalOcean Spaces). Requires ``boto3`` and the S3_* variables.
"""
from __future__ import annotations

import io
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from flask import current_app

from app.utils.tenancy import TenantIsolationError, is_bypassed, require_tenant_id

ALLOWED = {
    "image": {"jpg", "jpeg", "png", "webp"},
    "document": {"pdf", "jpg", "jpeg", "png", "docx", "xlsx", "csv"},
    "audio": {"mp3", "m4a", "wav"},
    "video": {"mp4", "webm"},
}

# Leading bytes that identify real file types (prevents renamed executables).
_SIGNATURES = {
    "jpg": [b"\xff\xd8\xff"], "jpeg": [b"\xff\xd8\xff"],
    "png": [b"\x89PNG\r\n\x1a\n"],
    "webp": [b"RIFF"],
    "pdf": [b"%PDF-"],
    "docx": [b"PK\x03\x04"], "xlsx": [b"PK\x03\x04"],
    "mp3": [b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"],
    "mp4": [], "m4a": [], "wav": [b"RIFF"], "webm": [b"\x1aE\xdf\xa3"], "csv": [],
}


class StorageError(ValueError):
    pass


@dataclass
class StoredFile:
    key: str
    size: int
    content_type: str | None
    original_name: str


def _extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def validate_upload(filename: str, data: bytes, kind: str) -> str:
    ext = _extension(filename or "")
    if kind not in ALLOWED or ext not in ALLOWED[kind]:
        raise StorageError(f"File type .{ext or '?'} is not allowed for {kind} uploads.")
    max_bytes = current_app.config["MAX_CONTENT_LENGTH"]
    if len(data) == 0:
        raise StorageError("The file is empty.")
    if len(data) > max_bytes:
        raise StorageError(f"The file is larger than {max_bytes // (1024 * 1024)} MB.")
    sigs = _SIGNATURES.get(ext, [])
    if sigs and not any(data.startswith(s) for s in sigs):
        raise StorageError("The file contents do not match its type.")
    return ext


def _tenant_prefix(tenant_id: int) -> str:
    return f"tenants/{int(tenant_id)}/"


def _check_key(key: str) -> None:
    if ".." in key or key.startswith("/"):
        raise StorageError("Invalid file key.")
    if is_bypassed():
        return
    if not key.startswith(_tenant_prefix(require_tenant_id())):
        raise TenantIsolationError("This file belongs to another church.")


class LocalStorage:
    def __init__(self, root: str):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise StorageError("Invalid file key.")
        return path

    def put(self, key: str, data: bytes, content_type: str | None) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise FileNotFoundError(key)
        return path.read_bytes()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()


class S3Storage:
    def __init__(self, cfg):
        try:
            import boto3  # noqa: WPS433 - optional dependency
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("STORAGE_BACKEND=s3 requires boto3 (pip install boto3).") from exc
        self.bucket = cfg["S3_BUCKET"]
        self.client = boto3.client(
            "s3", region_name=cfg.get("S3_REGION"), endpoint_url=cfg.get("S3_ENDPOINT_URL"),
            aws_access_key_id=cfg.get("S3_ACCESS_KEY_ID"),
            aws_secret_access_key=cfg.get("S3_SECRET_ACCESS_KEY"),
        )

    def put(self, key, data, content_type):  # pragma: no cover - needs network
        extra = {"ContentType": content_type} if content_type else {}
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, **extra)

    def get(self, key):  # pragma: no cover
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def delete(self, key):  # pragma: no cover
        self.client.delete_object(Bucket=self.bucket, Key=key)


def backend():
    cfg = current_app.config
    if cfg["STORAGE_BACKEND"] == "s3":
        return S3Storage(cfg)
    root = cfg.get("LOCAL_STORAGE_PATH") or os.path.join(current_app.instance_path, "private_uploads")
    return LocalStorage(root)


def save(file_storage_or_bytes, *, filename: str, kind: str, category: str,
         content_type: str | None = None) -> StoredFile:
    """Validate and store a file for the active church. Returns its private key."""
    tenant_id = require_tenant_id()
    data = (file_storage_or_bytes if isinstance(file_storage_or_bytes, (bytes, bytearray))
            else file_storage_or_bytes.read())
    ext = validate_upload(filename, bytes(data), kind)
    safe_category = "".join(ch for ch in category.lower() if ch.isalnum() or ch in "-_") or "files"
    key = f"{_tenant_prefix(tenant_id)}{safe_category}/{uuid.uuid4().hex}.{ext}"
    backend().put(key, bytes(data), content_type)
    return StoredFile(key=key, size=len(data), content_type=content_type, original_name=filename)


def open_file(key: str) -> io.BytesIO:
    _check_key(key)
    return io.BytesIO(backend().get(key))


def delete(key: str) -> None:
    _check_key(key)
    backend().delete(key)
