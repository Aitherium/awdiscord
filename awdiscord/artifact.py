"""Render an HTML page as a Discord embed.

Discord never renders HTML. What it renders is an embed: a title, a description,
up to 25 named fields, and an accent colour. This module turns a page into that
shape, deterministically and with the platform's own limits enforced rather than
discovered at send time.

Three properties are deliberate:

* **No dependencies.** ``awdiscord`` ships stdlib-only; this stays stdlib-only.
* **Fail loudly, cap quietly.** An unreadable file is an error (exit 2). A page
  too large for one embed is *not*: it is clipped to the platform ceiling and the
  embed says how many sections were dropped, because a card that silently loses
  half a document is worse than one that admits it.
* **Deterministic.** The same page always yields the same embed, so a test can
  assert the ceiling rather than hope for it.

The page is treated as hostile input: it is parsed, never executed, and nothing
it contains becomes markup on the Discord side except the small markdown subset
Discord itself understands.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

#: Discord's documented embed ceilings.
TITLE_MAX = 256
DESCRIPTION_MAX = 4096
FIELD_NAME_MAX = 256
FIELD_VALUE_MAX = 1024
FIELDS_MAX = 25
FOOTER_MAX = 2048
EMBED_TOTAL_MAX = 6000

#: How much of the 6000 total the description may claim before fields lose room.
DESCRIPTION_BUDGET = 1500

_ELLIPSIS = "…"
_HEADINGS = ("h1", "h2")
_BLOCKS = ("p", "li", "blockquote", "table")
_COLOR_RE = re.compile(r"--accent\s*:\s*(#[0-9A-Fa-f]{6})")


class ArtifactError(Exception):
    """The page could not be read or understood at all."""


@dataclass
class Section:
    heading: str
    body: str


@dataclass
class Artifact:
    title: str = ""
    description: str = ""
    color: int | None = None
    sections: list[Section] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    dropped: int = 0


def _clip(text: str, limit: int) -> str:
    """Trim to ``limit`` characters, ending with an ellipsis when it had to cut."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + _ELLIPSIS


def _clip_words(text: str, limit: int) -> str:
    """Prefer cutting on a word boundary so a field does not end mid-sentence."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[: max(0, limit - 1)]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip() + _ELLIPSIS


class _Extractor(HTMLParser):
    """Collect a title, a description, sections and links from a page.

    Text is emitted as the same markdown Discord renders: bold, code, list items
    and https links survive; every other tag is flattened to its text.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.artifact = Artifact()
        self._open: list[str] = []
        self._buf: list[str] = []
        self._in_style = False
        self._style: list[str] = []
        self._saw_h1 = False
        self._link_href: str | None = None
        self._link_text: list[str] = []
        self._heading: str | None = None
        self._skip_depth = 0
        self._row_cells = 0

    # -- helpers ---------------------------------------------------------
    def _has(self, name: str) -> bool:
        return name in self._open

    def _flush(self) -> str:
        text = "".join(self._buf)
        self._buf = []
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\s*\n\s*", "\n", text)
        return text.strip()

    # -- parser callbacks ------------------------------------------------
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in ("script", "svg", "template"):
            self._skip_depth += 1
            return
        if tag == "style":
            self._in_style = True
            return
        if tag == "meta":
            d = dict(attrs)
            if d.get("name", "").lower() == "description" and d.get("content"):
                self.artifact.description = d["content"].strip()
            return
        if tag == "a":
            self._link_href = dict(attrs).get("href", "")
            self._link_text = []
            return

        self._open.append(tag)

        if tag == "title":
            self._buf = []
        elif tag in _HEADINGS:
            self._flush()
            if tag == "h1" and not self._saw_h1:
                self._saw_h1 = True
        elif tag == "strong" or tag == "b":
            self._buf.append("**")
        elif tag == "code":
            self._buf.append("`")
        elif tag == "li":
            self._flush()
            self._buf.append("- ")
        elif tag in ("td", "th"):
            if self._row_cells:
                # The markup between two cells is pure whitespace; drop it so the
                # separator lands inside the row instead of on its own line.
                while self._buf and not self._buf[-1].strip():
                    self._buf.pop()
                if self._buf:
                    self._buf[-1] = self._buf[-1].rstrip()
                self._buf.append(" · ")
            self._row_cells += 1
        elif tag == "br":
            self._buf.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("script", "svg", "template"):
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "style":
            self._in_style = False
            return
        if tag == "a":
            text = re.sub(r"\s+", " ", "".join(self._link_text)).strip()
            href = self._link_href or ""
            if href.startswith("https://") and text:
                self._buf.append(f"[{text}]({href})")
                if href not in self.artifact.links and len(self.artifact.links) < 25:
                    self.artifact.links.append(href)
            elif text:
                self._buf.append(text)
            self._link_href = None
            self._link_text = []
            return

        if tag in self._open:
            while self._open and self._open.pop() != tag:
                pass

        if tag == "tr":
            self._buf.append("\n")
            self._row_cells = 0
            return

        if tag == "title":
            self.artifact.title = self._flush()
        elif tag in _HEADINGS:
            text = self._flush()
            self._heading = text or None
            if text:
                self.artifact.sections.append(Section(text, ""))
        elif tag == "strong" or tag == "b":
            self._buf.append("**")
        elif tag == "code":
            self._buf.append("`")
        elif tag in _BLOCKS:
            text = self._flush()
            if not text:
                return
            if self._heading and self.artifact.sections:
                current = self.artifact.sections[-1]
                current.body = (current.body + "\n" + text).strip()
            elif not self.artifact.description:
                self.artifact.description = text
            else:
                if not self.artifact.sections:
                    self.artifact.sections.append(Section("", ""))
                current = self.artifact.sections[-1]
                current.body = (current.body + "\n" + text).strip()

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_style:
            self._style.append(data)
            return
        if self._link_href is not None:
            self._link_text.append(data)
            return
        if self._open and self._open[-1] == "title":
            self._buf.append(data)
            return
        self._buf.append(data)

    def close(self):
        super().close()
        css = "".join(self._style)
        match = _COLOR_RE.search(css)
        if match:
            try:
                self.artifact.color = int(match.group(1)[1:], 16)
            except ValueError:  # pragma: no cover - regex already constrains this
                self.artifact.color = None
        if not self.artifact.title and self.artifact.sections:
            self.artifact.title = self.artifact.sections[0].heading


