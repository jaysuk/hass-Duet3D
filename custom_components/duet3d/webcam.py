"""Pure helpers for the webcam: reading Duet Web Control's settings and extracting a frame.

No Home Assistant imports, and the settings are untrusted: DWC writes whatever the
user typed, and the file is absent or empty on a board that never had one saved.
"""
from __future__ import annotations

from typing import Any

# Where DWC keeps its settings on the board, in both standalone and SBC mode.
DWC_SETTINGS_PATH = "0:/sys/dwc-settings.json"

_MAX_FRAME_BYTES = 4 * 1024 * 1024
_JPEG_START = b"\xff\xd8"
_JPEG_END = b"\xff\xd9"


def resolve_url(url: Any, host: str, base_url: str) -> str | None:
    """A usable absolute URL from what DWC stored, or ``None``.

    DWC's placeholder for the address the page was loaded from is ``[HOSTNAME]``
    (``{hostname}`` is accepted too), and users also type paths relative to the board.
    """
    if not isinstance(url, str):
        return None
    url = url.strip().replace("[HOSTNAME]", host).replace("{hostname}", host)
    if not url:
        return None
    if url.startswith("/"):
        return base_url.rstrip("/") + url
    if url.startswith(("http://", "https://")):
        return url
    return None


def parse_dwc_settings(settings: Any, host: str, base_url: str) -> dict[str, Any]:
    """``{"url": ..., "stream": ...}`` from DWC's settings; ``url`` is ``None`` when unset.

    ``stream`` is True when DWC shows ``url`` as a live stream, which it does when the
    update interval is 0. DWC's ``liveUrl`` is only the page opened by clicking the
    image, so it is ignored.

    A webcam DWC has switched off (``enabled: false``) is treated as not there: its
    address is usually left over from one that has since been taken down. Old DWC
    versions nest the settings under ``main`` and then ``machine``, the latter
    overriding the former, as DWC does.
    """
    if isinstance(settings, dict) and isinstance(settings.get("main"), dict):
        merged = dict(settings["main"])
        machine = settings.get("machine")
        if isinstance(machine, dict):
            merged.update(machine)
        settings = merged
    webcam = settings.get("webcam") if isinstance(settings, dict) else None
    if not isinstance(webcam, dict) or webcam.get("enabled") is False:
        return {"url": None, "stream": False}
    interval = webcam.get("updateInterval")
    return {
        "url": resolve_url(webcam.get("url"), host, base_url),
        "stream": interval == 0 and not isinstance(interval, bool),
    }


def first_jpeg(data: bytes) -> bytes | None:
    """The first complete JPEG in ``data``, which may be a slice of an MJPEG stream."""
    start = data.find(_JPEG_START)
    if start < 0:
        return None
    end = data.find(_JPEG_END, start + 2)
    return None if end < 0 else data[start : end + 2]


async def read_frame(response) -> bytes | None:
    """One image from an aiohttp response.

    A snapshot URL is read whole. A stream URL (``multipart/x-mixed-replace``) never
    ends, so chunks are read until the first complete JPEG, up to a size cap.
    """
    if (response.content_type or "").startswith("multipart/"):
        buffer = b""
        async for chunk in response.content.iter_chunked(8192):
            buffer += chunk
            if (frame := first_jpeg(buffer)) is not None:
                return frame
            if len(buffer) > _MAX_FRAME_BYTES:
                return None
        return None
    data = await response.read()
    return data if data and len(data) <= _MAX_FRAME_BYTES else None
