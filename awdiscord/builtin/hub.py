"""Hub: run the platform from Discord - alerts, signups, outside issues, reports.

Operators only (``AWDISCORD_OPERATORS``). The commands are hidden from members
(Manage Server by default) and every handler, and every button press, checks
the operator list again: a manager of the server is not an operator of the fleet.

  /today            one message: alerts, 24h signups, outside issues, reports, tickets
  /alerts [n]       top active Pulse alerts, with Ack / Resolve buttons per row
  /signups [hours]  new accounts and blog subscribers (Chronicle growth.* events)
  /issues           outside GitHub issues/PRs across the org waiting on our reply
  /reports          open Relay moderation reports

Every source is read in-network over TLS verified against ``AWDISCORD_CA_BUNDLE``
(default ``/etc/aither-ca.pem`` when present; never unverified) with the fleet
``X-Internal-Key`` from ``AITHER_INTERNAL_SECRET``. A source that cannot be read
prints its error where its number would be: a blank is never a zero.

Only /today (counts) answers in the channel; /alerts, /signups, /issues and
/reports carry internal alert keys, outsiders' names, emails or moderation
content, so they answer privately (ephemeral) to the operator who ran them.

/reports reads Relay ``/v1/reports``, which wants a moderator bearer, not the
internal key, and whose host does not resolve on the fleet (measured
2026-10-31): until that is wired it answers with the error, never a zero.
"""
from __future__ import annotations

import hashlib
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Callable

from awdiscord.plugins import Command, Option, Plugin, PluginError

LIMIT = 2000
DEFAULT_CA = "/etc/aither-ca.pem"
URLS = {   # env var -> in-network default (the awdiscord container is on aither-network)
    "pulse": ("AWDISCORD_PULSE_URL", "https://aither-pulse:8081"),
    "chronicle": ("AWDISCORD_CHRONICLE_URL", "https://aitheros-chronicle:8121"),
    "relay": ("AWDISCORD_RELAY_URL", "https://aitheros-communication-core:8205"),
    "support": ("AWDISCORD_SUPPORT_URL", "https://aitheros-genesis:8001"),
}
GH_SEARCH = "https://api.github.com/search/issues"
MAX_BUTTON_ROWS = 5           # Discord: at most 5 action rows per message

OPENER: list = [None]         # tests swap in a fake transport


class SourceError(Exception):
    """One data source could not answer; the message is shown in its place."""


# ── transport ──────────────────────────────────────────────────────────────────

def base(name: str) -> str:
    env, default = URLS[name]
    return (os.getenv(env) or default).rstrip("/")


@lru_cache(maxsize=4)
def _context(cafile: str) -> ssl.SSLContext:
    # Building one reads a CA store (seconds on some hosts): once per file, not per call.
    return ssl.create_default_context(cafile=cafile or None)


def _tls() -> ssl.SSLContext:
    ca = os.getenv("AWDISCORD_CA_BUNDLE") or (DEFAULT_CA if os.path.isfile(DEFAULT_CA) else "")
    return _context(ca)


def _open(req: urllib.request.Request, timeout: float, tls: ssl.SSLContext | None) -> Any:
    opener = OPENER[0] or urllib.request.urlopen
    try:
        with opener(req, timeout=timeout, context=tls) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise SourceError(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise SourceError(f"unreachable ({reason})") from exc
    try:
        return json.loads(raw or b"null")
    except ValueError as exc:
        raise SourceError("non-JSON answer") from exc


def fleet(method: str, url: str, timeout: float = 8.0) -> Any:
    """One call to a fleet service with the internal key. Raises SourceError."""
    key = (os.getenv("AITHER_INTERNAL_SECRET") or "").strip()
    if not key:
        raise SourceError("AITHER_INTERNAL_SECRET unset")
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None,
                                 headers={"X-Internal-Key": key, "X-Caller-Type": "platform",
                                          "X-Service-Name": "awdiscord",
                                          "Accept": "application/json"})
    return _open(req, timeout, _tls())


def github(url: str, timeout: float = 15.0) -> Any:
    """api.github.com (public CA). Unauthenticated works, at a lower rate limit."""
    h = {"Accept": "application/vnd.github+json", "User-Agent": "awdiscord-hub"}
    token = os.getenv("AWDISCORD_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN") or ""
    if token:
        h["Authorization"] = f"Bearer {token}"
    return _open(urllib.request.Request(url, headers=h), timeout, _context(""))


# ── sources ────────────────────────────────────────────────────────────────────

def pulse_stats() -> dict:
    return fleet("GET", base("pulse") + "/alerts/stats") or {}


