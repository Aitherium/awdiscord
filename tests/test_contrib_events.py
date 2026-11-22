"""The shared contribution-event shape: stable ids, size buckets, bot logins, round trip."""
from awdiscord.contrib_events import (
    ContribEvent,
    is_bot_login,
    size_bucket,
    target_of,
)


def test_id_is_stable_and_case_blind():
    a = ContribEvent(kind="pr_merged", repo="Org/Repo", number=4, github_login="Alice",
                     sha="ff", size="s")
    b = ContribEvent(kind="pr_merged", repo="org/repo", number=4, github_login="alice",
                     sha="ff", size="xl", url="https://x")
    assert a.id == b.id and a.id.startswith("contrib_pr_merged:")
    assert a.id != ContribEvent(kind="pr_merged", repo="org/repo", number=5,
                                github_login="alice", sha="ff", size="s").id


def test_size_buckets():
    assert [size_bucket(n) for n in (0, 9, 10, 99, 100, 499, 500, 1999, 2000)] == [
        "xs", "xs", "s", "s", "m", "m", "l", "l", "xl"]


def test_bot_logins():
    assert is_bot_login("dependabot[bot]") and is_bot_login("github-actions")
    assert not is_bot_login("alice")


def test_problems_and_round_trip():
    ev = ContribEvent(kind="revert", repo="org/repo", number=9, github_login="bob",
                      target=target_of("Org/Repo", 4))
    assert ev.problems() == [] and ev.target == "org/repo#4"
    again = ContribEvent.from_dict(ev.to_dict())
    assert again.id == ev.id
    bad = ContribEvent(kind="pr_merged", repo="x", number=0, github_login="no spaces")
    assert len(bad.problems()) == 4
