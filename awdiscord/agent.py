"""The agent behind the bot: anything that turns a prompt into an answer.

`OpenAICompatAgent` speaks POST {base}/chat/completions, which is what an adk
agent (`adk serve`), llama.cpp, vLLM, Ollama and most gateways expose, so "any
agent" means any of those with no adapter. `A2AAgent` speaks A2A v0.3 (JSON-RPC
``message/send``) to any peer that publishes an agent card. `CallableAgent` wraps
a plain function for everything else.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit


class AgentError(Exception):
    """The agent could not answer. The message is safe to show a user."""


class AgentTimeoutError(AgentError):
    """The agent did not answer inside its timeout.

    A separate type so the bot can answer "this is taking a while, ask again"
    instead of surfacing the transport's ``TimeoutError`` to a member.
    """


def _http_error(who: str, code: int) -> AgentError:
    """An AgentError carrying the HTTP status, so a caller can act on 401/403/429.

    The message is unchanged; ``status`` is an extra attribute rather than part
    of the text because a brain resolver needs to tell a revoked key from a
    spent quota without parsing prose.
    """
    exc = AgentError(f"{who} answered HTTP {code}")
    exc.status = code
    return exc


class StreamFallbackError(Exception):
    """A streamed turn cannot be delivered as a stream; run ``ask`` instead.

    Raised by ``ask_stream`` only BEFORE it has yielded any text: the model asked for a
    tool, the turn carries images, or the stream failed or came back empty.
    """


def _sse_events(resp):
    """The JSON ``data:`` events of an OpenAI-style SSE body, up to ``[DONE]``."""
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else str(raw)
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            event = json.loads(data)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


#: A 502/503/504 or a refused connection is usually a backend between instances --
#: one half of an HA pair draining during a restart, a lane reloading. One such blip
#: used to drop a member's question for good ("The agent could not answer: the agent
#: answered HTTP 502", 2026-11-02, during a rolling Genesis restart). Retry those, a
#: few times with backoff; a timeout is NOT retried (the member already waited).
RETRY_STATUS = (502, 503, 504)
RETRY_DELAYS = (3.0, 10.0, 30.0)


def urlopen_json(req, *, timeout: float, context, who: str,
                 sleep: Callable[[float], None] = time.sleep,
                 delays: tuple = RETRY_DELAYS) -> dict:
    """Open ``req`` and decode JSON, retrying transient failures. Raises AgentError.

    ``who`` names the backend in the user-safe message. A ValueError from a non-JSON
    body propagates so callers keep their own "returned no answer" wording.
    """
    attempts = (0.0, *delays)
    for n, delay in enumerate(attempts):
        if delay:
            sleep(delay)
        last = n == len(attempts) - 1
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=context) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in RETRY_STATUS and not last:
                continue
            raise _http_error(who, exc.code) from exc
        except TimeoutError as exc:
            raise AgentTimeoutError(f"{who} is taking longer than expected") from exc
        except (urllib.error.URLError, OSError) as exc:
            if isinstance(getattr(exc, "reason", None), TimeoutError):
                raise AgentTimeoutError(f"{who} is taking longer than expected") from exc
            if not last:
                continue
            raise AgentError(f"{who} is unreachable right now") from exc
    raise AgentError(f"{who} is unreachable right now")


class EchoAgent:
    """Answers with the prompt. For tests and `awdiscord run --echo`."""
    name = "echo"

    def ask(self, prompt: str, *, persona: str | None = None, user: str = "",
            context: str = "", tools: list | None = None) -> str:
        return f"echo: {prompt}"

    def health(self) -> str:
        return "ok"


class CallableAgent:
    def __init__(self, fn: Callable[[str], str], name: str = "callable"):
        self._fn = fn
        self.name = name

    def ask(self, prompt: str, *, persona: str | None = None, user: str = "",
            context: str = "", tools: list | None = None) -> str:
        return str(self._fn(prompt))

    def health(self) -> str:
        return "ok"


MAX_TOOL_ROUNDS = 4

# Tool-call markup a model emits as TEXT when its server did not parse it into
# ``tool_calls`` -- DeepSeek DSML (any number of full-width or ASCII bars),
# Claude-style ``<invoke>``/``<function_calls>``, Qwen ``<tool_call>``. Measured
# 2026-10-31: a cloud fallback answered a Discord mention with
# ``<||DSML|| calls><||DSML|| invoke name="web_search">...`` and the bot posted
# it verbatim. A user must never see this.
_BAR = "[\uff5c|]+"  # FULLWIDTH VERTICAL LINE or ASCII bar
_DSML_OPEN = "<" + _BAR + r"\s*DSML\s*" + _BAR + r"\s*"
_DSML_CLOSE = "</" + _BAR + r"\s*DSML\s*" + _BAR + r"\s*"
_DSML_INVOKE = re.compile(
    _DSML_OPEN + r'invoke\s+name="([^"]+)"\s*>(.*?)' + _DSML_CLOSE + "invoke>", re.S)
_XML_INVOKE = re.compile(r'<invoke\s+name="([^"]+)"\s*>(.*?)</invoke>', re.S)
_DSML_PARAM = re.compile(
    _DSML_OPEN + r'parameter\s+name="([^"]+)"([^>]*)>(.*?)' + _DSML_CLOSE + "parameter>",
    re.S)
_XML_PARAM = re.compile(r'<parameter\s+name="([^"]+)"([^>]*)>(.*?)</parameter>', re.S)
_TOOL_CALL_JSON = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
_MARKUP_STRIP = (
    re.compile(_DSML_OPEN + r"calls>.*?" + _DSML_CLOSE + "calls>", re.S),
    re.compile(_DSML_OPEN + r"(?:calls|invoke|tool_calls|parameter)\b.*$", re.S),
    re.compile(r"<function_calls>.*?</function_calls>", re.S),
    re.compile(r"<(?:function_calls|invoke\s)[^>]*>.*$", re.S),
    re.compile(r"<tool_call>.*?</tool_call>", re.S),
    re.compile(r"<tool_call>.*$", re.S),
)


def has_tool_markup(text: str) -> bool:
    """True when ``text`` carries tool-call markup in a dialect this brick knows."""
    t = str(text or "")
    return (("DSML" in t and "<" in t) or "<invoke " in t or "<function_calls>" in t
            or "<tool_call>" in t)


def strip_tool_markup(text: str) -> str:
    """``text`` with every tool-call block (closed or cut off) removed."""
    t = str(text or "")
    for pat in _MARKUP_STRIP:
        t = pat.sub("", t)
    return t.strip()


def parse_tool_markup(text: str, known: set[str] | None = None) -> list[dict]:
    """Tool calls written as markup, in OpenAI ``tool_calls`` shape.

    Only names in ``known`` are returned when it is given: a call to a tool this
    turn never offered is not a call the bot can run.
    """
    t = str(text or "")
    calls: list[dict] = []
    found = ([(m, _DSML_PARAM) for m in _DSML_INVOKE.finditer(t)]
             + [(m, _XML_PARAM) for m in _XML_INVOKE.finditer(t)])
    for m, param_re in found:
        args: dict = {}
        for pm in param_re.finditer(m.group(2)):
            raw = pm.group(3).strip()
            val: object = raw
            if 'string="false"' in pm.group(2):
                try:
                    val = json.loads(raw)
                except ValueError:
                    val = raw
            args[pm.group(1)] = val
        calls.append({"name": m.group(1), "args": args})
    for m in _TOOL_CALL_JSON.finditer(t):
        try:
            obj = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("name"):
            args = obj.get("arguments") or obj.get("parameters") or {}
            calls.append({"name": str(obj["name"]),
                          "args": args if isinstance(args, dict) else {}})
    out = []
    for i, c in enumerate(calls):
        if known is not None and c["name"] not in known:
            continue
        out.append({"id": f"markup_{i}", "type": "function",
                    "function": {"name": c["name"], "arguments": json.dumps(c["args"])}})
    return out


def clean_answer(text: object, who: str = "the agent") -> str:
    """The user-visible answer, or AgentError: never markup, never empty."""
    t = str(text or "").strip()
    if has_tool_markup(t):
        t = strip_tool_markup(t)
        if not t:
            raise AgentError(f"{who} tried to call a tool instead of answering")
    if not t:
        raise AgentError(f"{who} returned an empty answer")
    return t


# Rides every public turn after the persona. A bare chat model in a group chat
# fills gaps with plausible specifics and offers work it cannot do; measured
# 2026-10-22: "feed this to the fork" became invented release notes and an
# offer to a stranger to "push it" to staging.
GROUND_RULES = (
    "Ground rules for this chat:\n"
    "- Never invent specifics (versions, release notes, branches, PRs, plans, "
    "steps you took). If it is not in this conversation or a tool result, say "
    "you don't know.\n"
    "- SEARCH BEFORE YOU ASK. When a message could be answered from the company's "
    "own knowledge, look first: call the company brain (company_brain_search) and "
    "any product/docs tool offered, and use web_search when the brain has nothing. "
    "Ask a clarifying question only when those come back empty or contradict each "
    "other, and say what you searched for. (2026-11-02: a customer asked for "
    "their own bot by name on a new PC and was asked what that meant, though the docs "
    "name the hosted app.)\n"
    "- When a message is ambiguous, ask one short question instead of guessing "
    "a task. Use the names people use, and keep a correction once given.\n"
    "- You cannot change, build, deploy or push anything from this chat, and you "
    "never offer to. Changes to the operator's systems are the operator's call.\n"
    "- Never vouch for an offer, a price, a discount or a person: without a "
    "verified source (a tool result or the company's own docs) say plainly that you "
    "cannot verify it and advise caution. (2026-11-07: an unverified \"50M tokens "
    "for $3.50\" offer was called an official promotion.)\n"
    "- When you produce or refer to a file, NEVER print a filesystem path (C:\\..., "
    "/home/..., /mnt/c/...). Publish it with the share_file tool and give the link "
    "it returns; the link and its sha256 are the file's name here."
)


def served_route(requested: str, data: dict) -> dict:
    """{requested, served_by, cross_model} from an OpenAI-compatible answer.

    AitherOS MicroScheduler states it in ``aither_route``; any other server is judged by
    the response ``model`` field. Unknown stays empty -- never the requested name.
    """
    route = data.get("aither_route") if isinstance(data, dict) else None
    route = route if isinstance(route, dict) else {}
    served = str(route.get("served_by") or (data or {}).get("model") or "")
    requested = str(route.get("requested") or requested or "")
    cross = route.get("cross_model")
    if cross is None:
        cross = bool(served and requested and served != requested)
    return {"requested": requested, "served_by": served, "cross_model": bool(cross)}


class OpenAICompatAgent:
    sees_images = True          # images go to ``vision_models``
    def __init__(self, base_url: str, model: str = "default", api_key: str | None = None,
                 personas: dict[str, str] | None = None, default_persona: str | None = None,
                 timeout: float = 120.0, max_tokens: int = 900, ca_bundle: str | None = None,
                 vision_models: list[str] | None = None, priority: str | None = None,
                 tool_model: str | None = None, transport=None,
                 extra_headers: dict[str, str] | None = None):
        self.base_url = base_url.rstrip("/")
        # A turn that offers tools goes to `tool_model` when set: a chat model
        # served through a fallback that drops `tools` never calls one (measured
        # 2026-10-31: 3 of 3 turns came back with tool_calls=null).
        self.tool_model = (tool_model or "").strip() or None
        # A person typed this message. A scheduler that queues by priority
        # (request ``metadata.priority``) should answer it ahead of background
        # agent work; unset = the field is never sent, for backends that reject it.
        self.priority = (priority or "").strip().lower() or None
        self.model = model
        # A turn that carries images goes to these, in order (the next one when
        # one fails); a text-only model answers 5xx or never sees the picture.
        # ``None`` means "not configured": the turn model itself is the vision
        # model. An explicit empty list means this endpoint has NO vision, which
        # is what a BYO profile can declare (sees_images becomes False).
        if vision_models is None:
            self.vision_models = [model]
            self.sees_images = True
        else:
            self.vision_models = [m for m in vision_models if m]
            self.sees_images = bool(self.vision_models)
        self.name = model
        # What ACTUALLY answered the last turn, from the scheduler's own `aither_route`
        # (or the response `model`). /status shows this beside the configured name: the
        # configured name alone reported a model that was not running (2026-11-01).
        self.last_route: dict = {}
        self._key = api_key
        self.personas = personas or {}
        self.default_persona = default_persona
        self._timeout = timeout
        self._max_tokens = max_tokens
        self._ctx = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else None
        # An optional transport for a BYO endpoint: it pins the validated IP on
        # every request and refuses redirects (awdiscord.brain). None = urllib.
        self._transport = transport
        self.extra_headers = dict(extra_headers or {})

    @classmethod
    def from_env(cls, prefix: str = "AWDISCORD_") -> "OpenAICompatAgent":
        url = os.getenv(prefix + "AGENT_URL")
        if not url:
            raise AgentError(f"set {prefix}AGENT_URL to an OpenAI-compatible /v1 base URL")
        personas = load_personas(os.getenv(prefix + "PERSONAS_DIR") or "")
        vision_raw = (os.getenv(prefix + "VISION_MODELS") or "").replace(" ", "")
        return cls(url, model=os.getenv(prefix + "AGENT_MODEL", "default"),
                   api_key=os.getenv(prefix + "AGENT_KEY") or None,
                   personas=personas,
                   default_persona=os.getenv(prefix + "DEFAULT_PERSONA") or None,
                   ca_bundle=os.getenv(prefix + "CA_BUNDLE") or None,
                   vision_models=([m for m in vision_raw.split(",") if m]
                                  if vision_raw else None),
                   priority=os.getenv(prefix + "AGENT_PRIORITY") or None,
                   tool_model=os.getenv(prefix + "TOOL_MODEL") or None)

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", **self.extra_headers}
        if self._key:
            h["Authorization"] = "Bearer " + self._key
        return h

    def ask(self, prompt: str, *, persona: str | None = None, user: str = "",
            context: str = "", tools: list | None = None,
            history: list | None = None, images: list[str] | None = None) -> str:
        """Answer, calling ``tools`` (awdiscord.plugins.Tool) when the model asks.

        Up to MAX_TOOL_ROUNDS rounds; a tool error goes back to the model as the
        tool's result so it can say what it could not find instead of failing.
        ``history`` = earlier {"role", "content"} turns of this conversation.
        ``images`` = data: URLs; the turn then goes to ``vision_models``.
        A turn with ``tools`` (and no images) goes to ``tool_model`` when set.
        """
        if images:
            last: AgentError | None = None
            for model in self.vision_models:
                try:
                    return self._ask(prompt, persona, user, context, tools, history,
                                     images, model)
                except AgentError as exc:
                    last = exc
            raise last or AgentError("no vision model could answer")
        model = self.tool_model if (tools and self.tool_model) else self.model
        return self._ask(prompt, persona, user, context, tools, history, None, model)

    def ask_stream(self, prompt: str, *, persona: str | None = None, user: str = "",
                   context: str = "", tools: list | None = None,
                   history: list | None = None, images: list[str] | None = None,
                   cancel=None):
        """``ask`` for a turn that needs no tool, as text deltas from an SSE stream.

        Same system prompt, history, model choice, tools offer, ``user`` and priority
        as ``ask``, plus ``stream: true``. Raises StreamFallbackError before yielding anything
        when the model asks for a tool (a ``tool_calls`` delta or tool markup), the turn
        has images, or the stream fails or is empty: the caller then runs ``ask`` with
        the same arguments, the full tool loop. Once text was yielded a failure just
        ends the stream (what was spoken stays spoken). Text from the first ``<`` on is
        held to the end, so tool-call markup is never handed out. ``cancel`` (an Event)
        stops reading.
        """
        if images:
            raise StreamFallbackError("a turn with images is not streamed")
        model = self.tool_model if (tools and self.tool_model) else self.model
        payload: dict = {"model": model,
                         "messages": self._messages(prompt, persona, context, history,
                                                    None),
                         "max_tokens": self._max_tokens, "stream": True}
        if tools:
            payload["tools"] = [t.schema() for t in tools]
        if user:
            payload["user"] = user
        if self.priority:
            payload["metadata"] = {"priority": self.priority, "source": "chat:awdiscord"}
        req = urllib.request.Request(self.base_url + "/chat/completions",
                                     data=json.dumps(payload).encode(),
                                     headers=self._headers(), method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=self._timeout, context=self._ctx)
        except Exception as exc:  # noqa: BLE001 - any open failure means "use ask()"
            raise StreamFallbackError(f"stream failed: {type(exc).__name__}") from exc
        yielded = False
        held: list[str] = []
        text: list[str] = []
        try:
            for event in _sse_events(resp):
                if cancel is not None and cancel.is_set():
                    return
                if event.get("model") or event.get("aither_route"):
                    self.last_route = served_route(model, event)
                choices = event.get("choices") or []
                if not choices or not isinstance(choices[0], dict):
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                if delta.get("tool_calls") or choice.get("finish_reason") == "tool_calls":
                    if yielded:
                        return
                    raise StreamFallbackError("the model asked for a tool")
                piece = str(delta.get("content") or "")
                if not piece:
                    continue
                text.append(piece)
                if held or "<" in piece:
                    held.append(piece)
                    continue
                yielded = True
                yield piece
        except StreamFallbackError:
            raise
        except Exception as exc:  # noqa: BLE001 - a broken stream mid-answer
            if not yielded:
                raise StreamFallbackError(f"stream failed: {type(exc).__name__}") from exc
            return
        finally:
            with contextlib.suppress(OSError):
                resp.close()
        if held:
            rest = "".join(held)
            if has_tool_markup("".join(text)):
                if not yielded:
                    raise StreamFallbackError("the model wrote a tool call")
                rest = strip_tool_markup(rest)
            if rest.strip():
                yielded = True
                yield rest
        if not yielded:
            raise StreamFallbackError("the stream carried no answer")

    def _messages(self, prompt, persona, context, history, images) -> list:
        import datetime as _dt
        messages = []
        name = (persona or self.default_persona or "").lower()
        system = self.personas.get(name) or ""
        # `context` = what this server's plugins tell the agent (persona
        # fragments, skill names); it rides after the persona, never instead.
        today = _dt.datetime.now(_dt.timezone.utc).strftime("%A %d %B %Y")
        team = ""
        others = sorted(p.capitalize() for p in self.personas if p != name)
        if others:
            # Without this a question about "Hera" gets web results for a
            # company called HERA instead of an answer about a colleague.
            team = (f"{', '.join(others)} are your fellow agents on this team. A "
                    "question about one of them means that agent, not an outside "
                    "person or company of the same name.")
        system = "\n\n".join(x for x in (system, team, context, GROUND_RULES,
                                         f"Today is {today} (UTC).") if x)
        messages.append({"role": "system", "content": system})
        for turn in history or []:
            if turn.get("role") in ("user", "assistant") and turn.get("content"):
                messages.append({"role": turn["role"], "content": str(turn["content"])})
        if images:
            messages.append({"role": "user", "content": [{"type": "text", "text": prompt}] + [
                {"type": "image_url", "image_url": {"url": u}} for u in images]})
        else:
            messages.append({"role": "user", "content": prompt})
        return messages

    def _ask(self, prompt, persona, user, context, tools, history, images, model) -> str:
        messages = self._messages(prompt, persona, context, history, images)
        by_name = {t.name: t for t in tools or []}
        for _round in range(MAX_TOOL_ROUNDS + 1):
            payload = {"model": model, "messages": messages,
                       "max_tokens": self._max_tokens}
            if by_name and _round < MAX_TOOL_ROUNDS:
                payload["tools"] = [t.schema() for t in by_name.values()]
            if user:
                payload["user"] = user
            if self.priority:
                # "chat:" = a person is waiting; schedulers that keep a slot for
                # interactive callers match on that prefix, not on this brick's name.
                payload["metadata"] = {"priority": self.priority, "source": "chat:awdiscord"}
            msg = self._complete(payload)
            calls = msg.get("tool_calls") or []
            offered = "tools" in payload
            if not calls and offered and has_tool_markup(msg.get("content") or ""):
                # The server did not parse the model's call: run it ourselves.
                calls = parse_tool_markup(msg.get("content") or "", set(by_name))
                if calls:
                    msg = {"content": strip_tool_markup(msg.get("content") or ""),
                           "tool_calls": calls}
            if not calls:
                return clean_answer(msg.get("content"))
            if not offered:
                # The last round offers no tools, yet some models (and servers) still
                # emit a call. A person is waiting: answer from what the tools already
                # found rather than surfacing "kept calling tools" (Discord, 2026-11-01).
                return self._final_answer(payload, messages, msg)
            messages.append({"role": "assistant", "content": msg.get("content") or "",
                             "tool_calls": calls})
            for call in calls:
                fn = call.get("function") or {}
                tool = by_name.get(fn.get("name", ""))
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    result = tool.run(args) if tool else f"no tool named {fn.get('name')!r}"
                except Exception as exc:  # the model reads the failure, the bot survives
                    result = f"tool error: {type(exc).__name__}: {exc}"
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                 "content": str(result)[:8000]})
        raise AgentError("the agent kept calling tools without answering")

    def _final_answer(self, payload: dict, messages: list, msg: dict) -> str:
        """A plain-words answer when the tool budget is spent but the model asked again.

        Uses whatever text came with the stray call; otherwise asks once more, with no
        tools offered and an explicit instruction, for an answer from what is already in
        the conversation. Only if that is empty too does the caller get the error.
        """
        text = strip_tool_markup(msg.get("content") or "").strip()
        if text:
            return clean_answer(text)
        retry = {k: v for k, v in payload.items() if k not in ("tools", "tool_choice")}
        retry["messages"] = messages + [{
            "role": "user",
            "content": ("Tools are no longer available. Answer the question now, in plain "
                        "words, from what you have already found. If something is still "
                        "unknown, say what and stop."),
        }]
        again = self._complete(retry)
        text = strip_tool_markup(again.get("content") or "").strip()
        if text:
            return clean_answer(text)
        raise AgentError("the agent kept calling tools without answering")

    def _complete(self, payload: dict) -> dict:
        if self._transport is not None:
            try:
                status, body = self._transport.post(
                    "/chat/completions", self._headers(),
                    json.dumps(payload).encode(), self._timeout)
            except urllib.error.HTTPError as exc:
                raise _http_error("the agent", exc.code) from exc
            except TimeoutError as exc:
                raise AgentTimeoutError("the agent is taking longer than expected") from exc
            except Exception as exc:      # noqa: BLE001 - the answer is safe to show
                raise AgentError(
                    f"the agent is unreachable right now ({type(exc).__name__})") from exc
            if status >= 400:
                raise _http_error("the agent", status)
            try:
                data = json.loads(body.decode("utf-8"))
            except ValueError as exc:
                raise AgentError("the agent returned no answer") from exc
        else:
            req = urllib.request.Request(self.base_url + "/chat/completions",
                                         data=json.dumps(payload).encode(),
                                         headers=self._headers(), method="POST")
            data = urlopen_json(req, timeout=self._timeout, context=self._ctx,
                                who="the agent")
        self.last_route = served_route(payload.get("model", ""), data)
        try:
            return data["choices"][0]["message"] or {}
        except (KeyError, IndexError, TypeError) as exc:
            raise AgentError("the agent returned no answer") from exc

    def served_line(self) -> str:
        """'configured X; last answered by Y' -- substitution spelled out, never hidden."""
        r = self.last_route
        if not r.get("served_by"):
            return f"{self.name} (no turn answered yet)"
        if r.get("cross_model"):
            return (f"{self.name}; last turn asked for {r['requested']} and was ANSWERED "
                    f"BY {r['served_by']} (substituted)")
        return f"{self.name}; last turn answered by {r['served_by']}"

    def health(self) -> str:
        if self._transport is not None:
            try:
                status, _ = self._transport.get("/models", self._headers(), 10.0)
                return "ok" if status == 200 else f"HTTP {status}"
            except Exception as exc:      # noqa: BLE001 - reported, never raised
                return f"unreachable ({type(exc).__name__})"
        req = urllib.request.Request(self.base_url + "/models", headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=10, context=self._ctx) as r:
                return "ok" if r.status == 200 else f"HTTP {r.status}"
        except urllib.error.HTTPError as exc:
            return f"HTTP {exc.code}"
        except Exception as exc:
            return f"unreachable ({type(exc).__name__})"


class GenesisAgent:
    """The operator's agent: AitherOS Genesis ``POST {base}/chat``, the same agent
    loop (fleet tools, memory, delegation) the relay uses for a tagged Aither.

    `OpenAICompatAgent` against a bare model is a chat model with web plugins; it
    cannot see the fleet, so an operator asking "what is down?" got "use Disk
    Cleanup". This one is for operators only (see `Dispatcher.owner_agent`):
    a public guild member must never reach fleet tools through the bot.

    One Genesis session per Discord server+channel+user, so a conversation
    carries over where it started and never crosses into another server.
    """
    # Genesis /chat did not look at an attached image (2026-10-17), so the
    # dispatcher has a vision model describe it into the prompt instead.
    sees_images = False
    # Genesis brings its own tools and never runs the bot's plugin tools, so the
    # dispatcher runs the plugins' grounding tools (``Tool.grounds``) with the
    # question first and hands the result in ``context``.
    runs_bot_tools = False

    def __init__(self, base_url: str, persona: str = "aither", effort: int = 6,
                 timeout: float = 600.0, ca_bundle: str | None = None,
                 caller_type: str = "platform"):
        self.base_url = base_url.rstrip("/")
        self.name = f"genesis:{persona}"
        self.persona = persona
        self.effort = effort
        self._timeout = timeout
        self._caller_type = caller_type
        self._ctx = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else None

    @classmethod
    def from_env(cls, prefix: str = "AWDISCORD_") -> "GenesisAgent | None":
        url = os.getenv(prefix + "OWNER_AGENT_URL")
        if not url:
            return None
        return cls(url, persona=os.getenv(prefix + "OWNER_AGENT_PERSONA") or "aither",
                   effort=int(os.getenv(prefix + "OWNER_AGENT_EFFORT") or 6),
                   ca_bundle=os.getenv(prefix + "CA_BUNDLE") or None)

    def ask(self, prompt: str, *, persona: str | None = None, user: str = "",
            context: str = "", tools: list | None = None,
            history: list | None = None, images: list[str] | None = None,
            guild_id: str = "", channel_id: str = "",
            surface: str = "") -> str:
        # `tools` are the bot's own plugins; Genesis brings its own and never runs
        # these. `context` (the server's plugin persona plus any grounding the
        # dispatcher retrieved) rides IN the message, ahead of the question:
        # without it a question about the server's own features is answered blind.
        # `images` (data: URLs) ride as `attachments`: Genesis routes a turn
        # carrying one to its vision role.
        #
        # `guild_id`/`channel_id` are the Discord origin of the turn. They
        # namespace the Genesis session, so the owner's HOME conversation is
        # never reloaded in a partner server, and they select the `surface`
        # Genesis uses to keep owner-private memory out of a partner turn.
        context = str(context or "").strip()
        message = (f"[Server context from this Discord bot's plugins]\n{context}\n\n"
                   f"[The member's message]\n{prompt}") if context else prompt
        recent = [{"role": t["role"], "content": str(t["content"])}
                  for t in history or []
                  if t.get("role") in ("user", "assistant") and t.get("content")]
        if not surface:
            from awdiscord.served import surface_for
            surface = surface_for(guild_id)
        session_id = (f"discord-{guild_id or 'dm'}-{channel_id or 'dm'}-"
                      f"{user or 'anon'}")
        payload = {
            "message": message,
            "session_id": session_id,
            "persona": persona or self.persona,
            "stream": False,
            "agentic": True,
            "max_effort": self.effort,
            "context": {"source": "discord", "platform": "discord", "user_id": user,
                        "guild_id": guild_id, "channel_id": channel_id,
                        "surface": surface,
                        "tagged": True, "recent_history": recent},
        }
        if context:
            payload["context"]["server_context"] = context
        if images:
            payload["attachments"] = list(images)
        headers = {"Content-Type": "application/json", "X-Caller-Type": self._caller_type}
        # Without the fleet internal key Genesis meters the bot as ONE anonymous
        # free-tier user: ~20 turns per 10 minutes shared by everyone, effort
        # clamped -- the owner got "HTTP 429" on 2026-10-18. The key is the one
        # bootstrap credential every fleet service holds (the vault itself is
        # read with it); read fresh so a rotation is picked up.
        key = (os.getenv("AITHER_INTERNAL_SECRET") or "").strip()
        if key:
            headers["X-Internal-Key"] = key
        req = urllib.request.Request(
            self.base_url + "/chat", data=json.dumps(payload).encode(), method="POST",
            headers=headers)
        try:
            data = urlopen_json(req, timeout=self._timeout, context=self._ctx,
                                who="the fleet agent")
        except ValueError as exc:
            raise AgentError("the fleet agent returned no answer") from exc
        text = ""
        if isinstance(data, dict):
            text = data.get("response") or data.get("content") or ""
        return clean_answer(text, "the fleet agent")

    def health(self) -> str:
        try:
            with urllib.request.urlopen(self.base_url + "/health", timeout=10,
                                        context=self._ctx) as r:
                return "ok" if r.status == 200 else f"HTTP {r.status}"
        except urllib.error.HTTPError as exc:
            return f"HTTP {exc.code}"
        except Exception as exc:
            return f"unreachable ({type(exc).__name__})"


#: Roles that carry the AGENT's words in an A2A v0.3 Task history. v0.3 says
#: "agent"; earlier drafts and OpenAI-shaped clients say "assistant".
_AGENT_ROLES = ("agent", "assistant")


def _a2a_parts_text(parts) -> str:
    """The text of an A2A ``parts`` list. A bare string part counts as text.

    The kind is ``kind`` in v0.3 and ``type`` in earlier drafts; a part of any
    other kind (a file, structured data) is not words and is skipped.
    """
    out = []
    for part in parts or []:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, dict):
            if str(part.get("kind") or part.get("type") or "") == "text":
                out.append(str(part.get("text") or ""))
    return "\n".join(x for x in out if x).strip()


def _is_a2a_message(box: dict) -> bool:
    """True when ``box`` is itself an A2A Message, not a Task that wraps one."""
    if str(box.get("kind") or "") == "message":
        return True
    return "parts" in box and "status" not in box


def _a2a_status_text(status: dict) -> str:
    """The peer's own words from ``status.message``: a string, text or parts."""
    detail = status.get("message") if isinstance(status, dict) else None
    if isinstance(detail, dict):
        text = _a2a_parts_text(detail.get("parts"))
        if text:
            return text
        detail = detail.get("text") or detail.get("content") or ""
    return str(detail or "").strip()


