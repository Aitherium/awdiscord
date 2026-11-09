"""version_check and the overlay's dist-info sync."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))

from awdiscord import __version__, version_check  # noqa: E402

SYNC = PKG.parents[2] / ".DEPLOYMENT" / "awdiscord" / "sync_dist_info.py"


def test_versions_reports_drift(monkeypatch):
    import importlib.metadata as md
    monkeypatch.setattr(md, "version", lambda name: "0.5.0")
    got = version_check.versions()
    assert got == {"code": __version__, "metadata": "0.5.0", "match": False}
    monkeypatch.setattr(md, "version", lambda name: __version__)
    assert version_check.versions()["match"] is True


def _load_sync():
    spec = importlib.util.spec_from_file_location("sync_dist_info", SYNC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_sync_rewrites_only_the_version_line(tmp_path, monkeypatch):
    if not SYNC.is_file():                     # the brick's own repo has no .DEPLOYMENT
        import pytest
        pytest.skip("monorepo-only deploy helper")
    pkg = tmp_path / "fakepkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text('__version__ = "0.6.20"\n')
    dist = tmp_path / "fakepkg-0.5.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: fakepkg\nVersion: 0.5.0\n"
                                   "Summary: x\n\nVersion: 0.5.0 in the body stays\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    sync = _load_sync()
    assert sync.sync("fakepkg") == 0
    text = (dist / "METADATA").read_text()
    assert "\nVersion: 0.6.20\n" in text
    assert text.count("Version: 0.5.0") == 1           # only the header changed
    assert sync.sync("fakepkg") == 0                    # idempotent
    assert sync.sync("no_such_pkg_xyz") == 2
