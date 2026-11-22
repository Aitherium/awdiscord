"""Answers about our own stack, from our own registry and corpus -- not the web.

2026-11-22, #bot on the OccupySaaS server: asked how to solve the Byzantine
generals problem and how to deploy agent swarms with AitherRelay/awrelay,
AitherNet, index.aitherium.com and awgit, the bot said "No documentation for an
awpermit protocol", "No documentation for awrun", "No documentation for awdelphi",
"No documentation for AitherNet" and "These are things you're proposing, not
things that exist". Every one of those is a real entry: awpermit, awrun, awdelphi
and awrelay are public bricks with live Pages sites, awgit is public, AitherNet is
awnet, and index.aitherium.com is one of our owned surfaces. The bot had looked
only at a web search, and a search that returns nothing about a product is not
evidence that the product is absent.

This module holds a name to what we publish about it, from sources we own:

1. the ecosystem registry a static host serves (``ecosystem.json``: every brick
   with its real ``status``, kind, tagline, install line and pairs_with);
2. the awkno offline corpus (``pages/*.json``: description with its Docs URL,
   synopsis and adopt line);
3. the owned zones and public surfaces page (``world.json``).

Stdlib only, like the rest of the brick. Data is CONFIGURED, never bundled: a
deployment that has mounted nothing gets ``available() == False`` and every
caller is inert. ``AWDISCORD_FIRSTPARTY_DIR`` holds ``ecosystem.json`` and
``awkno-pages/``; ``AWDISCORD_ECOSYSTEM_FILE`` names the registry directly and
``AWDISCORD_ECOSYSTEM_URL`` is a live fallback (https://aitherium.com/ecosystem.json).
"""
from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.request

#: The tool name the model calls, and the name agent.py counts as evidence.
TOOL_NAME = "first_party_lookup"

#: The apex copy a static host serves; a deployment sets this when it wants a live
#: fallback beside a mounted snapshot. Not a default: a public brick must not
#: silently answer everyone from Aitherium's registry.
ECOSYSTEM_URL_ENV = "AWDISCORD_ECOSYSTEM_URL"

CACHE_TTL = 600.0
_CACHE: dict = {"at": 0.0, "doc": None, "pages": None, "world": None}

#: Words that begin with "aw" but are not our bricks. Kept short and literal: every
#: real brick is also resolved against the registry, so this only stops a false
#: positive from a plain English sentence reaching the lookup.
_AW_STOP = frozenset("""
away awful awfully awesome awash awake awaken awoken award awards awarded awardee
aware awareness await awaits awaiting awhile awkward awry awol awacs awning awaltz
""".split())

_AW = re.compile(r"(?<![\w.-])(aw[a-z0-9]{2,})(?![\w-])", re.I)
_AITHER = re.compile(r"(?<![\w.])(Aither[A-Za-z0-9]*|aitherium\.(?:com|org|io))", re.I)
_SUBDOMAIN = re.compile(r"(?<![\w.-])((?:[a-z0-9-]+\.)+aitherium\.(?:com|org|io))", re.I)
_DOCS_LINE = re.compile(r"(?im)^\s*Docs\s*$\s*(\S+)")
_STATUS_NOTE = {
    "public": "public: a public repo exists and serves Pages.",
    "unpublished": "unpublished: built in the monorepo; no public repo or Pages site yet.",
    "planned": "planned: named, not built.",
}


def _first_party_dir() -> str:
    return (os.getenv("AWDISCORD_FIRSTPARTY_DIR") or "").strip()


def registry_path() -> str:
    """The registry file to read, or '' when none is configured."""
    explicit = (os.getenv("AWDISCORD_ECOSYSTEM_FILE") or "").strip()
    if explicit:
        return explicit
    root = _first_party_dir()
    if root:
        candidate = os.path.join(root, "ecosystem.json")
        if os.path.isfile(candidate):
            return candidate
    return ""


def pages_dir() -> str:
    """The awkno pages directory to read, or '' when none is configured."""
    explicit = (os.getenv("AWDISCORD_AWKNO_PAGES") or "").strip()
    if explicit:
        return explicit
    root = _first_party_dir()
    return os.path.join(root, "awkno-pages") if root else ""


def _url() -> str:
    return (os.getenv(ECOSYSTEM_URL_ENV) or "").strip()


def available() -> bool:
    """True when a first-party source is CONFIGURED, readable or not.

    A configured-but-unreadable corpus must reach ``lookup`` so it can say "could not
    be read" -- never "not configured", which reads as "there is nothing to find".
    """
    if registry_path() or _url() or _first_party_dir():
        return True
    d = pages_dir()
    return bool(d and os.path.isdir(d))


