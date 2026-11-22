"""The fact floor: a chat answer built from a search says only what the search found.

2026-11-19, a public channel: after a web search the bot said "DuckDuckGo operates
its own search backend rather than proxying Google" (its results come mostly from
Bing), called the YaCy search engine "YACI", and addressed a "Demetri" nobody in
the conversation had mentioned. Nothing checked the answer against the results the
search had returned.

The newsroom has a floor for stories (``lib/newsroom.verification_floor``): it
counts independent outlets before a story reaches the company accounts. A chat
answer needs the claim-level counterpart, so this runs after the tool loop of any
turn whose search tools returned results:

1. Names. Every proper name in the answer must appear in the retrieved text or the
   conversation. A near miss of a name the results do contain ("YACI" for "YaCy")
   is corrected to the results' spelling; a name found nowhere ("Demetri") drops
   its sentence.
2. Claims. One call to the turn's own model (no new service) lists the sentences
   the results do not support or contradict; those are dropped. Reasoning and
   derivations are never sent to that check, and a claim our own first-party
   registry supports is never dropped.
3. A heading whose whole body was removed goes with it: the answer never carries an
   empty section. The answer says it left something out, and ends with its sources.

Stdlib only, like the rest of the brick; ``apply`` is pure except for the
``complete`` callable it is handed.
"""
from __future__ import annotations

import json
import re
from typing import Callable

#: Tools whose result is retrieved text an answer may be grounded in.
SEARCH_TOOLS = frozenset({"web_search", "deep_research", "fetch_url", "news_headlines"})
#: Our own registry/corpus lookup returns retrieved text too, and it is evidence for
#: a name or a claim exactly as a search result is (2026-11-22: a first-party answer
#: was gutted because the floor only knew about web searches).
FIRST_PARTY_TOOL = "first_party_lookup"
EVIDENCE_TOOLS = SEARCH_TOOLS | {FIRST_PARTY_TOOL}

UNSURE_NOTE = ("I'm not sure about part of what I first wrote: the retrieved sources "
               "did not support it, so I left it out.")
UNSURE_ALL = ("I'm not sure: the sources I found do not support a clear answer to this.")
MAX_SOURCES = 5
MAX_EVIDENCE_CHARS = 12000

VERIFY_SYSTEM = (
    "You check a chat answer against the retrieved sources (web results and our own "
    "first-party registry) it was written from. Flag a sentence as UNSUPPORTED only "
    "when it states a checkable fact about the outside world -- a person, company, "
    "product, date, number, price, event -- that the sources contradict, or that is "
    "presented as retrieved fact and the sources do not say it. NEVER flag reasoning, "
    "a derivation, a logical or mathematical argument, an explanation of how an "
    "algorithm works, or well-established computer-science knowledge merely because it "
    "is absent from the sources: those need no citation. A sentence the first-party "
    "registry, README or docs support IS supported. When unsure, keep the sentence. "
    "Reply with JSON only, no prose: {\"unsupported\": [sentence numbers]}.")

#: A sentence is REASONING when it argues rather than reports. A derivation is not a
#: checkable claim; on 2026-11-22 the floor deleted a Byzantine-fault-tolerance
#: explanation because a web search did not "support" it.
_REASONING = re.compile(
    r"(?i)\b(because|therefore|thus|hence|so that|suppose|supposing|assume|"
    r"assuming|given that|implies?|it follows|we can (?:show|prove|see)|"
    r"proof|proves?|proved|derive[sd]?|derivation|invariant|quorum|"
    r"(?:if|when) .{0,80}? then|step \d+|let (?:there|us|n|f|k|me))\b"
    r"|[=<>≤≥→⇒]|\bO\([^)]*\)")
#: A heading that opens reasoning: everything under it, until the next heading, is
#: protected from the name and claim filters. "How I'd actually solve this" is an
#: answer, not a retrieved fact.
_REASONING_HEADING = re.compile(
    r"(?i)\b(how (?:i|we|you)(?:'d| would)?|how to|approach|the real answer|"
    r"solution|solve|solving|design|derive|derivation|reasoning|proof|algorithm|"
    r"plan|implementation|mechanics|step[- ]by[- ]step)\b")

