"""Text edits to ``commodity`` directives for ``fetch-metadata``.

The metadata command edits hand-maintained ledger files, so it never
re-renders or reformats them. Instead it locates each directive by the
``filename``/``lineno`` beancount recorded at load time and makes minimal
text edits:

* adding a key appends ``key: "value"`` after the directive's metadata block;
* updating a key rewrites only the value on its existing line, preserving
  indentation, the key, and any trailing ``; comment``.

Edits within one file are applied bottom-to-top so earlier line numbers stay
valid. Files are written atomically. A directive whose recorded line no
longer looks like ``<date> commodity <CODE>`` aborts the whole file.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .models import MetadataValue, metadata_value_text

logger = logging.getLogger(__name__)

DEFAULT_INDENT = "  "

_DIRECTIVE_RE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}\s+commodity\s+(\S+)")


@dataclass(slots=True, frozen=True)
class DirectiveEdit:
    """Planned edits to one existing ``Commodity`` directive.

    ``adds`` are ``(key, value)`` pairs inserted after the metadata block in
    order. ``updates`` are ``(key, new_value)`` pairs for keys already
    present whose value should be replaced (only produced with ``--refresh``).
    """

    commodity: str
    lineno: int
    adds: tuple[tuple[str, MetadataValue], ...] = ()
    updates: tuple[tuple[str, MetadataValue], ...] = ()


@dataclass(slots=True, frozen=True)
class NewMetadataDirective:
    """A brand-new ``commodity`` directive to append to the output file."""

    commodity: str
    directive_date: date
    key_values: tuple[tuple[str, MetadataValue], ...]


@dataclass(slots=True, frozen=True)
class MetadataWriteResult:
    """Outcome of applying edits to one file."""

    path: Path
    diff: str
    changed: bool
    errors: tuple[str, ...] = field(default_factory=tuple)


def _quote(value: str) -> str:
    """Render a metadata value as a double-quoted, escaped Beancount string."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _render_value(value: MetadataValue) -> str:
    """Render a metadata value: strings quoted/escaped, numbers bare."""
    if isinstance(value, str):
        return _quote(value)
    return metadata_value_text(value)


def _render_key_line(indent: str, key: str, value: MetadataValue) -> str:
    """Render ``<indent>key: <value>``."""
    return f"{indent}{key}: {_render_value(value)}"


def _parse_key_line(line: str) -> tuple[str, str, str, str] | None:
    """Split a metadata line into ``(indent, key, gap, comment)``.

    ``gap`` is the whitespace between the value and a trailing comment;
    ``comment`` includes the leading ``;`` or is empty. Comment detection is
    quote-aware: a ``;`` inside a quoted value is not a comment. Returns None
    when the line is not a ``key: value`` metadata line.
    """
    stripped = line.lstrip(" \t")
    indent = line[: len(line) - len(stripped)]
    if not stripped or stripped.startswith(";"):
        return None
    colon = stripped.find(":")
    if colon == -1:
        return None
    key = stripped[:colon].strip()
    if not key:
        return None

    after = stripped[colon + 1 :]
    start = len(after) - len(after.lstrip(" \t"))

    if start < len(after) and after[start] in "\"'":
        quote = after[start]
        index = start + 1
        while index < len(after):
            char = after[index]
            if char == "\\":
                index += 2
                continue
            if char == quote:
                index += 1
                break
            index += 1
        remainder = after[index:]
    else:
        index = start
        while index < len(after) and after[index] != ";":
            index += 1
        remainder = after[index:]

    semi = remainder.find(";")
    if semi == -1:
        gap, comment = remainder, ""
    else:
        gap, comment = remainder[:semi], remainder[semi:]
    return indent, key, gap, comment


def _replace_line_value(line: str, key: str, new_value: MetadataValue) -> str | None:
    """Replace the value on ``line`` if it belongs to ``key``; else None."""
    parsed = _parse_key_line(line)
    if parsed is None or parsed[1] != key:
        return None
    indent, parsed_key, gap, comment = parsed
    return f"{indent}{parsed_key}: {_render_value(new_value)}{gap}{comment}"


def _metadata_block(lines: list[str], directive_index: int) -> tuple[int, int]:
    """Return the half-open ``[start, end)`` line range of a directive's block.

    The block is the run of indented non-blank lines immediately after the
    directive line (indented comments included).
    """
    start = directive_index + 1
    end = start
    while end < len(lines):
        raw = lines[end]
        if not raw.strip():
            break
        if raw[0] not in (" ", "\t"):
            break
        end += 1
    return start, end


def _detect_indent(lines: list[str], start: int, end: int) -> str:
    """Indentation of the first metadata line, or two spaces when empty."""
    if start < end:
        raw = lines[start]
        stripped = raw.lstrip(" \t")
        indent = raw[: len(raw) - len(stripped)]
        if indent:
            return indent
    return DEFAULT_INDENT


def _unified_diff(original: str, updated: str, path: Path) -> str:
    """Render a unified diff between two file contents."""
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path),
        )
    )


