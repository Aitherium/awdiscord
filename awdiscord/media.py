"""Attachments -> what a model can read: images, video frames, audio, text.

A Discord message that is only a picture or a clip has no words, and the bot
used to answer it with a greeting because it never looked at attachments.
`prepare` turns them into:

  * ``images``  - data: URLs (JPEG/PNG/...), what a vision model takes
  * ``notes``   - one line per attachment so the model knows what it was sent
  * ``texts``   - the transcript of an audio clip or a video's soundtrack, and
                  the contents of text files and documents (PDF, DOCX)
  * ``unread``  - media that could NOT be read, so the caller can delegate it
                  instead of telling the person to go and ask someone else

An audio attachment is transcribed by the fleet speech-to-text service named by
``AWDISCORD_STT_URL`` (a multipart ``file`` field in, ``{"text": ...}`` out --
the shape both AitherVoice's ``/transcribe`` and any OpenAI-compatible
``/v1/audio/transcriptions`` take). A video becomes ONE contact sheet: six
frames spread over the clip, tiled 3x2 in time order, plus its soundtrack's
transcript. One image works with every vision path, including ones that only
read the first image of a turn. That needs ffmpeg; without it the bot asks
Discord's media proxy for the first frame instead and says so.

Stdlib only, like the rest of the core (PDF text wants the optional ``pypdf``
extra and degrades to a note without it). Every failure becomes a note, never an
exception: a broken attachment must not cost the user their answer.
"""
from __future__ import annotations

import base64
import io
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi")
AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac",
              ".aiff", ".aif", ".wma", ".amr")
DOC_EXTS = (".pdf", ".docx")
TEXT_EXTS = (".txt", ".md", ".py", ".js", ".ts", ".tsx", ".json", ".yaml", ".yml",
             ".toml", ".ini", ".cfg", ".log", ".csv", ".html", ".css", ".sh", ".ps1",
             ".sql", ".xml", ".rs", ".go", ".java", ".c", ".cpp", ".h", ".cs")
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_VIDEO_BYTES = 100 * 1024 * 1024
MAX_AUDIO_BYTES = 50 * 1024 * 1024
MAX_DOC_BYTES = 40 * 1024 * 1024
MAX_TEXT_BYTES = 64 * 1024
MAX_TRANSCRIPT_CHARS = 16000
MAX_IMAGES = 4
SHEET_FRAMES = 6            # 3x2
MAX_SIDE = 1280             # images are downscaled to this before they are sent
STT_TIMEOUT = 180.0

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


@dataclass
class Media:
    images: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    #: attachments that are media but could not be read here, by kind and name
    unread: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.images or self.notes or self.texts)

    def default_question(self) -> str:
        """What a message with attachments and no words is asking."""
        if any("video" in n for n in self.notes):
            return "What happens in this video? Describe it."
        if any(n.startswith("audio") for n in self.notes):
            return "What does this audio say? Transcribe and explain it."
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
    # An audio content type wins over the extension: a `.webm` voice note is
    # `audio/webm`, and reading it as video produced a silent frame sheet.
    if ctype.startswith("audio/"):
        return "audio"
    if ctype.startswith("image/") or name.endswith(IMAGE_EXTS):
        return "image"
    if ctype.startswith("video/") or name.endswith(VIDEO_EXTS):
        return "video"
    if name.endswith(AUDIO_EXTS):
        return "audio"
    if name.endswith(DOC_EXTS) or ctype == "application/pdf":
        return "doc"
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


# -- speech to text ------------------------------------------------------------------

def _stt_ctx():
    ca = os.getenv("AWDISCORD_CA_BUNDLE")
    return ssl.create_default_context(cafile=ca) if ca else None


def stt_url() -> str:
    """The fleet STT endpoint, e.g. ``https://...:8084/voice/transcribe``.

    ``AWDISCORD_VOICE_STT_URL`` is the name the bot's own voice transport uses
    for the same door, so a deployment configures it once.
    """
    return (os.getenv("AWDISCORD_VOICE_STT_URL")
            or os.getenv("AWDISCORD_STT_URL") or "").strip()


