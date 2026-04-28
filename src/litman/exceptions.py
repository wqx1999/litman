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


class PaperNotFoundError(LitmanError):
    """No paper with the given id exists in the vault."""


class ModifyError(LitmanError):
    """`lit modify` rejected an op: forbidden field, malformed key=value,
    or wrong operation kind for the field's type (e.g. --add-tag on a scalar).
    """


class TaxonomyError(LitmanError):
    """`lit taxonomy` rejected an op: unknown dict, fixed-enum write,
    missing source value, or rm refused due to outstanding references.
    """
