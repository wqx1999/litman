"""litman exception hierarchy.

User-facing errors derive from `LitmanError`; the CLI catches it at the top
level and prints a friendly stderr message. Anything not a subclass of
`LitmanError` indicates a bug and propagates as a normal Python traceback.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ruamel.yaml.error import YAMLError


class LitmanError(Exception):
    """Base class for all litman-specific errors.

    ``code`` names the failure for the GUI, which shows a short line picked
    by it instead of the message; ``params`` fill that line: names, ids,
    paths inside the library, never an absolute path. With no code the GUI
    shows the line of the action that failed. :func:`coded` sets them where
    the error is raised; a class whose every instance means one thing sets
    them on the class.
    """

    code: str | None = None
    params: Mapping[str, str] = MappingProxyType({})


def coded[E: LitmanError](error: E, code: str, **params: str) -> E:
    """``error`` with its GUI ``code`` and ``params`` set, for ``raise coded(...)``."""
    error.code = code
    error.params = MappingProxyType(dict(params))
    return error


class LibraryNotFoundError(LitmanError):
    """No `lit-config.yaml` discoverable from the given path or env."""


class ParentNotFoundError(LitmanError):
    """`lit init` parent directory does not exist or is not a directory."""

    code = "parent-missing"


class VaultExistsError(LitmanError):
    """`lit init` target vault path already exists and is non-empty."""

    code = "target-exists"


class UndecodablePathError(LitmanError):
    """`lit init` target vault path is not UTF-8 text (see core/undecodable.py)."""


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

    code = "paper-not-found"


class CorruptMetadataError(LitmanError):
    """A metadata.yaml / repo-meta.yaml / sidecar exists but cannot be read
    or parsed as YAML.

    Raised by ``litman.core.document.load_yaml_or_raise`` so a single corrupt
    or unreadable file surfaces as a friendly, path-naming error at the CLI
    top level instead of a raw ``YAMLError`` / ``OSError`` traceback. The
    distinction that motivates a dedicated type: write commands
    (``lit rename`` / ``lit modify`` / ``lit taxonomy`` / ``lit rm`` /
    ``lit code``) round-trip-load *other* papers reached through reverse
    references, so an unrelated corrupt file must not crash the operation
    on a paper the user never touched.

    Distinct from ``ConfigError`` (which is about ``lit-config.yaml``).

    Its GUI code is ``file-unreadable``; the server adds the file's path
    inside the library, which only it knows.
    """

    code = "file-unreadable"

    def __init__(self, path: object, cause: Exception | None = None) -> None:
        self.path = path
        if isinstance(cause, MalformedMetadataError):
            super().__init__(f"Cannot use {path}: {cause}, then retry.")
            return
        detail = f" ({cause})" if cause is not None else ""
        super().__init__(
            f"Cannot read YAML file {path}{detail}. "
            "Fix the file (invalid YAML or non-UTF-8 encoding), then retry."
        )


class MalformedMetadataError(YAMLError):
    """A metadata.yaml parses, but one of its list fields holds something else.

    ``topics: peptide`` is a hand edit that dropped the list. Read as it
    stands, a string iterates as its characters: litman would file seven
    one-letter topics and write them back on the next ``--add-tag``. So the
    file is refused, as one whose YAML does not parse is, and the person who
    edited it decides what it should say.

    A ``YAMLError`` on purpose: every reader of a paper's metadata already
    treats that as "this file is broken" — ``list_papers`` leaves the paper
    out, ``load_yaml_or_raise`` turns it into :class:`CorruptMetadataError` —
    which is exactly the handling wanted, with no new except clause to miss.
    So it is not a :class:`LitmanError`, yet never reaches the CLI as itself.
    """

    def __init__(self, field: str, value: object) -> None:
        self.field = field
        if isinstance(value, str) and value:
            item = "'" + value.replace("'", "''") + "'"
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            item = str(value)
        else:
            item = "..."
        self.example = f"{field}: [{item}]"
        super().__init__(f"{field!r} is not a list; write it as `{self.example}`")


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


class TaxonomySimilarError(TaxonomyError):
    """`lit taxonomy add` refused a new value that looks like one already in
    the dict (or like another new value of the same call).

    Carries ``similar``: each refused value mapped to its ``(matching value,
    reason)`` matches, closest first, so the web UI can name the match
    without parsing the message. The message lists the closest match
    per refused value, then how to proceed.
    """

    def __init__(self, similar: dict[str, list[tuple[str, str]]]) -> None:
        self.similar = similar
        lines = []
        for value, matches in similar.items():
            closest, reason = matches[0]
            more = f" (+{len(matches) - 1} more)" if len(matches) > 1 else ""
            lines.append(f"{value!r} looks like {closest!r} ({reason}){more}.")
        lines.append("Use the matching value, or add --force to register it anyway.")
        super().__init__("\n".join(lines))


class UnreadableTaxonomyError(LitmanError):
    """TAXONOMY.md cannot be read: it is missing, unreadable, or not UTF-8.

    Raised by :func:`litman.core.taxonomy.read_taxonomy_text`, the reader
    every command, check and web UI route goes through, so each names the
    file and the way out instead of stopping on a raw ``UnicodeDecodeError``
    or ``OSError``. ``reason`` is the short cause (``line 14 is not UTF-8``,
    ``Permission denied``) that health-check's finding repeats, and
    ``not_utf8`` picks its hint. litman never guesses another encoding: the
    same bytes decode to different text under each guess, and the person who
    saved the file is the one who knows which it was.

    Not a :class:`TaxonomyError`: the web UI turns those into a 400 for a bad
    request, and a damaged file is not one.
    """

    code = "file-unreadable"
    params = MappingProxyType({"path": "TAXONOMY.md"})

    def __init__(self, path: object, reason: str, *, not_utf8: bool) -> None:
        self.path = path
        self.reason = reason
        self.not_utf8 = not_utf8
        if not_utf8:
            message = f"Cannot read {path}: {reason}. Re-save it as UTF-8, then retry."
        else:
            message = (
                f"Cannot read {path} ({reason}). "
                "Run `lit health-check` to see how to fix it."
            )
        super().__init__(message)


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


class SupplementaryError(LitmanError):
    """A paper's SI files could not be added or removed: a file failed
    verification or needs confirmation, a source is missing or sits inside
    the library, an SI name is unknown, or the ``supplementary`` field is
    malformed.
    """


class CodeError(LitmanError):
    """`lit code` rejected an op: invalid URL / repo-name, collision with an
    existing clone, missing paper for --paper binding, or ``git clone`` failure.
    """


class RepoBindPendingError(CodeError):
    """A re-clone's bind failed past its decision point: it lands on the next `lit`."""


