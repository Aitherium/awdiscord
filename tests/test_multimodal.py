"""Multimodal Discord: an attachment is read in whatever form it arrives, and a
turn that produces a file carries it back out.

Owner, 2026-11-01: a recording posted with "can you transcribe and understand"
came back as "I can't see media, please @lyra directly". Three things are pinned
here:

1. audio and a video's soundtrack are transcribed through the fleet STT and the
   transcript reaches the model (``awdiscord.media``);
2. a clip that could NOT be transcribed is delegated by the bot itself -- the
   answer never sends the member off to ask another agent (``Dispatcher``);
3. what a tool produced goes out with the answer (``awdiscord.outbox`` ->
   ``files`` on the payload -> ``Rest.upload``).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awdiscord.agent import AgentError  # noqa: E402
from awdiscord.dispatch import Dispatcher  # noqa: E402
from awdiscord.media import Media, kind, prepare  # noqa: E402
from awdiscord.outbox import Outbox, OutboxError, safe_filename  # noqa: E402
from awdiscord.plugins import Plugin, PluginHost, Tool  # noqa: E402
from awdiscord.store import GuildStore  # noqa: E402


def _agent(answer="ok"):
    class Agent:
        name = "agent"

        def __init__(self):
            self.prompts = []

        def ask(self, prompt, *, persona=None, user="", **kwargs):
            self.prompts.append(prompt)
            return answer

        def health(self):
            return "ok"
    return Agent()


def _host(tools=(), operators=()):
    plugin = Plugin(name="p", version="0.1", description="", tools=list(tools),
                    default_enabled=True)
    return PluginHost(GuildStore(":memory:"), operators=set(operators), plugins=[plugin])


# -- input: audio and video -----------------------------------------------------------

def test_audio_attachment_is_transcribed_into_the_prompt():
    heard = []

    def transcribe(raw, name, mime):
        heard.append((raw, name, mime))
        return "remember the milk and call Sam at four"

    m = prepare([{"filename": "note.ogg", "url": "https://cdn/n.ogg",
                  "content_type": "audio/ogg", "size": 40_000}],
                fetch=lambda url, limit: b"OggS-bytes", ffmpeg=None, transcribe=transcribe)
    assert heard == [(b"OggS-bytes", "note.ogg", "audio/ogg")]
    assert "remember the milk" in m.prompt("what did I say?")
    assert any("you HEARD it" in n for n in m.notes)
    assert m.unread == []
    assert "audio" in m.default_question()


def test_audio_detected_by_extension_when_discord_sends_no_content_type():
    assert kind({"filename": "Voice Message.m4a"}) == "audio"
    assert kind({"filename": "clip.webm", "content_type": "audio/webm"}) == "audio"
    assert kind({"filename": "clip.webm", "content_type": "video/webm"}) == "video"


def test_an_audio_clip_with_no_stt_service_is_marked_unread():
    m = prepare([{"filename": "note.mp3", "url": "u", "content_type": "audio/mpeg"}],
                fetch=lambda url, limit: b"ID3", ffmpeg=None, transcribe=None)
    assert m.texts == [] and m.unread
    assert "note.mp3" in m.unread[0] and "<u>" in m.unread[0]
    assert "rather than asking the member to resend it" in m.prompt("q")


def test_video_transcribes_its_soundtrack_as_well_as_showing_frames(monkeypatch):
    import awdiscord.media as media

    monkeypatch.setattr(media, "video_audio", lambda raw, ff: b"RIFF-pcm")
    calls = []

    def stt(raw, name, mime):
        calls.append(name)
        return "the speaker says the launch is on Friday"

    def fetch(url, limit):
        return b"MP4-bytes"

    monkeypatch.setattr(media, "video_sheet", lambda raw, ff: ("data:image/jpeg;base64,AA", 12.0))
    m = media.prepare([{"filename": "talk.mp4", "url": "https://cdn/t.mp4",
                        "content_type": "video/mp4", "size": 9_000_000}],
                      fetch=fetch, ffmpeg="/usr/bin/ffmpeg", transcribe=stt)
    assert calls == ["talk.mp4.wav"]
    assert "launch is on Friday" in m.prompt("what was announced?")
    assert any("transcript of its soundtrack" in n for n in m.notes)
    assert m.images and m.unread == []


def test_pdf_and_docx_text_reaches_the_prompt():
    import io
    import zipfile

    def docx_bytes(text):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml",
                       '<w:document xmlns:w="http://schemas.openxmlformats.org/'
                       f'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{text}'
                       "</w:t></w:r></w:p></w:body></w:document>")
        return buf.getvalue()

    m = prepare([{"filename": "brief.docx", "url": "u", "size": 900}],
                fetch=lambda url, limit: docx_bytes("Ship on Friday"),
                ffmpeg=None, transcribe=None)
    assert kind({"filename": "brief.docx"}) == "doc"
    assert "Ship on Friday" in m.prompt("summarise it")
    assert any("document brief.docx" in n for n in m.notes)


# -- the delegation fallback -----------------------------------------------------------

def test_unread_media_is_delegated_by_the_bot_never_pushed_back_to_the_member():
    seen = []

    def delegate(agent="", brief="", deliverable="", caller=None):
        seen.append((agent, brief, caller))
        return "Delegated to lyra: job `j1`."

    agent = _agent()
    host = _host([Tool("delegate", "hand work on", delegate,
                       {"type": "object", "properties": {}}, wants_caller=True)])
    d = Dispatcher(agent, host=host, owners=["42"])
    d.media_reader = lambda atts: Media(
        notes=["audio note.ogg: could not be read here"],
        unread=["audio note.ogg <https://cdn/n.ogg>"])
    out = d.chat("42", "g1", "transcribe this", channel="c",
                 attachments=[{"filename": "note.ogg", "url": "https://cdn/n.ogg"}])
    assert out["content"] == "ok"
    assert seen and seen[0][0] == "lyra" and "https://cdn/n.ogg" in seen[0][1]
    prompt = agent.prompts[-1]
    assert "already handed it to Lyra yourself" in prompt
    assert "never ask them to contact Lyra" in prompt


# -- output: files a turn produces -----------------------------------------------------

def test_a_tool_that_produces_a_file_carries_it_out_with_the_answer():
    class DrawingAgent:
        name = "drawer"

        def ask(self, prompt, *, persona=None, user="", tools=None, **kwargs):
            for tool in tools or []:
                if tool.name == "draw":
                    tool.run({"prompt": "a red kite"})
            return "Here is the kite."

        def health(self):
            return "ok"

    def draw(prompt="", caller=None):
        box = caller["outbox"]
        box.add("kite.png", b"\x89PNG-bytes", "image/png", caption=prompt)
        return "Attached kite.png"

    host = _host([Tool("draw", "draw it", draw,
                       {"type": "object", "properties": {}}, wants_caller=True)])
    out = Dispatcher(DrawingAgent(), host=host).chat("42", "g1", "draw a kite", channel="c")
    assert out["content"] == "Here is the kite."
    assert [f["filename"] for f in out["files"]] == ["kite.png"]
    assert out["files"][0]["content_type"] == "image/png"
    assert out["files"][0]["data"] == b"\x89PNG-bytes"


def test_outbox_caps_and_names_say_so_instead_of_dropping():
    box = Outbox(max_files=1, max_bytes=10)
    box.add("../../etc/passwd", b"0123456789", "text/plain")
    assert box.files[0].filename == "passwd"       # a name, never a path
    with pytest.raises(OutboxError, match="at most 1"):
        box.add("b.txt", b"x")
    with pytest.raises(OutboxError, match="at most"):
        Outbox(max_bytes=2).add("big.bin", b"0123")
    with pytest.raises(OutboxError, match="empty"):
        Outbox().add("none.bin", b"")
    assert safe_filename("assets/a b?.png") == "a_b_.png"
    assert safe_filename("../../etc/passwd") == "passwd"


def test_rest_upload_is_multipart_with_the_file_and_the_caption():
    from awdiscord.rest import Rest

    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"id": "m1", "attachments": [{"url": "https://cdn/x.png"}]}'

    def opener(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = req.data
        seen["ct"] = req.get_header("Content-type")
        return Resp()

    token = "A" * 24 + ".abcd." + "B" * 27
    got = Rest(token, opener=opener).upload("c1", "x.png", b"PNGDATA", "image/png", "hi")
    assert got["id"] == "m1"
    assert seen["url"].endswith("/channels/c1/messages")
    assert seen["ct"].startswith("multipart/form-data; boundary=")
    assert b'name="files[0]"; filename="x.png"' in seen["body"]
    assert b"PNGDATA" in seen["body"] and b"image/png" in seen["body"]


def test_a_failed_transcription_never_costs_the_answer():
    def boom(raw, name, mime):
        raise RuntimeError("stt is down")

    class Agent:
        name = "a"

        def ask(self, prompt, **kwargs):
            return "I could not read it, but here is what I know."

        def health(self):
            return "ok"

    d = Dispatcher(Agent())
    d.media_reader = lambda atts: prepare(
        [{"filename": "a.mp3", "url": "u", "content_type": "audio/mpeg"}],
        fetch=lambda url, limit: b"ID3", ffmpeg=None, transcribe=boom)
    out = d.chat("1", None, "transcribe", attachments=[{"filename": "a.mp3", "url": "u"}])
    assert out["content"].startswith("I could not read it")


def test_an_agent_error_still_returns_a_reply(monkeypatch):
    class Agent:
        name = "a"

        def ask(self, prompt, **kwargs):
            raise AgentError("the agent is unreachable right now")

        def health(self):
            return "down"

    out = Dispatcher(Agent()).chat("1", None, "hi")
    assert "could not answer" in out["content"]
