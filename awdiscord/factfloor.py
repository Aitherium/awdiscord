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
   the results do not support or contradict; those are dropped.
3. The answer says it left something out, and ends with the sources it was built
   from.

Stdlib only, like the rest of the brick; ``apply`` is pure except for the
``complete`` callable it is handed.
"""
from __future__ import annotations

import json
import re
from typing import Callable

#: Tools whose result is retrieved text an answer may be grounded in.
SEARCH_TOOLS = frozenset({"web_search", "deep_research", "fetch_url", "news_headlines"})

UNSURE_NOTE = ("I'm not sure about part of what I first wrote: the search results "
               "did not support it, so I left it out.")
UNSURE_ALL = ("I'm not sure: the search results I found do not support a clear "
              "answer to this.")
MAX_SOURCES = 5
MAX_EVIDENCE_CHARS = 12000

VERIFY_SYSTEM = (
    "You check a chat answer against the web search results it was written from. "
    "A sentence is UNSUPPORTED when it states a fact (who, what, how something "
    "works, a number, a date, a name) that the search results do not say, or that "
    "they contradict. Greetings, questions, advice, opinions marked as such, and "
    "sentences that only say what the results say are supported. Reply with JSON "
    "only, no prose: {\"unsupported\": [sentence numbers]}.")

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
          complete: Callable[[list], str] | None = None) -> tuple[str, dict]:
    """Hold ``answer`` to the retrieved ``evidence``.

    ``reference`` is the rest of what the answer may draw names from: the question,
    earlier turns, the server context, the persona. ``complete(messages) -> text``
    is the claim checker; None skips step 2.

    Returns (answer, report) with report keys ``corrected`` {wrong: right},
    ``unknown_names``, ``dropped`` (sentences), ``checked`` (step 2 ran) and
    ``sources``.
    """
    report: dict = {"corrected": {}, "unknown_names": [], "dropped": 0,
                    "checked": False, "sources": []}
    chunks = [str(c) for c in evidence or [] if str(c).strip()]
    if not chunks or not str(answer or "").strip():
        return answer, report
    ev = "\n".join(chunks)
    ref_lower = (ev + "\n" + str(reference or "")).lower()
    ev_names = evidence_names(ev)

    lines = _split(answer)
    keep = [[True] * len(line) for line in lines]
    for li, line in enumerate(lines):
        for si, sentence in enumerate(line):
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
                 for si, s in enumerate(line) if keep[li][si] and len(s.split()) >= 3]
        bad = unsupported_sentences([lines[li][si].strip() for li, si in slots],
                                    ev[:MAX_EVIDENCE_CHARS], complete)
        if bad is not None:
            report["checked"] = True
            for n in bad:
                li, si = slots[n - 1]
                keep[li][si] = False

    report["dropped"] = sum(not k for line in keep for k in line)
    out_lines = []
    for li, line in enumerate(lines):
        kept = [s for si, s in enumerate(line) if keep[li][si]]
        if not kept:
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
