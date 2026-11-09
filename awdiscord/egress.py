"""The host's own public IP addresses: learn them, then keep them out of replies.

A bot that runs at home egresses from the home connection. Any page it reads
for a member ("what is my IP", a header echo, a geolocation lookup) can carry
that address back into the channel. This module learns the addresses the host
egresses from and swaps them, in every reply the bot posts, for a DECOY.

The decoy looks like a real address but comes only from the ranges reserved
for documentation (RFC 5737: 192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24;
RFC 3849: 2001:db8::/32), so it can never point at a real third party. One
decoy per family is picked at process start and reused: asking twice gives the
same answer.

  egress_ips()   the learned addresses (IPv4 and IPv6), best effort
  refresh()      probe again now (short timeouts; failures keep the old set)
  start()        a daemon thread that refreshes every REFRESH_SECONDS
  decoy(v)       this process's decoy address for IP version ``v`` (4 or 6)
  redact(text)   ``text`` with the host's addresses replaced by the decoy

IPv6 is matched by /64 prefix: a home connection hands every device on the
LAN an address in the same /64, and the prefix alone identifies the line.
IPv4 is also matched in its dashed form (``cpe-198-51-100-7.example.net``),
the shape a reverse-DNS name carries.

``AWDISCORD_REDACT_IPS`` (comma-separated) adds addresses an operator knows
about; ``AWDISCORD_EGRESS_PROBE=0`` turns the network probe off (tests).
"""
from __future__ import annotations

import ipaddress
import logging
import os
import random
import re
import threading
import time
import urllib.request
from typing import Callable, Iterable

_log = logging.getLogger(__name__)

REFRESH_SECONDS = 3600
PROBE_TIMEOUT = 4.0
#: the only ranges a decoy is drawn from: reserved for documentation, never routed
DECOY_NETS_V4 = ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
DECOY_NET_V6 = "2001:db8::/32"

#: services that answer with the caller's address as plain text, per family
PROBES_V4 = ("https://api.ipify.org", "https://ipv4.icanhazip.com")
PROBES_V6 = ("https://api6.ipify.org", "https://ipv6.icanhazip.com")

def pick_decoys(rng: random.Random | None = None) -> dict:
    """``{4: IPv4Address, 6: IPv6Address}`` drawn from the documentation ranges.

    Host parts avoid the network (.0) and broadcast (.255) look of an IPv4 /24,
    so the decoy reads like an ordinary customer address.
    """
    rng = rng or random.SystemRandom()
    net4 = ipaddress.ip_network(rng.choice(DECOY_NETS_V4))
    v4 = net4.network_address + rng.randint(2, 254)
    net6 = ipaddress.ip_network(DECOY_NET_V6)
    v6 = net6.network_address + rng.getrandbits(96)
    return {4: ipaddress.IPv4Address(v4), 6: ipaddress.IPv6Address(v6)}


DECOYS = pick_decoys()          # one per process start; tests may replace it


def decoy(version: int = 4):
    """This process's decoy address for IP ``version`` (4 or 6)."""
    return DECOYS[6 if version == 6 else 4]


_lock = threading.Lock()
_state: dict = {"ips": frozenset(), "at": 0.0, "thread": None}

_V4_RE = re.compile(r"(?<![\w.])(\d{1,3})([.-])(\d{1,3})\2(\d{1,3})\2(\d{1,3})(?![\w])")
# an IPv6 candidate: hex groups and colons (with at least two colons), optional
# zone or embedded IPv4 tail; parsed by ipaddress, so false candidates are skipped
_V6_RE = re.compile(r"(?<![\w:.])(?=[0-9A-Fa-f:]*:[0-9A-Fa-f]*:)[0-9A-Fa-f:]{2,39}"
                    r"(?:\.\d{1,3}){0,3}(?![\w:])")


