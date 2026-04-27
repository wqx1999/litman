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
