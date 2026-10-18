"""Attachments -> what a model can read: images, video frames, text.

A Discord message that is only a picture or a clip has no words, and the bot
used to answer it with a greeting because it never looked at attachments.
`prepare` turns them into:

  * ``images``  - data: URLs (JPEG/PNG/...), what a vision model takes
  * ``notes``   - one line per attachment so the model knows what it was sent
  * ``texts``   - the contents of small text files (code, logs, markdown)

A video becomes ONE contact sheet: six frames spread over the clip, tiled 3x2
in time order. One image works with every vision path, including ones that
only read the first image of a turn. That needs ffmpeg; without it the bot
asks Discord's media proxy for the first frame instead and says so.

Stdlib only, like the rest of the core. Every failure becomes a note, never an
exception: a broken attachment must not cost the user their answer.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass, field

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi")
TEXT_EXTS = (".txt", ".md", ".py", ".js", ".ts", ".tsx", ".json", ".yaml", ".yml",
             ".toml", ".ini", ".cfg", ".log", ".csv", ".html", ".css", ".sh", ".ps1",
             ".sql", ".xml", ".rs", ".go", ".java", ".c", ".cpp", ".h", ".cs")
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_VIDEO_BYTES = 100 * 1024 * 1024
MAX_TEXT_BYTES = 64 * 1024
MAX_IMAGES = 4
SHEET_FRAMES = 6            # 3x2
MAX_SIDE = 1280             # images are downscaled to this before they are sent


@dataclass
class Media:
    images: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.images or self.notes or self.texts)

    def default_question(self) -> str:
        """What a message with attachments and no words is asking."""
        if any("video" in n for n in self.notes):
            return "What happens in this video? Describe it."
        if self.images:
            return "What's in this image? Describe it."
        return "Look at the attached file and tell me what it is."

    def prompt(self, text: str) -> str:
        """``text`` with the notes and file contents a text-only reader needs."""
        parts = [text]
        if self.notes:
            parts.append("Attachments:\n" + "\n".join(f"- {n}" for n in self.notes))
        parts += self.texts
        return "\n\n".join(p for p in parts if p)


def kind(att: dict) -> str:
    ctype = str(att.get("content_type") or "").lower()
    name = str(att.get("filename") or "").lower()
    if ctype.startswith("image/") or name.endswith(IMAGE_EXTS):
        return "image"
    if ctype.startswith("video/") or name.endswith(VIDEO_EXTS):
        return "video"
    if ctype.startswith("audio/"):
        return "audio"
    if ctype.startswith("text/") or "json" in ctype or name.endswith(TEXT_EXTS):
        return "text"
    return "file"


def _fetch(url: str, limit: int, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "awdiscord"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"larger than {limit // (1024 * 1024)} MB")
    return data


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def _run(argv: list[str], timeout: float = 90.0) -> bytes:
    p = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace")[-300:])
    return p.stdout


def _data_url(raw: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def _image(raw: bytes, mime: str, ffmpeg: str | None) -> str:
    """A data URL, downscaled to MAX_SIDE JPEG when ffmpeg is there."""
    if ffmpeg is None:
        return _data_url(raw, mime or "image/png")
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in")
        with open(src, "wb") as f:
            f.write(raw)
        scale = (f"scale='min({MAX_SIDE},iw)':'min({MAX_SIDE},ih)'"
                 ":force_original_aspect_ratio=decrease")
        out = _run([ffmpeg, "-v", "error", "-i", src, "-frames:v", "1", "-vf", scale,
                    "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "4", "pipe:1"])
    return _data_url(out, "image/jpeg")


def _duration(path: str) -> float:
    probe = shutil.which("ffprobe")
    if probe is None:
        return 0.0
    out = _run([probe, "-v", "error", "-show_entries", "format=duration",
                "-of", "json", path], timeout=30)
    try:
        return float(json.loads(out)["format"]["duration"])
    except (KeyError, ValueError, TypeError):
        return 0.0


def video_sheet(raw: bytes, ffmpeg: str) -> tuple[str, float]:
    """(data URL of a 3x2 sheet of frames in time order, clip seconds)."""
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "clip")
        with open(src, "wb") as f:
            f.write(raw)
        secs = _duration(src)
        # fps so SHEET_FRAMES samples span the clip; a clip of unknown length
        # gets one frame a second, which still fills the sheet from its start.
        rate = SHEET_FRAMES / secs if secs > 0 else 1.0
        vf = f"fps={rate:.6f},scale=480:-2,tile=3x2"
        out = _run([ffmpeg, "-v", "error", "-i", src, "-vf", vf, "-frames:v", "1",
                    "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "4", "pipe:1"])
    return _data_url(out, "image/jpeg"), secs


def _size(att: dict) -> str:
    n = int(att.get("size") or 0)
    return f"{n / (1024 * 1024):.1f} MB" if n >= 1024 * 1024 else f"{n // 1024} KB"


def prepare(attachments: list[dict] | None, fetch=_fetch, ffmpeg: str | None = "auto") -> Media:
    """Read up to MAX_IMAGES visual attachments and any small text files.

    ``fetch(url, limit) -> bytes`` and ``ffmpeg`` are injectable for tests;
    ``ffmpeg="auto"`` finds it on PATH.
    """
    media = Media()
    ff = _ffmpeg() if ffmpeg == "auto" else ffmpeg
    for att in attachments or []:
        name = str(att.get("filename") or "attachment")
        url = att.get("url") or ""
        k = kind(att)
        try:
            if k == "image":
                if len(media.images) >= MAX_IMAGES:
                    media.notes.append(f"image {name} (not shown: only {MAX_IMAGES} per message)")
                    continue
                raw = fetch(url, MAX_IMAGE_BYTES)
                media.images.append(_image(raw, str(att.get("content_type") or ""), ff))
                media.notes.append(f"image {name} (shown to you)")
            elif k == "video":
                if len(media.images) >= MAX_IMAGES:
                    media.notes.append(f"video {name} (not shown: only {MAX_IMAGES} per message)")
                    continue
                if ff is not None:
                    sheet, secs = video_sheet(fetch(url, MAX_VIDEO_BYTES), ff)
                    media.images.append(sheet)
                    length = f"{secs:.1f}s, " if secs else ""
                    media.notes.append(
                        f"video {name} ({length}{_size(att)}): you see it as one image of "
                        f"{SHEET_FRAMES} frames spread across the clip, in time order "
                        "left to right, top row first. You cannot hear its audio.")
                else:
                    thumb = (att.get("proxy_url") or url) + (
                        "&" if "?" in (att.get("proxy_url") or url) else "?") + "format=jpeg"
                    media.images.append(_data_url(fetch(thumb, MAX_IMAGE_BYTES), "image/jpeg"))
                    media.notes.append(f"video {name} ({_size(att)}): you see only its first "
                                       "frame. You cannot hear its audio.")
            elif k == "text":
                raw = fetch(url, MAX_TEXT_BYTES)
                body = raw.decode("utf-8", "replace")
                media.texts.append(f"--- {name} ---\n{body}\n--- end {name} ---")
                media.notes.append(f"file {name} (its text is below)")
            elif k == "audio":
                media.notes.append(f"audio {name} ({_size(att)}): you cannot hear it here; "
                                   "say so if it matters")
            else:
                media.notes.append(f"file {name} ({att.get('content_type') or 'unknown type'}, "
                                   f"{_size(att)}): not readable here")
        except Exception as exc:  # noqa: BLE001 - a broken attachment never costs the answer
            media.notes.append(f"{k} {name}: could not be read ({type(exc).__name__}: "
                               f"{str(exc)[:120]})")
    return media
