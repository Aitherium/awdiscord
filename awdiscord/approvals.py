"""Approval cards: an agent asks, an operator presses a button, the answer goes back.

The card is an ordinary Discord message with Approve / Deny buttons. Three
things make the button safe to trust:

* its custom_id carries an HMAC of (approval id, verb) under a secret only the
  runtime holds, so a crafted interaction cannot approve something that was
  never raised, or flip a Deny into an Approve;
* only user ids on the operator list may press it - anyone else gets a polite
  refusal and nothing is recorded;
* a decision is taken exactly once, even across processes: the first press
  creates a marker file with O_EXCL and every later press reads the result.

The store is a directory of JSON files. No database, no server.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

PREFIX = "awd"
VERBS = ("approve", "deny")


class ApprovalError(Exception):
    """A press that must not count: forged, unknown, or not an operator."""


@dataclass
class Decision:
    approval_id: str
    verdict: str            # "approved" | "denied"
    decided_by: str
    decided_at: float
    already: bool = False   # True when this press found an earlier decision
    callback: str = ""      # "", "delivered", or "failed: <reason>"


class ApprovalDesk:
    def __init__(self, store_dir: str | os.PathLike, secret: bytes | str,
                 operators: set[str] | list[str], timeout: float = 10.0):
        if not secret:
            raise ValueError("an approval secret is required; without it a button can be forged")
        self._secret = secret.encode() if isinstance(secret, str) else bytes(secret)
        self.operators = {str(o) for o in operators}
        if not self.operators:
            raise ValueError("at least one operator user id is required")
        self.root = Path(store_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self._timeout = timeout
        self._lock = threading.Lock()

    # -- raising -----------------------------------------------------------
    def raise_card(self, title: str, body: str, requested_by: str = "agent",
                   callback_url: str | None = None, approval_id: str | None = None,
                   meta: dict | None = None) -> dict:
        aid = approval_id or secrets.token_hex(6)
        if not aid.replace("-", "").isalnum() or len(aid) > 40:
            raise ValueError("approval_id must be alphanumeric (dashes allowed), <= 40 chars")
        record = {
            "id": aid, "title": title[:250], "body": body[:3500],
            "requested_by": requested_by, "callback_url": callback_url or "",
            "meta": meta or {}, "status": "pending", "raised_at": time.time(),
        }
        path = self._path(aid)
        if path.exists():
            raise ValueError(f"approval {aid} already exists")
        self._write(path, record)
        return record

    def get(self, approval_id: str) -> dict | None:
        path = self._path(approval_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def pending(self) -> list[dict]:
        out = []
        for p in sorted(self.root.glob("*.json")):
            rec = json.loads(p.read_text(encoding="utf-8"))
            if rec.get("status") == "pending":
                out.append(rec)
        return out

    # -- the card ------------------------------------------------------------
    def custom_id(self, approval_id: str, verb: str) -> str:
        if verb not in VERBS:
            raise ValueError(verb)
        return f"{PREFIX}:{verb}:{approval_id}:{self._sign(approval_id, verb)}"

    def card(self, record: dict) -> dict:
        """The message payload for a pending approval."""
        aid = record["id"]
        return {
            "embeds": [{
                "title": "Approval needed: " + record["title"],
                "description": record["body"],
                "footer": {"text": f"{aid} - requested by {record['requested_by']}"},
                "color": 0x5EC9CC,
            }],
            "components": [{"type": 1, "components": [
                {"type": 2, "style": 3, "label": "Approve",
                 "custom_id": self.custom_id(aid, "approve")},
                {"type": 2, "style": 4, "label": "Deny",
                 "custom_id": self.custom_id(aid, "deny")},
            ]}],
            "allowed_mentions": {"parse": []},
        }

    def decided_card(self, record: dict) -> dict:
        """The same card after the decision: verdict shown, buttons disabled."""
        payload = self.card(record)
        verdict = record.get("status", "pending")
        payload["embeds"][0]["fields"] = [{
            "name": verdict.upper(),
            "value": f"by <@{record.get('decided_by', '?')}>", "inline": False}]
        payload["embeds"][0]["color"] = 0x3BA55D if verdict == "approved" else 0xED4245
        for row in payload["components"]:
            for button in row["components"]:
                button["disabled"] = True
        return payload

    # -- the press -----------------------------------------------------------
    @staticmethod
    def is_ours(custom_id: str) -> bool:
        return isinstance(custom_id, str) and custom_id.startswith(PREFIX + ":")

    def resolve(self, custom_id: str, user_id: str) -> Decision:
        parts = custom_id.split(":")
        if len(parts) != 4 or parts[0] != PREFIX or parts[1] not in VERBS:
            raise ApprovalError("not an approval button")
        _, verb, aid, sig = parts
        if not hmac.compare_digest(sig, self._sign(aid, verb)):
            raise ApprovalError("button signature does not verify")
        if str(user_id) not in self.operators:
            raise ApprovalError("only an operator can decide this")
        with self._lock:
            record = self.get(aid)
            if record is None:
                raise ApprovalError("unknown approval")
            marker = self.root / f"{aid}.decided"
            try:
                fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                record = self._wait_decided(aid)
                return Decision(aid, record["status"], record.get("decided_by", ""),
                                record.get("decided_at", 0.0), already=True,
                                callback=record.get("callback", ""))
            os.close(fd)
            record["status"] = "approved" if verb == "approve" else "denied"
            record["decided_by"] = str(user_id)
            record["decided_at"] = time.time()
            self._write(self._path(aid), record)
        decision = Decision(aid, record["status"], record["decided_by"], record["decided_at"])
        if record.get("callback_url"):
            decision.callback = self._deliver(record, decision)
            record["callback"] = decision.callback
            self._write(self._path(aid), record)
        return decision

    # -- internals -------------------------------------------------------------
    def _sign(self, approval_id: str, verb: str) -> str:
        mac = hmac.new(self._secret, f"{approval_id}:{verb}".encode(), hashlib.sha256)
        return mac.hexdigest()[:24]

    def _path(self, approval_id: str) -> Path:
        safe = "".join(c for c in approval_id if c.isalnum() or c == "-")
        return self.root / f"{safe}.json"

    def _wait_decided(self, aid: str, tries: int = 50) -> dict:
        for _ in range(tries):
            record = self.get(aid) or {}
            if record.get("status") in ("approved", "denied"):
                return record
            time.sleep(0.02)
        raise ApprovalError("decision is being recorded; press again in a moment")

    @staticmethod
    def _write(path: Path, record: dict) -> None:
        tmp = path.with_suffix(f".tmp{os.getpid()}{threading.get_ident()}")
        tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)

    def _deliver(self, record: dict, decision: Decision) -> str:
        body = json.dumps({k: v for k, v in asdict(decision).items()
                           if k not in ("already", "callback")} | {"meta": record.get("meta", {})})
        sig = hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()
        req = urllib.request.Request(
            record["callback_url"], data=body.encode(), method="POST",
            headers={"Content-Type": "application/json", "X-Awdiscord-Signature": sig})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return "delivered" if 200 <= resp.status < 300 else f"failed: HTTP {resp.status}"
        except Exception as exc:  # recorded on the approval, never raised into the press
            return f"failed: {type(exc).__name__}: {exc}"[:200]
