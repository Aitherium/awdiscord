"""Files a turn produces, carried out with its answer.

The bot could only ever answer in text: a generated image or a spoken reply had
nowhere to go. An ``Outbox`` is the turn's tray -- a plugin tool drops a file in
it and the answer goes out with the file attached. The gateway uploads what is
in it (``awdiscord.rest.Rest.upload``) after posting the text, and the tool's
result tells the model what it produced, so the answer can point at it.

    outbox = caller["outbox"]                      # set by PluginHost.tools_for
    outbox.add("logo.png", png_bytes, "image/png")
    return "Attached logo.png (24 KB)."

Caps are Discord's, enforced here rather than at the upload: 8 MB a file, four
files a turn. Over the cap is an error the tool can state, never a silent drop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_FILES = 4
_NAME_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


class OutboxError(ValueError):
    """The file cannot go out with this answer; the message is safe to show."""


def safe_filename(name: str, default: str = "file.bin") -> str:
    """A plain file name: no directories, no spaces, at most 80 characters."""
    base = PurePosixPath(str(name or "").replace("\\", "/")).name
    cleaned = "".join(c if c in _NAME_ALLOWED else "_" for c in base).strip("._")
    return (cleaned[:80] or default)


@dataclass
class File:
    filename: str
    data: bytes
    content_type: str = "application/octet-stream"
    caption: str = ""

    def as_dict(self) -> dict:
        """What the gateway uploads (bytes stay bytes: this never leaves the process)."""
        return {"filename": self.filename, "data": self.data,
                "content_type": self.content_type, "caption": self.caption}


@dataclass
class Outbox:
    """The files of one turn."""
    files: list[File] = field(default_factory=list)
    max_files: int = MAX_FILES
    max_bytes: int = MAX_FILE_BYTES

    def __bool__(self) -> bool:
        return bool(self.files)

    def __len__(self) -> int:
        return len(self.files)

    def add(self, filename: str, data: bytes, content_type: str = "",
            caption: str = "") -> File:
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise OutboxError("nothing to attach: the file is empty")
        if len(data) > self.max_bytes:
            raise OutboxError(
                f"that file is {len(data) / (1024 * 1024):.1f} MB; Discord takes at most "
                f"{self.max_bytes // (1024 * 1024)} MB here")
        if len(self.files) >= self.max_files:
            raise OutboxError(f"at most {self.max_files} files go with one answer")
        f = File(safe_filename(filename), bytes(data),
                 content_type or "application/octet-stream", str(caption or ""))
        self.files.append(f)
        return f

    def summary(self) -> str:
        """A line the model can put in its answer, or '' when nothing is attached."""
        if not self.files:
            return ""
        return "Attached: " + ", ".join(f.filename for f in self.files)
