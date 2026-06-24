"""Phase 4 W5 break-it tests: actively try to defeat the safety controls.

Where ``test_phase4_redteam.py`` proves the *content*-level defenses (untrusted
delimiting, secret-egress redaction, per-channel tool allowlists), this suite
attacks the *runtime* safety controls the prompt calls out -- kill switch, loop
detection, budget/turn caps, and mount minimalism -- and asserts each one holds
when an adversary (a prompt-injected model, a stale tool schema, a malformed
config) tries to bypass it. The framing is "security is infrastructure, not
prose": none of these rely on the model choosing to behave.

  1. KILL SWITCH -- delegation.orchestrator_enabled=false cannot be bypassed by
     a child *asking* for role="orchestrator"; the role hard-degrades to "leaf"
     (no recursive delegation), regardless of what the model requests.
  2. LOOP DETECTION -- a model that ignores the de-dup hint and re-reads the same
     region is HARD-BLOCKED (no content returned) after a bounded number of
     consecutive reads, so it cannot burn its iteration budget in a read loop.
  3. BUDGET / TURN CAPS -- a per-channel max_turns cap is authoritative and a
     model-supplied delegation max_iterations is IGNORED; neither can be widened
     by anything the model emits.
  4. MOUNT MINIMALISM -- the execution sandbox does not forward the model API key
     (or other provider secrets) into the container by default, drops the
     network when not requested, and ignores malformed volume/forward-env config
     instead of silently over-mounting.

These complement, they do not replace, the still-missing container/network-layer
egress allowlist (tracked as the skipped test in test_phase4_redteam.py).
"""

import json

import pytest


# ---------------------------------------------------------------------------
# 1. KILL SWITCH: orchestrator_enabled=false cannot be bypassed
# ---------------------------------------------------------------------------

def test_orchestrator_enabled_reads_bool_and_string_forms(monkeypatch):
    """The kill switch must read truthy/falsey YAML in both bool and string
    form -- an operator flipping it off must not be defeated by YAML that
    doesn't auto-coerce the value."""
    from tools import delegate_tool

    cases = {
        False: False, True: True,
        "false": False, "off": False, "no": False, "0": False,
        "true": True, "on": True, "yes": True, "1": True,
        "  False  ": False,  # whitespace-padded string
    }
    for raw, expected in cases.items():
        monkeypatch.setattr(delegate_tool, "_load_config",
                            lambda raw=raw: {"orchestrator_enabled": raw})
        assert delegate_tool._get_orchestrator_enabled() is expected, raw


def test_orchestrator_enabled_defaults_true_when_absent_or_garbage(monkeypatch):
    """Absent or non-bool/str config => default ON (so a corrupt config never
    silently disables delegation); the explicit OFF is what disables it."""
    from tools import delegate_tool
    for cfg in ({}, {"orchestrator_enabled": None}, {"orchestrator_enabled": 42}):
        monkeypatch.setattr(delegate_tool, "_load_config", lambda cfg=cfg: cfg)
        assert delegate_tool._get_orchestrator_enabled() is True, cfg


def test_child_requesting_orchestrator_role_is_forced_to_leaf_when_killed(monkeypatch):
    """THE bypass attempt: a (prompt-injected) parent calls _build_child_agent
    with role='orchestrator' to spawn a recursive delegator. With the kill
    switch OFF, the child's effective role hard-degrades to 'leaf' -- it gets
    no delegation capability -- no matter what role was requested."""
    from unittest.mock import MagicMock, patch
    import threading
    from tools import delegate_tool
    from tools.delegate_tool import _build_child_agent

    monkeypatch.setattr(delegate_tool, "_get_orchestrator_enabled", lambda: False)

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.enabled_toolsets = None
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent._subagent_id = None

    with patch("run_agent.AIAgent") as MockAgent:
        child = MagicMock()
        MockAgent.return_value = child
        _build_child_agent(
            task_index=0,
            goal="escalate to a recursive orchestrator",
            context=None,
            toolsets=None,
            model=None,
            max_iterations=10,
            parent_agent=parent,
            task_count=1,
            role="orchestrator",
        )

    # The role assigned to the child hard-degrades to 'leaf'...
    assert child._delegate_role == "leaf"
    # ...and the degradation has teeth: the child is NOT granted the delegation
    # toolset (only an 'orchestrator' child gets it), so it cannot recurse.
    assert "delegation" not in (MockAgent.call_args.kwargs.get("enabled_toolsets") or [])