def reset() -> None:
    """Drop the cache (tests, and a caller that knows the corpus just changed)."""
    _CACHE.update({"at": 0.0, "doc": None, "pages": None, "world": None})


def _read_json(path: str) -> object:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _fetch_json(url: str) -> object:
    """Read an operator-configured URL. The caller sets it, like AWDISCORD_SEARCH_URL."""
    ca = (os.getenv("AWDISCORD_CA_BUNDLE") or "").strip()
    context = ssl.create_default_context(cafile=ca) if ca else None
    req = urllib.request.Request(url, headers={"User-Agent": "awdiscord firstparty"})
    with urllib.request.urlopen(req, timeout=10, context=context) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _fresh() -> bool:
    return bool(_CACHE.get("doc") is not None
                and time.monotonic() - float(_CACHE.get("at") or 0.0) < CACHE_TTL)


def registry() -> dict:
    """The registry document (bricks/products/stacks), cached; {} when unreadable."""
    if _fresh():
        return _CACHE["doc"] or {}
    doc: object = None
    path = registry_path()
    if path:
        try:
            doc = _read_json(path)
        except (OSError, ValueError):
            doc = None
    if doc is None and _url():
        try:
            doc = _fetch_json(_url())
        except Exception:  # noqa: BLE001 - an offline registry never blocks an answer
            doc = None
    _CACHE["doc"] = doc if isinstance(doc, dict) else {}
    _CACHE["at"] = time.monotonic()
    return _CACHE["doc"]


def pages() -> dict:
    """``{topic: page}`` from the awkno corpus, cached; {} when not mounted."""
    if _CACHE.get("pages") is not None:
        return _CACHE["pages"]
    out: dict = {}
    directory = pages_dir()
    if directory and os.path.isdir(directory):
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".json") or name.startswith("_"):
                continue
            try:
                page = _read_json(os.path.join(directory, name))
            except (OSError, ValueError):
                continue
            if not isinstance(page, dict):
                continue
            topic = str(page.get("topic") or name[:-5]).strip().lower()
            if topic:
                out[topic] = page
    _CACHE["pages"] = out
    return out


def world() -> dict:
    """The awkno ``world`` page (owned zones and public surfaces), or {}."""
    if _CACHE.get("world") is None:
        _CACHE["world"] = pages().get("world") or {}
    return _CACHE["world"]


