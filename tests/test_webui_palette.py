"""Every accent shade the web UI's source names is a colour its theme defines.

Tailwind v4 emits no CSS for a colour the theme does not define, and nothing
warns: `ring-accent-400` simply produced no rule, so focus rings fell back to
the text colour (near-black in light mode, near-white in dark). The theme lives
in `frontend/src/index.css`, once in `@theme` and again in `.dark`; a shade
counts as defined only when both blocks set it, so each theme has its own value.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_DEFINITION = re.compile(r"--color-accent-(\d+)\s*:")
_USE = re.compile(r"\baccent-(\d+)\b")
_SEPARATOR = re.compile(r"""[\s'"`{}]""")


def _rule_body(css: str, header: str) -> str:
    """The body of the rule whose header is exactly `header` at a line start."""
    css = _COMMENT.sub("", css)
    m = re.search(r"(?m)^" + re.escape(header) + r"\s*\{", css)
    if m is None:
        return ""
    depth = 1
    for i in range(m.end(), len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[m.end() : i]
    return css[m.end() :]


def defined_accent_shades(css: str) -> set[str]:
    """Shades defined in both the light theme and the `.dark` override."""
    light = set(_DEFINITION.findall(_rule_body(css, "@theme")))
    dark = set(_DEFINITION.findall(_rule_body(css, ".dark")))
    return light & dark


def _token(line: str, start: int, end: int) -> str:
    lo, hi = start, end
    while lo > 0 and not _SEPARATOR.match(line[lo - 1]):
        lo -= 1
    while hi < len(line) and not _SEPARATOR.match(line[hi]):
        hi += 1
    return line[lo:hi]


def undefined_accent_uses(text: str, defined: set[str]) -> list[tuple[int, str]]:
    """(line number, class) for every accent shade in `text` outside `defined`.

    Comments count: Tailwind scans them too.
    """
    out: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in _USE.finditer(line):
            if m.group(1) not in defined:
                out.append((lineno, _token(line, m.start(), m.end())))
    return out


def _sources(root: Path) -> list[Path]:
    src = root / "frontend" / "src"
    return sorted(p for pattern in ("*.ts", "*.tsx") for p in src.rglob(pattern))


def stray_accent_uses(root: Path) -> list[str]:
    """`path:line: class` for every undefined accent shade under `root`."""
    css = (root / "frontend" / "src" / "index.css").read_text(encoding="utf-8")
    defined = defined_accent_shades(css)
    out: list[str] = []
    for path in _sources(root):
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        out += [f"{rel}:{n}: {tok}" for n, tok in undefined_accent_uses(text, defined)]
    return out


def test_every_accent_shade_the_ui_names_is_defined() -> None:
    css = (_ROOT / "frontend" / "src" / "index.css").read_text(encoding="utf-8")
    defined = defined_accent_shades(css)
    # Control: the shades the UI leans on were read from both blocks, and the
    # scan saw accent classes at all, so an empty result means something.
    assert {"200", "500", "700"} <= defined, defined
    seen = sum(len(_USE.findall(p.read_text(encoding="utf-8"))) for p in _sources(_ROOT))
    assert seen > 0, "no accent class found under frontend/src"
    stray = stray_accent_uses(_ROOT)
    assert not stray, (
        f"accent shades index.css does not define in both @theme and .dark "
        f"(defined: {sorted(defined, key=int)}); Tailwind emits nothing for them:\n  "
        + "\n  ".join(stray)
    )


_CSS = """\
@theme {
  /* --color-accent-300: a comment, not a definition */
  --color-accent-400: #3399ff;
  --color-accent-500: #007aff;
}
.dark {
  --color-accent-500: #0a84ff; /* focus ring */
}
.dark .bg-white {
  background-color: var(--color-stone-50);
}
"""


def test_a_shade_counts_only_when_both_themes_define_it() -> None:
    assert defined_accent_shades(_CSS) == {"500"}


def test_the_scanner_names_each_stray_shade_and_its_line() -> None:
    tsx = (
        "const a = 'focus:ring-1 focus:ring-accent-500'\n"
        "const b = `rounded ${open ? 'ring-1 ring-accent-400' : ''}`\n"
        "// a comment naming accent-300 still reaches Tailwind\n"
        "const c = 'text-accent-500/80 border-accent-50'\n"
    )
    assert undefined_accent_uses(tsx, {"500"}) == [
        (2, "ring-accent-400"),
        (3, "accent-300"),
        (4, "border-accent-50"),
    ]


def test_a_stray_shade_in_a_source_tree_is_reported(tmp_path: Path) -> None:
    src = tmp_path / "frontend" / "src"
    (src / "ui").mkdir(parents=True)
    (src / "index.css").write_text(_CSS, encoding="utf-8")
    (src / "ui" / "Field.tsx").write_text(
        "export const cls =\n  'focus:outline-none focus:ring-1 focus:ring-accent-400'\n",
        encoding="utf-8",
    )
    (src / "util.ts").write_text("export const ok = 'ring-accent-500'\n", encoding="utf-8")
    (src / "notes.md").write_text("accent-900 outside .ts/.tsx\n", encoding="utf-8")
    assert stray_accent_uses(tmp_path) == ["frontend/src/ui/Field.tsx:2: focus:ring-accent-400"]
