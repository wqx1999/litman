# Brand assets

Current artwork, referenced by `README.md`:

| File | Use |
|---|---|
| `logo-hero.png`, `logo-hero-dark.png` | README banner (light / dark) |
| `logo.svg`, `logo-dark.svg` | full logo, vector |
| `wordmark.svg`, `wordmark-dark.svg` | wordmark alone |
| `mark.svg`, `mark-dark.svg` | mark alone |
| `icon.svg` | app / shortcut icon source |

## Do not delete: `logo1.png`, `logo2.png`, `logo2.svg`

Nothing in this repo links to them. They exist for PyPI.

`pyproject.toml` sets `readme = "README.md"`, so each release freezes a copy of
the README as its PyPI long description — permanently, per version. Those copies
embed images as `raw.githubusercontent.com/wqx1999/litman/main/assets/…`, and the
URL is pinned to **`main`**, not to the release tag. A reader opening
[litman 1.0.0](https://pypi.org/project/litman/1.0.0/) today fetches those two
paths from `main` as it stands today.

litman 1.0.0, 1.0.1 and 1.1.0 all shipped READMEs pointing at `logo1.png` and
`logo2.png` (1.0.0 at `logo2.svg`). Delete them and those three pages lose their
images, forever, no matter what the current README says.

So they stay, holding the current artwork: `logo1.png` = the square mark,
`logo2.png` = the banner. Replacing the *contents* is not only safe, it back-fills
the new brand into every published page. Renaming or deleting is not.

The same trap catches any future rename. Change what a file contains; never what
it is called.