# Capitalised words that are not names: sentence starters that slip past the
# first-word rule (after a colon or a bullet), calendar words, and the
# abbreviations a tech answer uses without any source.
_NOT_NAMES = frozenset("""
i i'm i'd i'll i've a an the this that these those it its it's they we you he she
yes no ok okay hi hello hey thanks thank sure note sources source also however but
and or so if when while then there here what which who why how my your our their
monday tuesday wednesday thursday friday saturday sunday january february march
april may june july august september october november december
english internet web ai api apis url urls http https html css json xml faq gpu gpus
cpu cpus ram ssd os pc usb ui ux id ids ip vpn dns tls ssl llm llms tos eu uk us usa
pdf csv sql gb mb tb kb ghz utc
""".split())

_URL = re.compile(r"https?://[^\s<>()\"'\]]+")
_CODE = re.compile(r"`[^`]*`")
_NAME = re.compile(r"(?<![\w@#/.])[A-Z][A-Za-z0-9]*(?:['’-][A-Za-z0-9]+)*")
_LEAD = re.compile(r"^[\s\-*•>#\d.)(_`\"']*")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[\"'*(\[]?[A-Z0-9])")
#: A first-party name as it is written in a sentence (ids are lower-case: awpermit).
_FP_NAME = re.compile(r"(?i)\b(aw[a-z0-9]{2,}|aither[a-z0-9.]*)\b")
_FP_NOT_NAMES = frozenset("""
away awful awesome awfully awash awake awaken awoken award awards awarded aware
awareness await awaits awaiting awhile awkward awry awol awacs awning
""".split())


def evidence_urls(evidence: list[str], limit: int = MAX_SOURCES) -> list[str]:
    """The distinct URLs in the retrieved text, first seen first."""
    out: list[str] = []
    for chunk in evidence or []:
        for m in _URL.finditer(str(chunk)):
            url = m.group(0).rstrip(".,;:!?")
            if url not in out:
                out.append(url)
            if len(out) >= limit:
                return out
    return out


def _bare(name: str) -> str:
    n = name.lower()
    for tail in ("'s", "’s"):
        if n.endswith(tail):
            n = n[:-len(tail)]
    return n


def names_in(sentence: str) -> list[str]:
    """Name-like words in ``sentence``.

    The first word of a sentence is capitalised anyway, so it counts only when it
    looks like a name on its own: all capitals ("YACI") or inner capitals
    ("DuckDuckGo"). URLs and inline code are not prose and are skipped.
    """
    text = _CODE.sub(" ", _URL.sub(" ", sentence))
    lead = _LEAD.match(text)
    start = lead.end() if lead else 0
    out = []
    for m in _NAME.finditer(text):
        word = m.group(0)
        if len(word) < 2 or _bare(word) in _NOT_NAMES:
            continue
        if m.start() <= start and not (word.isupper() or any(c.isupper()
                                                             for c in word[1:])):
            continue
        out.append(word)
    return out


def _mentioned(text_lower: str, bare: str) -> bool:
    """True when ``bare`` occurs in ``text_lower`` as a whole word.

    A plain substring test let an invented "Tom" pass because the question held
    "atomic", and "Demetri" pass because the results had "Demetrius".
    """
    if not bare:
        return False
    return re.search(r"(?<!\w)" + re.escape(bare) + r"(?!\w)", text_lower) is not None


def evidence_names(evidence: str) -> list[str]:
    """Name-like words in the retrieved text, in first-seen order.

    A capitalised word that only ever begins a sentence is not a name. Scanning
    every capitalised word let "Must users install anything?" in the results make
    a near-miss correction rewrite the real "Rust" as "Must", and a sentence-opening
    "Being" rewrite "Bing". A word counts when it occurs mid-sentence, or is
    mixed-case/all-caps wherever it sits, so a correction can only ever land on a
    word the results actually use as a name.
    """
    seen: set[str] = set()
    out: list[str] = []
    for line in str(evidence or "").split("\n"):
        for sentence in _SENTENCE_END.split(line):
            for name in names_in(sentence):
                key = name.lower()
                if key not in seen:
                    seen.add(key)
                    out.append(name)
    return out


