"""Discord: a mention of the bot's OWN integration (managed) role counts as a
self-mention.

Servers auto-create a managed role named after the bot, so the @ picker offers
both `@Bot` (the user) and `@Bot` (the role). A role-ping lands in
`message.role_mentions`, not `message.mentions`, so the user-only mention gate
used to drop it silently — the bot looked like it "ignored mentions sometimes"
depending on which entry the sender clicked. These tests pin the fix:

  - _self_role_mention_ids() recognizes ONLY the bot's own managed role
    (tags.bot_id == this bot), not unrelated/shared roles.
  - In a require_mention channel, a role-ping of the bot is handled (not
    dropped), and the role-mention token is stripped from the text the agent
    sees -- so `@Bot /help` works whether @Bot was the user or the role.
"""

import asyncio
from types import SimpleNamespace

import pytest

from tests.e2e.conftest import (
    BOT_USER_ID,
    E2E_MESSAGE_SETTLE_DELAY,
    GUILD_ID,
    CHANNEL_ID,
    get_response_text,
    make_discord_message,
)

pytestmark = pytest.mark.asyncio


def _bot_managed_role(role_id: int = 77777, bot_id: int = BOT_USER_ID):
    """A managed integration role belonging to THIS bot."""
    return SimpleNamespace(id=role_id, managed=True,
                           tags=SimpleNamespace(bot_id=bot_id))


def _unrelated_role(role_id: int = 88888):
    """A normal (non-managed) role the bot does not own."""
    return SimpleNamespace(id=role_id, managed=False, tags=None)


def _channel_with_bot_roles(*bot_roles):
    """A text channel whose guild exposes guild.me.roles = bot_roles."""
    guild = SimpleNamespace(
        id=GUILD_ID, name="Test Server",
        me=SimpleNamespace(roles=list(bot_roles)),
    )
    return SimpleNamespace(id=CHANNEL_ID, name="general", guild=guild)


def _msg_role_ping(content, role_mentions, channel):
    msg = make_discord_message(content=content, channel=channel, mentions=[])
    msg.role_mentions = list(role_mentions)
    return msg


# ---------------------------------------------------------------------------
# Helper: only the bot's own managed role counts
# ---------------------------------------------------------------------------

def test_self_role_mention_ids_matches_bot_managed_role(discord_adapter):
    role = _bot_managed_role()
    channel = _channel_with_bot_roles(role)
    msg = _msg_role_ping(f"<@&{role.id}> hi", [role], channel)
    assert discord_adapter._self_role_mention_ids(msg) == {role.id}


def test_self_role_mention_ids_ignores_unrelated_role(discord_adapter):
    bot_role = _bot_managed_role()
    other = _unrelated_role()
    channel = _channel_with_bot_roles(bot_role)  # bot does NOT have `other`
    msg = _msg_role_ping(f"<@&{other.id}> hi", [other], channel)
    assert discord_adapter._self_role_mention_ids(msg) == set()


def test_self_role_mention_ids_ignores_managed_role_of_other_bot(discord_adapter):
    """A managed role tagged to a DIFFERENT bot must not be treated as ours."""
    other_bot_role = SimpleNamespace(
        id=66666, managed=True, tags=SimpleNamespace(bot_id=BOT_USER_ID + 1))
    channel = _channel_with_bot_roles(other_bot_role)
    msg = _msg_role_ping(f"<@&{other_bot_role.id}> hi", [other_bot_role], channel)
    assert discord_adapter._self_role_mention_ids(msg) == set()


def test_self_role_mention_ids_empty_without_role_mentions(discord_adapter):
    channel = _channel_with_bot_roles(_bot_managed_role())
    msg = _msg_role_ping("plain text", [], channel)
    assert discord_adapter._self_role_mention_ids(msg) == set()


# ---------------------------------------------------------------------------
# End-to-end: a role-ping is NOT dropped by the require_mention gate
# ---------------------------------------------------------------------------

async def _dispatch(adapter, msg):
    await adapter._handle_message(msg)
    await asyncio.sleep(E2E_MESSAGE_SETTLE_DELAY)


async def test_bot_role_ping_is_handled_not_dropped(discord_adapter):
    """`@Bot(role) /help` in a require_mention channel → recognized as a
    mention, token stripped, /help dispatched (mirrors the @Bot(user) case)."""
    role = _bot_managed_role()
    channel = _channel_with_bot_roles(role)
    msg = _msg_role_ping(f"<@&{role.id}> /help", [role], channel)
    await _dispatch(discord_adapter, msg)
    response = get_response_text(discord_adapter)
    assert response is not None
    assert "/new" in response


async def test_unrelated_role_ping_is_still_dropped(discord_adapter):
    """Pinging some other role (not the bot's) must NOT wake the bot in a
    require_mention channel — the fix is scoped, not a blanket role bypass."""
    bot_role = _bot_managed_role()
    other = _unrelated_role()
    channel = _channel_with_bot_roles(bot_role)
    msg = _msg_role_ping(f"<@&{other.id}> /help", [other], channel)
    await _dispatch(discord_adapter, msg)
    assert get_response_text(discord_adapter) is None
