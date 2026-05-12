"""litman exception hierarchy.

User-facing errors derive from `LitmanError`; the CLI catches it at the top
level and prints a friendly stderr message. Anything not a subclass of
`LitmanError` indicates a bug and propagates as a normal Python traceback.
"""

from __future__ import annotations


class LitmanError(Exception):
    """Base class for all litman-specific errors."""


class LibraryNotFoundError(LitmanError):
    """No `lit-config.yaml` discoverable from the given path or env."""


class ParentNotFoundError(LitmanError):
    """`lit init` parent directory does not exist or is not a directory."""


class VaultExistsError(LitmanError):
    """`lit init` target vault path already exists and is non-empty."""


class ImporterError(LitmanError):
    """An importer (CrossRef / arXiv / PDF) failed to fetch or parse metadata."""


class IDError(LitmanError):
    """Cannot derive a canonical paper id from the given inputs."""


class AddError(LitmanError):
    """`lit add` failed to materialize the paper folder."""


class DuplicateDOIError(AddError):
    """`lit add` refused because the DOI is already registered in the vault.

    Subclasses ``AddError`` so any caller catching the broader error type still
    matches; tests and CLI rendering can pattern-match more specifically when
    they need to surface "this is a true duplicate, not a transient failure".
    """


class PaperNotFoundError(LitmanError):
    """No paper with the given id exists in the vault."""


class AmbiguousPaperIdError(LitmanError):
    """Partial id supplied to a resolve step matches multiple papers.

    Raised by ``litman.core.viewer.resolve_paper_id`` (used by ``lit open``).
    Carries ``query`` (original input) and ``candidates`` (the matching
    paper ids) so the CLI can render a candidate list and prompt the user
    for a more specific id, exiting non-zero.
    """

    def __init__(self, query: str, candidates: list[str]) -> None:
        self.query = query
        self.candidates = candidates
        super().__init__(
            f"Multiple papers match {query!r}: {', '.join(candidates)}."
        )


class ModifyError(LitmanError):
    """`lit modify` rejected an op: forbidden field, malformed key=value,
    or wrong operation kind for the field's type (e.g. --add-tag on a scalar).
    """


class TaxonomyError(LitmanError):
    """`lit taxonomy` rejected an op: unknown dict, fixed-enum write,
    missing source value, or rm refused due to outstanding references.
    """


class RenameError(LitmanError):
    """`lit rename` rejected an op: invalid new id, name collision,
    identical old/new, or attempting to rename a non-existent paper.
    """


class RmError(LitmanError):
    """`lit rm` rejected an op: invalid id, outstanding references in other
    papers' ref-list fields, or `[[id]]` wikilinks in notes that the user
    has not opted to cascade through.
    """


class TrashError(LitmanError):
    """`lit trash` rejected an op: missing source, ambiguous id, restore
    collision with an active paper, or empty/unknown entry lookup.
    """


class CodeError(LitmanError):
    """`lit code` rejected an op: invalid URL / repo-name, collision with an
    existing clone, missing paper for --paper binding, or ``git clone`` failure.
    """


class ConfigError(LitmanError):
    """`lit-config.yaml` could not be parsed or failed schema validation.

    Distinct from ``LibraryNotFoundError`` (file is missing entirely): here
    the file exists but is malformed YAML, has an unknown key, holds a value
    of the wrong type, or violates a field constraint.
    """


class SyncError(LitmanError):
    """`lit sync` rejected an op: rclone not installed, remote misconfigured,
    sync not yet set up in lit-config.yaml, or rclone returned a non-zero
    exit status (network down, auth failure, transfer error, etc.).
    """


class VaultRegistryError(LitmanError):
    """`lit vault` (M8) rejected an op against ``~/.config/litman/vaults.yaml``:
    invalid name shape, duplicate / unknown name, registry file malformed,
    or a path supplied to ``add`` does not point at a valid vault.

    Distinct from ``LibraryNotFoundError`` (which is about the *active*
    vault not being discoverable in a single command run): registry errors
    surface from the user-level registry layer, not from any single vault.
    """
