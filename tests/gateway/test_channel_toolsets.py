"""Per-channel toolset allowlist (Phase 1: #tldr = fetch+read only).

These tests assert the *invariant* that a per-channel ``channel_toolsets``
binding produces a strict tool allowlist enforced as infrastructure -- a
low-privilege channel like ``#tldr`` cannot reach terminal/write/execute tools
even if instructed to. They are not change-detector snapshots.
"""

from gateway.platforms.base import resolve_channel_toolsets, resolve_channel_max_turns
from toolsets import resolve_toolset, TOOLSETS

TLDR_ID = "1518563840814747793"
PARENT_ID = "1518563840814747793"

# Tools a fetch+read mode must never be handed.
FORBIDDEN_IN_TLDR = {
    "terminal", "process", "write_file", "patch", "execute_code",
    "delegate_task", "cronjob",
}


def _cfg(bindings):
    return {"channel_toolsets": bindings}


def test_file_read_toolset_is_read_only():
    """The file_read toolset grants read/search but never write/patch."""
    tools = set(resolve_toolset("file_read"))
    assert "read_file" in tools
    assert "search_files" in tools
    assert "write_file" not in tools
    assert "patch" not in tools


def test_resolve_exact_channel_match():
    bindings = [{"id": TLDR_ID, "toolsets": ["web", "file_read"]}]
    assert resolve_channel_toolsets(_cfg(bindings), TLDR_ID) == ["web", "file_read"]


def test_resolve_single_string_form():
    bindings = [{"id": TLDR_ID, "toolset": "web"}]
    assert resolve_channel_toolsets(_cfg(bindings), TLDR_ID) == ["web"]


def test_resolve_parent_fallback_for_threads():
    """A forum thread inherits its parent channel's allowlist."""
    bindings = [{"id": PARENT_ID, "toolsets": ["web", "file_read"]}]
    # Thread id differs from any binding; parent matches.
    got = resolve_channel_toolsets(_cfg(bindings), "999thread", PARENT_ID)
    assert got == ["web", "file_read"]


def test_no_binding_returns_none_keeps_platform_default():
    bindings = [{"id": "other-channel", "toolsets": ["web"]}]
    assert resolve_channel_toolsets(_cfg(bindings), TLDR_ID) is None
    assert resolve_channel_toolsets({}, TLDR_ID) is None
    assert resolve_channel_toolsets({"channel_toolsets": []}, TLDR_ID) is None


def test_explicit_no_tools_allowlist_returns_empty_list():
    """An explicit empty allowlist (#general pure chat) returns [] -- distinct
    from None. [] means "send ZERO tools to the model"; None means "no binding,
    keep the platform default". Both the empty-list and "none" forms work."""
    GENERAL_ID = "1517149565608792096"
    assert resolve_channel_toolsets(_cfg([{"id": GENERAL_ID, "toolsets": []}]), GENERAL_ID) == []
    assert resolve_channel_toolsets(_cfg([{"id": GENERAL_ID, "toolset": "none"}]), GENERAL_ID) == []
    # A non-matching binding still yields None (default), not [].
    assert resolve_channel_toolsets(_cfg([{"id": "other", "toolsets": []}]), GENERAL_ID) is None


def test_dedup_preserves_order():
    bindings = [{"id": TLDR_ID, "toolsets": ["web", "web", "file_read"]}]
    assert resolve_channel_toolsets(_cfg(bindings), TLDR_ID) == ["web", "file_read"]


def test_tldr_allowlist_excludes_all_dangerous_tools():
    """The security invariant: expanding #tldr's allowlist yields no
    shell/write/execute tool, while still granting fetch + read."""
    allow = resolve_channel_toolsets(
        _cfg([{"id": TLDR_ID, "toolsets": ["web", "file_read"]}]), TLDR_ID
    )
    granted: set[str] = set()
    for ts in allow:
        assert ts in TOOLSETS, f"unknown toolset {ts!r}"
        granted.update(resolve_toolset(ts))
    # Fetch + read present.
    assert {"web_search", "web_extract", "read_file"} <= granted
    # Nothing dangerous.
    assert not (granted & FORBIDDEN_IN_TLDR), granted & FORBIDDEN_IN_TLDR


def test_full_discord_toolset_would_have_been_dangerous():
    """Sanity: without the override, the discord platform toolset DOES include
    the dangerous tools -- proving the override is what removes them."""
    full = set(resolve_toolset("hermes-discord"))
    assert FORBIDDEN_IN_TLDR & full  # the platform default is privileged


# --- per-channel turn cap -------------------------------------------------

def test_max_turns_resolved_from_binding():
    bindings = [{"id": TLDR_ID, "toolsets": ["web"], "max_turns": 5}]
    assert resolve_channel_max_turns(_cfg(bindings), TLDR_ID) == 5


def test_max_turns_none_when_absent():
    bindings = [{"id": TLDR_ID, "toolsets": ["web"]}]
    assert resolve_channel_max_turns(_cfg(bindings), TLDR_ID) is None


def test_max_turns_parent_fallback():
    bindings = [{"id": PARENT_ID, "toolsets": ["web"], "max_turns": 3}]
    assert resolve_channel_max_turns(_cfg(bindings), "999thread", PARENT_ID) == 3


def test_max_turns_rejects_bool_and_nonpositive():
    # bool is an int subclass -- must not be accepted as a turn count.
    assert resolve_channel_max_turns(_cfg([{"id": TLDR_ID, "max_turns": True}]), TLDR_ID) is None
    assert resolve_channel_max_turns(_cfg([{"id": TLDR_ID, "max_turns": 0}]), TLDR_ID) is None
    assert resolve_channel_max_turns(_cfg([{"id": TLDR_ID, "max_turns": -1}]), TLDR_ID) is None