def fleet_transcribe(raw: bytes, filename: str = "audio.wav", mime: str = "",
                     timeout: float | None = None) -> str:
    """Transcribe one clip through the fleet STT service and return its text.

    Multipart ``file`` in, ``{"text": ...}`` out: the shape AitherVoice's
    ``/transcribe`` and an OpenAI-compatible ``/v1/audio/transcriptions`` both
    take, so the endpoint alone decides which backend answers. Raises when the
    endpoint is unset or refuses; the caller turns that into a note.
    """
    url = stt_url()
    if not url:
        raise RuntimeError("AWDISCORD_STT_URL is not set")
    boundary = "awdiscord" + base64.b16encode(os.urandom(8)).decode().lower()
    safe = "".join(c for c in (filename or "audio.wav") if c.isalnum() or c in "._-")
    body = b"".join([
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{safe or 'audio.wav'}\"\r\n"
        f"Content-Type: {mime or 'application/octet-stream'}\r\n\r\n".encode(),
        raw, f"\r\n--{boundary}--\r\n".encode()])
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "User-Agent": "awdiscord"})
    with urllib.request.urlopen(req, timeout=timeout or STT_TIMEOUT,
                                context=_stt_ctx()) as r:
        data = json.loads(r.read().decode("utf-8") or "{}")
    text = str((data or {}).get("text") or "").strip()
    if not text:
        raise RuntimeError("the STT service returned no text")
    return text[:MAX_TRANSCRIPT_CHARS]


def _default_transcribe():
    """The transcriber a bare ``prepare()`` uses: the fleet one when configured."""
    return fleet_transcribe if stt_url() else None


def _fleet_with_timeout(timeout: float | None):
    """``fleet_transcribe`` bound to a longer wait, for a note no one is waiting on."""
    def run(raw: bytes, filename: str, mime: str) -> str:
        return fleet_transcribe(raw, filename, mime, timeout=timeout)
    return run


def video_audio(raw: bytes, ffmpeg: str) -> bytes:
    """The soundtrack of a clip as 16 kHz mono WAV (what STT wants)."""
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "clip")
        with open(src, "wb") as f:
            f.write(raw)
        return _run([ffmpeg, "-v", "error", "-i", src, "-vn", "-ac", "1", "-ar", "16000",
                     "-f", "wav", "pipe:1"])


# -- documents -----------------------------------------------------------------------