class ConfigError(LitmanError):
    """`lit-config.yaml` could not be parsed or failed schema validation.

    Distinct from ``LibraryNotFoundError`` (file is missing entirely): here
    the file exists but is malformed YAML, has an unknown key, holds a value
    of the wrong type, or violates a field constraint.
    """

    code = "file-unreadable"
    params = MappingProxyType({"path": "lit-config.yaml"})


class SyncError(LitmanError):
    """`lit sync` rejected an op: rclone not installed, remote misconfigured,
    sync not yet set up in lit-config.yaml, or rclone returned a non-zero
    exit status (network down, auth failure, transfer error, etc.).

    ``rclone_tail`` is rclone's last log lines when they end the message, ``""``
    otherwise: ``lit sync`` leaves them out of an error it recognises, as the
    record ``lit sync status`` shows keeps them.
    """

    rclone_tail: str = ""

    def without_rclone_tail(self) -> str:
        """The message without rclone's last lines: litman's own words."""
        text = str(self)
        if self.rclone_tail and text.endswith("\n" + self.rclone_tail):
            return text[: -len(self.rclone_tail) - 1]
        return text


class SyncChoiceRequiredError(SyncError):
    """Two-way sync cannot go on until the user picks which side wins.

    Nothing has been synced when it is raised: on a first sync where both this
    machine and the cloud already hold a library, after the sync filters
    changed, or when rclone can no longer continue from its last sync records.
    The way out is ``lit sync --first-from cloud|local``, which only the user
    may choose.
    """


class SyncStoppedError(SyncError):
    """rclone stopped a two-way sync on one of its safety checks.

    More than half of the files were deleted on one side, or every file
    changed on both. Only the user can tell whether that was meant, so the
    GUI's auto sync stops until they decide; ``lit sync --force`` goes on.
    """


class SyncAuthError(SyncError):
    """The cloud refused the sign-in rclone holds for it, so nothing was synced.

    An OAuth token that expired or was revoked, or a password the server (a
    WebDAV one, say) no longer accepts. ``sign_in`` says how the user signs in
    again: ``"browser"`` (``rclone config reconnect``), ``"password"`` (typed
    into ``rclone config``), or ``None`` when rclone could not tell.
    """

    def __init__(self, message: str, sign_in: str | None = None) -> None:
        super().__init__(message)
        self.sign_in = sign_in


class SyncBusyError(SyncError):
    """Another sync of the same library holds its lock, so this one did not start.

    Nothing was synced. ``lit sync`` exits 23 with it; the GUI's auto sync
    tries again a little later.
    """


class SyncLockError(SyncError):
    """A sync lock could not be taken because the file system refused locks altogether.

    Not another holder: the folder that keeps litman's sync records allows no
    file locks (some network file systems), so no sync can run from this
    machine until that folder is on a disk that does.
    """


class BisyncLockedError(SyncError):
    """rclone's own lock of this library's sync records is still in place.

    Left by a sync that was cut off, or held by one that runs outside litman's
    own lock. It expires on its own (``--max-lock``).
    """


class ExportError(LitmanError):
    """`lit export` (M12.1) rejected an op: mutually-exclusive flags missing
    or doubled, target file is hand-edited (no litman sentinel) and `--force`
    not supplied, or the resolved vault has no papers matching the filters
    when the user explicitly opted in to filtering.
    """


class VaultRegistryError(LitmanError):
    """`lit vault` (M8) rejected an op against ``~/.config/litman/vaults.yaml``:
    invalid name shape, duplicate / unknown name, registry file malformed,
    or a path supplied to ``add`` does not point at a valid vault.

    Distinct from ``LibraryNotFoundError`` (which is about the *active*
    vault not being discoverable in a single command run): registry errors
    surface from the user-level registry layer, not from any single vault.
    """


class IndexWriteError(LitmanError):
    """INDEX.json could not be written, where nothing can go on without it."""


class SelfUpdateError(LitmanError):
    """`lit self-update` cannot upgrade: neither uv nor pipx is available, or
    the chosen installer's upgrade subprocess failed / timed out.
    """