def pulse_alerts() -> list[dict]:
    """Active alerts, one row per alert_key with occurrences summed, worst first."""
    body = fleet("GET", base("pulse") + "/alerts") or {}
    grouped: dict[str, dict] = {}
    for a in body.get("alerts") or []:
        key = str(a.get("alert_key") or a.get("id") or "?")
        row = grouped.setdefault(key, {"key": key, "title": a.get("title") or key,
                                       "severity": 0.0, "count": 0, "acknowledged": True,
                                       "last_seen": ""})
        row["severity"] = max(row["severity"], float(a.get("severity") or 0))
        row["count"] += int(a.get("occurrence_count") or 1)
        row["acknowledged"] = row["acknowledged"] and bool(a.get("acknowledged"))
        row["last_seen"] = max(row["last_seen"], str(a.get("last_seen") or ""))
    return sorted(grouped.values(), key=lambda r: (-r["severity"], -r["count"]))


def pulse_act(action: str, key: str) -> dict:
    if action not in ("acknowledge", "resolve"):
        raise SourceError(f"unknown action {action!r}")
    quoted = urllib.parse.quote(key, safe="")
    return fleet("POST", f"{base('pulse')}/alerts/{quoted}/{action}") or {}


GROWTH = (("account.registered", "account"), ("newsletter.subscribed", "blog subscriber"))


def _when(ev: dict) -> datetime | None:
    raw = str(ev.get("timestamp") or "")
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _mask(email: str) -> str:
    name, at, domain = email.partition("@")
    return f"{name[:1]}***@{domain}" if at else email


def signups(hours: int = 24, now: datetime | None = None) -> list[dict]:
    """New accounts + blog subscribers since ``hours`` ago, newest first.

    Raises SourceError naming every event type that could not be read.
    """
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=hours)
    rows, errors = [], []
    for kind, label in GROWTH:
        q = urllib.parse.urlencode({"event_type": f"growth.{kind}", "limit": 500})
        try:
            events = (fleet("GET", f"{base('chronicle')}/api/events?{q}") or {}).get("events") or []
        except SourceError as exc:
            errors.append(f"{kind}: {exc}")
            continue
        for ev in events:
            ts = _when(ev)
            data = ev.get("data") or {}
            if isinstance(data.get("data"), dict):      # the posted body, stored whole
                data = data["data"]
            if ts is None or ts < cutoff or data.get("already_subscribed"):
                continue
            source = str(data.get("source") or "")
            method = source.split(":", 1)[1] if source.startswith("registration:") \
                else (source or "unknown")
            who = data.get("username") or _mask(str(data.get("email") or "")) or "(unnamed)"
            rows.append({"kind": label, "who": str(who), "method": method, "at": ts})
    if errors and not rows:
        raise SourceError("; ".join(errors))
    rows.sort(key=lambda r: r["at"], reverse=True)
    if errors:
        rows.append({"error": "; ".join(errors)})
    return rows


def _org() -> str:
    return os.getenv("AITHER_GITHUB_ORG", "Aitherium")


def _ours() -> set[str]:
    raw = os.getenv("AITHER_COMMUNITY_OWN_ACCOUNTS", "wizzense,aither-wzns")
    return {a.strip().lower() for a in raw.split(",") if a.strip()}


def is_outsider(login: str, user_type: str = "User") -> bool:
    login = (login or "").lower()
    return bool(login) and user_type != "Bot" and not login.endswith("[bot]") \
        and login not in _ours()


def issues_waiting() -> list[dict]:
    """Open outside issues/PRs across the org (not the monorepo) with no reply from us.

    The same question lib/routines/community_owner_alerts.awaiting_reply asks;
    copied because a package must not import the monorepo's lib/.
    """
    org = _org()
    q = urllib.parse.urlencode({"q": f"org:{org} -repo:{org}/AitherOS is:open",
                                "per_page": 100})
    try:
        items = (github(f"{GH_SEARCH}?{q}") or {}).get("items") or []
    except SourceError as exc:
        raise SourceError(f"GitHub {exc}") from exc
    ours, waiting = _ours(), []
    for i in items:
        u = i.get("user") or {}
        if not is_outsider(u.get("login", ""), u.get("type", "User")):
            continue
        answered, unverified = False, ""
        if i.get("comments") and i.get("comments_url"):
            try:
                answered = any(((c.get("user") or {}).get("login", "").lower() in ours)
                               for c in github(i["comments_url"]) or [])
            except SourceError as exc:
                # Unknown stays on the list (never hidden) but is COUNTED as unverified,
                # so a rate-limited GitHub shows up as an error, not a growing number.
                unverified = str(exc)
        if not answered:
            repo = str(i.get("repository_url", "")).rsplit("/", 1)[-1]
            waiting.append({"ref": f"{repo}#{i.get('number')}",
                            "kind": "PR" if "pull_request" in i else "issue",
                            "title": str(i.get("title") or ""), "url": i.get("html_url"),
                            "by": u.get("login", ""), "unverified": unverified,
                            "created_at": str(i.get("created_at") or "")})
    waiting.sort(key=lambda w: w["created_at"])
    return waiting