def _pdf_text(raw: bytes) -> str:
    """Text of a PDF. Needs the optional ``pypdf``; refuses clearly without it."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("PDF text needs the pypdf extra") from exc
    reader = PdfReader(io.BytesIO(raw))
    pages = [(page.extract_text() or "").strip() for page in reader.pages[:60]]
    text = "\n\n".join(p for p in pages if p)
    if not text:
        raise RuntimeError("the PDF has no extractable text (a scan?)")
    return text[:MAX_TRANSCRIPT_CHARS]


def _docx_text(raw: bytes) -> str:
    """Text of a Word document: the paragraphs of ``word/document.xml``."""
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        xml = z.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    lines = ["".join(t.text or "" for t in p.iter(f"{_W_NS}t")) for p in root.iter(f"{_W_NS}p")]
    text = "\n".join(line for line in lines if line.strip())
    if not text:
        raise RuntimeError("the document has no text")
    return text[:MAX_TRANSCRIPT_CHARS]


def _document(raw: bytes, name: str) -> str:
    return _pdf_text(raw) if name.lower().endswith(".pdf") else _docx_text(raw)


def _transcript_block(name: str, text: str) -> str:
    return f"--- transcript of {name} ---\n{text}\n--- end {name} ---"


def prepare(attachments: list[dict] | None, fetch=_fetch, ffmpeg: str | None = "auto",
            transcribe="auto") -> Media:
    """Read up to MAX_IMAGES visual attachments, audio, documents and text files.

    ``fetch(url, limit) -> bytes``, ``ffmpeg`` and ``transcribe(raw, name, mime)
    -> str`` are injectable for tests. ``ffmpeg="auto"`` finds it on PATH, and
    ``transcribe="auto"`` uses the fleet STT when ``AWDISCORD_STT_URL`` is set
    (``None`` turns speech off, so the clip is reported as unread).
    """
    media = Media()
    ff = _ffmpeg() if ffmpeg == "auto" else ffmpeg
    stt = _default_transcribe() if transcribe == "auto" else transcribe
    for att in attachments or []:
        name = str(att.get("filename") or "attachment")
        url = att.get("url") or ""
        mime = str(att.get("content_type") or "")
        k = kind(att)
        try:
            if k == "image":
                if len(media.images) >= MAX_IMAGES:
                    media.notes.append(f"image {name} (not shown: only {MAX_IMAGES} per message)")
                    continue
                raw = fetch(url, MAX_IMAGE_BYTES)
                media.images.append(_image(raw, mime, ff))
                media.notes.append(f"image {name} (shown to you)")
            elif k == "video":
                if len(media.images) >= MAX_IMAGES:
                    media.notes.append(f"video {name} (not shown: only {MAX_IMAGES} per message)")
                    continue
                raw = b""
                heard = ""
                if stt is not None and ff is not None:
                    raw = fetch(url, MAX_VIDEO_BYTES)
                    try:
                        heard = stt(video_audio(raw, ff), f"{name}.wav", "audio/wav")
                        media.texts.append(_transcript_block(f"{name} (soundtrack)", heard))
                    except Exception as exc:  # noqa: BLE001 - the frames still arrive
                        media.unread.append(f"the soundtrack of video {name} <{url}>")
                        heard = ""
                        media.notes.append(_unheard_note(name, exc))
                if ff is not None:
                    sheet, secs = video_sheet(raw, ff)
                    media.images.append(sheet)
                    length = f"{secs:.1f}s, " if secs else ""
                    sound = ("you also have a transcript of its soundtrack below"
                             if heard else "you cannot hear its audio")
                    media.notes.append(
                        f"video {name} ({length}{_size(att)}): you see it as one image of "
                        f"{SHEET_FRAMES} frames spread across the clip, in time order "
                        f"left to right, top row first; {sound}.")
                else:
                    thumb = (att.get("proxy_url") or url) + (
                        "&" if "?" in (att.get("proxy_url") or url) else "?") + "format=jpeg"
                    media.images.append(_data_url(fetch(thumb, MAX_IMAGE_BYTES), "image/jpeg"))
                    media.notes.append(f"video {name} ({_size(att)}): you see only its first "
                                       "frame. You cannot hear its audio.")
            elif k == "audio":
                raw = fetch(url, MAX_AUDIO_BYTES)
                if stt is None:
                    raise RuntimeError("no speech-to-text service is configured")
                text = stt(raw, name, mime or "audio/ogg")
                media.texts.append(_transcript_block(name, text))
                media.notes.append(
                    f"audio {name} ({_size(att)}): you HEARD it - the transcript is below; "
                    "answer from what was said, never say you cannot hear it.")
            elif k == "doc":
                raw = fetch(url, MAX_DOC_BYTES)
                body = _document(raw, name)
                media.texts.append(f"--- {name} ---\n{body}\n--- end {name} ---")
                media.notes.append(f"document {name} (its text is below)")
            elif k == "text":
                raw = fetch(url, MAX_TEXT_BYTES)
                body = raw.decode("utf-8", "replace")
                media.texts.append(f"--- {name} ---\n{body}\n--- end {name} ---")
                media.notes.append(f"file {name} (its text is below)")
            else:
                media.notes.append(f"file {name} ({att.get('content_type') or 'unknown type'}, "
                                   f"{_size(att)}): not readable here")
                media.unread.append(f"file {name} <{url}>")
        except Exception as exc:  # noqa: BLE001 - a broken attachment never costs the answer
            if k in ("audio", "video", "doc"):
                media.unread.append(f"{k} {name} <{url}>")
                media.notes.append(_unheard_note(name, exc))
            else:
                media.notes.append(f"{k} {name}: could not be read ({type(exc).__name__}: "
                                   f"{str(exc)[:120]})")
    return media


def _unheard_note(name: str, exc: BaseException) -> str:
    """One line that never tells the member to go and find another bot."""
    return (f"{name}: could not be read here ({type(exc).__name__}: {str(exc)[:120]}). "
            "Hand it to the research agent rather than asking the member to resend it.")


def transcribe_attachment(att: dict, fetch=_fetch, ffmpeg: str | None = "auto",
                          transcribe="auto", timeout: float | None = None) -> str:
    """The transcript of ONE audio or video attachment. Raises when unreadable.

    ``prepare`` returns notes and never raises because a chat answer must survive a
    broken file. A proactive ingest is the opposite: the transcript IS the product,
    so the caller gets the failure and says so in the thread instead of filing a
    summary of nothing. A video is read through its soundtrack (16 kHz mono WAV),
    never its frames.

    ``timeout`` is the STT wait in seconds: the chat path keeps the short default
    (a member is waiting); an ingest that nobody is watching passes a longer one,
    because a two-minute note on the CPU lane takes minutes, not seconds.
    """
    k = kind(att)
    if k not in ("audio", "video"):
        raise ValueError(f"{att.get('filename') or 'the attachment'} is {k}, "
                         "not audio or video")
    if transcribe == "auto":
        stt = _default_transcribe() if timeout is None else (
            _fleet_with_timeout(timeout) if stt_url() else None)
    else:
        stt = transcribe
    if stt is None:
        raise RuntimeError("no speech-to-text service is configured (AWDISCORD_STT_URL)")
    name = str(att.get("filename") or "voice-note")
    mime = str(att.get("content_type") or "")
    url = str(att.get("url") or "")
    if not url:
        raise ValueError(f"{name} has no download URL")
    if k == "audio":
        return stt(fetch(url, MAX_AUDIO_BYTES), name, mime or "audio/ogg")
    ff = _ffmpeg() if ffmpeg == "auto" else ffmpeg
    if ff is None:
        raise RuntimeError("reading a video's soundtrack needs ffmpeg")
    return stt(video_audio(fetch(url, MAX_VIDEO_BYTES), ff), f"{name}.wav", "audio/wav")