def parse_html(source: str) -> Artifact:
    """Parse ``source`` into an :class:`Artifact`. Never raises on messy markup."""
    parser = _Extractor()
    try:
        parser.feed(source)
        parser.close()
    except Exception as exc:  # html.parser is lenient; a raise means real damage
        raise ArtifactError(f"could not parse the page: {exc}") from exc
    art = parser.artifact
    if not art.title and not art.description and not art.sections:
        raise ArtifactError("the page has no title, description or headings to render")
    return art


def parse_file(path) -> Artifact:
    """Read and parse an HTML file. Raises :class:`ArtifactError` on any failure."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return parse_html(fh.read())
    except OSError as exc:
        raise ArtifactError(f"cannot read {path}: {exc}") from exc


def to_embed(art: Artifact, *, source: str = "") -> dict:
    """Build a Discord embed that fits the platform's ceilings.

    The embed reports how many sections it had to drop, so a clipped card is
    honest about being clipped.
    """
    title = _clip(art.title or "Artifact", TITLE_MAX)
    description = _clip_words(art.description, DESCRIPTION_BUDGET)

    fields: list[dict] = []
    dropped = 0
    title_text = (art.title or "").strip()
    for section in art.sections:
        if not section.heading and not section.body:
            continue
        # An <h1> that repeats the <title> is the page's own name, not a section.
        if section.heading and section.heading.strip() == title_text and not fields:
            if section.body and not description:
                description = _clip_words(section.body, DESCRIPTION_BUDGET)
            continue
        name = _clip(section.heading or "Notes", FIELD_NAME_MAX)
        value = _clip_words(section.body, FIELD_VALUE_MAX) or "_no text_"
        if not fields and not description:
            description = value
            continue
        if len(fields) >= FIELDS_MAX:
            dropped += 1
            continue
        fields.append({"name": name, "value": value, "inline": False})

    footer = _clip(f"{len(fields)} sections" + (f" · {source}" if source else ""), FOOTER_MAX)

    # Enforce the 6000-character total by dropping trailing fields.
    def total() -> int:
        return (
            len(title)
            + len(description)
            + len(footer)
            + sum(len(f["name"]) + len(f["value"]) for f in fields)
        )

    while fields and total() > EMBED_TOTAL_MAX:
        fields.pop()
        dropped += 1
    if dropped:
        footer = _clip(f"+{dropped} more sections not shown", FOOTER_MAX)

    embed: dict = {"title": title, "footer": {"text": footer}}
    if description:
        embed["description"] = description
    if fields:
        embed["fields"] = fields
    if art.color is not None:
        embed["color"] = art.color
    art.dropped = dropped
    return embed


def to_markdown(art: Artifact) -> str:
    """A plain-text rendering for clients that cannot take an embed."""
    out = [f"# {art.title}".strip()]
    if art.description:
        out.append(art.description)
    for section in art.sections:
        if section.heading:
            out.append(f"## {section.heading}")
        if section.body:
            out.append(section.body)
    if art.links:
        out.append("Links: " + " ".join(art.links[:10]))
    return "\n\n".join(part for part in out if part)


def embed_size(embed: dict) -> int:
    """The character total Discord measures against its 6000 ceiling."""
    return (
        len(embed.get("title", ""))
        + len(embed.get("description", ""))
        + len(embed.get("footer", {}).get("text", ""))
        + sum(len(f.get("name", "")) + len(f.get("value", "")) for f in embed.get("fields", []))
    )
