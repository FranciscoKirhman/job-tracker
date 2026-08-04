"""Read/write flat JS object-literal arrays embedded in the tracker HTML.

Several data blocks in the tracker (FAILED_SOURCES, REVIEWED_SOURCES,
NEW_JOB_RECOMMENDATIONS, ADJACENT_OPPORTUNITIES) are JS array literals with
unquoted keys and single-quoted string values -- not strict JSON -- e.g.:

    [
      {source:'Parexel',status:'No comparable',checked:'2026-08-02 ...'},
      ...
    ]

This module handles the flat case only: an array of objects whose values
are all single-quoted strings (no nesting, no numbers/booleans/arrays as
values). That covers every block this codebase currently needs to both
read AND write; deeper structures (e.g. reportSections) are read-only via
plain regex elsewhere since nothing needs to write them back.
"""

from __future__ import annotations

import re
from typing import Any


def find_statement_bounds(content: str, var_name: str) -> tuple[int, int] | None:
    """Return (start, end) spanning the value of `const/let NAME = <value>;`,
    tracking brace/bracket/string depth so nested structures don't confuse it.
    """
    match = re.search(rf"(?:const|let)\s+{re.escape(var_name)}\s*=\s*", content)
    if not match:
        return None
    start = match.end()
    depth = 0
    started = False
    in_string: str | None = None
    i = start
    while i < len(content):
        c = content[i]
        if in_string:
            if c == "\\":
                i += 2
                continue
            if c == in_string:
                in_string = None
        elif c in ("'", '"'):
            in_string = c
        elif c in "{[":
            depth += 1
            started = True
        elif c in "}]":
            depth -= 1
            if started and depth == 0:
                return (start, i + 1)
        i += 1
    return None


_OBJECT_PATTERN = re.compile(r"\{([^{}]*)\}")
_FIELD_PATTERN = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*'((?:[^'\\]|\\.)*)'")


def _unescape(value: str) -> str:
    return value.replace("\\'", "'").replace("\\\\", "\\")


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def parse_flat_array(text: str) -> list[dict[str, Any]]:
    """Parse a `[{k:'v',...}, ...]` literal (flat string values only)."""
    entries = []
    for obj_match in _OBJECT_PATTERN.finditer(text):
        fields = {}
        for field_match in _FIELD_PATTERN.finditer(obj_match.group(1)):
            fields[field_match.group(1)] = _unescape(field_match.group(2))
        if fields:
            entries.append(fields)
    return entries


def serialize_flat_array(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "[]"
    parts = []
    for entry in entries:
        fields = ",".join(f"{key}:'{_escape(str(value))}'" for key, value in entry.items())
        parts.append("{" + fields + "}")
    return "[\n  " + ",\n  ".join(parts) + "\n]"


def read_flat_array(html: str, var_name: str) -> list[dict[str, Any]]:
    bounds = find_statement_bounds(html, var_name)
    if not bounds:
        return []
    return parse_flat_array(html[bounds[0] : bounds[1]])


def write_flat_array(html: str, var_name: str, entries: list[dict[str, Any]]) -> str:
    bounds = find_statement_bounds(html, var_name)
    if not bounds:
        raise RuntimeError(f"{var_name} not found in tracker")
    start, end = bounds
    return html[:start] + serialize_flat_array(entries) + html[end:]
