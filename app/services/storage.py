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
import re
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


NOT_SET_UP = ("Photos and documents can't be saved yet because file storage isn't set up. "
              "Please tell your ChurchPlus administrator.")


def _endpoint_base(url: str | None) -> str | None:
    """Keep only https://host – Cloudflare shows the bucket's S3 API address with /bucket-name on the end."""
    from urllib.parse import urlparse
    if not url:
        return None
    parts = urlparse(url.strip())
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else url.strip()


def config_problem(cfg) -> str | None:
    """What is wrong with the file-storage settings, in plain words (None when they look complete)."""
    if cfg.get("STORAGE_BACKEND") == "s3":
        missing = [n for n in ("S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY") if not cfg.get(n)]
        if missing:
            return "S3 storage is switched on but " + ", ".join(missing) + (" is" if len(missing) == 1 else " are") + " not set."
        endpoint = cfg.get("S3_ENDPOINT_URL")
        if endpoint:
            from urllib.parse import urlparse
            host = urlparse(endpoint).hostname or ""
            if not endpoint.startswith("https://"):
                return "S3_ENDPOINT_URL must start with https://"
            if "<" in endpoint or ">" in endpoint or "ACCOUNT_ID" in endpoint.upper() or not re.fullmatch(r"[A-Za-z0-9.-]+", host):
                return ("S3_ENDPOINT_URL still contains a placeholder or is not a valid address. For Cloudflare R2 it is "
                        "https://<your 32-character account ID>.r2.cloudflarestorage.com, copied from the R2 overview page.")
        return None
    if cfg.get("ENV_NAME") == "production" and not os.environ.get("LOCAL_STORAGE_PATH"):
        return ("Files are kept on the server's temporary disk, which Render wipes on every deploy. "
                "Set the S3_* keys, or attach a Render Disk and set LOCAL_STORAGE_PATH.")
    return None


def explain_error(exc: Exception) -> str:
    """Turn an S3 error into advice an administrator can act on."""
    code = ""
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        code = (resp.get("Error") or {}).get("Code", "")
    text = f"{type(exc).__name__} {code} {exc}"
    if "InvalidAccessKeyId" in text or "InvalidArgument" in text and "Access Key" in text:
        return "The access key ID is not recognised. Copy S3_ACCESS_KEY_ID again from your storage provider."
    if "SignatureDoesNotMatch" in text:
        return "The secret key does not match the access key. Copy S3_SECRET_ACCESS_KEY again (no spaces)."
    if "NoSuchBucket" in text:
        return "The bucket was not found. Check S3_BUCKET (and S3_ENDPOINT_URL for Cloudflare R2)."
    if "AccessDenied" in text or "Forbidden" in text or "403" in text:
        return "The keys work but are not allowed to use this bucket. Give them read and write access to it."
    if "EndpointConnectionError" in text or "Could not connect" in text or "ConnectTimeout" in text:
        return "The storage service could not be reached. Check S3_ENDPOINT_URL (and S3_REGION for AWS)."
    if "Invalid endpoint" in text:
        return "S3_ENDPOINT_URL is not a valid address. Copy the S3 API address from the R2 overview page."
    if "PermanentRedirect" in text or "AuthorizationHeaderMalformed" in text:
        return "The region is wrong for this bucket. Set S3_REGION to the bucket's region (use auto for Cloudflare R2)."
    return f"Storage error: {type(exc).__name__}{(' ' + code) if code else ''}."


def check_round_trip() -> tuple[bool, str]:
    """Write, read back and delete a small test file. Used by the platform console and CLI."""
    cfg = current_app.config
    problem = config_problem(cfg)
    if problem and cfg.get("STORAGE_BACKEND") == "s3":
        return False, problem
    key = f"platform/storage-check/{uuid.uuid4().hex}.txt"
    payload = b"ChurchPlus storage check"
    try:
        store = backend()
        store.put(key, payload, "text/plain")
        ok = store.get(key) == payload
        store.delete(key)
    except Exception as exc:  # noqa: BLE001 - report any failure in plain words
        current_app.logger.exception("Storage check failed")
        return False, explain_error(exc)
    if not ok:
        return False, "A test file was saved but came back different."
    where = f"bucket {cfg.get('S3_BUCKET')}" if cfg.get("STORAGE_BACKEND") == "s3" else "the server disk"
    return True, f"File storage works: a test file was saved to {where}, read back and removed." + (f" Note: {problem}" if problem else "")


