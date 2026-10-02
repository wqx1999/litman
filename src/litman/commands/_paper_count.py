"""How the cascade commands say how many papers they touch.

Five commands rewrite many papers at once — ``lit taxonomy rename``, ``merge``
and ``rm``, ``lit project rename`` and ``rm`` — and they write by folder
(``core/ripple``). A cloud-sync conflict leaves two folders declaring one id,
so a folder count is not a paper count: the confirmation, listing ids, said 1
while the result, counting files, said 2. Both sides now count papers, and
name the folders only when the two numbers part — which is also the one place
a user learns the conflicted copy is there.

The id lists these take come from ``find_referencing_papers`` over a
per-folder paper list, so a conflict pair appears in them twice; that
repetition is the only signal used here.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from rich.markup import escape


def plural_papers(n: int) -> str:
    return f"{n} paper{'s' if n != 1 else ''}"


def duplicate_count(ids: Sequence[str]) -> int:
    """How many entries of ``ids`` repeat one already seen."""
    return len(ids) - len(set(ids))


def paper_count(n_folders: int, n_duplicates: int) -> str:
    """``N papers``, with the folder count appended when it differs.

    ``n_folders`` is what was (or will be) written; ``n_duplicates`` is how
    many of those folders hold a paper another one already holds.
    """
    n_papers = n_folders - n_duplicates
    text = f"[bold]{plural_papers(n_papers)}[/]"
    if n_duplicates > 0 and n_papers > 0:
        copies = (
            "1 is a conflicted copy"
            if n_duplicates == 1
            else f"{n_duplicates} are conflicted copies"
        )
        text += f" ({n_folders} folders — {copies})"
    return text


def paper_id_lines(ids: Sequence[str], limit: int = 10) -> list[str]:
    """The confirmation's bullet list: each id once, in order.

    An id held by more than one folder says so, so the reader can tell which
    paper the extra folder in the count belongs to.
    """
    folders = Counter(ids)
    unique = list(dict.fromkeys(ids))
    lines = []
    for pid in unique[:limit]:
        line = f"  - {escape(pid)}"
        if folders[pid] > 1:
            line += f" [dim]({folders[pid]} folders)[/]"
        lines.append(line)
    if len(unique) > limit:
        lines.append(f"  ... and {len(unique) - limit} more")
    return lines
