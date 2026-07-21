"""Install narrowly-scoped ``lit`` command approvals for supported agents.

Agent Skills standardises skill content, not each host application's command
permission store.  The onboarding flow therefore installs one native rule per
agent alongside the skills:

* Claude Code: ``~/.claude/settings.json``
* Antigravity CLI: ``~/.gemini/antigravity-cli/settings.json``
* Codex: ``$CODEX_HOME/rules/litman.rules`` (``~/.codex`` by default)
* Cursor CLI: ``~/.cursor/cli-config.json``
* OpenCode: ``~/.config/opencode/opencode.json[c]``

Only commands whose executable is ``lit`` are approved.  This module never
enables an agent's global bypass/auto-approve mode.  Existing configuration is
merged, never replaced; malformed or structurally incompatible user config is
left untouched and reported as a warning so skill installation can still
succeed.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import uuid
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal, TypedDict


class PermissionResult(TypedDict):
    agent: str
    path: Path
    mode: Literal["created", "updated", "unchanged", "skipped"]
    rule: str
    warning: str | None

_CODEX_RULE = '''# Managed by litman. Remove this file to restore Codex's default prompt.
prefix_rule(
    pattern = ["lit"],
    decision = "allow",
    justification = "Allow the installed litman skills to use the lit CLI",
    match = [
        "lit hello",
        "lit list --format json",
    ],
    not_match = [
        "litman --version",
    ],
)
'''
_CODEX_MARKER = "# Managed by litman."


def _result(
    agent: str,
    path: Path,
    mode: Literal["created", "updated", "unchanged", "skipped"],
    rule: str,
    warning: str | None = None,
) -> PermissionResult:
    return {
        "agent": agent,
        "path": path,
        "mode": mode,
        "rule": rule,
        "warning": warning,
    }


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomically replace ``path`` while preserving its permission bits."""
    path.parent.mkdir(parents=True, exist_ok=True)
    target = path.resolve() if path.is_symlink() else path
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
    tmp = target.with_name(f".{target.name}.litman-{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        if mode is not None:
            tmp.chmod(mode)
        os.replace(tmp, target)
    finally:
        with suppress(FileNotFoundError):
            tmp.unlink()


def _load_json_object(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return {}, None
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"Could not safely update {path}: {exc}"
    if not isinstance(value, dict):
        return None, f"Could not safely update {path}: top level is not an object."
    return value, None


def _merge_json_permissions(
    *,
    agent: str,
    path: Path,
    rule: str,
    update: Callable[[dict[str, Any]], str | None],
) -> PermissionResult:
    data, warning = _load_json_object(path)
    if data is None:
        return _result(agent, path, "skipped", rule, warning)
    before = json.dumps(data, sort_keys=True, ensure_ascii=False)
    structural_warning = update(data)
    if structural_warning is not None:
        return _result(agent, path, "skipped", rule, structural_warning)
    after = json.dumps(data, sort_keys=True, ensure_ascii=False)
    if before == after:
        return _result(agent, path, "unchanged", rule)
    try:
        _atomic_write_text(
            path,
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        )
    except OSError as exc:
        return _result(
            agent,
            path,
            "skipped",
            rule,
            f"Could not write {path}: {exc}",
        )
    return _result(agent, path, "created" if before == "{}" else "updated", rule)


def _append_allow_rules(
    data: dict[str, Any],
    entries: tuple[str, ...],
    *,
    config_path: Path,
) -> str | None:
    permissions = data.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        return f"Could not safely update {config_path}: 'permissions' is not an object."
    allow = permissions.setdefault("allow", [])
    if not isinstance(allow, list) or not all(isinstance(x, str) for x in allow):
        return f"Could not safely update {config_path}: 'permissions.allow' is not a string list."
    for entry in entries:
        if entry not in allow:
            allow.append(entry)
    return None


def _configured_rules(path: Path, list_name: str) -> list[str]:
    data, _ = _load_json_object(path)
    permissions = data.get("permissions", {}) if data else {}
    rules = permissions.get(list_name, []) if isinstance(permissions, dict) else []
    return [rule for rule in rules if isinstance(rule, str)] if isinstance(rules, list) else []


def install_claude_lit_permission() -> PermissionResult:
    path = Path.home() / ".claude" / "settings.json"
    entries = ["Bash(lit *)"]
    if sys.platform == "win32":
        entries.append("PowerShell(lit *)")
    result = _merge_json_permissions(
        agent="claude",
        path=path,
        rule=", ".join(entries),
        update=lambda data: _append_allow_rules(
            data, tuple(entries), config_path=path
        ),
    )
    blockers = {
        "Bash",
        "Bash(*)",
        "Bash(lit *)",
        "PowerShell",
        "PowerShell(*)",
        "PowerShell(lit *)",
    }
    conflicts = sorted(
        blockers
        & set(_configured_rules(path, "ask") + _configured_rules(path, "deny"))
    )
    if result["warning"] is None and conflicts:
        result["warning"] = (
            "Claude Code ask/deny rules take precedence over allow rules; "
            f"review these entries in /permissions: {', '.join(conflicts)}"
        )
    return result


def install_antigravity_lit_permission() -> PermissionResult:
    path = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
    result = _merge_json_permissions(
        agent="agy",
        path=path,
        rule="command(lit)",
        update=lambda data: _append_allow_rules(
            data, ("command(lit)",), config_path=path
        ),
    )
    if result["warning"] is None and path.exists():
        blockers = {"command(*)", "command(lit)"}
        conflicts = sorted(
            blockers
            & set(
                _configured_rules(path, "ask")
                + _configured_rules(path, "deny")
            )
        )
        if conflicts:
            result["warning"] = (
                "Antigravity ask/deny rules take precedence over allow rules; "
                f"review these entries in /permissions: {', '.join(conflicts)}"
            )
    return result


def install_cursor_lit_permission() -> PermissionResult:
    path = Path.home() / ".cursor" / "cli-config.json"
    result = _merge_json_permissions(
        agent="cursor",
        path=path,
        rule="Shell(lit)",
        update=lambda data: _append_allow_rules(
            data, ("Shell(lit)",), config_path=path
        ),
    )
    conflicts = sorted(
        {"Shell(lit)"} & set(_configured_rules(path, "deny"))
    )
    if result["warning"] is None and conflicts:
        result["warning"] = (
            "Cursor deny rules take precedence over allow rules; remove "
            "Shell(lit) from permissions.deny to stop lit prompts."
        )
    return result


def _codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def install_codex_lit_permission() -> PermissionResult:
    path = _codex_home() / "rules" / "litman.rules"
    if path.exists():
        try:
            current = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return _result(
                "codex", path, "skipped", 'prefix_rule(["lit"])', str(exc)
            )
        if current == _CODEX_RULE:
            return _result("codex", path, "unchanged", 'prefix_rule(["lit"])')
        if not current.startswith(_CODEX_MARKER):
            return _result(
                "codex",
                path,
                "skipped",
                'prefix_rule(["lit"])',
                f"Left user-owned rule file untouched: {path}",
            )
    try:
        _atomic_write_text(path, _CODEX_RULE)
    except OSError as exc:
        return _result(
            "codex", path, "skipped", 'prefix_rule(["lit"])', str(exc)
        )
    return _result("codex", path, "created", 'prefix_rule(["lit"])')


def _opencode_path() -> tuple[Path, str | None]:
    root = Path.home() / ".config" / "opencode"
    jsonc = root / "opencode.jsonc"
    plain = root / "opencode.json"
    if jsonc.exists():
        # Strict JSON is valid JSONC and can be merged without destroying the
        # user's layout. A commented JSONC file is left untouched; the lower
        # opencode.json layer still supplies the rule in the common case.
        parsed, error = _load_json_object(jsonc)
        if parsed is not None:
            return jsonc, None
        return plain, (
            f"Left non-strict JSONC untouched ({error}); OpenCode loads "
            "opencode.jsonc after opencode.json, so a later conflicting bash "
            "rule may still override litman's allow rule."
        )
    if plain.exists():
        return plain, None
    return jsonc, None


def install_opencode_lit_permission() -> PermissionResult:
    path, path_warning = _opencode_path()

    def update(data: dict[str, Any]) -> str | None:
        permissions = data.setdefault("permission", {})
        if isinstance(permissions, str):
            if permissions == "allow":
                return None
            if permissions not in {"ask", "deny"}:
                return (
                    f"Could not safely update {path}: 'permission' has "
                    f"unknown scalar {permissions!r}."
                )
            permissions = {"*": permissions}
            data["permission"] = permissions
        if not isinstance(permissions, dict):
            return f"Could not safely update {path}: 'permission' is not an object."
        bash = permissions.setdefault("bash", {})
        if isinstance(bash, str):
            if bash == "allow":
                return None
            if bash not in {"ask", "deny"}:
                return (
                    f"Could not safely update {path}: 'permission.bash' "
                    f"has unknown scalar {bash!r}."
                )
            bash = {"*": bash}
            permissions["bash"] = bash
        if not isinstance(bash, dict):
            return f"Could not safely update {path}: 'permission.bash' is not an object."
        # OpenCode uses last matching rule wins. Reinsert these keys at the end
        # so an earlier catch-all such as "*": "ask" does not win.
        bash.pop("lit", None)
        bash.pop("lit *", None)
        bash["lit"] = "allow"
        bash["lit *"] = "allow"
        return None

    result = _merge_json_permissions(
        agent="opencode",
        path=path,
        rule='permission.bash: {"lit": "allow", "lit *": "allow"}',
        update=update,
    )
    if path_warning and result["warning"] is None:
        result["warning"] = path_warning
    return result
