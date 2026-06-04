"""Generate a large mock vault for `lit graph` scale testing (M35 C1).

Builds a vault with ~N papers carrying projects / topics / methods / data /
code-clones, relation edges, a bibliographic head (year / authors / journal /
doi / status / type / priority for the click-to-open detail card), a handful of
corrupt-metadata papers, and a few dangling references, so the paper-centric
colour / cluster / focus view can be exercised at scale by hand:

    python tests/tools/make_mock_vault.py /tmp/mock_parent --papers 10000 --projects 40
    lit graph --library /tmp/mock_parent/literature_vault

This is a manual harness (B/C-group), NOT a pytest fixture — the deterministic
data-layer behaviour is covered by tests/core/test_graph_model.py. It exists so
the "colour-by-dimension + focus-into-a-value doesn't choke" claim (C1) can be
validated visually, and corrupt/invalid rendering checked at volume.
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

from litman.core.library import create_vault
from litman.core.taxonomy import update_user_dict_section

# Small pools for fake bibliographic heads so the click-to-open detail card has
# something to show at scale (year / authors / journal / doi / status / type /
# priority). Values stay within the fixed enums (checks.py:_FIXED_VALUES) so a
# --check render is not tripped by the mock.
_FAMILIES = [
    "Smith", "Lee", "Kim", "Wang", "Doe", "Roe", "Chen", "Patel",
    "Garcia", "Müller", "Sato", "Ivanov", "Nguyen", "Khan", "Rossi",
]
_GIVENS = [
    "John", "Jane", "Min", "Lin", "Soo", "Eun", "Bo", "Ji",
    "Alex", "Sam", "Wei", "Ana", "Omar", "Yuki", "Ravi",
]
_VENUES = [
    "Bioinformatics", "Nature", "NeurIPS", "ICML", "J. Chem. Inf. Model.",
    "Nat. Mach. Intell.", "PNAS", "JACS", "arXiv", "bioRxiv",
]
_STATUSES = ["inbox", "inbox", "inbox", "skim", "deep-read", "dropped"]
_TYPES = ["research", "research", "review", "benchmark", "dataset", ""]
_PRIORITIES = ["A", "B", "C", "", ""]


def _write_paper(vault: Path, pid: str, lines: list[str]) -> None:
    d = vault / "papers" / pid
    d.mkdir(parents=True, exist_ok=True)
    (d / "metadata.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (d / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF")


def build(parent: Path, n_papers: int, n_projects: int, seed: int) -> Path:
    rng = random.Random(seed)
    vault = create_vault(parent)

    projects = [f"project-{i:02d}" for i in range(n_projects)]
    topics = [f"topic-{i:02d}" for i in range(max(5, n_projects // 2))]
    methods = [f"method-{i:02d}" for i in range(max(4, n_projects // 4))]
    datasets = [f"data-{i:02d}" for i in range(max(3, n_projects // 6))]

    cfg_lines = ["library_name: literature_vault", "projects:"]
    for p in projects:
        cfg_lines.append(f"  {p}: /tmp/{p}")
    (vault / "lit-config.yaml").write_text("\n".join(cfg_lines) + "\n", encoding="utf-8")

    tax = vault / "TAXONOMY.md"
    tax.chmod(0o644)
    text = tax.read_text(encoding="utf-8")
    text = update_user_dict_section(text, "topics", topics)
    text = update_user_dict_section(text, "methods", methods)
    text = update_user_dict_section(text, "data", datasets)
    tax.write_text(text)

    ids = [f"p{i:06d}" for i in range(n_papers)]
    n_corrupt = max(1, n_papers // 500)
    corrupt = set(rng.sample(ids, n_corrupt))

    reverse = {"related": "related", "extends": "extended-by", "contradicts": "contradicted-by"}

    # Phase 1: decide each non-corrupt paper's dimension fields.
    fields_by_pid: dict[str, list[tuple[str, list[str]]]] = {}
    for i, pid in enumerate(ids):
        if pid in corrupt:
            continue
        flds: list[tuple[str, list[str]]] = []
        # 80% belong to 1-2 projects; 20% unassigned (orphan papers are info).
        if rng.random() < 0.8:
            flds.append(("projects", rng.sample(projects, rng.choice([1, 1, 2]))))
        # 1-2 topics (some papers are topic-pivots across topic clusters).
        flds.append(("topics", rng.sample(topics, rng.choice([1, 1, 2]))))
        # ~60% a method, ~40% a dataset (so those colour modes are populated).
        if rng.random() < 0.6:
            flds.append(("methods", [rng.choice(methods)]))
        if rng.random() < 0.4:
            flds.append(("data", [rng.choice(datasets)]))
        # ~10% reference a code repo (dangling — no codes/ dir written — so the
        # drift ring is exercised at scale).
        if rng.random() < 0.1:
            flds.append(("code-clones", [f"repo-{i}"]))
        fields_by_pid[pid] = flds

    # Phase 2: relation edges. ~25% of papers relate to an earlier one; MOST get
    # a valid bidirectional pairing (reverse field written back on the target),
    # while a few omit the reverse or point at a nonexistent id so the invalid /
    # broken-pairing red path is exercised without drowning the network in red.
    reverse_by_pid: dict[str, list[tuple[str, str]]] = {}
    for i, pid in enumerate(ids):
        if pid in corrupt or i == 0 or rng.random() >= 0.25:
            continue
        field = rng.choice(["related", "extends", "contradicts"])
        roll = rng.random()
        if roll < 0.05:
            target = "p999999"  # nonexistent -> missing-endpoint (invalid)
        else:
            earlier = [t for t in ids[:i] if t not in corrupt]
            if not earlier:
                continue
            target = rng.choice(earlier)
        fields_by_pid[pid].append((field, [target]))
        # 85% of real-target relations get a valid reverse pairing.
        if roll >= 0.05 and rng.random() < 0.85:
            reverse_by_pid.setdefault(target, []).append((reverse[field], pid))

    # Write corrupt papers (unparseable YAML) then the assembled papers.
    for pid in corrupt:
        d = vault / "papers" / pid
        d.mkdir(parents=True, exist_ok=True)
        (d / "metadata.yaml").write_text("{broken: yaml: here:", encoding="utf-8")
        (d / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF")

    for i, pid in enumerate(ids):
        if pid in corrupt:
            continue
        merged: dict[str, list[str]] = {}
        pairs = fields_by_pid[pid] + [
            (f, [src]) for f, src in reverse_by_pid.get(pid, [])
        ]
        for field, vals in pairs:
            bucket = merged.setdefault(field, [])
            for v in vals:
                if v not in bucket:
                    bucket.append(v)
        # Bibliographic head for the detail card.
        n_auth = rng.choice([1, 2, 2, 3, 4, 6, 9])
        authors = [
            f"{rng.choice(_FAMILIES)}, {rng.choice(_GIVENS)}" for _ in range(n_auth)
        ]
        lines = [
            f"id: {pid}",
            f"title: Mock paper {i}",
            f"year: {rng.randint(2015, 2025)}",
            "authors:",
        ]
        lines += [f"  - {a}" for a in authors]
        lines.append(f"journal: {rng.choice(_VENUES)}")
        lines.append(f"doi: 10.1234/mock.{i:06d}")
        lines.append(f"status: {rng.choice(_STATUSES)}")
        paper_type = rng.choice(_TYPES)
        if paper_type:
            lines.append(f"type: {paper_type}")
        priority = rng.choice(_PRIORITIES)
        if priority:
            lines.append(f"priority: {priority}")
        for field, vals in merged.items():
            lines.append(f"{field}:")
            lines += [f"  - {v}" for v in vals]
        _write_paper(vault, pid, lines)

    return vault


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a mock vault for lit graph scale testing.")
    ap.add_argument("parent", type=Path, help="Parent dir; literature_vault/ is created inside.")
    ap.add_argument("--papers", type=int, default=10000)
    ap.add_argument("--projects", type=int, default=40)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    vault = build(args.parent, args.papers, args.projects, args.seed)
    print(f"Built mock vault at {vault}")
    print(f"  lit graph --library {vault}")


if __name__ == "__main__":
    main()