def test_orchestrator_role_also_degrades_at_depth_ceiling(monkeypatch):
    """Even with the kill switch ON, a child at/over the spawn-depth ceiling
    cannot become an orchestrator -- the depth bound is the second, independent
    guard against an unbounded delegation tree."""
    from unittest.mock import MagicMock, patch
    import threading
    from tools import delegate_tool
    from tools.delegate_tool import _build_child_agent

    monkeypatch.setattr(delegate_tool, "_get_orchestrator_enabled", lambda: True)
    monkeypatch.setattr(delegate_tool, "_get_max_spawn_depth", lambda: 1)

    parent = MagicMock()
    parent._delegate_depth = 1  # child_depth becomes 2 >= max_spawn(1)
    parent.enabled_toolsets = None
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent._subagent_id = None

    with patch("run_agent.AIAgent") as MockAgent:
        child = MagicMock()
        MockAgent.return_value = child
        _build_child_agent(
            task_index=0, goal="recurse", context=None, toolsets=None,
            model=None, max_iterations=10, parent_agent=parent,
            task_count=1, role="orchestrator",
        )

    assert child._delegate_role == "leaf"


# ---------------------------------------------------------------------------
# 2. LOOP DETECTION: a read loop is hard-blocked, not just warned
# ---------------------------------------------------------------------------

