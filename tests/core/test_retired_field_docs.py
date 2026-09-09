"""AC-8: the shipped prose never teaches the retired paper-level `priority`.

Both SKILL.md files hardcode the CLI's capability boundary in prose — what it
can retrieve, which fields the INDEX projection carries, what the write surface
is, and worked examples — and the three user docs do the same for humans. None
of that is generated, so retiring a field (ADR-025) leaves stale instructions
that read as authoritative. An agent following them would emit a command the
CLI now refuses.

The gate matches the RETIRED SHAPES, not the word "priority": the word itself
is still legitimate everywhere — as the `priority-<project>` field, as the
`--priority` filter flag, and as ordinary English ("resolves ... in priority
order" in 3-concepts.md §vault resolution). Matching on shapes is what keeps
this from becoming a test nobody can satisfy.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

_FILES = (
    "src/litman/skills/lit-library/SKILL.md",
    "src/litman/skills/lit-reading/SKILL.md",
    "docs/3-concepts.md",
    "docs/4-commands.md",
    "docs/5-tutorial.md",
)

# Each pattern is a shape the retired field had and its replacement cannot.
_RETIRED_SHAPES: tuple[tuple[str, str, str], ...] = (
    (
        "set-priority",
        r"--set\s+priority=",
        "the retired write form; the grade is set with --set priority-<project>=",
    ),
    (
        "yaml-field",
        r"(?m)^\s*priority:\s",
        "a metadata.yaml sample still carrying the paper-level field",
    ),
    (
        "taxonomy-section",
        r"(?m)^##\s+priority\b",
        "a TAXONOMY.md sample still carrying the `## priority` section, which "
        "`lit init` no longer writes",
    ),
    (
        "field-table-row",
        r"\|\s*`priority`\s*\|",
        "a table row naming `priority` as a field of its own",
    ),
)


@pytest.mark.parametrize("relpath", _FILES)
@pytest.mark.parametrize(
    "name,pattern,why",
    _RETIRED_SHAPES,
    ids=lambda v: v if isinstance(v, str) and " " not in v else "",
)
def test_no_retired_priority_shape(
    relpath: str, name: str, pattern: str, why: str
) -> None:
    text = (_ROOT / relpath).read_text(encoding="utf-8")
    hits = [m.group(0) for m in re.finditer(pattern, text)]
    assert not hits, f"{relpath} still shows {name} ({why}): {hits}"


def test_the_gate_would_actually_catch_a_regression() -> None:
    """Control. Every pattern above must match the prose litman used to ship,
    or the parametrized tests pass by matching nothing at all."""
    was_shipped = (
        "| `priority` | string enum or null | null | `A`, `B`, `C`. |\n"
        "lit modify --set priority=A\n"
        "priority: A\n"
        "## priority (fixed enum, not extensible)\n"
    )
    for name, pattern, _why in _RETIRED_SHAPES:
        assert re.search(pattern, was_shipped), name


def test_the_replacement_shape_is_documented() -> None:
    """The docs must not merely be silent about the grade — the per-project
    form has to be taught somewhere in each user-facing doc."""
    for relpath in _FILES:
        text = (_ROOT / relpath).read_text(encoding="utf-8")
        assert "priority-" in text, relpath
