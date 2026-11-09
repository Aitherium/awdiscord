"""The host's own public addresses are learned and redacted from replies - offline.

Every address here is a documentation address (RFC 5737 / RFC 3849).
"""
from __future__ import annotations

import ipaddress

import pytest
from awdiscord import egress
from awdiscord.dispatch import reply_parts, scrub_host_paths

V4 = ipaddress.ip_address("203.0.113.7")
V6 = ipaddress.ip_address("2001:db8:abcd:12::7")
# fixed decoys so a random draw can never collide with the stand-in "real" ones
D4 = ipaddress.ip_address("192.0.2.44")
D6 = ipaddress.ip_address("2001:db8:ffff::44")
DOC_NETS = [ipaddress.ip_network(n) for n in
            ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32")]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("AWDISCORD_REDACT_IPS", raising=False)
    monkeypatch.setitem(egress._state, "ips", frozenset())
    monkeypatch.setattr(egress, "DECOYS", {4: D4, 6: D6})
    # documentation addresses stand in for real ones: count them as public here
    nets = [ipaddress.ip_network(n) for n in ("203.0.113.0/24", "2001:db8::/32")]
    monkeypatch.setattr(egress, "_is_public",
                        lambda ip: ip.is_global or any(ip in n for n in nets))
    yield


def _probe(answers):
    def get(url, timeout):
        body = answers.get(url)
        if body is None:
            raise OSError("unreachable")
        return body
    return get


def test_refresh_learns_both_families():
    ips = egress.refresh(_probe({egress.PROBES_V4[0]: "203.0.113.7\n",
                                 egress.PROBES_V6[1]: "2001:db8:abcd:12::7"}))
    assert ips == {V4, V6}


def test_a_failed_probe_keeps_the_address_it_was_hiding():
    egress.refresh(_probe({egress.PROBES_V4[0]: "203.0.113.7"}))
    assert egress.refresh(_probe({})) == {V4}


def test_refresh_ignores_garbage_and_private_answers():
    ips = egress.refresh(_probe({egress.PROBES_V4[0]: "<html>rate limited</html>",
                                 egress.PROBES_V4[1]: "10.0.0.1"}))
    assert ips == frozenset()


@pytest.mark.parametrize("text", [
    "your IP is 203.0.113.7.",
    "origin: 203.0.113.7, 198.51.100.1",
    "rDNS cpe-203-0-113-7.res.example.net",
    "padded syn-203-000-113-007.res.example.net",
    "v6 2001:db8:abcd:12::7 here",
    "same /64 2001:db8:abcd:12:aaaa:bbbb:cccc:dddd from a laptop",
    "mapped ::ffff:203.0.113.7",
])
def test_redact_hides_the_host_address(text):
    out = egress.redact(text, {V4, V6})
    assert "203.0.113.7" not in out and "2001:db8:abcd:12:" not in out
    assert "-113-7" not in out and "113-007" not in out
    assert any(d in out for d in ("192.0.2.44", "192-0-2-44", "2001:db8:ffff::44"))


@pytest.mark.parametrize("text", [
    "other 203.0.113.8 stays", "other /64 2001:db8:abcd:13::7 stays",
    "a version 1.2.3.4 string", "time 12:30:45", "MAC aa:bb:cc:dd:ee:ff",
])
def test_redact_leaves_other_text_alone(text):
    assert egress.redact(text, {V4, V6}) == text


def test_operator_listed_addresses_are_redacted(monkeypatch):
    monkeypatch.setenv("AWDISCORD_REDACT_IPS", "198.51.100.9, not-an-ip")
    assert "198.51.100.9" not in egress.redact("from 198.51.100.9")


def test_every_reply_is_redacted(monkeypatch):
    monkeypatch.setitem(egress._state, "ips", frozenset({V4}))
    assert "203.0.113.7" not in scrub_host_paths("I see 203.0.113.7")
    payload = reply_parts("ipify says 203.0.113.7 and C:\\Users\\x\\secret.txt")
    assert "203.0.113.7" not in payload["content"]
    assert "ipify says 192.0.2.44 and" in payload["content"]
    assert "secret.txt" in payload["content"] and "C:\\Users" not in payload["content"]


def test_probe_off_never_starts_a_thread(monkeypatch):
    monkeypatch.setenv("AWDISCORD_EGRESS_PROBE", "0")
    monkeypatch.setitem(egress._state, "thread", None)
    egress.start()
    assert egress._state["thread"] is None


def test_decoys_come_only_from_the_documentation_ranges():
    for _ in range(2000):
        d = egress.pick_decoys()
        assert d[4].version == 4 and d[6].version == 6
        assert any(d[4] in n for n in DOC_NETS[:3]), d[4]
        assert d[6] in DOC_NETS[3], d[6]
        assert not str(d[4]).endswith((".0", ".1", ".255"))


def test_the_process_decoy_is_drawn_from_those_ranges_and_stable():
    import importlib
    fresh = importlib.reload(egress)        # a "process start"
    try:
        first4, first6 = fresh.decoy(4), fresh.decoy(6)
        assert any(first4 in n for n in DOC_NETS[:3]) and first6 in DOC_NETS[3]
        assert fresh.decoy(4) == first4 and fresh.decoy(6) == first6
    finally:
        importlib.reload(egress)


def test_a_reply_carries_the_decoy_and_never_the_real_address(monkeypatch):
    monkeypatch.setitem(egress._state, "ips", frozenset({V4, V6}))
    for _ in range(3):                      # asked again: the same decoy
        out = reply_parts(f"Your IP is {V4} (v6 {V6})")["content"]
        assert str(V4) not in out and str(V6) not in out
        assert out == f"Your IP is {D4} (v6 {D6})"
