"""Packs: install a plugin, or an awdk agent pack, into this bot with one command.

    awdiscord pack install ./my-pack            a directory
    awdiscord pack install https://github.com/org/repo[#subdir]
    awdiscord pack list
    awdiscord pack remove my-pack

Two kinds of pack are understood:

* **plugin pack** - a ``.py`` file or a package directory defining
  ``PLUGIN = Plugin(...)``. Copied as is.
* **agent pack** (awdk / awpack shape) - a directory with ``brain_pack.yaml``
  and/or ``agent.yaml`` and optional ``skills/*.md``. It becomes a plugin named
  after the pack: its system prompt rides along on ``/ask`` wherever the plugin
  is on, its skills are listed to the agent, and ``/<name> ask`` talks to the
  agent in that pack's voice. Reading YAML needs PyYAML (``pip install pyyaml``).

Installed packs live in the plugin dir (``~/.awdiscord/plugins``), so
``awdiscord plugins`` lists them and ``/plugins enable <name>`` turns them on.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from awdiscord.plugins import Command, Option, Plugin, PluginError, plugin_dirs

AGENT_PACK_FILES = ("brain_pack.yaml", "agent.yaml")
_NAME = re.compile(r"[^a-z0-9_-]+")


def pack_home() -> Path:
    return plugin_dirs()[-1]


def is_agent_pack(path: Path) -> bool:
    return path.is_dir() and not (path / "__init__.py").exists() and \
        any((path / f).exists() for f in AGENT_PACK_FILES)


def _yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        import yaml
    except ImportError as exc:
        raise PluginError("agent packs need PyYAML: pip install pyyaml") from exc
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def command_name(raw: str) -> str:
    name = _NAME.sub("-", raw.lower()).strip("-_")[:32]
    if len(name) < 2:
        raise PluginError(f"cannot make a command name from {raw!r}")
    return name


def agent_pack_plugin(path: Path) -> Plugin:
    """Bridge an awdk agent pack directory into a Plugin."""
    brain, agent = _yaml(path / "brain_pack.yaml"), _yaml(path / "agent.yaml")
    name = command_name(str(agent.get("name") or brain.get("identity") or path.name))
    prompt = str(brain.get("system_prompt") or agent.get("system_prompt") or "").strip()
    title = str(brain.get("app_name") or agent.get("name") or name)
    skills = {}
    sdir = path / "skills"
    if sdir.is_dir():
        for f in sorted(sdir.rglob("*.md")):
            key = f.parent.name if f.name.upper() == "SKILL.MD" else f.stem
            skills[key] = f.read_text(encoding="utf-8")
    description = " ".join(str(agent.get("description") or title).split())[:100]

    def ask(ctx):
        question = str(ctx.opt("question"))
        context = ctx.plugin.persona

        def work() -> str:
            if ctx.agent is None:
                return "No agent is configured."
            return ctx.agent.ask(question, user=ctx.user, context=context)
        return ctx.defer(work)

    def skill(ctx):
        want = str(ctx.opt("name") or "").strip()
        if not want:
            return ctx.reply("Skills: " + (", ".join(sorted(skills)) or "none"),
                             private=True)
        body = skills.get(want)
        if body is None:
            return ctx.reply(f"No skill {want!r}.", private=True)
        return ctx.reply(body[:1900], private=True)

    plugin = Plugin(
        name=name, version=str(agent.get("version") or "0.0.0"),
        description=description or f"{title} agent pack",
        persona=prompt, skills=skills,
        commands=[Command(name, f"{title} (agent pack)", subcommands=[
            Command("ask", f"Ask {title}", ask, options=[
                Option("question", "Your question", required=True, max_length=1500)]),
            Command("skills", "What this pack knows how to do", skill, options=[
                Option("name", "Show one skill", max_length=60)]),
        ])])
    return plugin


# ── install / remove ──────────────────────────────────────────────────────────

def _fetch(src: str, workdir: Path) -> Path:
    """A local path, or a git URL (optionally ``#subdir``) cloned shallowly."""
    if re.match(r"^(https://|git@)", src):
        url, _, sub = src.partition("#")
        dest = workdir / "clone"
        try:
            subprocess.run(["git", "clone", "--depth", "1", "-q", url, str(dest)],
                           check=True, timeout=120, capture_output=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise PluginError(f"could not clone {url}: {exc}") from exc
        target = dest / sub if sub else dest
        shutil.rmtree(dest / ".git", ignore_errors=True)
        return target
    path = Path(src).expanduser()
    if not path.exists():
        raise PluginError(f"no such pack: {src}")
    return path


def _validate(path: Path) -> Plugin:
    """Load the pack the way the bot will, so a broken pack fails at install time."""
    if is_agent_pack(path):
        return agent_pack_plugin(path)
    import importlib.util
    target = path / "__init__.py" if path.is_dir() else path
    if target.suffix != ".py" or not target.exists():
        raise PluginError(f"{path.name}: not a plugin (.py / package) or an agent pack "
                          f"({' / '.join(AGENT_PACK_FILES)})")
    spec = importlib.util.spec_from_file_location(
        f"awdiscord_validate_{path.stem}", target,
        submodule_search_locations=[str(path)] if path.is_dir() else None)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    plugin = getattr(mod, "PLUGIN", None)
    if not isinstance(plugin, Plugin):
        raise PluginError(f"{path.name}: no PLUGIN = Plugin(...) defined")
    return plugin


def install(src: str, force: bool = False) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        path = _fetch(src, Path(tmp))
        plugin = _validate(path)
        home = pack_home()
        home.mkdir(parents=True, exist_ok=True)
        dest = home / (plugin.name.replace("-", "_") + (".py" if path.is_file() else ""))
        if dest.exists():
            if not force:
                raise PluginError(f"{plugin.name} is already installed (use --force)")
            remove(plugin.name)
        if path.is_file():
            shutil.copy2(path, dest)
        else:
            shutil.copytree(path, dest, ignore=shutil.ignore_patterns(
                ".git", "__pycache__", "*.pyc", ".venv", "node_modules"))
    return {"name": plugin.name, "version": plugin.version, "path": str(dest),
            "kind": "agent-pack" if is_agent_pack(dest) else "plugin",
            "commands": [c.name for c in plugin.commands]}


def remove(name: str) -> Path:
    home = pack_home()
    for cand in (home / name, home / name.replace("-", "_"),
                 home / f"{name.replace('-', '_')}.py"):
        if cand.exists():
            if cand.is_dir():
                shutil.rmtree(cand)
            else:
                cand.unlink()
            return cand
    raise PluginError(f"{name} is not installed in {home}")