def unverified_note(waiting: list[dict]) -> str:
    """`` (partial: N could not be checked for our reply: HTTP 403)``, or ``""``."""
    bad = [w["unverified"] for w in waiting if w.get("unverified")]
    if not bad:
        return ""
    return (f" (partial: {len(bad)} could not be checked for our reply: "
            f"{'; '.join(sorted(set(bad)))[:200]})")


def reports() -> dict:
    body = fleet("GET", base("relay") + "/v1/reports?status=pending") or {}
    items = sorted(body.get("reports") or [], key=lambda r: str(r.get("created_at") or ""),
                   reverse=True)
    return {"count": int(body.get("count", len(items))), "items": items}


def tickets() -> dict:
    out = {}
    for status in ("open", "in_progress"):
        q = urllib.parse.urlencode({"status": status, "limit": 500})
        body = fleet("GET", f"{base('support')}/api/v1/support/tickets/all?{q}") or {}
        out[status] = int(body.get("total", len(body.get("tickets") or [])))
    return out


# ── rendering ──────────────────────────────────────────────────────────────────

def clip(text: str) -> str:
    return text if len(text) <= LIMIT else text[: LIMIT - 14] + "\n(truncated)"


def _grab(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except SourceError as exc:
        return exc
    except Exception as exc:  # noqa: BLE001 - one broken source must not sink /today
        return SourceError(f"{type(exc).__name__}: {exc}")


def today(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    jobs = {"stats": pulse_stats, "signups": lambda: signups(24, now),
            "issues": issues_waiting, "reports": reports, "tickets": tickets}
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {k: pool.submit(_grab, fn) for k, fn in jobs.items()}
        got = {k: f.result() for k, f in futures.items()}

    def line(label: str, value: Any, fmt: Callable[[Any], str]) -> str:
        if isinstance(value, Exception):
            return f"{label}: could not read ({value})"
        return f"{label}: {fmt(value)}"

    def stats(s: dict) -> str:
        active = int(s.get("active_count") or 0)
        unacked = active - int(s.get("acknowledged_count") or 0)
        return f"{active} active, {int(s.get('critical_count') or 0)} critical, {unacked} unacked"

    def growth(rows: list) -> str:
        n = {label: 0 for _, label in GROWTH}
        partial = ""
        for r in rows:
            if "error" in r:
                partial = f" (partial: {r['error']})"
            else:
                n[r["kind"]] += 1
        return (f"{n['account']} new accounts, {n['blog subscriber']} new blog subscribers"
                + partial)

    lines = [f"**Aitherium today** - {now:%a %b %d %H:%M} UTC",
             line("Alerts", got["stats"], stats),
             line("Last 24h", got["signups"], growth),
             line("GitHub", got["issues"], lambda w: f"{len(w)} outside issues/PRs waiting "
                                                     "on a reply" + unverified_note(w)),
             line("Moderation", got["reports"], lambda r: f"{r['count']} open reports"),
             line("Support", got["tickets"], lambda t: f"{t['open']} open tickets, "
                                                       f"{t['in_progress']} in progress")]
    return clip("\n".join(lines))


def _sev(s: float) -> str:
    return "crit" if s >= 0.9 else "high" if s >= 0.7 else "warn"


def alerts_message(rows: list[dict], n: int) -> dict:
    shown = rows[:n]
    if not shown:
        return {"content": "No active Pulse alerts."}
    lines = [f"**{len(rows)} active alert keys** (top {len(shown)})"]
    components = []
    for i, r in enumerate(shown, 1):
        ack = " (acked)" if r["acknowledged"] else ""
        lines.append(f"{i}. [{_sev(r['severity'])}] {r['title'][:90]} - `{r['key'][:80]}` "
                     f"x{r['count']}{ack}")
        if len(components) < MAX_BUTTON_ROWS:
            ids = (_button("a", r["key"]), _button("r", r["key"]))
            components.append({"type": 1, "components": [
                {"type": 2, "style": 2, "label": f"Ack {i}", "custom_id": ids[0],
                 "disabled": r["acknowledged"]},
                {"type": 2, "style": 3, "label": f"Resolve {i}", "custom_id": ids[1]}]})
    return {"content": clip("\n".join(lines)), "components": components}


def signups_message(rows: list[dict], hours: int) -> str:
    real = [r for r in rows if "error" not in r]
    lines = [f"**{len(real)} signups in the last {hours}h**"]
    lines += [f"- {r['at']:%m-%d %H:%M} {r['kind']}: {r['who']} via {r['method']}"
              for r in real[:40]]
    lines += [f"could not read: {r['error']}" for r in rows if "error" in r]
    return clip("\n".join(lines))


def issues_message(waiting: list[dict]) -> str:
    if not waiting:
        return f"No outside issues or PRs in {_org()} are waiting on a reply."
    lines = [f"**{len(waiting)} outside issues/PRs waiting on a reply** (oldest first)"
             + unverified_note(waiting)]
    lines += [f"- {w['ref']} {w['kind']} by {w['by']} (since {w['created_at'][:10]})"
              f"{' [reply unverified]' if w.get('unverified') else ''}: "
              f"{w['title'][:80]}\n  <{w['url']}>" for w in waiting[:15]]
    return clip("\n".join(lines))


def reports_message(rep: dict) -> str:
    lines = [f"**{rep['count']} open moderation reports**"]
    for r in rep["items"][:5]:
        lines.append(f"- {str(r.get('created_at') or '')[:16]} #{r.get('channel', '?')} "
                     f"{r.get('message_author', '?')}: {r.get('reason') or '(no reason)'} - "
                     f"\"{str(r.get('message_content') or '')[:120]}\"")
    return clip("\n".join(lines))


# ── handlers ───────────────────────────────────────────────────────────────────

def _operator(ctx) -> None:
    if not ctx.user or ctx.user not in ctx.host.operators:
        raise PluginError("Only the bot's operators can use the hub.")


def _safe(fn: Callable[[], Any]) -> Callable[[], Any]:
    def run() -> Any:
        try:
            return fn()
        except SourceError as exc:
            return f"Could not read: {exc}"
    return run


def _today(ctx):
    _operator(ctx)
    return ctx.defer(today)


def _alerts(ctx):
    _operator(ctx)
    n = max(1, min(int(ctx.opt("n") or 5), 20))
    return ctx.defer(_safe(lambda: alerts_message(pulse_alerts(), n)), private=True)


def _signups(ctx):
    _operator(ctx)
    hours = max(1, min(int(ctx.opt("hours") or 24), 24 * 30))
    return ctx.defer(_safe(lambda: signups_message(signups(hours), hours)), private=True)


def _issues(ctx):
    _operator(ctx)
    return ctx.defer(_safe(lambda: issues_message(issues_waiting())), private=True)


def _reports(ctx):
    _operator(ctx)
    # Reported content, its author and the reason are moderation data: never public.
    return ctx.defer(_safe(lambda: reports_message(reports())), private=True)


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _button(kind: str, key: str) -> str:
    """``<kind>:<alert_key>``, or ``<kind>#<digest>`` when the key overflows a custom_id
    (Pulse keys run past 90 chars); the press then finds the key among active alerts."""
    try:
        return PLUGIN.button_id(f"{kind}:{key}")
    except PluginError:
        return PLUGIN.button_id(f"{kind}#{_digest(key)}")


def _key_for(digest: str) -> str:
    for row in pulse_alerts():
        if _digest(row["key"]) == digest:
            return row["key"]
    raise SourceError("that alert is no longer active")


def on_button(ctx, payload: str):
    _operator(ctx)                      # a forwarded or copied button re-checks here
    kind, sep, ref = payload[:1], payload[1:2], payload[2:]
    action = {"a": "acknowledge", "r": "resolve"}.get(kind)
    if action is None or sep not in (":", "#") or not ref:
        raise PluginError("That button is not one of the hub's.")

    def work() -> str:
        key = ref
        try:
            if sep == "#":
                key = _key_for(ref)
            pulse_act(action, key)
        except SourceError as exc:
            return f"Pulse refused to {action} `{key}`: {exc}"
        return f"{'Acknowledged' if kind == 'a' else 'Resolved'} `{key}` (by <@{ctx.user}>)."
    return ctx.defer(work, private=True)


PLUGIN = Plugin(
    name="hub",
    version="0.1.0",
    description="Operators run the platform from Discord: alerts, signups, issues, reports",
    default_enabled=True,
    commands=[
        Command("today", "Alerts, signups, outside issues, reports and tickets in one message",
                _today, admin=True, dm=True),
        Command("alerts", "Top active Pulse alerts with Ack / Resolve buttons", _alerts,
                admin=True, dm=True, options=[Option("n", "How many (default 5)",
                                                     type="integer")]),
        Command("signups", "New accounts and blog subscribers", _signups, admin=True, dm=True,
                options=[Option("hours", "Look back this many hours (default 24)",
                                type="integer")]),
        Command("issues", "Outside GitHub issues/PRs waiting on our reply", _issues,
                admin=True, dm=True),
        Command("reports", "Open Relay moderation reports", _reports, admin=True, dm=True),
    ],
    on_button=on_button,
)
