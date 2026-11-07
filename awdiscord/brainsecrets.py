"""A server's BYO API key: one file per guild, never in guilds.db.

The guild store is plaintext JSON in SQLite that an operator (and any backup of
it) can read; an inference key must not live there. Each key is its own file
under ``{AWDISCORD_STATE_DIR}/brains/<guild>.secret``, created with
``os.open(..., O_CREAT|O_WRONLY, 0o600)`` inside a directory created ``0o700``
-- the mode is requested at creation, not chmod-ed on after the bytes are on
disk -- and the store keeps only a pointer (``credential_ref = file:<guild>``)
and a short fingerprint.

WINDOWS LIMITATION, stated so nobody trusts the mode there: NTFS ignores the
POSIX mode bits, so ``0600`` on Windows depends on the ACL inherited from the
state directory (by default the user's own profile, which is not world
readable, but it is not the same guarantee). A deployment that must hold keys
on a shared Windows host should use the Aitherium vault path instead; the file
store is the stdlib-only default for a self-hosted bot.

Nothing here logs a key, and no error message includes one.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from awdiscord.brain import BrainError

_log = logging.getLogger("awdiscord.brainsecrets")

_SAFE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _safe_guild(guild: str) -> str:
    """A guild id as a file name; anything else is hashed, never path-traversed."""
    raw = str(guild or "")
    if _SAFE.match(raw):
        return raw
    import hashlib
    return "g" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class BrainSecrets:
    """The key bytes, one 0600 file per guild, under a 0700 directory."""

    def __init__(self, state_dir: str | Path, dirname: str = "brains"):
        self.root = Path(state_dir)
        self.dir = self.root / dirname

    def path(self, guild: str) -> Path:
        return self.dir / f"{_safe_guild(guild)}.secret"

    def _ensure_dir(self) -> None:
        # mode= is honoured by os.mkdir on POSIX; an existing dir keeps its mode.
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError as exc:
            # Windows: mode bits are not an ACL; the inherited ACL still applies.
            _log.debug("kernel dir mode not applied (%s)", type(exc).__name__)

    def put(self, guild: str, key: str) -> Path:
        """Write ``key`` at mode 0600. Raises BrainError on an empty key."""
        if not str(key or ""):
            raise BrainError("an API key is required")
        self._ensure_dir()
        path = self.path(guild)
        fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.write(fd, str(key).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            # Windows: mode bits are not an ACL; see the module docstring.
            _log.debug("secret file mode not applied (%s)", type(exc).__name__)
        return path

    def get(self, guild: str) -> str:
        """The key, or "" when this server has none (never raises)."""
        try:
            return self.path(guild).read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def delete(self, guild: str) -> None:
        try:
            self.path(guild).unlink()
        except FileNotFoundError:
            _log.debug("no stored key to delete for this server")
        except OSError as exc:
            # A key that cannot be removed must not read as "off".
            _log.warning("could not delete the stored key (%s)", type(exc).__name__)
            raise BrainError("could not delete the stored key") from exc
