"""Self-assignable roles (``awdiscord/roles.py``): every rule is a REFUSAL.

The allowlist is the gate, but it is NOT the only one: the role's permission bits
are read live at assign time, so a role an admin allowed months ago and edited
since is still refused. Each test below pins one arm of that fail-closed order,
including the arms that look redundant (a managed role, an unparseable bit string,
the bot's own role).
"""
from __future__ import annotations

import pytest
from awdiscord import guildspec, install, roles
from awdiscord.rest import RestError

ADMIN = 1 << 3
MANAGE_GUILD = 1 << 5
BAN = 1 << 2
KICK = 1 << 1
MANAGE_ROLES = 1 << 28
MENTION_EVERYONE = 1 << 17
MODERATE_MEMBERS = 1 << 40
VIEW_CHANNEL = 1 << 10


def _role(**kw):
    row = {"id": "10", "name": "Python", "permissions": str(VIEW_CHANNEL),
           "position": 1}
    row.update(kw)
    return row


class FakeRest:
    """Records the write; answers 403/404/429 on demand."""

    def __init__(self, fail: int | None = None):
        self.calls = []
        self.fail = fail

    def request(self, method, path, body=None, auth=True):
        self.calls.append((method, path))
        if self.fail is not None:
            raise RestError(self.fail, "nope")
        return {}


# ── the privileged-bit union ────────────────────────────────────────────────

def test_privileged_bits_covers_all_three_lists():
    assert set(roles.PRIVILEGED_BITS) >= (
        set(guildspec.DANGEROUS_ROLE_BITS) | set(install.FORBIDDEN_PERMISSIONS)
        | set(install.MANAGE_FORBIDDEN))
    # MANAGE_FORBIDDEN is the only list carrying MODERATE_MEMBERS, and
    # FORBIDDEN_PERMISSIONS the only one carrying MANAGE_WEBHOOKS: a union that
    # quietly dropped either would let that bit through.
    assert roles.PRIVILEGED_BITS["MODERATE_MEMBERS"] == MODERATE_MEMBERS
    assert "MANAGE_WEBHOOKS" in roles.PRIVILEGED_BITS


@pytest.mark.parametrize("bit,name", [
    (ADMIN, "ADMINISTRATOR"),
    (MANAGE_GUILD, "MANAGE_GUILD"),
    (MANAGE_ROLES, "MANAGE_ROLES"),
    (BAN, "BAN_MEMBERS"),
    (KICK, "KICK_MEMBERS"),
    (MENTION_EVERYONE, "MENTION_EVERYONE"),
    (MODERATE_MEMBERS, "MODERATE_MEMBERS"),
    # Server-wide bits none of the install/layout lists carry: a role allowlisted
    # while harmless and later edited to hold one must still be refused.
    (1 << 4, "MANAGE_CHANNELS"),
    (1 << 13, "MANAGE_MESSAGES"),
    (1 << 27, "MANAGE_NICKNAMES"),
    (1 << 33, "MANAGE_EVENTS"),
    (1 << 34, "MANAGE_THREADS"),
])
def test_each_privileged_bit_is_named(bit, name):
    assert name in roles.privileged_names(str(VIEW_CHANNEL | bit))


def test_an_unreadable_permission_string_is_privileged_not_zero():
    assert roles.privileged_names("not-a-number") == ["UNREADABLE_PERMISSIONS"]
    assert roles.privileged_names(None) == ["UNREADABLE_PERMISSIONS"]


def test_an_ordinary_role_carries_no_privileged_bit():
    assert roles.privileged_names(str(VIEW_CHANNEL)) == []


# ── denial(): the allowlist gate, then the live bits ────────────────────────

def test_a_role_not_on_the_allowlist_is_refused_even_when_harmless():
    assert "not one members may give themselves" in roles.denial(_role(), set())


def test_the_allowlist_is_compared_by_id_never_by_name():
    # Same name, different id: a rename (or a re-created role) must not inherit
    # an earlier decision.
    assert roles.denial(_role(id="11", name="Python"), {"10"})


@pytest.mark.parametrize("role", [None, {}, {"id": ""}, "Python"])
def test_a_malformed_role_fails_closed(role):
    assert roles.denial(role, {"10"})


def test_an_allowed_harmless_role_is_the_one_allow_path():
    assert roles.denial(_role(), {"10"}) == ""


