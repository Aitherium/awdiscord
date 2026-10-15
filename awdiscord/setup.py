"""`awdiscord setup` - from a bot token to a running bot, with no portal clicking.

Discord has no API to CREATE an application, so the one manual step is: create
it at https://discord.com/developers/applications, open Bot, Reset Token, copy.
Everything after that is done here, and each step is a check that can fail:

  1. the token is a bot token (a user token is refused) and Discord accepts it
  2. the app profile is written: description, tags, icon, install settings
     (PATCH /applications/@me; fields Discord ignores for bot tokens are reported)
  3. a model is found: the first OpenAI-compatible server answering on this
     machine (Bonsai / llama.cpp, Ollama, LM Studio, vLLM, an `adk serve` agent),
     or the URL you give
  4. operators = the app owner (or every team member), so approvals work at once
  5. settings go to ~/.awdiscord/awdiscord.env (mode 600), which `run` reads
  6. slash commands (core + plugins) are registered and the install link printed

Re-running is safe: every step converges on the same state.
"""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import stat
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from awdiscord.install import MINIMAL_PERMISSIONS, install_url

_log = logging.getLogger(__name__)

# Local OpenAI-compatible servers, in the order a fresh box most likely has them.
CANDIDATES = (
    ("bonsai (llama.cpp)", "http://127.0.0.1:8090/v1"),
    ("llama.cpp", "http://127.0.0.1:8080/v1"),
    ("ollama", "http://127.0.0.1:11434/v1"),
    ("lm studio", "http://127.0.0.1:1234/v1"),
    ("vllm", "http://127.0.0.1:8000/v1"),
)

# Fields PATCH /applications/@me accepts. Discord silently ignores some of them
# for a bot token (the ToS / privacy URLs, measured 2026-10-04), so `apply_profile`
# compares what came back and reports the difference instead of assuming.
PROFILE_FIELDS = ("description", "tags", "icon", "cover_image", "install_params",
                  "integration_types_config", "terms_of_service_url",
                  "privacy_policy_url", "interactions_endpoint_url")


def env_path() -> Path:
    return Path(os.getenv("AWDISCORD_STATE_DIR", str(Path.home() / ".awdiscord"))) \
        / "awdiscord.env"


@dataclass
class Backend:
    label: str
    url: str
    models: list[str] = field(default_factory=list)