def _norm(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _aliases(identifier: str) -> list[str]:
    """Spellings a person uses for one id: awrelay, AitherRelay, aither-relay."""
    low = str(identifier or "").lower()
    out = {low}
    if low.startswith("aw") and len(low) > 2:
        out.add("aither" + low[2:])       # awnet -> aithernet, awrelay -> aitherrelay
    return sorted(out)


def _docs_of(page: dict) -> str:
    match = _DOCS_LINE.search(str(page.get("description") or ""))
    return match.group(1).strip() if match else ""


def _problem_of(page: dict) -> str:
    """The page's Problem paragraph, one clipped line: what the brick actually does."""
    match = re.search(r"(?is)Problem\s*\n+(.+?)(?:\n\s*\n|\nInstall\b|\Z)",
                      str(page.get("description") or ""))
    return " ".join((match.group(1) if match else "").split())[:400]


def _rows(doc: dict) -> list[dict]:
    """Every registry entry as one flat row; first-party names resolve against these."""
    out: list[dict] = []
    for brick in (doc or {}).get("bricks") or []:
        if not isinstance(brick, dict) or not brick.get("id"):
            continue
        identifier = str(brick["id"]).strip()
        out.append({
            "id": identifier,
            "name": str(brick.get("reads_as") or brick.get("name") or identifier),
            "status": str(brick.get("status") or ""),
            "kind": str(brick.get("kind") or "brick"),
            "tagline": " ".join(str(brick.get("tagline") or "").split()),
            "install": str(brick.get("install") or ""),
            "docs": "",
            "category": "brick",
            "aliases": _aliases(identifier),
        })
    for product in (doc or {}).get("products") or []:
        if not isinstance(product, dict) or not (product.get("name") or product.get("id")):
            continue
        identifier = str(product.get("name") or product.get("id")).strip()
        page = str(product.get("page") or "").strip()
        docs = (("https://aitherium.com" + page) if page.startswith("/")
                else str(product.get("try_url") or "").strip())
        out.append({
            "id": identifier,
            "name": str(product.get("reads_as") or identifier),
            "status": str(product.get("status") or ""),
            "kind": "product",
            "tagline": " ".join(str(product.get("tagline") or "").split()),
            "install": "",
            "docs": docs,
            "category": "product",
            "aliases": _aliases(identifier),
        })
    for stack in (doc or {}).get("stacks") or []:
        if not isinstance(stack, dict) or not (stack.get("id") or stack.get("name")):
            continue
        identifier = str(stack.get("id") or stack.get("name")).strip()
        out.append({
            "id": identifier,
            "name": str(stack.get("reads_as") or stack.get("name") or identifier),
            "status": str(stack.get("status") or ""),
            "kind": "stack",
            "tagline": " ".join(str(stack.get("tagline") or "").split()),
            "install": "",
            "docs": "",
            "category": "stack",
            "aliases": _aliases(identifier),
        })
    return out


def enrich(rows: list[dict]) -> list[dict]:
    """Add the awkno page's Docs URL, synopsis and adopt line to each registry row."""
    corpus = pages()
    for row in rows:
        page = corpus.get(row["id"].lower())
        if not isinstance(page, dict):
            continue
        docs = _docs_of(page)
        if docs and not row.get("docs"):
            row["docs"] = docs
        if not row.get("tagline"):
            row["tagline"] = " ".join(str(page.get("synopsis") or "").split())
        row["synopsis"] = " ".join(str(page.get("synopsis") or "").split())
        row["adopt"] = " ".join(str(page.get("adopt") or "").split())
        row["what"] = _problem_of(page)
    return rows


def mentioned_names(question: str) -> list[str]:
    """First-party-looking names in ``question``, first seen first (deduped)."""
    text = str(question or "")
    out: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        key = _norm(name)
        if key and key not in seen:
            seen.add(key)
            out.append(name)

    for match in _AW.finditer(text):
        word = match.group(1)
        if word.lower() not in _AW_STOP:
            add(word)
    for match in _SUBDOMAIN.finditer(text):
        add(match.group(1))
    for match in _AITHER.finditer(text):
        add(match.group(1))
    return out


def resolve(name: str, rows: list[dict] | None = None) -> list[tuple[int, dict]]:
    """Registry rows matching ``name``, best first: ``[(score, row)]``.

    Score 0 is an exact id/alias, 1 the id is a prefix of the name ("awpermit
    protocol"), 2 the id occurs inside it. A name the registry does not hold
    returns [] -- which is not proof the thing does not exist.
    """
    query = _norm(name)
    if not query:
        return []
    rows = rows if rows is not None else enrich(_rows(registry()))
    hits: list[tuple[int, dict]] = []
    for row in rows:
        identifier = _norm(row.get("id"))
        names = {identifier, _norm(row.get("name"))}
        names |= {_norm(alias) for alias in row.get("aliases") or []}
        if query in names:
            hits.append((0, row))
        elif len(identifier) >= 4 and query.startswith(identifier):
            hits.append((1, row))
        elif len(identifier) >= 4 and identifier in query:
            hits.append((2, row))
    hits.sort(key=lambda pair: (pair[0], len(_norm(pair[1].get("id"))), pair[1].get("id")))
    return hits


def _domain_in(text: str) -> str:
    match = _SUBDOMAIN.search(str(text or ""))
    return match.group(1).lower() if match else ""


def _world_matches(domain: str) -> list[str]:
    """The world page's lines that mention ``domain`` (empty when it is not listed)."""
    body = str(world().get("body") or "")
    return [line.strip() for line in body.splitlines() if domain.lower() in line.lower()]


def _header(doc: dict, used_url: bool = False) -> str:
    count = (doc or {}).get("count")
    generated = str((doc or {}).get("generated_at") or "").strip()
    bits = ["Aitherium ecosystem registry"]
    if count:
        bits[0] += f" ({count} entries)"
    if generated:
        bits.append("generated " + generated)
    bits.append("awkno corpus")
    if used_url:
        bits.append("live https://aitherium.com/ecosystem.json")
    return "Source: " + ", ".join(bits)


def _render(hits: list[tuple[int, dict]], name: str, header: str) -> str:
    lines = [f"First-party lookup: {name}", header, ""]
    urls: list[str] = []
    for _, row in hits[:5]:
        docs = str(row.get("docs") or "")
        if docs:
            urls.append(docs)
        status = str(row.get("status") or "status unknown")
        lines.append(f"- {row.get('id')} — status: {status}; kind: {row.get('kind') or 'brick'}")
        tagline = str(row.get("tagline") or "").strip()
        if tagline and tagline not in (">-", ">"):
            lines.append(f"  {tagline}")
        synopsis = str(row.get("synopsis") or "").strip()
        if synopsis and synopsis.lower() not in tagline.lower():
            lines.append(f"  synopsis: {synopsis}")
        what = str(row.get("what") or "").strip()
        if what:
            lines.append(f"  what: {what}")
        adopt = str(row.get("adopt") or "").strip()
        if adopt:
            lines.append(f"  adopt: {adopt}")
        install = str(row.get("install") or "").strip()
        if install:
            lines.append(f"  install: {install}")
        if docs:
            lines.append(f"  docs: {docs}")
        note = _STATUS_NOTE.get(status)
        if note:
            lines.append(f"  ({note})")
    lines += ["", "This is our own product registry, not a web finding. State the status "
              "shown, and do not call a listed product nonexistent or a proposal. Only a "
              "name absent from this registry is 'not in our published registry'."]
    sources = ["https://aitherium.com/ecosystem.json"] + urls
    lines.append("Sources: " + " ".join(dict.fromkeys(sources)))
    return "\n".join(lines)


def _render_world(domain: str, header: str, rows: list[dict]) -> str:
    matches = _world_matches(domain)
    lines = [f"First-party lookup: {domain}", header, ""]
    if matches:
        lines.append("- one of our owned surfaces:")
        lines += [f"  {line}" for line in matches[:4]]
    else:
        lines.append("- NOT found in our world page; do not claim it is one of ours "
                     "without another source.")
    label = domain.split(".")[0]
    related = [row for row in rows
               if label and label not in ("aitherium", "www")
               and label in _norm(row.get("id"))
               and row.get("category") == "brick"]
    if related:
        lines.append("")
        lines.append("Related brick:")
        for row in related[:3]:
            docs = str(row.get("docs") or "")
            lines.append(f"- {row.get('id')} — status: {row.get('status')}; "
                         f"{row.get('tagline') or ''}".rstrip())
            if docs:
                lines.append(f"  docs: {docs}")
    lines += ["", "This is our own world page, not a web finding."]
    urls = [str(row.get("docs")) for row in related if row.get("docs")]
    lines.append("Sources: " + " ".join(
        dict.fromkeys(["https://aitherium.com/ecosystem.json"] + urls)))
    return "\n".join(lines)


def lookup(name: str) -> str:
    """The tool entry point: everything we publish about one first-party name."""
    name = str(name or "").strip()
    if not name:
        return ("Give a name to look up: an aw* brick, an Aither* product, or an "
                "aitherium.com address.")
    if not available():
        return ("No first-party corpus is configured here, so I could not check our own "
                "registry. Answer from the tools you have and say the registry was not "
                "reachable -- do not conclude a product does not exist.")
    doc = registry()
    rows = enrich(_rows(doc))
    if not rows and not pages():
        return (f"First-party lookup: {name}\n\n"
                "The configured first-party corpus could not be read (registry: "
                f"{registry_path() or _url() or 'none'}; pages: {pages_dir() or 'none'}). "
                "Say the registry was not reachable; do NOT conclude a product does "
                "not exist.")
    header = _header(doc, used_url=bool(_url()) and not registry_path())
    domain = _domain_in(name)
    if domain:
        return _render_world(domain, header, rows)
    hits = resolve(name, rows)
    if not hits:
        return (f"First-party lookup: {name}\n{header}\n\n"
                "Not found in the registry or the corpus. Say it is not in our published "
                "registry; do NOT call it nonexistent or a proposal, and do not invent a "
                "page for it.")
    return _render(hits, name, header)


def grounding(question: str) -> str:
    """A short first-party block for ``question``, or '' when it names nothing of ours.

    Added to a turn's context before the model answers, so our own registry is
    consulted before (and alongside) any web search even when the model forgets to
    call ``first_party_lookup``.
    """
    if not available():
        return ""
    names = mentioned_names(question)
    if not names:
        return ""
    doc = registry()
    rows = enrich(_rows(doc))
    if not rows and not pages():
        return ""
    header = _header(doc, used_url=bool(_url()) and not registry_path())
    blocks: list[str] = []
    seen: set[str] = set()
    for name in names:
        domain = _domain_in(name)
        if domain:
            if not _world_matches(domain):
                continue
            key = domain.lower()
            block = _render_world(domain, header, rows)
        else:
            hits = resolve(name, rows)
            if not hits:
                continue
            key = "|".join(str(row.get("id")) for _, row in hits[:3]).lower()
            block = _render(hits, name, header)
        if key in seen:
            continue
        seen.add(key)
        blocks.append(block)
    if not blocks:
        return ""
    return ("First-party sources for the names in this question. These are OUR OWN "
            "products and docs: consult them BEFORE web search, state each one's real "
            "status, and never call a listed product nonexistent.\n\n"
            + "\n\n".join(blocks[:4]))


def sources() -> list[str]:
    """The first-party sources this deployment can reach, for a footer or a log."""
    out: list[str] = []
    if registry_path():
        out.append(registry_path())
    if _url():
        out.append(_url())
    directory = pages_dir()
    if directory and os.path.isdir(directory):
        out.append(directory)
    return out