def _read_text(path: Path) -> tuple[str, str]:
    """Read a file preserving its line endings; return ``(content, newline)``.

    ``newline`` is ``"\r\n"`` when the file uses CRLF, else ``"\n"``. A
    missing file yields ``("", "\n")``.
    """
    if not path.exists():
        return "", "\n"
    content = path.read_bytes().decode("utf-8")
    return content, ("\r\n" if "\r\n" in content else "\n")


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` via a temp file + rename in the same dir.

    The destination's existing permission bits are preserved; new files fall
    back to ``0644``. ``mkstemp`` creates a ``0600`` file, so without this the
    rename would silently tighten the mode of a hand-maintained ledger.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = (path.stat().st_mode & 0o7777) if path.exists() else 0o644
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            logger.debug("could not remove temp file %s", tmp_name)
        raise


def apply_directive_edits(
    path: Path,
    edits: list[DirectiveEdit],
    *,
    write: bool,
) -> MetadataWriteResult:
    """Apply metadata edits to one file, bottom-to-top.

    Args:
        path: The ledger file containing the directives.
        edits: Edits for directives in this file.
        write: When True, write the result atomically. When False, only
            compute the diff and changed flag.

    Returns:
        A ``MetadataWriteResult`` with the unified diff, whether anything
        changed, and any errors. On error the file is left untouched.
    """
    original, newline = _read_text(path)
    normalized = original.replace("\r\n", "\n")
    lines = normalized.splitlines()
    errors: list[str] = []

    for edit in sorted(edits, key=lambda item: item.lineno, reverse=True):
        index = edit.lineno - 1
        if index < 0 or index >= len(lines):
            errors.append(
                f"{path}:{edit.lineno}: line no longer looks like a "
                f"`{edit.commodity}` commodity directive; file skipped"
            )
            return MetadataWriteResult(path, "", False, tuple(errors))
        match = _DIRECTIVE_RE.match(lines[index])
        if match is None or match.group(1) != edit.commodity:
            errors.append(
                f"{path}:{edit.lineno}: line no longer looks like a "
                f"`{edit.commodity}` commodity directive; file skipped"
            )
            return MetadataWriteResult(path, "", False, tuple(errors))

        start, end = _metadata_block(lines, index)
        updated_keys: set[str] = set()
        for key, new_value in edit.updates:
            replaced = False
            for line_index in range(start, end):
                parsed = _parse_key_line(lines[line_index])
                if parsed is None or parsed[1] != key:
                    continue
                if key in updated_keys:
                    logger.warning(
                        "duplicate `%s` key in %s:%d; editing the first occurrence only",
                        key,
                        path,
                        edit.lineno,
                    )
                    break
                replaced_line = _replace_line_value(lines[line_index], key, new_value)
                if replaced_line is not None:
                    lines[line_index] = replaced_line
                    updated_keys.add(key)
                    replaced = True
            if not replaced and key not in updated_keys:
                logger.warning("`%s` not found in %s:%d; no update applied", key, path, edit.lineno)

        if edit.adds:
            indent = _detect_indent(lines, start, end)
            lines[end:end] = [_render_key_line(indent, key, value) for key, value in edit.adds]

    updated_normalized = "\n".join(lines)
    if normalized.endswith("\n") and lines:
        updated_normalized += "\n"
    updated_content = updated_normalized.replace("\n", newline)

    changed = updated_content != original
    if changed and write:
        _atomic_write(path, updated_content)

    diff = _unified_diff(original, updated_content, path) if changed else ""
    return MetadataWriteResult(path, diff, changed, tuple(errors))


def _declared_commodities(path: Path) -> set[str]:
    """Commodity codes already declared in ``path`` (empty if it doesn't exist)."""
    if not path.exists():
        return set()
    found: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _DIRECTIVE_RE.match(line)
        if match is not None:
            found.add(match.group(1))
    return found


def append_new_directives(
    path: Path,
    directives: list[NewMetadataDirective],
    *,
    write: bool,
) -> MetadataWriteResult:
    """Append new ``commodity`` directives, skipping any already declared.

    Args:
        path: Output file (e.g. ``commodities.bean``). Created if needed.
        directives: Directives to append.
        write: When True, write atomically; otherwise only compute the diff.

    Returns:
        A ``MetadataWriteResult`` for the output file.
    """
    original, newline = _read_text(path)
    declared = _declared_commodities(path)
    blocks: list[str] = []
    for directive in directives:
        if directive.commodity in declared:
            logger.warning(
                "%s already has a commodity directive in %s; not duplicating",
                directive.commodity,
                path,
            )
            continue
        declared.add(directive.commodity)
        lines = [f"{directive.directive_date.isoformat()} commodity {directive.commodity}"]
        lines.extend(
            _render_key_line(DEFAULT_INDENT, key, value) for key, value in directive.key_values
        )
        blocks.append("\n".join(lines))

    if not blocks:
        return MetadataWriteResult(path, "", False, ())

    addition = "\n\n".join(blocks) + "\n"
    base = original.replace("\r\n", "\n")
    if base and not base.endswith("\n"):
        base += "\n"
    if base:
        addition = "\n" + addition
    updated_content = (base + addition).replace("\n", newline)

    changed = updated_content != original
    if changed and write:
        _atomic_write(path, updated_content)

    diff = _unified_diff(original, updated_content, path) if changed else ""
    return MetadataWriteResult(path, diff, changed, ())
