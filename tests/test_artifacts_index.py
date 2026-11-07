"""/artifact: the saved-page index and the slash command that reads it.

Discord's autocomplete is the picker: the bot answers each keystroke with up to
25 choices. These tests hold the contract the picker depends on — ranked
search, the 25-choice ceiling, a stale page flagged rather than trusted, and a
missing page answered privately rather than with an empty card.
"""

import json

import pytest
from awdiscord.artifacts import (
    MAX_CHOICES,
    ArtifactIndex,
    ArtifactIndexError,
    record_for,
    slug,
)
from awdiscord.dispatch import AUTOCOMPLETE, CHOICES, COMMAND, MESSAGE, Dispatcher, commands

PAGE = "<title>From wesyn to awdk</title><p>A review.</p><h2>Verdict</h2><p>Clean.</p>"


def _page(tmp_path, name="page.html", body=PAGE):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


class _Agent:
    def health(self):
        return "ok"


def _dispatcher(index):
    disp = Dispatcher(_Agent())
    disp.artifacts = index
    return disp


def _command(name, private=None):
    options = [{"name": "name", "value": name}]
    if private is not None:
        options.append({"name": "private", "value": private})
    return {"type": COMMAND, "id": "1", "token": "t",
            "member": {"user": {"id": "42"}},
            "data": {"name": "artifact", "options": options}}


def _typing(text):
    return {"type": AUTOCOMPLETE, "id": "1", "token": "t",
            "data": {"name": "artifact",
                     "options": [{"name": "name", "value": text, "focused": True}]}}


# -- the index -------------------------------------------------------------

def test_slug_is_stable_and_typeable():
    assert slug("From wesyn to awdk") == "from-wesyn-to-awdk"
    assert slug("  !!! ") == "artifact"


def test_a_missing_index_reads_as_empty(tmp_path):
    assert ArtifactIndex(tmp_path / "absent").list() == []


def test_save_then_get_round_trips_with_a_digest(tmp_path):
    index = ArtifactIndex(tmp_path / "idx")
    record = index.save(record_for(_page(tmp_path), title="From wesyn to awdk"))
    assert len(record.sha256) == 64
    assert index.get("from-wesyn-to-awdk").path == record.path


def test_saving_the_same_name_replaces_instead_of_duplicating(tmp_path):
    index = ArtifactIndex(tmp_path / "idx")
    page = _page(tmp_path)
    index.save(record_for(page, title="One", name="same"))
    index.save(record_for(page, title="Two", name="same"))
    assert [r.title for r in index.list()] == ["Two"]


def test_search_ranks_exact_then_prefix_then_substring(tmp_path):
    index = ArtifactIndex(tmp_path / "idx")
    page = _page(tmp_path)
    for name in ("deploy-status", "deploy", "old-deploy-notes"):
        index.save(record_for(page, title=name, name=name))
    assert [r.name for r in index.find("deploy")] == [
        "deploy", "deploy-status", "old-deploy-notes"]


def test_choices_never_exceed_discords_ceiling(tmp_path):
    index = ArtifactIndex(tmp_path / "idx")
    page = _page(tmp_path)
    for i in range(40):
        index.save(record_for(page, title=f"Page {i}", name=f"page-{i}"))
    choices = index.choices("")
    assert len(choices) == MAX_CHOICES
    assert all(len(c["name"]) <= 100 for c in choices)


def test_a_changed_file_is_flagged_stale(tmp_path):
    index = ArtifactIndex(tmp_path / "idx")
    page = _page(tmp_path)
    index.save(record_for(page, title="Live"))
    page.write_text(PAGE + "<p>edited</p>", encoding="utf-8")
    assert index.get("live").is_stale()
    assert "stale" in index.choices("live")[0]["name"]


