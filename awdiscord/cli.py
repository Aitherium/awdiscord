"""awdiscord on the command line.

    awdiscord setup                                token -> app profile, model, commands, link
    awdiscord install-url --app-id 123             the link a server admin clicks
    awdiscord whoami                               prove the token is a bot
    awdiscord register [--guild ID]                publish core + plugin commands
    awdiscord run [--echo]                         the bot (needs awdiscord[gateway])
    awdiscord plugins                              what loads here, and what failed
    awdiscord new-plugin NAME                      scaffold a plugin
    awdiscord pack install|list|remove ...         plugin packs and awdk agent packs
    awdiscord post --channel ID "text"             post as the bot
    awdiscord webhook URL "text"                   post through a channel webhook
    awdiscord approval "title" "body"              raise a card via a running bot's inbox
    awdiscord doctor
    awdiscord --self-test                          prove the rails hold, offline

Configuration is environment: DISCORD_BOT_TOKEN (or AWDISCORD_TOKEN),
AWDISCORD_AGENT_URL, AWDISCORD_OPERATORS, AWDISCORD_APPROVAL_SECRET, ...
`setup` saves them to ~/.awdiscord/awdiscord.env; a real env var always wins.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.request


def _token() -> str:
    return os.getenv("AWDISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN") or ""


def cmd_install_url(args) -> int:
    from awdiscord.install import install_url
    app_id = args.app_id or os.getenv("AWDISCORD_APP_ID") or os.getenv("DISCORD_APP_ID")
    if not app_id and _token():
        from awdiscord.rest import Rest
        app_id = Rest(_token()).application()["id"]
    if not app_id:
        print("give --app-id, or set DISCORD_APP_ID / DISCORD_BOT_TOKEN")
        return 1
    print(install_url(app_id, guild_id=args.guild))
    return 0


def cmd_whoami(args) -> int:
    from awdiscord.guard import TokenRefusedError
    from awdiscord.rest import Rest, RestError
    try:
        rest = Rest(_token())
        me = rest.me()
        guilds = rest.guilds()
    except (TokenRefusedError, RestError) as exc:
        print(f"refused: {exc}")
        return 1
    print(json.dumps({"bot": True, "username": me.get("username"), "id": me["id"],
                      "guilds": len(guilds)}, indent=2))
    return 0


def cmd_register(args) -> int:
    from awdiscord.agent import load_personas
    from awdiscord.dispatch import commands
    from awdiscord.rest import Rest
    rest = Rest(_token())
    rest.me()
    app_id = os.getenv("AWDISCORD_APP_ID") or os.getenv("DISCORD_APP_ID") \
        or rest.application()["id"]
    personas = sorted(load_personas(os.getenv("AWDISCORD_PERSONAS_DIR", "")))
    # Same set `run` registers: a register without the plugins would delete
    # their commands from Discord until the next restart.
    host = None if os.getenv("AWDISCORD_NO_PLUGINS") in ("1", "true", "yes") else _host()
    out = rest.put_commands(app_id, commands(personas, host=host), guild_id=args.guild)
    print("registered: " + ", ".join(c["name"] for c in out))
    return 0


def _host():
    from awdiscord.plugins import build_host
    from awdiscord.store import GuildStore
    only = [p for p in os.getenv("AWDISCORD_PLUGINS", "").replace(" ", "").split(",") if p]
    return build_host(GuildStore(":memory:"), only=only or None)


def cmd_plugins(args) -> int:
    """What this machine would load, and why anything failed to."""
    host = _host()
    rows = [{"name": p.name, "version": p.version, "source": p.source,
             "default": "always" if p.always_on else ("on" if p.default_enabled else "off"),
             "commands": [c.name for c in p.commands]} for p in host.plugins.values()]
    if args.json:
        print(json.dumps({"plugins": rows, "errors": host.errors}, indent=2))
    else:
        for row in rows:
            print(f"{row['name']:<14} {row['version']:<8} {row['default']:<7} "
                  f"/{' /'.join(row['commands'])}   ({row['source']})")
        for src, err in sorted(host.errors.items()):
            print(f"FAILED  {src}: {err}")
    return 1 if host.errors else 0


_PLUGIN_TEMPLATE = '''"""{name} - an awdiscord plugin."""
from awdiscord.plugins import Command, Option, Plugin


def hello(ctx):
    count = int(ctx.store.get("count", 0)) + 1
    ctx.store.set("count", count)
    return ctx.reply(f"hello <@{{ctx.user}}> - said {{count}} time(s) in this server")


def ask(ctx):
    question = ctx.opt("question")
    return ctx.defer(lambda: ctx.agent.ask(question, user=ctx.user))


PLUGIN = Plugin(
    name="{name}",
    version="0.1.0",
    description="Describe what {name} does",
    persona="",          # added to the agent's system prompt where this plugin is on
    commands=[
        Command("{name}", "{name} commands", subcommands=[
            Command("hello", "Say hello", hello),
            Command("ask", "Ask the agent", ask,
                    options=[Option("question", "Your question", required=True)]),
        ]),
    ],
)
'''


def cmd_new_plugin(args) -> int:
    import re
    from pathlib import Path

    from awdiscord.plugins import plugin_dirs
    name = args.name.lower()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,30}", name):
        print("name: lowercase letters, digits, - or _, 2-31 chars")
        return 1
    target = Path(args.dir) if args.dir else plugin_dirs()[-1]
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{name.replace('-', '_')}.py"
    if path.exists():
        print(f"exists: {path}")
        return 1
    path.write_text(_PLUGIN_TEMPLATE.format(name=name), encoding="utf-8")
    print(f"wrote {path}")
    print("next: awdiscord plugins   (check it loads)")
    print(f"      awdiscord register  (publish its commands), then /plugins enable {name}")
    return 0


def cmd_run(args) -> int:
    from awdiscord.agent import EchoAgent
    from awdiscord.gateway import Bot
    if args.inbox_port:
        # Started by a supervisor (adk addon, awsh): it health-checks GET /health on
        # this port. The inbox needs a bearer; mint one once and keep it in the env file.
        os.environ["AWDISCORD_INBOX_PORT"] = str(args.inbox_port)
        if not os.getenv("AWDISCORD_INBOX_TOKEN"):
            import secrets

            from awdiscord.setup import write_env
            tok = secrets.token_hex(24)
            write_env({"AWDISCORD_INBOX_TOKEN": tok})
            os.environ["AWDISCORD_INBOX_TOKEN"] = tok
    Bot.from_env(agent=EchoAgent() if args.echo else None).run()
    return 0


def cmd_post(args) -> int:
    from awdiscord.rest import Rest
    msg = Rest(_token()).create_message(args.channel, {"content": args.text[:2000]})
    print(json.dumps({"id": msg["id"], "channel_id": args.channel}))
    return 0


def cmd_webhook(args) -> int:
    from awdiscord.webhook import WebhookError, post_webhook
    try:
        msg = post_webhook(args.url, args.text, username=args.username)
    except WebhookError as exc:
        print(f"error: {exc}")
        return 1
    print(json.dumps({"id": msg.get("id")}))
    return 0


def cmd_approval(args) -> int:
    url = os.getenv("AWDISCORD_INBOX_URL", "http://127.0.0.1:8371").rstrip("/")
    body = {"title": args.title, "body": args.body}
    if args.channel:
        body["channel_id"] = args.channel
    if args.callback:
        body["callback_url"] = args.callback
    req = urllib.request.Request(url + "/approvals", data=json.dumps(body).encode(),
                                 method="POST", headers={
                                     "Content-Type": "application/json",
                                     "Authorization": "Bearer " + os.getenv(
                                         "AWDISCORD_INBOX_TOKEN", "")})
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())
    return 0


def cmd_doctor(args) -> int:
    from awdiscord import _doctor
    return _doctor.report()


def self_test() -> int:
    """Prove each rail can fail. Offline; exit 0 only if every check held."""
    from awdiscord.agent import EchoAgent
    from awdiscord.approvals import ApprovalDesk, ApprovalError
    from awdiscord.dispatch import DEFERRED, MESSAGE, UPDATE, Dispatcher
    from awdiscord.guard import TokenRefusedError, check_bot_token, require_bot_identity
    from awdiscord.install import FORBIDDEN_PERMISSIONS, MINIMAL_PERMISSIONS, install_url
    from awdiscord.ratelimit import RateLimiter

    failures = []

    def check(name, cond):
        print(("ok    " if cond else "FAIL  ") + name)
        if not cond:
            failures.append(name)

    def raises(exc, fn):
        try:
            fn()
        except exc:
            return True
        return False

    url = install_url("123456789012345678")
    check("install link asks bot + applications.commands",
          "scope=bot+applications.commands" in url)
    check("install link asks minimal permissions", f"permissions={MINIMAL_PERMISSIONS}" in url)
    admin = FORBIDDEN_PERMISSIONS["ADMINISTRATOR"]
    check("install link refuses administrator",
          raises(ValueError, lambda: install_url("1", permissions=admin)))
    check("user mfa token refused",
          raises(TokenRefusedError, lambda: check_bot_token("mfa." + "x" * 60)))
    check("Bearer grant refused", raises(TokenRefusedError, lambda: check_bot_token("Bearer abc")))
    check("user account refused", raises(TokenRefusedError,
                                         lambda: require_bot_identity({"id": "1", "bot": False})))

    clock = [0.0]
    lim = RateLimiter(per_user=(2, 60.0), clock=lambda: clock[0])
    check("rate limit allows a burst", lim.check("u").allowed and lim.check("u").allowed)
    check("rate limit refuses past the burst", not lim.check("u").allowed)
    clock[0] += 31
    check("rate limit refills", lim.check("u").allowed)

    with tempfile.TemporaryDirectory() as tmp:
        desk = ApprovalDesk(tmp, "s3cret", ["42"])
        rec = desk.raise_card("deploy", "ship it?")
        cid = desk.custom_id(rec["id"], "approve")
        forged = cid[:-1] + ("0" if cid[-1] != "0" else "1")
        check("forged button refused", raises(ApprovalError, lambda: desk.resolve(forged, "42")))
        check("non-operator refused", raises(ApprovalError, lambda: desk.resolve(cid, "7")))
        d1 = desk.resolve(cid, "42")
        d2 = desk.resolve(desk.custom_id(rec["id"], "deny"), "42")
        check("operator approves", d1.verdict == "approved" and not d1.already)
        check("second press cannot flip it", d2.verdict == "approved" and d2.already)

        disp = Dispatcher(EchoAgent(), RateLimiter(), desk)
        ask = {"type": 2, "id": "1", "token": "t", "guild_id": "g",
               "member": {"user": {"id": "42"}},
               "data": {"name": "ask", "options": [{"name": "question", "value": "hi"}]}}
        res = disp.handle(ask)
        check("/ask defers then answers", res.response["type"] == DEFERRED
              and res.deferred()["content"] == "echo: hi")
        share = disp.handle({**ask, "data": {"name": "share"}})
        check("/share re-posts the answer", share.response["type"] == MESSAGE
              and "echo: hi" in share.response["data"]["content"])
        rec2 = desk.raise_card("x", "y")
        press = disp.handle({"type": 3, "member": {"user": {"id": "42"}},
                             "data": {"custom_id": desk.custom_id(rec2["id"], "deny")}})
        check("button press updates the card", press.response["type"] == UPDATE
              and press.log.get("approval") == "denied")

    from awdiscord.plugins import build_host
    from awdiscord.store import GuildStore
    host = build_host(GuildStore(":memory:"), agent=EchoAgent())
    pdisp = Dispatcher(EchoAgent(), RateLimiter(), None, host=host)

    def sub(cmd, verb, **opts):
        return {"type": 2, "id": "2", "token": "t", "guild_id": "g",
                "member": {"user": {"id": "7"}, "permissions": "0"},
                "data": {"name": cmd, "options": [{"type": 1, "name": verb, "options": [
                    {"name": k, "value": v} for k, v in opts.items()]}]}}
    check("a plugin that is off in a server refuses", "is off in this server"
          in pdisp.handle(sub("task", "add", title="x")).response["data"]["content"])
    check("a non-manager cannot enable a plugin", "Only server managers"
          in pdisp.handle(sub("plugins", "enable", name="projects")).response["data"]["content"])

    print(f"\n{'PASS' if not failures else 'FAIL'}: {len(failures)} failing check(s)")
    return 0 if not failures else 1


def main(argv=None) -> int:
    # GENERATED doctor intercept (gen_aw_doctor.py) -- do not edit
    _dv = locals().get("argv")
    if (_dv if _dv is not None else __import__("sys").argv[1:])[:1] == ["doctor"]:
        from ._doctor import report
        return report()
    # GENERATED repo-state intercept (gen_aw_doctor.py) -- do not edit
    try:
        from awgit import state as _aw_state
    except Exception:
        _aw_state = None
    if _aw_state is not None:
        _sv = locals().get("argv")
        if _aw_state.cli_banner(_sv if _sv is not None else __import__("sys").argv[1:]):
            return 0
    p = argparse.ArgumentParser(prog="awdiscord", description=__doc__.split("\n")[0])
    p.add_argument("--self-test", action="store_true")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("install-url")
    s.add_argument("--app-id")
    s.add_argument("--guild")
    s.set_defaults(fn=cmd_install_url)
    sub.add_parser("whoami").set_defaults(fn=cmd_whoami)
    s = sub.add_parser("register")
    s.add_argument("--guild")
    s.set_defaults(fn=cmd_register)
    s = sub.add_parser("run")
    s.add_argument("--echo", action="store_true", help="answer with an echo agent")
    s.add_argument("--inbox-port", type=int, default=0,
                   help="open the local inbox (GET /health) on this port")
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("post")
    s.add_argument("--channel", required=True)
    s.add_argument("text")
    s.set_defaults(fn=cmd_post)
    s = sub.add_parser("webhook")
    s.add_argument("url")
    s.add_argument("text")
    s.add_argument("--username")
    s.set_defaults(fn=cmd_webhook)
    s = sub.add_parser("approval")
    s.add_argument("title")
    s.add_argument("body")
    s.add_argument("--channel")
    s.add_argument("--callback")
    s.set_defaults(fn=cmd_approval)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    s = sub.add_parser("plugins", help="list the plugins this machine loads")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_plugins)
    s = sub.add_parser("new-plugin", help="scaffold a plugin into the plugin dir")
    s.add_argument("name")
    s.add_argument("--dir")
    s.set_defaults(fn=cmd_new_plugin)
    s = sub.add_parser("setup", help="token -> configured app, model, commands, install link")
    s.add_argument("--agent-url", help="OpenAI-compatible /v1 URL (default: detect locally)")
    s.add_argument("--model")
    s.add_argument("--description")
    s.add_argument("--tags", help="comma-separated, at most 5")
    s.add_argument("--icon", help="PNG/JPG/GIF/WEBP, 1024x1024")
    s.add_argument("--cover", help="rich presence cover image, 1024x576")
    s.add_argument("--terms-url")
    s.add_argument("--privacy-url")
    s.add_argument("--guild", help="register commands in this server only (instant)")
    s.add_argument("--no-register", action="store_true")
    s.set_defaults(fn=cmd_setup)
    s = sub.add_parser("pack", help="install, list or remove packs")
    s.add_argument("action", choices=["install", "list", "remove"])
    s.add_argument("target", nargs="?", help="dir, .py file or git URL[#subdir]; or a name")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_pack)
    args = p.parse_args(argv)
    if args.self_test:
        return self_test()
    if not getattr(args, "fn", None):
        p.print_help()
        return 2
    if args.fn is not cmd_setup:
        # Settings `setup` saved; a real environment variable always wins.
        from awdiscord.setup import load_env_file
        load_env_file()
    return args.fn(args)


def cmd_pack(args) -> int:
    from awdiscord import packs
    from awdiscord.plugins import PluginError
    try:
        if args.action == "list":
            home = packs.pack_home()
            names = sorted(p.name for p in home.iterdir()
                           if not p.name.startswith(("_", "."))) if home.is_dir() else []
            print("\n".join(names) if names else f"no packs in {home}")
            return 0
        if not args.target:
            print(f"pack {args.action} needs a target")
            return 2
        if args.action == "install":
            out = packs.install(args.target, force=args.force)
            print(f"installed {out['name']} {out['version']} ({out['kind']}) -> {out['path']}")
            print(f"commands: /{' /'.join(out['commands'])}")
            print(f"next: restart the bot (or `awdiscord register`), "
                  f"then /plugins enable {out['name']}")
            return 0
        print(f"removed {packs.remove(args.target)}")
        return 0
    except PluginError as exc:
        print(f"FAIL  {exc}")
        return 1


def cmd_setup(args) -> int:
    import getpass

    from awdiscord.guard import TokenRefusedError
    from awdiscord.rest import RestError
    from awdiscord.setup import read_env, run_setup
    token = _token() or read_env().get("DISCORD_BOT_TOKEN", "")
    if not token:
        print("Create an app at https://discord.com/developers/applications, open Bot,\n"
              "press Reset Token and paste it here (it is not shown or logged).")
        token = getpass.getpass("bot token: ").strip()
    try:
        run_setup(token, agent_url=args.agent_url, model=args.model,
                  description=args.description,
                  tags=args.tags.split(",") if args.tags else None,
                  icon=args.icon, cover=args.cover, terms_url=args.terms_url,
                  privacy_url=args.privacy_url, guild=args.guild,
                  register=not args.no_register)
    except (TokenRefusedError, RestError, ValueError, OSError) as exc:
        print(f"FAIL  {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