def _distance(a: str, b: str, cap: int) -> int:
    """Levenshtein distance, or cap + 1 as soon as it is known to exceed ``cap``."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def near_miss(name: str, evidence_names: list[str]) -> str:
    """The results' spelling of a name the answer got slightly wrong, or ''."""
    bare = _bare(name)
    if len(bare) < 4:
        return ""
    cap = 1 if len(bare) <= 5 else 2
    best, best_d = "", cap + 1
    for cand in evidence_names:
        d = _distance(bare, cand.lower(), cap)
        if 0 < d < best_d:
            best, best_d = cand, d
    return best


def _split(answer: str) -> list[list[str]]:
    return [_SENTENCE_END.split(line) if line.strip() else [line]
            for line in answer.split("\n")]


def _is_heading(text: str) -> bool:
    """A Markdown heading, a bold label, or a short reasoning label ending in ':'."""
    stripped = str(text or "").strip()
    if not stripped:
        return False
    if stripped.startswith("#"):
        return True
    if stripped.startswith("**") and stripped.endswith("**") and len(stripped) <= 200:
        return True
    return (len(stripped) <= 120 and stripped.endswith(":")
            and bool(_REASONING_HEADING.search(stripped)))


def _first_party_named(sentence: str, first_party_lower: str) -> bool:
    """True when ``sentence`` names one of our own entries present in first-party text."""
    if not first_party_lower:
        return False
    for match in _FP_NAME.finditer(str(sentence)):
        token = match.group(1).lower()
        if token in _FP_NOT_NAMES:
            continue
        if _mentioned(first_party_lower, token):
            return True
    return False


def _drop_empty_sections(headings: list[bool], keep: list[list[bool]],
                         lines: list[list[str]]) -> None:
    """Remove a heading whose section has no surviving body sentence.

    2026-11-22: the filter deleted every body sentence under "How I'd actually solve
    this (the real answer)" and shipped the bare header. A header with nothing under
    it is worse than no header: it says the answer has a part it does not have.
    """
    count = len(keep)
    li = 0
    while li < count:
        if not headings[li]:
            li += 1
            continue
        end = li + 1
        while end < count and not headings[end]:
            end += 1
        body_kept = any(keep[k][si] and lines[k][si].strip()
                        for k in range(li + 1, end) for si in range(len(keep[k])))
        if not body_kept:
            for si in range(len(keep[li])):
                keep[li][si] = False
        li = end


def _parse_unsupported(text: str, count: int) -> list[int] | None:
    m = re.search(r"\{.*\}", str(text or ""), re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    raw = data.get("unsupported") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return None
    return sorted({int(x) for x in raw
                   if isinstance(x, (int, float, str)) and str(x).strip().isdigit()
                   and 1 <= int(x) <= count})


def unsupported_sentences(sentences: list[str], evidence: str,
                          complete: Callable[[list], str]) -> list[int] | None:
    """1-based numbers of the sentences the results do not support, or None when the
    check could not be made (the model failed or answered in no parsable shape)."""
    if not sentences:
        return []
    numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(sentences, 1))
    messages = [{"role": "system", "content": VERIFY_SYSTEM},
                {"role": "user", "content": (f"Search results:\n{evidence}\n\n"
                                             f"Answer sentences:\n{numbered}")}]
    try:
        reply = complete(messages)
    except Exception:  # noqa: BLE001 - an unavailable checker never blocks the answer
        return None
    return _parse_unsupported(reply, len(sentences))


