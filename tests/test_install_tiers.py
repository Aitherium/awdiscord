"""The three install tiers: minimal chat, elevated any-server, elevated pinned.

The last test is a drift guard on the public page: it reads the .tsx by absolute
path and asserts the two permission literals it prints equal the constants here,
and that the Administrator bit appears in neither. A page that quietly ships an
admin grant fails the suite.
"""
import pathlib
import re
import urllib.parse

import pytest
from awdiscord import install

#: The public lander. TRACKED but absent from a sparse checkout, so this test is
#: guarded on the file rather than on a working directory.
PAGE = pathlib.Path(__file__).resolve().parents[3] / (
    "apps/AitherVeil/src/app/(website)/discord/page.tsx"
)


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


def test_setup_link_asks_the_elevated_set_with_no_guild():
    q = _q(install.setup_install_url("123"))
    assert q["client_id"] == ["123"]
    assert q["permissions"] == [str(install.MANAGE_PERMISSIONS)]
    assert "applications.commands" in q["scope"][0]
    assert "guild_id" not in q
    assert "disable_guild_select" not in q


def test_setup_link_defaults_to_manage_permissions():
    assert install.setup_install_url("123") == install.setup_install_url(
        "123", install.MANAGE_PERMISSIONS)


def test_setup_link_refuses_every_manage_forbidden_bit():
    for name, bit in install.MANAGE_FORBIDDEN.items():
        with pytest.raises(ValueError, match="dangerous permissions"):
            install.setup_install_url("123", install.MANAGE_PERMISSIONS | bit)
        assert name in install.MANAGE_FORBIDDEN


def test_setup_link_refuses_a_non_numeric_app_id():
    for bad in ("", "app", "12 34", None):
        with pytest.raises(ValueError):
            install.setup_install_url(bad)


def test_manage_url_still_pins_the_guild_and_setup_does_not():
    pinned = _q(install.manage_url("123", "456"))
    open_link = _q(install.setup_install_url("123"))
    assert pinned["guild_id"] == ["456"]
    assert pinned["disable_guild_select"] == ["true"]
    assert "guild_id" not in open_link
    # Same permission set, same scopes: only the pin differs.
    assert pinned["permissions"] == open_link["permissions"]
    assert pinned["scope"] == open_link["scope"]


def test_install_url_carries_no_guild_by_default():
    assert "guild_id" not in _q(install.install_url("123"))


@pytest.mark.skipif(not PAGE.is_file(), reason="Veil page not materialised in this checkout")
def test_public_page_permission_literals_match_the_builders():
    src = PAGE.read_text(encoding="utf-8")
    literals = [int(n) for n in re.findall(r"permissions=(\d+)", src)]
    assert literals, "the page must carry its install links"
    assert int(install.MINIMAL_PERMISSIONS) in literals
    assert int(install.MANAGE_PERMISSIONS) in literals
    for value in literals:
        assert not value & install.FORBIDDEN_PERMISSIONS["ADMINISTRATOR"], value
        assert not value & install.MANAGE_FORBIDDEN["ADMINISTRATOR"], value
        assert not value & install.MANAGE_FORBIDDEN["MANAGE_GUILD"], value
    # The page must not build a permission number from a bit OR, where 8 could slip in.
    assert "1 << 3" not in src
