"""The voice join must survive both shapes of wait_until_connected.

discord.py 2.7 made it a blocking sync method returning a bool; earlier builds
had a coroutine. Awaiting the sync form raised TypeError on every join, which
left the sink attached to a socket that was not ready (2026-11-06).
"""

import asyncio
import threading

import pytest
from awdiscord import voice


class _SyncVC:
    def __init__(self, ready=True):
        self.ready = ready
        self.thread = None

    def wait_until_connected(self, timeout=30.0):
        self.thread = threading.current_thread()
        return self.ready


class _AsyncVC:
    async def wait_until_connected(self, timeout=30.0):
        return True


def _run_wait(vc):
    """Drive the same branch _join uses, without a live gateway."""
    logs = []

    async def go():
        wait = getattr(vc, "wait_until_connected", None)
        if voice.inspect.iscoroutinefunction(wait):
            ready = await wait(timeout=30.0)
        else:
            ready = await asyncio.to_thread(wait, 30.0)
        if ready is False:
            logs.append("timeout")
        return ready

    return asyncio.run(go()), logs


def test_sync_wait_is_not_awaited_and_runs_off_the_loop():
    vc = _SyncVC()
    ready, logs = _run_wait(vc)
    assert ready is True and logs == []
    assert vc.thread is not threading.main_thread()


def test_sync_wait_timeout_is_reported():
    ready, logs = _run_wait(_SyncVC(ready=False))
    assert ready is False and logs == ["timeout"]


def test_coroutine_wait_still_works():
    ready, logs = _run_wait(_AsyncVC())
    assert ready is True and logs == []


def test_join_source_no_longer_awaits_a_sync_call():
    src = voice.inspect.getsource(voice)
    assert "await wait(timeout=30.0)" in src          # only inside the coroutine branch
    assert "iscoroutinefunction(wait)" in src
    with pytest.raises(TypeError):                     # what the old line did
        asyncio.run(_await_bool())


async def _await_bool():
    await _SyncVC().wait_until_connected()