class S3Storage:
    def __init__(self, cfg):
        if config_problem(cfg):
            current_app.logger.error("File storage is not set up: %s", config_problem(cfg))
            raise StorageError(NOT_SET_UP)
        try:
            import boto3  # noqa: WPS433 - optional dependency
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("STORAGE_BACKEND=s3 requires boto3 (pip install boto3).") from exc
        from botocore.config import Config
        self.bucket = cfg["S3_BUCKET"]
        endpoint = _endpoint_base(cfg.get("S3_ENDPOINT_URL"))
        options = {"signature_version": "s3v4", "retries": {"max_attempts": 3, "mode": "standard"},
                   "connect_timeout": 10, "read_timeout": 30}
        if endpoint:   # Cloudflare R2, Backblaze B2, Wasabi…: only send checksums the provider understands
            options.update(request_checksum_calculation="when_required", response_checksum_validation="when_required")
        self.client = boto3.client(
            "s3", region_name=cfg.get("S3_REGION") or ("auto" if endpoint else None), endpoint_url=endpoint,
            aws_access_key_id=cfg.get("S3_ACCESS_KEY_ID"),
            aws_secret_access_key=cfg.get("S3_SECRET_ACCESS_KEY"),
            config=Config(**options),
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


def _convert_phone_picture(data: bytes, filename: str, kind: str, category: str):
    """A picture in a format we don't keep (iPhone HEIC, no extension…) becomes a JPEG/PNG.
    Returns (data, filename, content_type), or None to store the file unchanged."""
    from app.services import images
    ext = _extension(filename or "")
    if kind not in ("image", "document") or ext in ALLOWED[kind] or not data or not images.looks_like_image(data):
        return None
    limit = current_app.config["MAX_CONTENT_LENGTH"]
    if len(data) > limit:
        raise StorageError(f"The file is larger than {limit // (1024 * 1024)} MB.")
    try:
        out, new_ext, ctype = images.normalize(data, max_side=800 if category == "branding" else 2400,
                                               allow_transparency=category == "branding")
    except images.ImageError as exc:
        raise StorageError(str(exc)) from exc
    stem = filename.rsplit(".", 1)[0] if "." in (filename or "") else (filename or "photo")
    return out, f"{stem}.{new_ext}", ctype


def save(file_storage_or_bytes, *, filename: str, kind: str, category: str,
         content_type: str | None = None) -> StoredFile:
    """Validate and store a file for the active church. Returns its private key."""
    tenant_id = require_tenant_id()
    data = (file_storage_or_bytes if isinstance(file_storage_or_bytes, (bytes, bytearray))
            else file_storage_or_bytes.read())
    converted = _convert_phone_picture(bytes(data), filename, kind, category)
    if converted is not None:
        data, filename, content_type = converted
    ext = validate_upload(filename, bytes(data), kind)
    safe_category = "".join(ch for ch in category.lower() if ch.isalnum() or ch in "-_") or "files"
    key = f"{_tenant_prefix(tenant_id)}{safe_category}/{uuid.uuid4().hex}.{ext}"
    try:
        backend().put(key, bytes(data), content_type)
    except StorageError:
        raise
    except Exception as exc:   # e.g. cloud storage unreachable or not configured
        current_app.logger.exception("Storing a file failed (%s)", safe_category)
        raise StorageError("The file couldn't be stored right now. Please try again in a few minutes.") from exc
    return StoredFile(key=key, size=len(data), content_type=content_type, original_name=filename)


def open_file(key: str) -> io.BytesIO:
    _check_key(key)
    return io.BytesIO(backend().get(key))


def delete(key: str) -> None:
    _check_key(key)
    backend().delete(key)
