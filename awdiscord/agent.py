"""The agent behind the bot: anything that turns a prompt into an answer.

`OpenAICompatAgent` speaks POST {base}/chat/completions, which is what an adk
agent (`adk serve`), llama.cpp, vLLM, Ollama and most gateways expose, so "any
agent" means any of those with no adapter. `CallableAgent` wraps a plain
function for everything else.
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable


class AgentError(Exception):
    """The agent could not answer. The message is safe to show a user."""


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

# Rides every public turn after the persona. A bare chat model in a group chat
# fills gaps with plausible specifics and offers work it cannot do; measured
# 2026-10-22: "feed this to the fork" became invented release notes and an
# offer to a stranger to "push it" to staging.
GROUND_RULES = (
    "Ground rules for this chat:\n"
    "- Never invent specifics (versions, release notes, branches, PRs, plans, "
    "steps you took). If it is not in this conversation or a tool result, say "
    "you don't know.\n"
    "- When a message is ambiguous, ask one short question instead of guessing "
    "a task. Use the names people use, and keep a correction once given.\n"
    "- You cannot change, build, deploy or push anything from this chat, and you "
    "never offer to. Changes to the operator's systems are the operator's call."
)


class OpenAICompatAgent:
    sees_images = True          # images go to ``vision_models``
    def __init__(self, base_url: str, model: str = "default", api_key: str | None = None,
                 personas: dict[str, str] | None = None, default_persona: str | None = None,
                 timeout: float = 120.0, max_tokens: int = 900, ca_bundle: str | None = None,
                 vision_models: list[str] | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        # A turn that carries images goes to these, in order (the next one when
        # one fails); a text-only model answers 5xx or never sees the picture.
        self.vision_models = [m for m in vision_models or [] if m] or [model]
        self.name = model
        self._key = api_key
        self.personas = personas or {}
        self.default_persona = default_persona
        self._timeout = timeout
        self._max_tokens = max_tokens
        self._ctx = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else None

    @classmethod
    def from_env(cls, prefix: str = "AWDISCORD_") -> "OpenAICompatAgent":
        url = os.getenv(prefix + "AGENT_URL")
        if not url:
            raise AgentError(f"set {prefix}AGENT_URL to an OpenAI-compatible /v1 base URL")
        personas = load_personas(os.getenv(prefix + "PERSONAS_DIR") or "")
        return cls(url, model=os.getenv(prefix + "AGENT_MODEL", "default"),
                   api_key=os.getenv(prefix + "AGENT_KEY") or None,
                   personas=personas,
                   default_persona=os.getenv(prefix + "DEFAULT_PERSONA") or None,
                   ca_bundle=os.getenv(prefix + "CA_BUNDLE") or None,
                   vision_models=(os.getenv(prefix + "VISION_MODELS") or "").replace(
                       " ", "").split(","))

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
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
        return self._ask(prompt, persona, user, context, tools, history, None, self.model)

    def _ask(self, prompt, persona, user, context, tools, history, images, model) -> str:
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
        by_name = {t.name: t for t in tools or []}
        for _round in range(MAX_TOOL_ROUNDS + 1):
            payload = {"model": model, "messages": messages,
                       "max_tokens": self._max_tokens}
            if by_name and _round < MAX_TOOL_ROUNDS:
                payload["tools"] = [t.schema() for t in by_name.values()]
            if user:
                payload["user"] = user
            msg = self._complete(payload)
            calls = msg.get("tool_calls") or []
            if not calls:
                text = msg.get("content")
                if not text or not str(text).strip():
                    raise AgentError("the agent returned an empty answer")
                return str(text).strip()
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

    def _complete(self, payload: dict) -> dict:
        req = urllib.request.Request(self.base_url + "/chat/completions",
                                     data=json.dumps(payload).encode(),
                                     headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout, context=self._ctx) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise AgentError(f"the agent answered HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AgentError("the agent is unreachable right now") from exc
        try:
            return data["choices"][0]["message"] or {}
        except (KeyError, IndexError, TypeError) as exc:
            raise AgentError("the agent returned no answer") from exc

    def health(self) -> str:
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

    One Genesis session per Discord user, so the conversation carries over.
    """
    # Genesis /chat did not look at an attached image (2026-10-17), so the
    # dispatcher has a vision model describe it into the prompt instead.
    sees_images = False

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
            history: list | None = None, images: list[str] | None = None) -> str:
        # `tools`/`context` are the bot's own plugins; Genesis brings its own.
        # `images` (data: URLs) ride as `attachments`: Genesis routes a turn
        # carrying one to its vision role.
        recent = [{"role": t["role"], "content": str(t["content"])}
                  for t in history or []
                  if t.get("role") in ("user", "assistant") and t.get("content")]
        payload = {
            "message": prompt,
            "session_id": f"discord-{user or 'anon'}",
            "persona": persona or self.persona,
            "stream": False,
            "agentic": True,
            "max_effort": self.effort,
            "context": {"source": "discord", "platform": "discord", "user_id": user,
                        "tagged": True, "recent_history": recent},
        }
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
            with urllib.request.urlopen(req, timeout=self._timeout, context=self._ctx) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise AgentError(f"the fleet agent answered HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AgentError("the fleet agent is unreachable right now") from exc
        except ValueError as exc:
            raise AgentError("the fleet agent returned no answer") from exc
        text = ""
        if isinstance(data, dict):
            text = data.get("response") or data.get("content") or ""
        if not str(text).strip():
            raise AgentError("the fleet agent returned an empty answer")
        return str(text).strip()

    def health(self) -> str:
        try:
            with urllib.request.urlopen(self.base_url + "/health", timeout=10,
                                        context=self._ctx) as r:
                return "ok" if r.status == 200 else f"HTTP {r.status}"
        except urllib.error.HTTPError as exc:
            return f"HTTP {exc.code}"
        except Exception as exc:
            return f"unreachable ({type(exc).__name__})"


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
