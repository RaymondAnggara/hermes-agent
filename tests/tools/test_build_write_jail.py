"""Phase 2 build-mode write jail (#coding !build).

A gateway build session registers a per-session ``write_jail_root`` task
override; file tools must then HARD-deny (not warn) any write resolving outside
that root, while leaving non-build sessions (no override) untouched. This is the
infrastructure that makes "write only inside the scratch repo" a boundary, not a
prompt instruction. These assert the invariant, not a snapshot.
"""

from pathlib import Path

import pytest

from tools.file_tools import _check_write_jail, write_file_tool
from tools.terminal_tool import register_task_env_overrides, clear_task_env_overrides


@pytest.fixture
def jail(tmp_path):
    """Register a write jail at tmp_path/repo for a unique task id; clean up."""
    root = tmp_path / "repo"
    root.mkdir()
    task_id = "test-build-jail"
    register_task_env_overrides(task_id, {"write_jail_root": str(root)})
    try:
        yield task_id, root
    finally:
        clear_task_env_overrides(task_id)


def test_no_jail_means_no_restriction():
    # A session with no override is not a build session — jail check is inert.
    assert _check_write_jail("/tmp/anything.txt", "no-such-task") is None


def test_write_inside_jail_is_allowed(jail):
    task_id, root = jail
    assert _check_write_jail(str(root / "src" / "main.py"), task_id) is None


def test_write_outside_jail_is_denied(jail):
    task_id, root = jail
    err = _check_write_jail(str(root.parent / "escape.txt"), task_id)
    assert err is not None
    assert "outside the build workspace" in err


def test_home_secret_path_denied_even_with_jail(jail):
    # Absolute traversal to a sensitive home path is outside the jail -> denied.
    task_id, root = jail
    assert _check_write_jail("~/.hermes/.env", task_id) is not None
    assert _check_write_jail("~/.ssh/authorized_keys", task_id) is not None


def test_write_file_tool_denies_outside_jail_before_touching_fs(jail):
    # The guard runs before any filesystem op, so an outside write is refused
    # without needing a terminal backend.
    task_id, root = jail
    out = write_file_tool(str(root.parent / "escape.txt"), "x", task_id=task_id)
    assert "outside the build workspace" in out


def test_relative_path_resolves_under_jail_cwd(jail):
    # With cwd registered to the jail root, a relative path stays inside it.
    task_id, root = jail
    register_task_env_overrides(
        task_id, {"write_jail_root": str(root), "cwd": str(root)}
    )
    assert _check_write_jail("notes.md", task_id) is None
