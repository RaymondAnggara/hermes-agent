"""Per-channel write/build elevation (Phase 2: #coding read-only -> !build).

These tests assert the *invariant* that #coding's default allowlist is
read-only, that a separate ``build_toolsets`` field carries the elevated
(write + terminal) set unlocked one-shot by the operator's ``!build`` command,
and that the two resolvers are independent (a channel with no ``build_toolsets``
has no build mode). Invariant assertions, not change-detector snapshots.
"""

from gateway.platforms.base import (
    resolve_channel_toolsets,
    resolve_channel_build_toolsets,
    resolve_channel_build_max_turns,
    resolve_channel_build_workspace,
)
from toolsets import resolve_toolset

CODING_ID = "1518544267742679070"
OTHER_ID = "1518563840814747793"

# A read-only #coding default must never hand out write/exec/terminal tools.
FORBIDDEN_READ_ONLY = {"terminal", "process", "write_file", "patch", "execute_code"}
# The elevated build set is allowed to write + run terminal (sandboxed).
EXPECTED_IN_BUILD = {"write_file", "patch", "terminal", "process"}


def _cfg(bindings):
    return {"channel_toolsets": bindings}


def _binding():
    return [{
        "id": CODING_ID,
        "toolsets": ["web", "file_read", "delegation"],
        "max_turns": 25,
        "build_toolsets": ["web", "file", "terminal", "delegation"],
        "build_max_turns": 60,
        "build_workspace": "/Users/raymond/projects/Tsorf-Agent-Improvement",
    }]


def test_default_coding_toolset_is_read_only():
    got = resolve_channel_toolsets(_cfg(_binding()), CODING_ID)
    assert got == ["web", "file_read", "delegation"]
    granted = set()
    for ts in got:
        granted.update(resolve_toolset(ts))
    assert not (granted & FORBIDDEN_READ_ONLY), granted & FORBIDDEN_READ_ONLY


def test_build_toolsets_resolve_to_write_set():
    got = resolve_channel_build_toolsets(_cfg(_binding()), CODING_ID)
    assert got == ["web", "file", "terminal", "delegation"]
    granted = set()
    for ts in got:
        granted.update(resolve_toolset(ts))
    assert EXPECTED_IN_BUILD <= granted, EXPECTED_IN_BUILD - granted


def test_build_max_turns_resolves():
    assert resolve_channel_build_max_turns(_cfg(_binding()), CODING_ID) == 60


def test_build_workspace_resolves():
    assert resolve_channel_build_workspace(_cfg(_binding()), CODING_ID) == (
        "/Users/raymond/projects/Tsorf-Agent-Improvement"
    )


def test_build_workspace_none_when_unset():
    bindings = [{"id": CODING_ID, "build_toolsets": ["file"]}]
    assert resolve_channel_build_workspace(_cfg(bindings), CODING_ID) is None
    assert resolve_channel_build_workspace(_cfg(_binding()), "no-such-id") is None


def test_channel_without_build_field_has_no_build_mode():
    # Read-only-only binding (no build_toolsets) -> build resolvers return None,
    # so !build is inert there.
    bindings = [{"id": OTHER_ID, "toolsets": ["web", "file_read"]}]
    assert resolve_channel_build_toolsets(_cfg(bindings), OTHER_ID) is None
    assert resolve_channel_build_max_turns(_cfg(bindings), OTHER_ID) is None


def test_build_resolvers_none_when_no_binding_matches():
    assert resolve_channel_build_toolsets(_cfg(_binding()), "no-such-id") is None
    assert resolve_channel_build_toolsets({}, CODING_ID) is None
    assert resolve_channel_build_toolsets({"channel_toolsets": []}, CODING_ID) is None


def test_build_single_string_form():
    bindings = [{"id": CODING_ID, "build_toolsets": "coding"}]
    assert resolve_channel_build_toolsets(_cfg(bindings), CODING_ID) == ["coding"]


def test_build_parent_fallback_for_threads():
    got = resolve_channel_build_toolsets(_cfg(_binding()), "999thread", CODING_ID)
    assert got == ["web", "file", "terminal", "delegation"]


def test_build_max_turns_rejects_bool():
    bindings = [{"id": CODING_ID, "build_toolsets": ["file"], "build_max_turns": True}]
    assert resolve_channel_build_max_turns(_cfg(bindings), CODING_ID) is None


def test_build_dedup_preserves_order():
    bindings = [{"id": CODING_ID, "build_toolsets": ["web", "file", "web", "terminal"]}]
    assert resolve_channel_build_toolsets(_cfg(bindings), CODING_ID) == ["web", "file", "terminal"]
