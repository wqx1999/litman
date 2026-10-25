"""The two shipped skills stay short enough to survive every agent that loads them.

DeepSeek Harness shows a model only the first 500 characters of a skill's
description, so what the skill is for, the line that sends the other half of
the work to the other skill, and the most common triggers have to sit there.
The body is held to a ceiling a little above its slimmed length: Claude Code
keeps only the first few thousand tokens of a skill once it compacts, and
rules past that point are silently gone. What a workflow needs only on one
branch lives in a helper file next to SKILL.md, which the agent reads when
SKILL.md says "read `<name>.md`"; a pointer to a file that does not ship, or a
file nothing points to, would leave that branch untaught.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from litman.core.skill import bundled_skill_root, install_skill

_HEAD = 500

# What each description must say within its first _HEAD characters: the
# boundary sentence, the other skill's name, two core triggers, and SI.
_HEAD_MUST_HAVE = {
    "lit-library": ("NOT for", "lit-reading", "加到文献库", "导出 bib", "SI"),
    "lit-reading": ("NOT for", "lit-library", "这篇文章", "继续读", "SI"),
}

# Body length in characters (everything after the frontmatter): the slimmed
# length plus about 10 percent.
_BODY_LIMIT = {
    "lit-library": 24_400,
    "lit-reading": 27_500,
}

# The helper files each skill ships next to SKILL.md, and the phrase that
# sends the agent to one.
_HELPERS = {
    "lit-library": {"add-errors.md", "legacy-repair.md"},
    "lit-reading": {"health-check.md", "si.md"},
}
_POINTER = re.compile(r"read `([\w.-]+\.md)`")


def _split(name: str) -> tuple[str, str]:
    text = bundled_skill_root(name).joinpath("SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{name}: no frontmatter"
    _, frontmatter, body = text.split("---\n", 2)
    description = YAML(typ="safe").load(io.StringIO(frontmatter))["description"]
    assert isinstance(description, str) and description
    return description, body


@pytest.mark.parametrize("name", sorted(_HEAD_MUST_HAVE))
def test_description_head_says_what_the_skill_is_for(name: str) -> None:
    head = _split(name)[0][:_HEAD]
    missing = [word for word in _HEAD_MUST_HAVE[name] if word not in head]
    assert not missing, f"{name}: not in the first {_HEAD} characters: {missing}"


@pytest.mark.parametrize("name", sorted(_BODY_LIMIT))
def test_body_stays_within_its_ceiling(name: str) -> None:
    body = _split(name)[1]
    assert len(body) <= _BODY_LIMIT[name], (
        f"{name}: body is {len(body)} characters, ceiling {_BODY_LIMIT[name]}"
    )


@pytest.mark.parametrize("name", sorted(_HELPERS))
def test_every_helper_file_is_pointed_to_and_shipped(name: str) -> None:
    pointed = set(_POINTER.findall(_split(name)[1]))
    shipped = {
        child.name
        for child in bundled_skill_root(name).iterdir()
        if child.is_file() and child.name.endswith(".md") and child.name != "SKILL.md"
    }
    assert pointed == _HELPERS[name], f"{name}: SKILL.md points to {sorted(pointed)}"
    assert shipped == _HELPERS[name], f"{name}: ships {sorted(shipped)}"


@pytest.mark.parametrize("name", sorted(_HELPERS))
def test_install_copies_the_helper_files(name: str, tmp_path: Path) -> None:
    target = tmp_path / name
    install_skill(target=target, name=name)
    for helper in sorted(_HELPERS[name]):
        bundled = bundled_skill_root(name).joinpath(helper).read_bytes()
        assert (target / helper).read_bytes() == bundled, helper