def apply(answer: str, evidence: list[str], reference: str = "",
          complete: Callable[[list], str] | None = None,
          first_party: list[str] | None = None) -> tuple[str, dict]:
    """Hold ``answer`` to the retrieved ``evidence``.

    ``reference`` is the rest of what the answer may draw names from: the question,
    earlier turns, the server context, the persona. ``complete(messages) -> text``
    is the claim checker; None skips step 2. ``first_party`` is what our own registry
    and corpus returned this turn: it is allowed as a source of names AND a sentence
    that names one of our entries is never dropped for being unsupported.

    Returns (answer, report) with report keys ``corrected`` {wrong: right},
    ``unknown_names``, ``dropped`` (sentences), ``checked`` (step 2 ran),
    ``reasoning`` (sentences protected as derivation) and ``sources``.
    """
    report: dict = {"corrected": {}, "unknown_names": [], "dropped": 0,
                    "checked": False, "reasoning": 0, "sources": []}
    chunks = [str(c) for c in evidence or [] if str(c).strip()]
    if not chunks or not str(answer or "").strip():
        return answer, report
    ev = "\n".join(chunks)
    fp_chunks = [str(c) for c in first_party or [] if str(c).strip()]
    fp_lower = "\n".join(fp_chunks).lower()
    ref_lower = (ev + "\n" + fp_lower + "\n" + str(reference or "")).lower()
    ev_names = evidence_names(ev + "\n" + "\n".join(fp_chunks))

    raw_lines = str(answer).split("\n")
    lines = _split(answer)
    headings = [_is_heading(raw) for raw in raw_lines]
    keep = [[True] * len(line) for line in lines]
    # Reasoning is an answer, not a retrieved fact: a derivation needs no citation.
    # It is never dropped and never sent to the claim checker. A reasoning heading
    # protects its whole section; a sentence-level marker protects that sentence.
    protected = [[False] * len(line) for line in lines]
    section_reasoning = False
    for li, line in enumerate(lines):
        if headings[li]:
            section_reasoning = bool(_REASONING_HEADING.search(raw_lines[li]))
            for si in range(len(line)):
                protected[li][si] = True     # a heading is never a checkable claim
            continue
        for si, sentence in enumerate(line):
            if section_reasoning or _REASONING.search(sentence):
                protected[li][si] = True
    for li, line in enumerate(lines):
        for si, sentence in enumerate(line):
            if protected[li][si]:
                continue
            for name in names_in(sentence):
                if _mentioned(ref_lower, _bare(name)):
                    continue
                fix = near_miss(name, ev_names)
                if fix:
                    report["corrected"][name] = fix
                    continue
                report["unknown_names"].append(name)
                keep[li][si] = False
    for wrong, right in report["corrected"].items():
        pat = re.compile(r"(?<![\w-])" + re.escape(wrong) + r"(?![\w-])")
        lines = [[pat.sub(right, s) for s in line] for line in lines]

    if complete is not None:
        slots = [(li, si) for li, line in enumerate(lines)
                 for si, s in enumerate(line)
                 if keep[li][si] and not protected[li][si] and len(s.split()) >= 3]
        bad = unsupported_sentences([lines[li][si].strip() for li, si in slots],
                                    ev[:MAX_EVIDENCE_CHARS], complete)
        if bad is not None:
            report["checked"] = True
            for n in bad:
                li, si = slots[n - 1]
                # Our own registry says this product exists; a checker that cannot
                # see that must not delete the claim (2026-11-22).
                if _first_party_named(lines[li][si], fp_lower):
                    continue
                keep[li][si] = False

    _drop_empty_sections(headings, keep, lines)
    report["reasoning"] = sum(protected[li][si] and not headings[li] and keep[li][si]
                              for li in range(len(keep)) for si in range(len(keep[li])))
    report["dropped"] = sum(not k for line in keep for k in line)
    out_lines = []
    for li, line in enumerate(lines):
        kept = [s for si, s in enumerate(line) if keep[li][si]]
        if not kept:
            continue
        if headings[li]:
            out_lines.append(raw_lines[li].rstrip())   # keep the heading verbatim
            continue
        out_lines.append(" ".join(s.strip() if i else s.rstrip()
                                  for i, s in enumerate(kept)))
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out_lines)).strip()
    if not re.search(r"[A-Za-z0-9]", text):
        text = UNSURE_ALL
    elif report["dropped"]:
        text += "\n\n" + UNSURE_NOTE

    sources = [u for u in evidence_urls(chunks) if u not in text]
    report["sources"] = sources
    if sources:
        text += "\n\nSources:\n" + "\n".join(f"- <{u}>" for u in sources)
    return text, report
