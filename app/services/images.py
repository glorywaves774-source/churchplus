"""Turn any phone or camera picture into a small, upright, private image before it is stored.

Phones produce pictures ChurchPlus could not store before: iPhone HEIC files, 5–15 MB camera
JPEGs, and files with no extension. Every picture now goes through :func:`normalize`:

* the type is read from the picture itself (not the file name), so HEIC/HEIF, JPEG, PNG,
  WEBP, GIF, BMP and TIFF are all accepted;
* it is turned the right way up (phones store rotation separately);
* it is shrunk to a sensible size (member photos 1200 px, documents 2400 px);
* hidden data is removed – including the **GPS location** phones add to photos;
* it is saved as JPEG (or PNG when it has transparency, e.g. a church logo).
"""
from __future__ import annotations

import io
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

try:                                   # iPhone HEIC/HEIF pictures
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF = True
except ImportError:                    # pragma: no cover - optional dependency
    HEIF = False

Image.MAX_IMAGE_PIXELS = 80_000_000   # refuses "decompression bomb" files (~80 megapixels)
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "heic", "heif", "gif", "bmp", "tif", "tiff"}


class ImageError(ValueError):
    pass


def looks_like_image(data: bytes) -> bool:
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
        return True
    except Exception:
        return False


def normalize(data: bytes, *, max_side: int = 1600, allow_transparency: bool = False,
              quality: int = 85) -> tuple[bytes, str, str]:
    """Return ``(bytes, extension, content_type)`` for a cleaned-up picture."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            im = Image.open(io.BytesIO(data))
            im.load()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise ImageError("This file is not a picture ChurchPlus can read. Use a photo from your camera or gallery.")
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageError("This picture is far too large. Please use a normal photo.")
    if getattr(im, "n_frames", 1) > 1:
        im.seek(0)                                     # animated GIF/WEBP: keep the first frame
    im = ImageOps.exif_transpose(im)                   # upright, as the camera saw it
    im.thumbnail((max_side, max_side), Image.LANCZOS)  # only ever shrinks
    has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    out = io.BytesIO()
    if allow_transparency and has_alpha:
        im.convert("RGBA").save(out, "PNG", optimize=True)          # no metadata copied
        return out.getvalue(), "png", "image/png"
    if has_alpha:                                      # flatten onto white
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    im.convert("RGB").save(out, "JPEG", quality=quality, optimize=True, progressive=True)  # EXIF/GPS dropped
    return out.getvalue(), "jpg", "image/jpeg"