def _a2a_reply_text(result: dict) -> str:
    """The agent's words in a ``message/send`` result, most specific first.

    (1) the result itself when it IS a Message -- a v0.3 ``SendMessageResponse``
    is ``Task | Message``, so a peer may answer with the bare Message;
    (2) ``result["message"]`` -- a direct reply; (3) the last agent/assistant
    turn of ``result["history"]``; (4) the same three inside a nested
    ``result["task"]`` (the legacy wrapped shape); (5) the last artifact.
    Returns "" when there is no text, so the caller decides what that means.
    """
    containers = [result]
    if isinstance(result.get("task"), dict):
        containers.append(result["task"])
    for box in containers:
        if _is_a2a_message(box):
            # Role "user" is the member's own request echoed back, not an answer.
            if str(box.get("role") or "") != "user":
                text = _a2a_parts_text(box.get("parts"))
                if text:
                    return text
        message = box.get("message")
        if isinstance(message, dict):
            text = _a2a_parts_text(message.get("parts"))
            if text:
                return text
        history = box.get("history")
        for turn in reversed(history if isinstance(history, list) else []):
            if isinstance(turn, dict) and str(turn.get("role") or "") in _AGENT_ROLES:
                text = _a2a_parts_text(turn.get("parts"))
                if text:
                    return text
    for box in containers:
        artifacts = box.get("artifacts")
        for artifact in reversed(artifacts if isinstance(artifacts, list) else []):
            if isinstance(artifact, dict):
                text = _a2a_parts_text(artifact.get("parts"))
                if text:
                    return text
    return ""