def _direct_get(url: str, timeout: float) -> str:
    """GET ``url`` without any proxy: the probe must see the HOST's address."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"User-Agent": "awdiscord-egress/1"})
    with opener.open(req, timeout=timeout) as resp:
        return resp.read(200).decode("ascii", "replace").strip()


def _is_public(ip) -> bool:
    """A globally routable address (tests widen it to the documentation ranges)."""
    return ip.is_global


def _operator_ips() -> set:
    out = set()
    for raw in (os.getenv("AWDISCORD_REDACT_IPS") or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.add(ipaddress.ip_address(raw))
        except ValueError:
            _log.warning("AWDISCORD_REDACT_IPS: %r is not an IP address", raw)
    return out


def _probe_enabled() -> bool:
    return (os.getenv("AWDISCORD_EGRESS_PROBE") or "1").strip().lower() not in (
        "0", "false", "no", "off")


def refresh(get: Callable[[str, float], str] | None = None) -> frozenset:
    """Probe the host's public addresses now. Returns the set in force afterwards.

    Args:
        get: ``(url, timeout) -> body`` transport; tests pass a fake.

    A family whose probes all fail keeps the addresses it had: a probe outage
    must never make redaction forget the address it was hiding.
    """
    get = get or _direct_get
    learned: dict = {4: set(), 6: set()}
    if _probe_enabled() or get is not _direct_get:
        for family, urls in ((4, PROBES_V4), (6, PROBES_V6)):
            for url in urls:
                try:
                    ip = ipaddress.ip_address(get(url, PROBE_TIMEOUT).split()[0])
                except Exception as exc:                          # noqa: BLE001
                    _log.debug("egress probe %s failed: %s", url, exc)
                    continue
                if ip.version == family and _is_public(ip):
                    learned[family].add(ip)
                    break
    with _lock:
        old = _state["ips"]
        keep = {ip for ip in old if not learned[ip.version]}
        ips = frozenset(keep | learned[4] | learned[6] | _operator_ips())
        _state["ips"] = ips
        _state["at"] = time.time()
    if ips != old:
        _log.info("egress addresses learned: %d IPv4, %d IPv6",
                  sum(1 for i in ips if i.version == 4), sum(1 for i in ips if i.version == 6))
    return ips


def egress_ips() -> frozenset:
    """The learned addresses plus any operator-listed ones (never probes inline)."""
    with _lock:
        ips = _state["ips"]
    return frozenset(ips | _operator_ips())


def _loop() -> None:
    while True:
        try:
            refresh()
        except Exception as exc:                                  # noqa: BLE001
            _log.warning("egress refresh failed: %s", exc)
        time.sleep(REFRESH_SECONDS)


def start(_host=None) -> None:
    """Start the hourly refresher once per process (a plugin ``on_ready`` hook)."""
    if not _probe_enabled():
        return
    with _lock:
        if _state["thread"] is not None:
            return
        t = threading.Thread(target=_loop, name="awdiscord-egress", daemon=True)
        _state["thread"] = t
    t.start()


def _matches(ip, ips: Iterable) -> bool:
    for own in ips:
        if own.version != ip.version:
            continue
        if ip.version == 4 and ip == own:
            return True
        if ip.version == 6 and int(ip) >> 64 == int(own) >> 64:
            return True
    return False


def redact(text: str, ips: Iterable | None = None) -> str:
    """``text`` with every one of the host's addresses replaced by the decoy.

    The replacement keeps the shape it found: a dashed reverse-DNS form gets a
    dashed decoy, an IPv6 address the IPv6 decoy.

    Args:
        text: an outbound reply.
        ips: the addresses to hide; default the learned set. Starts the refresher
            lazily, so a host whose plugin never ran ``on_ready`` still learns.
    """
    if not text:
        return text or ""
    if ips is None:
        start()
        ips = egress_ips()
    ips = frozenset(ips)
    if not ips:
        return text

    def _v4(m: re.Match) -> str:
        octets = (m.group(1), m.group(3), m.group(4), m.group(5))
        try:
            ip = ipaddress.IPv4Address(".".join(str(int(o)) for o in octets))
        except ValueError:
            return m.group(0)
        if not _matches(ip, ips):
            return m.group(0)
        return str(decoy(4)).replace(".", m.group(2))

    def _v6(m: re.Match) -> str:
        raw = m.group(0)
        try:
            ip = ipaddress.IPv6Address(raw)
        except ValueError:
            return raw
        mapped = ip.ipv4_mapped
        if mapped is not None and _matches(mapped, ips):
            return "::ffff:" + str(decoy(4))
        return str(decoy(6)) if _matches(ip, ips) else raw

    text = _V6_RE.sub(_v6, text)          # also catches ::ffff:<v4> forms
    if any(i.version == 4 for i in ips):
        text = _V4_RE.sub(_v4, text)
    return text
