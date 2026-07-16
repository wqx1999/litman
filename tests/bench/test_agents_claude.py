"""Tests for the claude adapter's served-model read, against a real init line.

Why this file exists: ``_MODEL_FAMILY`` maps ``"claude-sonnet-4-6"`` on the
assumption that claude's ``system/init`` echoes the model id we asked for rather
than resolving it to a dated one (``claude-sonnet-4-6-20260514``-style). That
assumption was carried by nobody but a comment. If it had been wrong, check 6
would have compared a requested id against a dated served id, found no family for
the latter, and failed every claude run at the gate — the exact "looks like a
result, is actually an instrument fault" class this bench is built to refuse.

Provenance of ``claude-init-model.raw.jsonl``: the maintainer recorded a real
``claude -p --model claude-sonnet-4-6 --output-format stream-json --verbose`` init
line on this machine (2026-07-16) and reported its contents; the assumption holds
— the id comes back VERBATIM. The fixture pins that string. Everything else in the
line is redacted, because a real claude init event carries ``cwd``,
``memory_paths``, ``skills``, ``slash_commands``, ``plugins``, ``agents``,
``session_id`` and ``uuid`` — i.e. the recording user's home paths and private
skill/plugin names — and this repo is public. Same rule as the three cursor
recordings alongside it: keep the field under test, redact the rest.

NEVER spawns (M34 §3.5 hard boundary).
"""

from __future__ import annotations

from pathlib import Path

from harness.agents import family_of, get_adapter
from harness.agents.claude import ClaudeAdapter, parse_stream

STREAMS_DIR = Path(__file__).resolve().parent / "fixtures" / "agent-streams"


def _init_line() -> list[str]:
    return (STREAMS_DIR / "claude-init-model.raw.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()


def test_claude_reports_the_requested_id_verbatim() -> None:
    """The load-bearing fact: NOT resolved to a dated id."""
    result = parse_stream(_init_line())
    assert result.model_served == "claude-sonnet-4-6"


def test_the_served_string_is_the_one_the_family_table_maps() -> None:
    """Pins the table against the real string. If claude ever starts resolving ids,
    this fails here — in a unit test — instead of at the gate of a live run."""
    served = parse_stream(_init_line()).model_served
    assert family_of(served) == "claude-sonnet-4.6"
    # And it is exactly what we would have requested, so check 6's exact-match
    # branch carries claude without the table being involved at all.
    assert served == ClaudeAdapter().default_model


def test_an_init_without_a_model_leaves_served_none() -> None:
    """Absent -> None ("not observed"), never a guess from what we requested."""
    result = parse_stream(['{"type":"system","subtype":"init","session_id":"x"}'])
    assert result.model_served is None


def test_reading_the_served_model_disturbs_nothing_else() -> None:
    """model_served was added to a path with a live TRR/RA baseline. It must be
    purely additive: an init-only stream yields no skills, no calls, no usage."""
    result = parse_stream(_init_line())
    assert result.skills == []
    assert result.lit_calls == []
    assert result.tool_results == []
    assert result.usage == {}
    assert result.final_text == ""


def test_claude_capabilities_declare_the_served_model() -> None:
    assert get_adapter("claude").capabilities.served_model is True


def test_the_fixture_carries_no_home_paths_or_private_names() -> None:
    """This repo is public and the recording was made against a real home.

    Guards the sanitization itself: every field a claude init line carries besides
    the model is either absent or redacted. A future re-record that pastes the raw
    line in fails here rather than in a git history nobody can rewrite.
    """
    import json

    event = json.loads((STREAMS_DIR / "claude-init-model.raw.jsonl").read_text(encoding="utf-8"))
    leaky = ("cwd", "memory_paths", "skills", "slash_commands", "plugins", "agents",
             "session_id", "uuid")
    for field in leaky:
        if field in event:
            assert str(event[field]).startswith("<redacted:"), (
                f"{field!r} is not redacted: a real claude init line leaks the "
                f"user's home paths and private skill names into a PUBLIC repo"
            )
    # The one field that must survive verbatim.
    assert event["model"] == "claude-sonnet-4-6"