class A2AAgent:
    """An A2A v0.3 peer: JSON-RPC 2.0 ``message/send``, stdlib only.

    Any peer that publishes ``/.well-known/agent-card.json`` and answers
    ``message/send`` -- an ``adk serve`` node, LangGraph, CrewAI, a Google A2A
    agent -- becomes the bot's agent without an adapter.

    Scope, stated so nobody expects OpenAICompatAgent parity:

    * One thread per Discord channel+user (``contextId``); the reply carries the
      task id, which is sent back so the server replays its own history. No
      ``history`` list is transported: the thread on the server IS the history.
    * ``persona`` rides in ``message.metadata``; A2A has no persona field, so a
      server that ignores metadata answers without one.
    * ``tools`` are not sent (the peer brings its own), and images are not
      supported (``sees_images = False``; a text part cannot carry one).
    * ``input-required`` surfaces the peer's ``status.message`` as "the agent
      asks: ..."; a task left ``working``/``submitted`` is an error, not a poll
      (this class does not call ``tasks/get``); any other state with no text is
      named rather than reported as an empty answer.
    * The task-id map is process-local and lost on restart; a stale id is
      recovered by one retry on a "Task not found" error.
    """
    sees_images = False         # message/send parts here are text only
    runs_bot_tools = False      # the peer brings its own; grounding rides `context`
    MAX_THREADS = 500           # cached task ids, mirroring Dispatcher._history

    def __init__(self, url: str, token: str | None = None,
                 timeout: float = 120.0, ca_bundle: str | None = None):
        self.url = url.rstrip("/")
        self.name = "a2a"
        self._token = token
        self._timeout = timeout
        self._ctx = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else None
        # contextId -> taskId. The server keys its history on taskId, not contextId.
        self._task_for: dict[str, str] = {}

    @classmethod
    def from_env(cls, prefix: str = "AWDISCORD_") -> "A2AAgent | None":
        """The agent when ``{prefix}A2A_URL`` is set, else None (no behaviour change)."""
        url = (os.getenv(prefix + "A2A_URL") or "").strip()
        if not url:
            return None
        if urlsplit(url).scheme.lower() not in ("http", "https"):
            raise AgentError(f"{prefix}A2A_URL must be an http(s) URL")
        return cls(url, token=os.getenv(prefix + "A2A_TOKEN") or None,
                   ca_bundle=os.getenv(prefix + "CA_BUNDLE") or None)

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._token:
            h["Authorization"] = "Bearer " + self._token
        return h

    def ask(self, prompt: str, *, persona: str | None = None, user: str = "",
            context: str = "", tools: list | None = None,
            history: list | None = None, images: list[str] | None = None,
            channel: str = "") -> str:
        """Answer through ``message/send``.

        ``tools``, ``history`` and ``images`` are accepted for signature parity
        with the other agents but are not transported (see the class docstring).
        ``context`` -- the server's plugin persona plus any grounding the
        dispatcher retrieved -- is framed into the text, because the peer never
        sees ``tools`` and this is the only way the grounding reaches it.
        """
        context = str(context or "").strip()
        text = (f"[Server context from this Discord bot's plugins]\n{context}\n\n"
                f"[The member's message]\n{prompt}") if context else prompt
        # Stable, readable, no secrets: one thread per channel+user.
        cid = f"discord:{channel or 'dm'}:{user or 'anon'}"
        message: dict = {"kind": "message", "role": "user",
                         "messageId": str(uuid.uuid4()), "contextId": cid,
                         "parts": [{"kind": "text", "text": text}]}
        metadata = {k: v for k, v in (("persona", persona), ("user", user)) if v}
        if metadata:
            message["metadata"] = metadata
        task_id = self._task_for.get(cid)
        if task_id:
            message["taskId"] = task_id
        try:
            result = self._rpc(message)
        except AgentError as exc:
            if not (task_id and "Task not found" in str(exc)):
                raise
            # The server lost the task (restart or eviction): open a new one.
            self._task_for.pop(cid, None)
            message.pop("taskId", None)
            result = self._rpc(message)
        status = result.get("status")
        status = status if isinstance(status, dict) else {}
        state = str(status.get("state") or "")
        if state == "failed":
            detail = _a2a_status_text(status)
            raise AgentError("the agent's task failed"
                             + (f": {detail[:200]}" if detail else ""))
        self._remember_task(cid, result)
        reply = _a2a_reply_text(result)
        if reply:
            return clean_answer(reply, "the agent")
        if state in ("working", "submitted"):
            raise AgentError("the agent accepted the task but has not answered yet")
        if state == "input-required":
            # A normal core state, and the one where the peer is literally
            # asking the member a question: say so instead of "empty answer".
            detail = _a2a_status_text(status)
            raise AgentError(f"the agent asks: {detail[:200]}" if detail
                             else "the agent is waiting for a clarification")
        if state:
            raise AgentError(f"the agent ended the task as '{state}' without an answer")
        return clean_answer(reply, "the agent")

    def _remember_task(self, cid: str, result: dict) -> None:
        """Cache the task id for ``cid``, evicting the oldest past MAX_THREADS."""
        task_id = result.get("id")
        if not task_id and isinstance(result.get("task"), dict):
            task_id = result["task"].get("id") or result["task"].get("taskId")
        if not task_id:
            # A bare Message result (Task | Message) names the task it answered.
            task_id = result.get("taskId")
        if not task_id:
            return
        self._task_for.pop(cid, None)
        self._task_for[cid] = str(task_id)
        while len(self._task_for) > self.MAX_THREADS:
            self._task_for.pop(next(iter(self._task_for)))

    def _rpc(self, message: dict) -> dict:
        """One ``message/send`` round trip. Returns the result object."""
        payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
                   "params": {"message": message}}
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(),
                                     headers=self._headers(), method="POST")
        try:
            data = urlopen_json(req, timeout=self._timeout, context=self._ctx,
                                who="the agent")
        except ValueError as exc:
            raise AgentError("the agent returned no answer") from exc
        if not isinstance(data, dict):
            raise AgentError("the agent returned no answer")
        err = data.get("error")
        if err:
            if isinstance(err, dict):
                code, msg = err.get("code", ""), err.get("message", "")
            else:
                code, msg = "", str(err)
            # Never echo the URL: the message is shown to a server member.
            raise AgentError(
                (f"the agent returned a protocol error: {code} {msg}").strip()[:200])
        result = data.get("result")
        if not isinstance(result, dict):
            raise AgentError("the agent returned no answer")
        return result

    def health(self) -> str:
        """The agent card's name, as ``ok (Name)``. Never raises (``/status`` shows it)."""
        parts = urlsplit(self.url)
        card_url = f"{parts.scheme}://{parts.netloc}/.well-known/agent-card.json"
        try:
            req = urllib.request.Request(card_url, headers=self._headers())
            with urllib.request.urlopen(req, timeout=10, context=self._ctx) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return f"HTTP {exc.code}"
        except Exception as exc:
            return f"unreachable ({type(exc).__name__})"
        name = data.get("name") if isinstance(data, dict) else ""
        return f"ok ({name})" if name else "ok"


def load_personas(directory: str | os.PathLike) -> dict[str, str]:
    """Read personas from a directory: `<name>.md`, or `<name>/SOUL.md`.

    The name is lower-cased; the file is the system prompt. A missing or empty
    directory yields {} so a bot with no personas still answers.
    """
    root = Path(directory) if directory else None
    if root is None or not root.is_dir():
        return {}
    out: dict[str, str] = {}
    for child in sorted(root.iterdir()):
        if child.is_file() and child.suffix == ".md":
            text = child.read_text(encoding="utf-8").strip()
            if text:
                out[child.stem.lower()] = text
        elif child.is_dir() and (child / "SOUL.md").is_file():
            text = (child / "SOUL.md").read_text(encoding="utf-8").strip()
            if text:
                out[child.name.lower()] = text
    return out
