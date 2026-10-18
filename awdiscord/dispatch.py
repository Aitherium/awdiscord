"""Interaction JSON in, response JSON out - the whole bot, with no network.

The runtime (gateway or HTTP endpoint) hands every interaction to
`Dispatcher.handle`, sends the initial response it returns, and, when there
is deferred work, runs it and edits the original reply with the result. That
split keeps every decision here testable with plain dicts.

Commands:
    /ask question [persona] [private] [file]   the agent answers (file: image/video/text)
    /share                              re-post your last answer to the channel
    /status                             agent health, limits, version
"""
from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable

from awdiscord.agent import AgentError
from awdiscord.approvals import ApprovalDesk, ApprovalError
from awdiscord.ratelimit import RateLimiter

PING, COMMAND, COMPONENT = 1, 2, 3
PONG, MESSAGE, DEFERRED, UPDATE = 1, 4, 5, 7
EPHEMERAL = 64
LIMIT = 2000
NO_MENTIONS = {"parse": []}


def commands(personas: list[str] | None = None, host=None) -> list[dict]:
    """The slash-command definitions, for PUT /applications/{id}/commands.

    With a plugin ``host``, every plugin command is included too; whether it
    answers in a given server is decided at run time by ``/plugins``.
    """
    ask_options = [{"type": 3, "name": "question", "description": "What to ask",
                    "required": True, "max_length": 1500}]
    if personas:
        ask_options.append({
            "type": 3, "name": "persona", "description": "Who answers",
            "required": False,
            "choices": [{"name": p.title(), "value": p} for p in personas[:25]]})
    ask_options.append({"type": 5, "name": "private",
                        "description": "Only you see the answer", "required": False})
    ask_options.append({"type": 11, "name": "file",
                        "description": "An image, video or text file to ask about",
                        "required": False})
    return [
        {"name": "ask", "description": "Ask the agent", "type": 1, "options": ask_options,
         "contexts": [0, 1], "integration_types": [0]},
        {"name": "share", "description": "Post your last answer to this channel", "type": 1,
         "contexts": [0], "integration_types": [0]},
        {"name": "status", "description": "Is the agent up, and how much can I ask", "type": 1,
         "contexts": [0, 1], "integration_types": [0]},
    ] + (host.command_defs() if host is not None else [])


def clip(text: str, limit: int = LIMIT) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 14] + "\n(truncated)"


@dataclass
class Result:
    response: dict
    deferred: Callable[[], dict] | None = None   # returns the PATCH @original payload
    log: dict = field(default_factory=dict)


