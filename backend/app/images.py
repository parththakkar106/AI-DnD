"""Scenario cover art: inline data URIs in, cacheable URLs out.

A scenario's `image` column holds either an `https://` URL or a base64
`data:image/…` URI (the editor downscales uploads before storing one). Sending
those data URIs inside list responses would balloon them, so lists advertise a
`image_url` pointing at `GET /api/scenarios/{id}/image` instead, and the bytes
are fetched once and cached by the browser.
"""

import base64
import binascii
import re

# Only raster formats a browser renders in an <img>. SVG is deliberately absent:
# it can carry script, and these bytes are served from our own origin.
DATA_URI_RE = re.compile(
    r"^data:(image/(?:png|jpeg|webp|gif|avif));base64,([A-Za-z0-9+/=\s]+)$",
    re.IGNORECASE,
)


def public_url(scenario_id: int, image: str, version: object) -> str:
    """The URL a client should load for this scenario's art ("" if none).

    `version` (any object with a stable repr — normally the row's updated_at)
    becomes a cache-buster, letting the image response be marked immutable
    while still refreshing the moment the author swaps the picture.
    """
    if not image:
        return ""
    if DATA_URI_RE.match(image):
        stamp = int(version.timestamp()) if hasattr(version, "timestamp") else 0
        return f"/api/scenarios/{scenario_id}/image?v={stamp}"
    # Anything else must be an absolute https URL. http:// is rejected rather
    # than passed through: the deployed app is https, so a browser would block
    # it as mixed content and the author would just see a broken image.
    return image if image.startswith("https://") else ""


def sanitize(value: object, max_length: int) -> str:
    """Coerce an untrusted `image` value from an import bundle to a safe one.

    Anything that isn't a supported data URI or an https URL — or that is too
    large to store — becomes "", so a hostile or merely foreign bundle can't
    smuggle in a `javascript:` URI or blow past the column cap.
    """
    if not isinstance(value, str) or not value or len(value) > max_length:
        return ""
    if DATA_URI_RE.match(value):
        return value if decode(value) is not None else ""
    return value if value.startswith("https://") else ""


def decode(image: str) -> tuple[bytes, str] | None:
    """`(bytes, content_type)` for a stored data URI, or None if it isn't one."""
    match = DATA_URI_RE.match(image or "")
    if not match:
        return None
    try:
        # validate=True rejects anything outside the base64 alphabet, so strip
        # the newlines a hand-pasted or line-wrapped URI may carry first.
        payload = re.sub(r"\s+", "", match.group(2))
        return base64.b64decode(payload, validate=True), match.group(1).lower()
    except (binascii.Error, ValueError):
        # Truncated or hand-edited base64 — treat as "no image" rather than 500.
        return None
