"""Import the sibling awvoice brick from the tree, as its own suite does.

The voice transport is a client of ``awvoice.loop``; CI installs nothing, so the
test run has to put the sibling package on the path itself.
"""
import sys
from pathlib import Path

_AWV = Path(__file__).resolve().parents[2] / "awvoice"
if str(_AWV) not in sys.path:
    sys.path.insert(0, str(_AWV))
