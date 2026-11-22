"""First-party lookup: our own bricks and products, from our own registry.

The web plugin searches the world; this plugin answers about us. It exists because
on 2026-11-22 a public server was told awpermit, awrun, awdelphi, awrelay and
AitherNet "do not exist" -- all public, all with Pages sites -- because the only
evidence the bot had was a web search (see awdiscord.firstparty).

Off unless the deployment mounts or points at a registry (``AWDISCORD_FIRSTPARTY_DIR``,
``AWDISCORD_ECOSYSTEM_FILE`` or ``AWDISCORD_ECOSYSTEM_URL``): the brick is public,
so it never silently answers a stranger's bot from Aitherium's own catalogue.
"""
from __future__ import annotations

from awdiscord import firstparty
from awdiscord.plugins import Plugin, Tool

TOOL = Tool(
    firstparty.TOOL_NAME,
    "Look up a name in Aitherium's own registry and offline corpus: aw* bricks, "
    "Aither* product names, and aitherium.com addresses. Call this FIRST, before "
    "web_search, whenever a question names anything of ours. A brick the registry "
    "lists EXISTS: report its real status (public, unpublished, planned) and Docs URL.",
    firstparty.lookup,
    {"type": "object", "properties": {
        "name": {"type": "string",
                 "description": "e.g. 'awpermit', 'AitherNet', 'index.aitherium.com'"}},
     "required": ["name"]},
)

PLUGIN = Plugin(
    name="firstparty",
    version="0.1.0",
    description="Our own bricks and products, looked up before the web",
    default_enabled=firstparty.available(),
    persona=("For anything about our own stack -- an aw* brick, an Aither* name, an "
             "aitherium.com site -- call first_party_lookup before web_search. A name "
             "the registry lists is a real product: never say it does not exist, and "
             "never call it a proposal. Report the status it gives (public, "
             "unpublished, planned) and its Docs link."),
    tools=[TOOL],
)
