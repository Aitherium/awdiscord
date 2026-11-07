"""The HTML -> Discord embed renderer.

The contract these tests hold: a page always yields an embed inside Discord's
documented ceilings, a clipped embed says it was clipped, and hostile markup
becomes text rather than anything the client would execute.
"""

import pytest
from awdiscord.artifact import (
    DESCRIPTION_MAX,
    EMBED_TOTAL_MAX,
    FIELD_NAME_MAX,
    FIELD_VALUE_MAX,
    FIELDS_MAX,
    TITLE_MAX,
    ArtifactError,
    embed_size,
    parse_file,
    parse_html,
    to_embed,
    to_markdown,
)

PAGE = """<!doctype html>
<title>Deploy status</title>
<style>:root { --accent: #2F6B4F; }</style>
<p class="kicker">web 0.4.2</p>
<h1>Deploy status</h1>
<p>Three services, one degraded.</p>
<h2>Services</h2>
<p>web <strong>ready</strong>, worker <strong>crashloop</strong>.</p>
<ul><li>Pin the version</li><li>Roll back if retry fails</li></ul>
<h2>Links</h2>
<p>See <a href="https://aitherium.com/status">the status page</a>.</p>
<script>alert('should never appear')</script>
"""


def _big_page(sections=40, body_len=2000):
    parts = ["<title>Big</title>"]
    for i in range(sections):
        parts.append(f"<h2>Section {i}</h2><p>{'x' * body_len}</p>")
    return "".join(parts)


def test_extracts_title_sections_and_colour():
    art = parse_html(PAGE)
    assert art.title == "Deploy status"
    assert art.color == 0x2F6B4F
    headings = [s.heading for s in art.sections]
    assert "Services" in headings and "Links" in headings


def test_script_and_style_text_never_reach_the_embed():
    art = parse_html(PAGE)
    embed = to_embed(art)
    blob = repr(embed)
    assert "should never appear" not in blob
    assert "--accent" not in blob


def test_strong_becomes_markdown_bold():
    art = parse_html(PAGE)
    embed = to_embed(art)
    joined = " ".join(f["value"] for f in embed.get("fields", []))
    assert "**ready**" in joined


def test_https_link_becomes_markdown_and_is_collected():
    art = parse_html(PAGE)
    embed = to_embed(art)
    joined = " ".join(f["value"] for f in embed.get("fields", []))
    assert "[the status page](https://aitherium.com/status)" in joined
    assert art.links == ["https://aitherium.com/status"]


def test_h1_matching_the_title_is_not_duplicated_as_a_field():
    art = parse_html(PAGE)
    embed = to_embed(art)
    names = [f["name"] for f in embed.get("fields", [])]
    assert "Deploy status" not in names


def test_embed_honours_every_documented_ceiling():
    art = parse_html(_big_page())
    embed = to_embed(art, source="big.html")
    assert embed_size(embed) <= EMBED_TOTAL_MAX
    assert len(embed.get("fields", [])) <= FIELDS_MAX
    assert len(embed["title"]) <= TITLE_MAX
    assert len(embed.get("description", "")) <= DESCRIPTION_MAX
    for field in embed.get("fields", []):
        assert len(field["name"]) <= FIELD_NAME_MAX
        assert len(field["value"]) <= FIELD_VALUE_MAX


def test_a_clipped_embed_says_how_many_sections_it_dropped():
    art = parse_html(_big_page())
    embed = to_embed(art)
    assert art.dropped > 0
    assert "more sections not shown" in embed["footer"]["text"]


def test_small_page_drops_nothing():
    art = parse_html(PAGE)
    embed = to_embed(art)
    assert art.dropped == 0
    assert embed_size(embed) <= EMBED_TOTAL_MAX


def test_markdown_rendering_is_plain_text():
    art = parse_html(PAGE)
    out = to_markdown(art)
    assert out.startswith("# Deploy status")
    assert "## Services" in out
    assert "should never appear" not in out


def test_missing_file_is_a_could_not_judge(tmp_path):
    with pytest.raises(ArtifactError):
        parse_file(tmp_path / "absent.html")


def test_a_page_with_nothing_to_say_is_an_error():
    with pytest.raises(ArtifactError):
        parse_html("<div><span></span></div>")


def test_title_falls_back_to_the_first_heading():
    art = parse_html("<h1>Only a heading</h1><p>Body text here.</p>")
    assert art.title == "Only a heading"


TABLE_PAGE = (
    "<title>Rows</title><p>Two options compared.</p><h2>Costs</h2>"
    "<table><tr><th>Name</th><th>Cost</th></tr>"
    "<tr><td>Alpha</td><td>1 hour</td></tr>"
    "<tr><td>Beta</td><td>2 days</td></tr></table>"
)


def test_table_rows_keep_their_columns_on_one_line():
    embed = to_embed(parse_html(TABLE_PAGE))
    value = next(f["value"] for f in embed["fields"] if f["name"] == "Costs")
    assert "Name · Cost" in value
    assert "Alpha · 1 hour" in value
    assert "Beta · 2 days" in value


def test_no_separator_leaks_to_the_start_of_a_row():
    embed = to_embed(parse_html(TABLE_PAGE))
    value = next(f["value"] for f in embed["fields"] if f["name"] == "Costs")
    for line in value.splitlines():
        assert not line.startswith("·")
