"""Generate a large mock vault for `lit graph` scale testing (M35 C1).

Builds a vault with ~N papers spread across ~M projects, with code-clones,
relation edges, a handful of corrupt-metadata papers, and a few dangling
references, so the hierarchical aggregate/drilldown view can be exercised at
scale by hand:

    python tests/tools/make_mock_vault.py /tmp/mock_parent --papers 10000 --projects 40
    lit graph --library /tmp/mock_parent/literature_vault

This is a manual harness (B/C-group), NOT a pytest fixture — the deterministic
data-layer behaviour is covered by tests/core/test_graph_model.py. It exists so
the "default project-level aggregation + click-to-drill doesn't choke" claim
(C1) can be validated visually, and corrupt/invalid rendering checked at volume.
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

from litman.core.library import create_vault
from litman.core.taxonomy import update_user_dict_section


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

    cfg_lines = ["library_name: literature_vault", "projects:"]
    for p in projects:
        cfg_lines.append(f"  {p}: /tmp/{p}")
    (vault / "lit-config.yaml").write_text("\n".join(cfg_lines) + "\n", encoding="utf-8")

    tax = vault / "TAXONOMY.md"
    tax.chmod(0o644)
    tax.write_text(update_user_dict_section(tax.read_text(encoding="utf-8"), "topics", topics))

    ids = [f"p{i:06d}" for i in range(n_papers)]
    n_corrupt = max(1, n_papers // 500)
    corrupt = set(rng.sample(ids, n_corrupt))

    for i, pid in enumerate(ids):
        if pid in corrupt:
            d = vault / "papers" / pid
            d.mkdir(parents=True, exist_ok=True)
            (d / "metadata.yaml").write_text("{broken: yaml: here:", encoding="utf-8")
            (d / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
            continue

        # 80% belong to 1-2 projects; 20% unassigned (orphan nodes are info).
        membership: list[str] = []
        if rng.random() < 0.8:
            k = rng.choice([1, 1, 2])
            membership = rng.sample(projects, k)

        lines = [
            f"id: {pid}",
            f"title: Mock paper {i} on {rng.choice(topics)}",
            "status: inbox",
        ]
        if membership:
            lines.append("projects:")
            lines += [f"  - {m}" for m in membership]
        lines.append("topics:")
        lines.append(f"  - {rng.choice(topics)}")

        # ~10% reference a code repo (mostly dangling — no codes/ dir written —
        # so the invalid-edge red path is exercised at scale).
        if rng.random() < 0.1:
            lines.append("code-clones:")
            lines.append(f"  - repo-{i}")

        # ~15% relate to an earlier paper (forward fields only).
        if i > 0 and rng.random() < 0.15:
            target = ids[rng.randint(0, i - 1)]
            field = rng.choice(["related", "extends", "contradicts"])
            lines.append(f"{field}:")
            lines.append(f"  - {target}")

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
