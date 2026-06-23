"""Tests for the coding-roles skill (Phase 2 #coding subagent dispatch).

Validates the SKILL.md is well-formed (parses under the same frontmatter
validator the skill manager uses) and that it encodes the Phase 2 safety
invariants: read-only by default, write only when build mode is armed, the
three roles, the structured-JSON result contract, and the read-the-target-
repo's-AGENTS.md rule. These are invariant assertions, not snapshot checks.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_MD = REPO_ROOT / "skills" / "coding-roles" / "SKILL.md"


@pytest.fixture(scope="module")
def skill_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def test_skill_md_exists():
    assert SKILL_MD.is_file(), f"missing {SKILL_MD}"


def test_frontmatter_valid_per_skill_manager(skill_text):
    # Reuse the real validator so the skill can never drift out of loadable shape.
    from tools.skill_manager_tool import _validate_frontmatter

    assert _validate_frontmatter(skill_text) is None


def test_declares_three_roles(skill_text):
    for role in ("Researcher", "Planner", "Coder"):
        assert role in skill_text, f"role {role} not documented"


def test_read_only_is_the_default(skill_text):
    # The default deliverable must be a plan, and write/terminal must be gated.
    assert "read-only" in skill_text.lower()
    assert "build mode" in skill_text.lower()
    assert "/build" in skill_text


def test_structured_json_contract_present(skill_text):
    for field in ('"status"', '"summary"', '"artifacts"', '"cost"'):
        assert field in skill_text, f"JSON contract missing {field}"
    for status in ("done", "blocked", "needs_input"):
        assert status in skill_text, f"terminal status {status} not documented"


def test_requires_reading_target_repo_conventions(skill_text):
    assert "AGENTS.md" in skill_text


def test_hub_and_spoke_not_mesh(skill_text):
    # Subagents must not free-chat with each other (deployment principle #5).
    lowered = skill_text.lower()
    assert "hub-and-spoke" in lowered
    assert "never" in lowered  # "subagents never talk to each other"


def test_no_self_merge_to_main(skill_text):
    lowered = skill_text.lower()
    assert "main" in lowered
    assert "diff" in lowered
