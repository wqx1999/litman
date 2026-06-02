"""Phase D — scenario card loader + handoff discipline.

Loads the transcribed cards from ``tests/bench/scenarios/*.yaml`` into
:class:`Card` objects, and enforces the §0 handoff discipline in code (not by
convention): :func:`executor_view` returns ONLY the ``intent`` (verbatim) +
``fixtures`` paths — it *withholds* ``precondition`` / ``expected_end_state`` /
``auto_fail`` / ``routing_label``. Giving any of those to the executor would
leak the answer and void the routing / execution test (M34 §3 Phase D).

The loader yields the FULL 33-card corpus. Five non-sandbox cards carry an
explicit ``skip_reason`` (4 ``needs_network`` — E1, J1, J1-corrupt, J2; 1
``needs_pty`` — D2-pty); CI marks those ``pytest.mark.skip``. The other 28 are
sandbox-runnable *definitions* — the deterministic core ships their definitions
+ the checker; actually executing them needs the deferred Phase E executor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

BENCH_DIR = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = BENCH_DIR / "scenarios"
FIXTURES_PDFS_DIR = BENCH_DIR / "fixtures" / "pdfs"

# Fields the executor must NEVER see (handoff discipline, scenarios §0).
WITHHELD_FIELDS: frozenset[str] = frozenset(
    {"precondition", "expected_end_state", "auto_fail", "routing_label"}
)


@dataclass(frozen=True)
class Handoff:
    """Exactly what the executor agent is handed: intent + fixture paths."""

    intent: str
    fixtures: list[Path]


@dataclass
class Card:
    """One transcribed scenario card.

    Mirrors the scenarios-proposal schema verbatim. ``routing_card`` (the I-*
    cards) carry ``cases`` / ``in_scope_skills`` instead of an execution
    end-state; those fields default empty for ordinary cards.
    """

    id: str
    title: str = ""
    layer: str = ""
    source: str = ""
    fixtures: list[int] = field(default_factory=list)
    needs_pty: bool = False
    needs_network: bool = False
    invariants: list[int] = field(default_factory=list)
    routing_label: Any = None
    precondition: str = ""
    seed: str | None = None
    intent: str = ""
    expected_end_state: list[Any] = field(default_factory=list)
    auto_fail: list[Any] = field(default_factory=list)
    adherence_flags: list[Any] = field(default_factory=list)
    notes: str = ""
    skip_reason: str | None = None
    # Routing-card extras.
    in_scope_skills: list[str] = field(default_factory=list)
    cases: list[Any] = field(default_factory=list)

    @property
    def is_routing(self) -> bool:
        return self.layer == "routing"


def _load_yaml_doc(path: Path) -> dict[str, Any]:
    yaml = YAML(typ="safe")
    data = yaml.load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: card YAML must be a mapping, got {type(data).__name__}")
    return data


def load_card(path: Path) -> Card:
    """Load + validate one card YAML into a :class:`Card`.

    Enforces the minimum identity contract: ``id`` is required, and a
    non-routing card must carry an ``intent`` (the only thing the executor
    sees). A routing card must carry ``cases``.
    """
    data = _load_yaml_doc(Path(path))
    if "id" not in data or not str(data["id"]).strip():
        raise ValueError(f"{path}: card is missing a non-empty 'id'")

    card = Card(
        id=str(data["id"]),
        title=str(data.get("title", "")),
        layer=str(data.get("layer", "")),
        source=str(data.get("source", "")),
        fixtures=list(data.get("fixtures") or []),
        needs_pty=bool(data.get("needs_pty", False)),
        needs_network=bool(data.get("needs_network", False)),
        invariants=list(data.get("invariants") or []),
        routing_label=data.get("routing_label"),
        precondition=str(data.get("precondition", "")),
        seed=data.get("seed"),
        intent=str(data.get("intent", "")),
        expected_end_state=list(data.get("expected_end_state") or []),
        auto_fail=list(data.get("auto_fail") or []),
        adherence_flags=list(data.get("adherence_flags") or []),
        notes=str(data.get("notes", "")),
        skip_reason=data.get("skip_reason"),
        in_scope_skills=list(data.get("in_scope_skills") or []),
        cases=list(data.get("cases") or []),
    )

    if card.is_routing:
        if not card.cases:
            raise ValueError(f"{path}: routing card {card.id!r} has no 'cases'")
    else:
        if not card.intent.strip():
            raise ValueError(
                f"{path}: card {card.id!r} has no 'intent' "
                "(the only field the executor sees)"
            )
    return card


def load_all_cards(scenarios_dir: Path = SCENARIOS_DIR) -> list[Card]:
    """Load every ``scenarios/*.yaml`` card, sorted by id for stable iteration."""
    cards = [load_card(p) for p in sorted(Path(scenarios_dir).glob("*.yaml"))]
    return sorted(cards, key=lambda c: c.id)


def executor_view(card: Card, fixtures_dir: Path = FIXTURES_PDFS_DIR) -> Handoff:
    """Project a card to the ONLY thing the executor agent may receive.

    Returns the verbatim ``intent`` + resolved fixture PDF paths. Every field in
    :data:`WITHHELD_FIELDS` (precondition / expected_end_state / auto_fail /
    routing_label) is structurally excluded — there is no code path that hands
    those to the executor (scenarios §0).
    """
    fixture_paths = [Path(fixtures_dir) / f"{fid}.pdf" for fid in card.fixtures]
    return Handoff(intent=card.intent, fixtures=fixture_paths)