def test_a_corrupt_index_is_an_error_not_an_empty_list(tmp_path):
    root = tmp_path / "idx"
    root.mkdir()
    (root / "index.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ArtifactIndexError):
        ArtifactIndex(root).list()


def test_remove_reports_whether_anything_went(tmp_path):
    index = ArtifactIndex(tmp_path / "idx")
    index.save(record_for(_page(tmp_path), title="Gone"))
    assert index.remove("gone") is True
    assert index.remove("gone") is False


# -- the slash command -----------------------------------------------------

def test_artifact_is_registered_with_autocomplete():
    defs = {c["name"]: c for c in commands()}
    assert "artifact" in defs
    name_opt = defs["artifact"]["options"][0]
    assert name_opt["autocomplete"] is True
    assert 1 in defs["artifact"]["contexts"]     # usable in DMs


def test_typing_returns_matching_choices(tmp_path):
    index = ArtifactIndex(tmp_path / "idx")
    index.save(record_for(_page(tmp_path), title="From wesyn to awdk"))
    result = _dispatcher(index).handle(_typing("wesyn"))
    assert result.response["type"] == CHOICES
    values = [c["value"] for c in result.response["data"]["choices"]]
    assert values == ["from-wesyn-to-awdk"]


def test_autocomplete_on_a_corrupt_index_offers_nothing_rather_than_crashing(tmp_path):
    root = tmp_path / "idx"
    root.mkdir()
    (root / "index.json").write_text("{bad", encoding="utf-8")
    result = _dispatcher(ArtifactIndex(root)).handle(_typing("x"))
    assert result.response == {"type": CHOICES, "data": {"choices": []}}


def test_picking_a_page_posts_its_card(tmp_path):
    index = ArtifactIndex(tmp_path / "idx")
    index.save(record_for(_page(tmp_path), title="From wesyn to awdk"))
    result = _dispatcher(index).handle(_command("from-wesyn-to-awdk"))
    assert result.response["type"] == MESSAGE
    embed = result.response["data"]["embeds"][0]
    assert embed["title"] == "From wesyn to awdk"
    assert "flags" not in result.response["data"]
    assert result.log["cmd"] == "artifact"


def test_private_flag_makes_the_card_ephemeral(tmp_path):
    index = ArtifactIndex(tmp_path / "idx")
    index.save(record_for(_page(tmp_path), title="Mine"))
    result = _dispatcher(index).handle(_command("mine", private=True))
    assert result.response["data"]["flags"] == 64


def test_an_unknown_name_answers_privately(tmp_path):
    result = _dispatcher(ArtifactIndex(tmp_path / "idx")).handle(_command("nope"))
    data = result.response["data"]
    assert data["flags"] == 64
    assert "No saved page" in data["content"]


def test_a_stale_page_says_so_on_the_card(tmp_path):
    index = ArtifactIndex(tmp_path / "idx")
    page = _page(tmp_path)
    index.save(record_for(page, title="Drifted"))
    page.write_text(PAGE + "<p>changed</p>", encoding="utf-8")
    result = _dispatcher(index).handle(_command("drifted"))
    footer = result.response["data"]["embeds"][0]["footer"]["text"]
    assert "changed since it was saved" in footer


def test_the_index_file_is_versioned_json(tmp_path):
    index = ArtifactIndex(tmp_path / "idx")
    index.save(record_for(_page(tmp_path), title="Shape"))
    data = json.loads(index.path.read_text(encoding="utf-8"))
    assert data["schema"] == "awdiscord.artifacts/v1"
    assert data["artifacts"][0]["name"] == "shape"


def test_index_follows_the_bots_state_dir(tmp_path, monkeypatch):
    from awdiscord.artifacts import default_root
    monkeypatch.delenv("AWDISCORD_ARTIFACT_DIR", raising=False)
    monkeypatch.setenv("AWDISCORD_STATE_DIR", str(tmp_path / "state"))
    assert default_root() == tmp_path / "state" / "artifacts"


def test_an_explicit_artifact_dir_beats_the_state_dir(tmp_path, monkeypatch):
    from awdiscord.artifacts import default_root
    monkeypatch.setenv("AWDISCORD_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AWDISCORD_ARTIFACT_DIR", str(tmp_path / "explicit"))
    assert default_root() == tmp_path / "explicit"
