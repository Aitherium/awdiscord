"""The bot's behaviour follows the fleet's affect (AitherSense), when it is reachable.

Owner, 2026-11-17: "worldmodel/aithercns/aithersense etc are modulating behavior and
voice and speech?" -- they modulated the VOICE's prosody and the desk avatar's face, but
not what the bot SAID, because nothing in this package read them.

AitherSense exposes ``GET /affect`` with ``prompt_modifier``: one line describing the
current mood (valence/arousal). This module fetches it, caches it (a mood does not
change between two messages), and returns it as a prefix for the system prompt.

Fail-open, loudly once: a blocked call or a down service yields ``""`` and the bot
answers exactly as before. A prompt must never hang on a feeling.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Callable, Optional

#: Swappable in tests: ``(url, timeout) -> parsed dict``.
FETCHER: list[Optional[Callable[[str, float], dict]]] = [None]
CACHE_S = 60.0
_cache: dict = {"at": 0.0, "text": "", "url": ""}


def _url() -> str:
    return (os.getenv("AWDISCORD_SENSE_AFFECT_URL") or "").strip()


def _fetch(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - env-configured https
        return json.loads(resp.read().decode("utf-8", "replace"))


def affect_prefix(timeout: float = 2.0, now: Optional[float] = None) -> str:
    """The mood line to prepend to the system prompt, or '' when there is none.

    Cached for CACHE_S per URL. Every failure path returns '' -- a missing feeling is
    not an error the person should ever see.
    """
    url = _url()
    if not url:
        return ""
    t = time.time() if now is None else now
    if _cache["url"] == url and (t - _cache["at"]) < CACHE_S:
        return _cache["text"]
    fetch = FETCHER[0] or _fetch
    text = ""
    try:
        body = fetch(url, timeout)
        if isinstance(body, dict):
            text = str(body.get("prompt_modifier") or "").strip()
    except Exception:  # noqa: BLE001 - any failure is "no mood tonight"
        text = ""
    _cache.update({"at": t, "text": text, "url": url})
    return text