def test_an_allowed_privileged_role_is_still_refused_at_assign_time():
    # THE point of reading bits live: the allowlist was decided earlier, the
    # role was edited later. This is mutation arm (a) -- a checker that only
    # consults the allowlist passes this role through.
    why = roles.denial(_role(permissions=str(VIEW_CHANNEL | ADMIN)), {"10"})
    assert "only a server manager" in why


@pytest.mark.parametrize("tag", ["bot_id", "integration_id", "premium_subscriber"])
def test_a_managed_role_is_never_assigned(tag):
    assert "integration" in roles.denial(_role(tags={tag: "1"}), {"10"})
    assert "integration" in roles.denial(_role(managed=True), {"10"})


def test_assignable_lists_only_the_roles_the_denial_accepts():
    live = [_role(id="10", name="Python"), _role(id="11", name="Admin", permissions=str(ADMIN)),
            _role(id="12", name="Rust")]
    assert [r["name"] for r in roles.assignable(live, {"10", "11"})] == ["Python"]


# ── resolve_role(): ambiguity is a refusal ─────────────────────────────────

def test_a_name_matching_two_roles_resolves_to_neither():
    live = [_role(id="1", name="Helper"), _role(id="2", name="helper")]
    assert roles.resolve_role(live, "Helper") is None


def test_a_name_resolves_case_insensitively_and_whitespace_insensitively():
    live = [_role(id="1", name=" Helper ")]
    assert roles.resolve_role(live, "  hElPeR ")["id"] == "1"


def test_an_empty_name_resolves_to_nothing():
    assert roles.resolve_role([_role()], "   ") is None


# ── parse_limit() ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,want", [("4", 4), (2, 2), ("0", 0), ("abc", 3),
                                      (None, 3), ("9999", roles.MAX_ROLES),
                                      ("-5", 0)])
def test_parse_limit_bounds_and_defaults(raw, want):
    assert roles.parse_limit(raw) == want


# ── bot_ceiling_problem(): say what Discord will not explain ──────────────

def test_without_manage_roles_the_member_is_told_which_bit_is_missing():
    why = roles.bot_ceiling_problem(_role(), 5, VIEW_CHANNEL)
    assert "Manage Roles" in why


def test_a_role_at_or_above_the_bot_is_refused_with_the_hierarchy_reason():
    why = roles.bot_ceiling_problem(_role(position=5), 5, MANAGE_ROLES)
    assert "highest role" in why


def test_the_bots_own_role_is_refused():
    assert "my own role" in roles.bot_ceiling_problem(
        _role(id="99"), 9, MANAGE_ROLES, bot_role_id="99")


def test_a_reachable_role_has_no_problem():
    assert roles.bot_ceiling_problem(_role(position=1), 5, MANAGE_ROLES) == ""


def test_unreadable_bot_bits_are_refused_not_assumed_zero():
    assert roles.bot_ceiling_problem(_role(), 5, "garbage")


# ── the write: Discord's status codes become sentences ────────────────────

def test_assign_puts_the_role():
    rest = FakeRest()
    roles.assign(rest, "111", "7", "10")
    assert rest.calls == [("PUT", "/guilds/111/members/7/roles/10")]


def test_remove_deletes_the_role():
    rest = FakeRest()
    roles.remove(rest, "111", "7", "10")
    assert rest.calls == [("DELETE", "/guilds/111/members/7/roles/10")]


@pytest.mark.parametrize("status,needle", [(403, "Manage Roles"), (404, "no longer exists"),
                                           (429, "rate-limiting"), (500, "refused")])
def test_a_failed_write_raises_a_readable_refusal(status, needle):
    with pytest.raises(roles.RoleError) as exc:
        roles.assign(FakeRest(fail=status), "111", "7", "10")
    assert needle in str(exc.value)


def test_a_missing_rest_client_refuses_before_any_call():
    with pytest.raises(roles.RoleError, match="cannot reach Discord"):
        roles.assign(None, "111", "7", "10")


def test_a_socket_error_is_a_refusal_not_a_traceback():
    class Dead:
        def request(self, *a, **k):
            raise OSError("connection reset")

    with pytest.raises(roles.RoleError, match="could not reach"):
        roles.remove(Dead(), "111", "7", "10")


def test_an_allowed_role_edited_to_manage_channels_is_refused_at_assign_time():
    role = _role(permissions=str(VIEW_CHANNEL | (1 << 4)))
    assert "MANAGE_CHANNELS" in roles.denial(role, {"10"})