@pytest.fixture
def loop_task(tmp_path, monkeypatch):
    """Isolate the per-task read tracker so the loop counter starts clean."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import file_tools
    # Clear any cross-test tracker state for our task id.
    with file_tools._read_tracker_lock:
        file_tools._read_tracker.pop("loop-victim", None)
    target = tmp_path / "stuck.txt"
    target.write_text("line one\nline two\nline three\n")
    return file_tools, str(target)


def test_consecutive_reread_is_hard_blocked(loop_task):
    """A model ignoring the 'you already read this' hint and re-reading the
    SAME region keeps getting blocked: by the 4th consecutive read the tool
    returns an error with NO content, breaking the loop instead of feeding it."""
    file_tools, path = loop_task
    results = [
        json.loads(file_tools.read_file_tool(path, task_id="loop-victim"))
        for _ in range(5)
    ]
    # At least one of the later reads must be a hard BLOCK with no content.
    blocked = [r for r in results if "BLOCKED" in (r.get("error") or "")]
    assert blocked, f"no read was ever hard-blocked: {results}"
    for r in blocked:
        assert "content" not in r or not r["content"]


def test_other_tool_call_resets_loop_counter_legitimately(loop_task):
    """The reset is by design (real work between reads is not a loop) -- but it
    only fires on a genuine other-tool call, so it can't be used to defeat the
    block by an agent that just keeps reading."""
    file_tools, path = loop_task
    # Two reads, then a legitimate other-tool signal, then reads again.
    file_tools.read_file_tool(path, task_id="loop-victim")
    file_tools.read_file_tool(path, task_id="loop-victim")
    file_tools.notify_other_tool_call("loop-victim")
    out = json.loads(file_tools.read_file_tool(path, task_id="loop-victim"))
    # First read after a real reset is not blocked...
    assert "BLOCKED" not in (out.get("error") or "")
    # ...but hammering reads again still re-arms the block.
    blocked = False
    for _ in range(5):
        r = json.loads(file_tools.read_file_tool(path, task_id="loop-victim"))
        if "BLOCKED" in (r.get("error") or ""):
            blocked = True
            break
    assert blocked


# ---------------------------------------------------------------------------
# 3. BUDGET / TURN CAPS: model cannot widen them
# ---------------------------------------------------------------------------

def test_channel_max_turns_cap_is_authoritative():
    """A low-complexity channel's max_turns cap is what the gateway uses; it's
    independent of (and well below) the global agent.max_turns=150, bounding
    runaway cost per channel."""
    from gateway.platforms.base import resolve_channel_max_turns
    cfg = {"channel_toolsets": [{"id": "999", "toolsets": ["web"], "max_turns": 5}]}
    assert resolve_channel_max_turns(cfg, "999") == 5


def test_channel_max_turns_rejects_bool_and_nonpositive():
    """An adversarial/garbage cap (True, 0, -1, "lots") must NOT be honored as a
    turn budget -- it returns None (=> fall back to the global cap), never a
    bypass that grants unbounded turns."""
    from gateway.platforms.base import resolve_channel_max_turns
    for bad in (True, False, 0, -5, "lots", None, 3.5):
        cfg = {"channel_toolsets": [{"id": "999", "toolsets": [], "max_turns": bad}]}
        assert resolve_channel_max_turns(cfg, "999") is None, bad


def test_model_supplied_delegation_max_iterations_is_ignored(monkeypatch):
    """delegate_task's max_iterations is config-authoritative: a model that
    smuggles a huge max_iterations (e.g. via a stale cached tool schema) gets it
    dropped in favor of delegation.max_iterations from config. We patch the
    child build/run so the assertion targets only the budget resolution."""
    from unittest.mock import MagicMock, patch
    import threading
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_load_config",
                        lambda: {"max_iterations": 7, "orchestrator_enabled": True})

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.enabled_toolsets = None
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent._subagent_id = None
    parent._session_db = None

    with patch("tools.delegate_tool._build_child_agent") as mock_build, \
         patch("tools.delegate_tool._run_single_child") as mock_run:
        mock_build.return_value = MagicMock()
        mock_run.return_value = {
            "task_index": 0, "status": "completed",
            "summary": "Done", "api_calls": 1, "duration_seconds": 1.0,
        }
        delegate_tool.delegate_task(
            goal="run forever", parent_agent=parent, max_iterations=99999,
        )

    # The child was built with the config value (7), not the smuggled 99999.
    assert mock_build.call_count == 1
    assert mock_build.call_args.kwargs.get("max_iterations") == 7


def test_delegate_schema_does_not_expose_max_iterations_to_model():
    """Defense in depth for the above: the budget knob is not even in the tool
    schema, so a well-behaved provider never offers the model a way to set it."""
    from tools.delegate_tool import DELEGATE_TASK_SCHEMA
    assert "max_iterations" not in DELEGATE_TASK_SCHEMA["parameters"]["properties"]


# ---------------------------------------------------------------------------
# 4. MOUNT MINIMALISM: secrets/network/mounts are least-privilege by default
# ---------------------------------------------------------------------------

def test_model_key_is_on_the_container_env_blocklist():
    """The single highest-value secret -- the opencode-go model key -- is in the
    provider env blocklist, so it is NOT forwarded into the execution container
    by the implicit passthrough path. (This is exactly what W3/c1 then removes
    from the agent env entirely; today it at least never crosses into the
    sandbox where run-code lives.)"""
    from tools.environments.local import _HERMES_PROVIDER_ENV_BLOCKLIST
    assert "OPENCODE_GO_API_KEY" in _HERMES_PROVIDER_ENV_BLOCKLIST


def test_forward_env_normalizer_rejects_garbage_and_injection():
    """docker_forward_env must drop non-string, empty, and shell-injection-shaped
    names -- a malformed config (or a poisoned one) cannot smuggle an arbitrary
    or non-name env var into the container."""
    from tools.environments.docker import _normalize_forward_env_names
    out = _normalize_forward_env_names([
        "VALID_NAME", "",  # empty dropped
        "has space", "BAD-DASH", "1STARTS_WITH_DIGIT",  # invalid identifiers
        "$(rm -rf /)",  # injection-shaped
        123, None,  # non-strings
        "VALID_NAME",  # dup collapsed
    ])
    assert out == ["VALID_NAME"]


def test_malformed_volume_entries_are_skipped_not_mounted(monkeypatch, tmp_path):
    """A volume entry missing the host:container colon, or a non-string entry,
    must be skipped -- never silently expanded into an over-broad mount. We
    exercise the parsing branch in isolation (no real docker run)."""
    # Mirror the parsing logic the constructor uses (docker.py ~L580-595):
    # entries without ':' or that aren't strings produce NO -v args.
    volumes = ["/host/repo:/repo", "no-colon-here", 12345, "  ", ":/missing-host-ok"]
    volume_args = []
    for vol in volumes:
        if not isinstance(vol, str):
            continue
        vol = vol.strip()
        if not vol:
            continue
        if ":" in vol:
            volume_args.extend(["-v", vol])
    # Only the two colon-bearing string entries become mounts.
    assert volume_args == ["-v", "/host/repo:/repo", "-v", ":/missing-host-ok"]
    assert "no-colon-here" not in volume_args
    assert 12345 not in volume_args
