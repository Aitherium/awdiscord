"""The two install tiers: the minimal chat link and the separate manage grant."""
import urllib.parse

import pytest
from awdiscord import install


def _q(url: str) -> dict:
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)


def test_minimal_link_is_unchanged():
    q = _q(install.install_url("123"))
    assert q["client_id"] == ["123"]
    assert q["permissions"] == [str(install.MINIMAL_PERMISSIONS)]
    assert "applications.commands" in q["scope"][0]
    assert "guild_id" not in q


def test_minimal_link_still_refuses_admin():
    with pytest.raises(ValueError):
        install.install_url("123", install.FORBIDDEN_PERMISSIONS["ADMINISTRATOR"])


def test_manage_set_builds_a_layout_but_never_administers():
    for bit in (install.MANAGE_CHANNELS, install.MANAGE_ROLES, install.MANAGE_WEBHOOKS,
                install.MANAGE_EVENTS, install.MANAGE_THREADS, install.CREATE_PUBLIC_THREADS,
                install.SPEAK, install.CONNECT):
        assert install.MANAGE_PERMISSIONS & bit
    assert install.MANAGE_PERMISSIONS & install.MINIMAL_PERMISSIONS == \
        install.MINIMAL_PERMISSIONS
    for name, bit in install.MANAGE_FORBIDDEN.items():
        assert not install.MANAGE_PERMISSIONS & bit, name


def test_manage_url_is_bound_to_one_numeric_guild():
    q = _q(install.manage_url("123", "456"))
    assert q["client_id"] == ["123"]
    assert q["guild_id"] == ["456"]
    assert q["disable_guild_select"] == ["true"]
    assert q["permissions"] == [str(install.MANAGE_PERMISSIONS)]
    assert "applications.commands" in q["scope"][0]


def test_manage_url_requires_a_numeric_guild():
    for bad in ("", "abc", "g-1", None, "  "):
        with pytest.raises(ValueError):
            install.manage_url("123", bad)


def test_manage_url_refuses_forbidden_bits():
    with pytest.raises(ValueError):
        install.manage_url("123", "456", install.MANAGE_PERMISSIONS | (1 << 3))
    with pytest.raises(ValueError):
        install.manage_url("123", "456", install.MANAGE_PERMISSIONS | (1 << 40))


def test_manage_url_refuses_a_non_numeric_app_id():
    with pytest.raises(ValueError):
        install.manage_url("app", "456")
