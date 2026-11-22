"""A persona bot wears its own portrait: the profile picture follows the portrait file.

Measured 2026-11-18: the Saga bot's Discord profile picture was Iris's portrait (set
once by hand, never checked), so every Saga post in a council thread showed Iris. The
portrait is the source of truth; on ready the bot compares the portrait's hash with
the one it last uploaded and uploads only on a change (Discord rate-limits avatar
edits, so an unchanged portrait never calls the API).

``AWDISCORD_PROFILE_AVATAR``: a URL or file path (default: the persona's portrait on
the site, ``AWDISCORD_PORTRAIT_BASE`` + ``<persona>.webp``). ``off`` disables.
"""
from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path

PORTRAIT_BASE = "https://aitherium.com/agents/"
STAMP = "profile_avatar.sha256"
_UA = {"User-Agent": "Mozilla/5.0 (awdiscord portrait sync)"}


def source_for(persona: str, env=os.environ) -> str:
    explicit = (env.get("AWDISCORD_PROFILE_AVATAR") or "").strip()
    if explicit.lower() in ("off", "0", "false", "no"):
        return ""
    if explicit:
        return explicit
    if not persona:
        return ""
    base = (env.get("AWDISCORD_PORTRAIT_BASE") or PORTRAIT_BASE).rstrip("/") + "/"
    return f"{base}{persona.strip().lower()}.webp"


def _read(src: str) -> bytes:
    if src.startswith(("http://", "https://")):
        with urllib.request.urlopen(urllib.request.Request(src, headers=_UA),
                                    timeout=20) as r:
            return r.read()
    return Path(src).read_bytes()


def _as_png(data: bytes) -> bytes:
    """Discord takes png/jpeg/gif image data; a webp portrait goes through ffmpeg."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff" or data[:4] == b"GIF8":
        return data
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("portrait is not png/jpeg/gif and ffmpeg is missing")
    out = subprocess.run([ffmpeg, "-loglevel", "error", "-i", "pipe:0", "-f", "image2pipe",
                          "-vcodec", "png", "pipe:1"], input=data, capture_output=True,
                         timeout=60, check=True)
    return out.stdout


def _mime(png: bytes) -> str:
    if png[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if png[:4] == b"GIF8":
        return "image/gif"
    return "image/png"


def sync(rest, persona: str, state_dir, env=os.environ, read=_read, convert=_as_png) -> str:
    """Upload the persona's portrait as this bot's profile picture when it changed.

    Returns ``uploaded`` | ``unchanged`` | ``off``. Raises on a failed fetch or upload
    (the caller logs it; the bot keeps running with the old picture).
    """
    src = source_for(persona, env)
    if not src:
        return "off"
    raw = read(src)
    digest = hashlib.sha256(raw).hexdigest()
    stamp = Path(state_dir) / STAMP
    if stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == digest:
        return "unchanged"
    img = convert(raw)
    uri = f"data:{_mime(img)};base64,{base64.b64encode(img).decode('ascii')}"
    rest.request("PATCH", "/users/@me", {"avatar": uri})
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(digest, encoding="utf-8")
    return "uploaded"