def probe_backend(url: str, timeout: float = 1.5, opener=None) -> list[str] | None:
    """Model ids served at ``url`` (an OpenAI-compatible /v1), or None if nothing answers."""
    opener = opener or urllib.request.urlopen
    try:
        with opener(urllib.request.Request(url.rstrip("/") + "/models"),
                    timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return [str(m.get("id")) for m in data.get("data", []) if m.get("id")]


def detect_backends(opener=None) -> list[Backend]:
    found = []
    for label, url in CANDIDATES:
        models = probe_backend(url, opener=opener)
        if models is not None:
            found.append(Backend(label, url, models))
    return found


def image_data_uri(path: str | Path) -> str:
    p = Path(path)
    data = p.read_bytes()
    if len(data) > 10 * 1024 * 1024:
        raise ValueError(f"{p.name} is over Discord's 10 MB limit")
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    if mime not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
        raise ValueError(f"{p.name}: Discord takes PNG, JPG, GIF or WEBP")
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def profile_body(description: str | None = None, tags: list[str] | None = None,
                 icon: str | None = None, cover: str | None = None,
                 terms_url: str | None = None, privacy_url: str | None = None,
                 user_install: bool = True) -> dict:
    """The PATCH body. Install settings are always the least-permission ones."""
    body: dict = {
        "install_params": {"scopes": ["bot", "applications.commands"],
                           "permissions": str(MINIMAL_PERMISSIONS)},
        # 0 = server install (bot + commands), 1 = user install (commands only,
        # so a person can /ask in DMs and any server without adding the bot).
        "integration_types_config": {
            "0": {"oauth2_install_params": {"scopes": ["bot", "applications.commands"],
                                            "permissions": str(MINIMAL_PERMISSIONS)}},
            **({"1": {"oauth2_install_params": {"scopes": ["applications.commands"],
                                                "permissions": "0"}}}
               if user_install else {}),
        },
    }
    if description is not None:
        if len(description) > 400:
            raise ValueError("description is capped at 400 characters")
        body["description"] = description
    if tags is not None:
        tags = [t.strip()[:20] for t in tags if t.strip()]
        if len(tags) > 5:
            raise ValueError("Discord allows at most 5 tags")
        body["tags"] = tags
    if icon:
        body["icon"] = image_data_uri(icon)
    if cover:
        body["cover_image"] = image_data_uri(cover)
    if terms_url:
        body["terms_of_service_url"] = terms_url
    if privacy_url:
        body["privacy_policy_url"] = privacy_url
    return body


def apply_profile(rest, body: dict) -> dict:
    """PATCH the app and report which requested fields did not stick."""
    app = rest.request("PATCH", "/applications/@me", body)
    ignored = []
    for key in ("description", "tags", "terms_of_service_url", "privacy_policy_url"):
        if key in body and app.get(key) != body[key]:
            ignored.append(key)
    if "icon" in body and not app.get("icon"):
        ignored.append("icon")
    return {"app": app, "ignored": ignored}


def operator_ids(app: dict) -> list[str]:
    """Who may press approval buttons: the team's members, or the owner."""
    team = app.get("team") or {}
    members = [str((m.get("user") or {}).get("id")) for m in team.get("members") or []
               if (m.get("user") or {}).get("id") and m.get("membership_state") == 2]
    if members:
        return sorted(set(members))
    owner = (app.get("owner") or {}).get("id")
    return [str(owner)] if owner else []


def read_env(path: Path | None = None) -> dict[str, str]:
    path = path or env_path()
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def write_env(values: dict[str, str], path: Path | None = None) -> Path:
    """Merge ``values`` into the env file; the file is 0600 (it holds the token)."""
    path = path or env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = {**read_env(path), **{k: v for k, v in values.items() if v is not None}}
    body = "# written by `awdiscord setup` - holds your bot token, keep it private\n" + \
        "".join(f"{k}={v}\n" for k, v in sorted(merged.items()))
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:  # os.open already asked for 0600; some filesystems ignore it
        _log.debug("chmod 0600 on %s failed: %s", path, exc)
    return path


def load_env_file(path: Path | None = None) -> int:
    """Put the env file's values into os.environ without overriding real env vars."""
    n = 0
    for k, v in read_env(path).items():
        if k not in os.environ:
            os.environ[k] = v
            n += 1
    return n


def run_setup(token: str, *, rest=None, agent_url: str | None = None,
              model: str | None = None, description: str | None = None,
              tags: list[str] | None = None, icon: str | None = None,
              cover: str | None = None, terms_url: str | None = None,
              privacy_url: str | None = None, guild: str | None = None,
              register: bool = True, opener=None, say=print) -> dict:
    """Every step of the docstring above. Returns a report; raises on a hard failure."""
    from awdiscord.dispatch import commands
    from awdiscord.plugins import build_host
    from awdiscord.rest import Rest
    from awdiscord.store import GuildStore

    rest = rest or Rest(token)
    me = rest.me()
    say(f"ok    token belongs to bot {me.get('username')} ({me['id']})")
    app = rest.application()
    app_id = str(app["id"])

    body = profile_body(description, tags, icon, cover, terms_url, privacy_url)
    applied = apply_profile(rest, body)
    say("ok    app profile + least-permission install settings written")
    for key in applied["ignored"]:
        say(f"note  Discord did not apply {key} for a bot token - set it once at "
            f"https://discord.com/developers/applications/{app_id}/information")

    if agent_url:
        models = probe_backend(agent_url, opener=opener)
        if models is None:
            say(f"warn  nothing answered at {agent_url}/models - saved anyway")
        backend = Backend("given", agent_url, models or [])
    else:
        found = detect_backends(opener=opener)
        backend = found[0] if found else None
        if backend:
            say(f"ok    model server: {backend.label} at {backend.url}")
        else:
            say("warn  no local model server found (tried "
                + ", ".join(u for _, u in CANDIDATES)
                + "). Start one, or re-run with --agent-url.")
    chosen_model = model or (backend.models[0] if backend and backend.models else None)

    ops = operator_ids(app)
    say(f"ok    operators: {', '.join(ops) if ops else 'none found - set AWDISCORD_OPERATORS'}")

    values = {"DISCORD_BOT_TOKEN": token, "AWDISCORD_APP_ID": app_id,
              "AWDISCORD_OPERATORS": ",".join(ops) or None}
    if backend:
        values["AWDISCORD_AGENT_URL"] = backend.url
    if chosen_model:
        values["AWDISCORD_AGENT_MODEL"] = chosen_model
    existing = read_env()
    if ops and "AWDISCORD_APPROVAL_SECRET" not in existing:
        import secrets
        values["AWDISCORD_APPROVAL_SECRET"] = secrets.token_hex(32)
    path = write_env(values)
    say(f"ok    settings saved to {path} (mode 600)")

    registered: list[str] = []
    if register:
        host = build_host(GuildStore(":memory:"))
        out = rest.put_commands(app_id, commands([], host=host), guild_id=guild)
        registered = [c["name"] for c in out]
        say(f"ok    registered /{' /'.join(registered)}")

    link = install_url(app_id)
    say(f"\nAdd the bot to a server:  {link}\nThen start it:            awdiscord run")
    return {"app_id": app_id, "bot": me.get("username"), "operators": ops,
            "backend": backend.url if backend else None, "model": chosen_model,
            "ignored": applied["ignored"], "commands": registered, "install_url": link,
            "env": str(path)}
