"""Regression: a parent's message_changed without a real edit must not replay the message after a restart.

Observed on a production Slack gateway (v0.21.5): a user's channel message was answered in its thread. The
gateway restarted, which empties the in-memory ``_processed_message_ts`` guard and ``_dedup``. Thread
replies were then deleted, so Slack sent ``message_changed`` for the parent (reply_count/latest_reply
changed; no ``edited``, same text). ``_normalize_changed_message`` turned it into a fresh message from the
original author, and the agent ran a 34-minute-old question again, twice (one event per deleted reply).

Only a real edit (``edited.ts`` and changed text) can carry new input; everything else is dropped.
"""

import asyncio
import importlib
import sys
from importlib.machinery import PathFinder
from types import ModuleType
from unittest.mock import AsyncMock

from gateway.config import PlatformConfig


def _load_installed_package(name):
    if PathFinder.find_spec(name) is None:
        return None
    prefix = f"{name}."
    displaced = {
        m: sys.modules.pop(m)
        for m in tuple(sys.modules)
        if (m == name or m.startswith(prefix)) and not isinstance(sys.modules[m], ModuleType)
    }
    try:
        return importlib.import_module(name)
    except ImportError:
        sys.modules.update(displaced)
        return None


_load_installed_package("slack_bolt")
_load_installed_package("slack_sdk")

SlackAdapter = importlib.import_module("plugins.platforms.slack.adapter").SlackAdapter

BOT = "U0BCLP7DB7B"
CHANNEL = "C0TESTCHAN1"
TEAM = "T0TESTTEAM1"
USER = "U0TESTUSER1"
PARENT_TS = "1787400000.000100"
TEXT = f"<@{BOT}> where are we at with the order"


def _fresh_adapter(delivered):
    """A just-restarted gateway: empty claim map and dedup."""
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-fake"))
    adapter._bot_user_id = BOT
    adapter._resolve_user_name = AsyncMock(return_value="adnan")

    async def _capture(event):
        delivered.append(event)

    adapter.handle_message = _capture
    return adapter


def _parent(**extra):
    return {"type": "message", "user": USER, "text": TEXT, "ts": PARENT_TS, "thread_ts": PARENT_TS,
            "client_msg_id": "cmid-1", **extra}


def _changed(message, event_ts, previous=None):
    event = {"type": "message", "subtype": "message_changed", "channel": CHANNEL, "channel_type": "channel",
             "team": TEAM, "ts": event_ts, "event_ts": event_ts, "message": message, "hidden": True}
    if previous is not None:
        event["previous_message"] = previous
    return event


def _run(adapter, *events):
    async def scenario():
        for e in events:
            await adapter._handle_slack_message(e, {"team_id": TEAM})
    asyncio.run(scenario())


def test_reply_deleted_after_restart_does_not_replay_the_parent():
    delivered = []
    adapter = _fresh_adapter(delivered)
    before = _parent(reply_count=2, latest_reply="1792114368.000100")
    after = _parent(reply_count=1, latest_reply="1792114320.000100")
    _run(adapter,
         _changed(after, "1792116261.000100", previous=before),
         _changed(_parent(reply_count=0), "1792116262.000200", previous=after))
    assert delivered == []


def test_parent_change_without_previous_message_is_dropped_too():
    delivered = []
    adapter = _fresh_adapter(delivered)
    _run(adapter, _changed(_parent(reply_count=3), "1792116261.000100"))
    assert delivered == []


def test_edited_stamp_with_unchanged_text_is_dropped():
    delivered = []
    adapter = _fresh_adapter(delivered)
    msg = _parent(edited={"user": USER, "ts": "1792116260.000000"})
    _run(adapter, _changed(msg, "1792116261.000100", previous=_parent()))
    assert delivered == []


def test_a_real_edit_after_restart_still_routes_once():
    delivered = []
    adapter = _fresh_adapter(delivered)
    msg = _parent(text=f"<@{BOT}> and the second order?", edited={"user": USER, "ts": "1792116260.000000"})
    _run(adapter, _changed(msg, "1792116261.000100", previous=_parent()))
    assert len(delivered) == 1
    assert delivered[0].message_id == PARENT_TS
