"""Import the sibling awvoice brick from the tree, as its own suite does.

The voice transport is a client of ``awvoice.loop``; CI installs nothing, so the
test run has to put the sibling package on the path itself.
"""
import os
import sys
from pathlib import Path

# The egress-address probe (awdiscord.egress) never touches the network in tests.
os.environ.setdefault("AWDISCORD_EGRESS_PROBE", "0")
# Nor does the join flow's learn-site probe.
os.environ.setdefault("AWDISCORD_ACADEMY_PROBE", "0")

_AWV = Path(__file__).resolve().parents[2] / "awvoice"
if str(_AWV) not in sys.path:
    sys.path.insert(0, str(_AWV))
