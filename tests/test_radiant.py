"""awdiscord.radiant: idempotent Knights Radiant roles; refuses without Manage Roles."""
from awdiscord import radiant


class FakeDiscord:
    def __init__(self, roles, perms=0):
        self.roles = [dict(r) for r in roles]
        self.perms = perms
        self.writes = []

    def call(self, method, path, body=None):
        if path == "/users/@me":
            return {"id": "bot"}
        if path.endswith("/members/bot"):
            return {"roles": ["botrole"]}
        if method == "GET" and path.endswith("/roles"):
            return self.roles + [{"id": "botrole", "name": "AitherOS", "color": 0,
                                  "permissions": str(self.perms)}]
        self.writes.append((method, path, body))
        if method == "POST":
            self.roles.append({"id": f"r{len(self.roles)}", "name": body["name"],
                               "color": body["color"]})
        elif method == "PATCH":
            rid = path.rsplit("/", 1)[-1]
            for r in self.roles:
                if r["id"] == rid:
                    r["color"] = body["color"]
        return {}


EXISTING = [{"id": "w", "name": "Windrunner", "color": radiant.colour("#38bdf8")},
            {"id": "b", "name": "bondsmith", "color": 123}]


def test_diff_creates_missing_recolours_wrong_never_deletes():
    acts = radiant.diff(EXISTING + [{"id": "x", "name": "Server Booster", "color": 1}])
    names = {a["name"]: a["op"] for a in acts}
    assert "Windrunner" not in names                      # right colour: untouched
    assert names["bondsmith"] == "recolour"               # case-insensitive match
    assert names["Skybreaker"] == "create" and names["Knight Radiant"] == "create"
    assert "Server Booster" not in names
    assert len(acts) == len(radiant.PLAN) - 1


def test_apply_refuses_without_manage_roles_and_writes_nothing(capsys):
    fake = FakeDiscord(EXISTING, perms=0)
    assert radiant.run(["apply", "--guild", "g"], client=fake) == 1
    assert fake.writes == []
    assert "REFUSED" in capsys.readouterr().err


def test_apply_with_manage_roles_converges_then_is_a_noop():
    fake = FakeDiscord(EXISTING, perms=radiant.MANAGE_ROLES)
    assert radiant.run(["apply", "--guild", "g"], client=fake) == 0
    assert radiant.diff(fake.roles) == []
    n = len(fake.writes)
    assert radiant.run(["apply", "--guild", "g"], client=fake) == 0
    assert len(fake.writes) == n                          # second run changes nothing


def test_plan_never_writes():
    fake = FakeDiscord([], perms=radiant.MANAGE_ROLES)
    assert radiant.run(["plan", "--guild", "g"], client=fake) == 0
    assert fake.writes == []
