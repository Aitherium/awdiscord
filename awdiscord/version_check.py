"""Is the installed package metadata telling the truth about this code?

An overlay image copies new ``.py`` files over an installed package and leaves
its ``*.dist-info`` alone, so ``pip show awdiscord`` keeps reporting the version
the image was first built with (measured 2026-11-08: dist-info 0.5.0 while the
code was 0.6.20, which produced a wrong "not deployed" report). The code's
``__version__`` is the truth; this module says when the metadata disagrees.

The bot logs ``version_drift`` at startup when they differ; the overlay recipe
(``.DEPLOYMENT/awdiscord/sync_dist_info.py``) fixes the metadata in the image.
"""
from __future__ import annotations


def versions(dist: str = "awdiscord") -> dict:
    """``{"code", "metadata", "match"}``. Never raises; an unreadable metadata
    version is ``None`` and does not count as a match."""
    from awdiscord import __version__ as code
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            meta = version(dist)
        except PackageNotFoundError:
            meta = None
    except Exception:                                         # noqa: BLE001
        meta = None
    return {"code": str(code), "metadata": meta, "match": meta == str(code)}