class Dispatcher:
    def __init__(self, agent, limiter: RateLimiter | None = None,
                 desk: ApprovalDesk | None = None, version: str = "",
                 remember: int = 1000, host=None, owner_agent=None,
                 owners=()):
        self.agent = agent
        # Operators' mentions and DMs go to `owner_agent` (the fleet agent with
        # its tools); everyone else gets `agent`. Fleet tools never reach a
        # public guild member through the bot.
        self.owner_agent = owner_agent
        self.owners = frozenset(str(o) for o in owners if str(o))
        self.limiter = limiter or RateLimiter()
        self.desk = desk
        self.host = host            # awdiscord.plugins.PluginHost, or None
        self.version = version
        self._last: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._history: dict[str, list[dict]] = {}      # channel -> recent turns
        self._remember = remember
        from awdiscord.media import prepare
        self.media_reader = prepare     # attachments -> Media; swapped in tests

    # -- entry -----------------------------------------------------------------
    def handle(self, interaction: dict) -> Result:
        kind = interaction.get("type")
        if kind == PING:
            return Result({"type": PONG})
        if kind == COMMAND:
            name = (interaction.get("data") or {}).get("name")
            handler = {"ask": self._ask, "share": self._share, "status": self._status}.get(name)
            if handler is None and self.host is not None and self.host.owns(name):
                return self._plugin(interaction, self.host.handle)
            if handler is None:
                return self._reply("Unknown command.", private=True)
            return handler(interaction)
        if kind == COMPONENT:
            if self.host is not None:
                from awdiscord.plugins import BUTTON_PREFIX
                cid = (interaction.get("data") or {}).get("custom_id", "")
                if cid.startswith(BUTTON_PREFIX):
                    return self._plugin(interaction, self.host.handle_button)
            return self._button(interaction)
        return self._reply("Unsupported interaction.", private=True)

    # -- commands --------------------------------------------------------------
    def _ask(self, it: dict) -> Result:
        user, guild = _user_id(it), it.get("guild_id")
        opts = _options(it)
        question = str(opts.get("question", "")).strip()
        if not question:
            return self._reply("Ask a question.", private=True)
        verdict = self.limiter.check(user, guild)
        if not verdict.allowed:
            return self._reply(f"Slow down - try again in {verdict.retry_after:g}s "
                               f"({verdict.scope} limit).", private=True,
                               log={"limited": verdict.scope})
        persona = opts.get("persona")
        private = bool(opts.get("private", False))
        context = self.host.persona_for(guild) if self.host is not None else ""

        file_id = opts.get("file")
        files = ((it.get("data") or {}).get("resolved") or {}).get("attachments") or {}
        attached = [files[file_id]] if file_id and file_id in files else []

        def work() -> dict:
            agent = self.agent_for(user)
            try:
                kwargs = self._agent_kwargs(guild, context, user, agent)
                prompt = question
                if attached:
                    prompt = self._attach(question, attached, agent, user, kwargs)
                answer = agent.ask(prompt, persona=persona, user=user, **kwargs)
            except AgentError as exc:
                return {"content": f"The agent could not answer: {exc}",
                        "allowed_mentions": NO_MENTIONS}
            self._remember_answer(user, question, answer)
            return {"content": clip(answer), "allowed_mentions": NO_MENTIONS}

        response = {"type": DEFERRED, "data": {"flags": EPHEMERAL} if private else {}}
        return Result(response, deferred=work, log={"cmd": "ask", "user": user})

    # -- @mentions and DMs -------------------------------------------------------
    HISTORY_TURNS = 6            # per channel: enough for "no, I meant ..." follow-ups

    def chat(self, user: str, guild: str | None, text: str, bot_id: str = "",
             channel: str = "", attachments: list[dict] | None = None) -> dict | None:
        """A message that mentions the bot (or a DM) -> the reply payload, or None.

        Same limits and plugin context as /ask; `/share` works on the answer too.
        Discord delivers the text of a message that mentions the bot (and of DMs)
        without the privileged message-content intent, so this needs none.
        ``attachments`` = Discord attachment dicts; images and video frames go to
        the agent as images, small text files as text (awdiscord.media).
        """
        if bot_id:
            text = re.sub(rf"<@!?{re.escape(bot_id)}>", "", text)
        text = text.strip()
        if not text and not attachments:
            return {"content": "Hi - ask me anything here, or use /ask.",
                    "allowed_mentions": NO_MENTIONS}
        verdict = self.limiter.check(user, guild)
        if not verdict.allowed:
            return {"content": f"Slow down - try again in {verdict.retry_after:g}s.",
                    "allowed_mentions": NO_MENTIONS}
        context = self.host.persona_for(guild) if self.host is not None else ""
        agent = self.agent_for(user)
        try:
            kwargs = self._agent_kwargs(guild, context, user, agent)
            past = self._history.setdefault(channel, []) if channel else []
            if past and _accepts(agent.ask, "history"):
                kwargs["history"] = list(past)
            prompt = clip(text, 1500)
            if attachments:
                prompt = self._attach(prompt, attachments, agent, user, kwargs)
                text = text or "(sent an attachment)"
            answer = agent.ask(prompt, user=user, **kwargs)
        except AgentError as exc:
            return {"content": f"The agent could not answer: {exc}",
                    "allowed_mentions": NO_MENTIONS}
        self._remember_answer(user, text, answer)
        if channel:
            past += [{"role": "user", "content": clip(text, 1500)},
                     {"role": "assistant", "content": clip(answer, 1500)}]
            del past[: max(0, len(past) - 2 * self.HISTORY_TURNS)]
            if len(self._history) > 500:            # bound memory across channels
                self._history.pop(next(iter(self._history)))
        return {"content": clip(answer), "allowed_mentions": NO_MENTIONS}

    def _share(self, it: dict) -> Result:
        user = _user_id(it)
        last = self._last.get(user)
        if last is None:
            return self._reply("Nothing to share yet - use /ask first.", private=True)
        verdict = self.limiter.check(user, it.get("guild_id"))
        if not verdict.allowed:
            return self._reply(f"Slow down - try again in {verdict.retry_after:g}s.",
                               private=True)
        question, answer = last
        text = f"<@{user}> asked: {clip(question, 300)}\n\n{answer}"
        return self._reply(clip(text), private=False, log={"cmd": "share", "user": user})

    def _status(self, it: dict) -> Result:
        try:
            health = self.agent.health()
        except Exception as exc:
            health = f"error ({type(exc).__name__})"
        personas = sorted(getattr(self.agent, "personas", {}) or {})
        lines = [f"agent: {getattr(self.agent, 'name', 'agent')} - {health}",
                 f"personas: {', '.join(personas) if personas else 'default only'}",
                 f"awdiscord {self.version}".strip()]
        return self._reply("\n".join(lines), private=True)

    def _attach(self, question: str, attachments: list[dict], agent, user: str,
                kwargs: dict) -> str:
        """The prompt for a turn with attachments; images land in ``kwargs``.

        An agent that sees images (``sees_images``) gets them as ``images``.
        One that does not gets them described by the public agent's vision
        model first: Genesis /chat ignored an attached image and answered with
        a directory listing after 414 s (measured 2026-10-17).
        """
        media = self.media_reader(attachments)
        question = question or media.default_question()
        prompt = media.prompt(question)
        if not media.images:
            return prompt
        if getattr(agent, "sees_images", False) and _accepts(agent.ask, "images"):
            kwargs["images"] = media.images
            return prompt
        seer = self.agent
        if seer is agent or not getattr(seer, "sees_images", False):
            return prompt
        try:
            seen = seer.ask(
                f"The user sent these images and asks: {question}\n\n"
                + "\n".join(media.notes) + "\n\nDescribe everything in them that "
                "bears on the question, and transcribe any visible text.",
                user=user, images=media.images)
        except AgentError as exc:
            return prompt + f"\n\n(The attached images could not be looked at: {exc})"
        return prompt + ("\n\nWhat the attached images show, as described by a vision "
                         f"model:\n{seen}")

    def agent_for(self, user: str):
        """The fleet agent for an operator, the public agent for everyone else."""
        if self.owner_agent is not None and user in self.owners:
            return self.owner_agent
        return self.agent

    def _agent_kwargs(self, guild, context: str, user: str = "", agent=None) -> dict:
        """Plugin context and tools for this server; omitted when empty so an
        agent written against the plain ask(prompt, persona, user) still works."""
        kwargs: dict = {}
        if context:
            kwargs["context"] = context
        tools = self.host.tools_for(guild, user) if self.host is not None else []
        if tools:
            kwargs["tools"] = tools
        ask = (agent or self.agent).ask
        return {k: v for k, v in kwargs.items() if _accepts(ask, k)}

    def _plugin(self, it: dict, route) -> Result:
        """A plugin command or button: rate-limited like /ask, and never allowed
        to take the bot down - a plugin exception becomes a private error."""
        user = _user_id(it)
        verdict = self.limiter.check(user, it.get("guild_id"))
        if not verdict.allowed:
            return self._reply(f"Slow down - try again in {verdict.retry_after:g}s "
                               f"({verdict.scope} limit).", private=True,
                               log={"limited": verdict.scope})
        try:
            result = route(it)
        except Exception as exc:
            return self._reply(f"That plugin failed: {type(exc).__name__}.", private=True,
                               log={"plugin_error": f"{type(exc).__name__}: {exc}"[:200]})
        if result is None:
            return self._reply("Unknown command.", private=True)
        return result

    # -- buttons ---------------------------------------------------------------
    def _button(self, it: dict) -> Result:
        custom_id = (it.get("data") or {}).get("custom_id", "")
        if self.desk is None or not ApprovalDesk.is_ours(custom_id):
            return self._reply("This button is not handled here.", private=True)
        user = _user_id(it)
        try:
            decision = self.desk.resolve(custom_id, user)
        except ApprovalError as exc:
            return self._reply(f"Not recorded: {exc}.", private=True,
                               log={"approval": "refused", "user": user})
        record = self.desk.get(decision.approval_id) or {}
        payload = self.desk.decided_card(record)
        return Result({"type": UPDATE, "data": payload},
                      log={"approval": decision.verdict, "id": decision.approval_id,
                           "already": decision.already, "callback": decision.callback})

    # -- helpers ---------------------------------------------------------------
    def _remember_answer(self, user: str, question: str, answer: str) -> None:
        self._last[user] = (question, answer)
        self._last.move_to_end(user)
        while len(self._last) > self._remember:
            self._last.popitem(last=False)

    @staticmethod
    def _reply(text: str, private: bool, log: dict | None = None) -> Result:
        data = {"content": clip(text), "allowed_mentions": NO_MENTIONS}
        if private:
            data["flags"] = EPHEMERAL
        return Result({"type": MESSAGE, "data": data}, log=log or {})


def _accepts(fn, name: str) -> bool:
    """Does ``fn`` take keyword ``name``? (an older agent has no tools/context)"""
    import inspect
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


def _user_id(it: dict) -> str:
    member = it.get("member") or {}
    user = member.get("user") or it.get("user") or {}
    return str(user.get("id", ""))


def _options(it: dict) -> dict:
    return {o.get("name"): o.get("value")
            for o in (it.get("data") or {}).get("options", []) or []}
