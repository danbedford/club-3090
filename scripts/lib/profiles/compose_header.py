"""Rewrite a compose's `# Profile (at-a-glance):` Status and Caveats fields as TEXT,
so every other line stays byte for byte.

compose_registry.compose_header_status() is the reader every guard uses (and
test-compose-status-drift holds a curated entry's registry status to it); this
module is the writer for the tools that change a status: deprecate-slug.sh, and
catalog.sh register / update for the local layer.
"""

from __future__ import annotations

import re
from typing import Optional

# A schema field line: `#` + 1-4 spaces + `Key:`. Continuation lines are indented
# deeper or don't look like a key, so a field never swallows the next one.
_KEY = re.compile(r"^#(\s{1,4})([A-Z][A-Za-z0-9 ._()/-]*?):(\s|$)")

# The enum's labels, as AGENTS.md "Status enum" writes them. The leading emoji is
# what compose_header_status() matches.
LABELS = {
    "production": "✅ Production",
    "caveats": "⚠️ Production w/ caveats",
    "experimental": "🧪 Experimental",
    "incubating": "🐣 Incubating",
    "preview": "👁️ Preview",
    "upstream-gated": "⏸️ Upstream-gated",
    "deprecated": "🗑️ Deprecated",
}
# Statuses whose header must carry a Caveats line (AGENTS.md).
NEEDS_CAVEATS = {"caveats", "incubating", "preview", "upstream-gated", "deprecated"}


class HeaderError(Exception):
    """No profile block, or not exactly one Status field in it."""


def schema_span(lines: list[str]) -> Optional[tuple[int, int]]:
    """[start, end) of the `# Profile (at-a-glance):` block, bounded the way
    compose_header_status reads it (up to the `# --` separator)."""
    start = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if start is None:
            if s.startswith("# Profile (at-a-glance):"):
                start = i + 1
            continue
        if s.startswith("# --") or s.startswith("#--"):
            return start, i
    return None if start is None else (start, len(lines))


def fields(lines: list[str], start: int, end: int) -> list[tuple[str, int, int]]:
    """(name, first line, end line) per field; the range covers its continuations."""
    out = []
    i = start
    while i < end:
        m = _KEY.match(lines[i])
        if not m:
            i += 1
            continue
        j = i + 1
        while (j < end and lines[j].startswith("#") and lines[j].lstrip("#").strip()
               and not _KEY.match(lines[j])):
            j += 1
        out.append((m.group(2), i, j))
        i = j
    return out


def status_value(text: str) -> Optional[str]:
    """The Status field's value as written (first line), or None when there is no
    profile block or not exactly one Status field."""
    lines = text.splitlines(keepends=True)
    span = schema_span(lines)
    if span is None:
        return None
    status = [f for f in fields(lines, *span) if f[0] == "Status"]
    if len(status) != 1:
        return None
    return re.sub(r"^#\s*Status:\s*", "", lines[status[0][1]]).rstrip("\r\n")


def says(text: str, status: str) -> bool:
    """Whether the Status field already carries `status`'s emoji (the token
    compose_header_status() matches)."""
    value = status_value(text)
    return value is not None and value.startswith(LABELS[status].split(" ", 1)[0])


def has_caveats(text: str) -> bool:
    lines = text.splitlines(keepends=True)
    span = schema_span(lines)
    return bool(span) and any(f[0] == "Caveats" for f in fields(lines, *span))


def set_status(text: str, status_label: str, caveats: Optional[str] = None,
               replace_caveats: bool = False) -> str:
    """The compose text with its Status field set to `status_label` (continuation
    lines folded into the one line). With `caveats`, a `Caveats:` line carrying it
    either replaces the first existing Caveats field (`replace_caveats`) or goes in
    right after Status, keeping any Caveats already there.

    Raises HeaderError when there is no profile block or not exactly one Status
    field in it."""
    lines = text.splitlines(keepends=True)
    span = schema_span(lines)
    if span is None:
        raise HeaderError("the compose has no `# Profile (at-a-glance):` header block")
    flds = fields(lines, *span)
    status = [f for f in flds if f[0] == "Status"]
    if len(status) != 1:
        raise HeaderError(f"the profile header has {len(status)} Status fields, expected 1")
    _, s0, s1 = status[0]
    head = re.match(r"^(#\s*)Status:\s*", lines[s0])
    indent, col = head.group(1), len(head.group(0))
    eol = "\r\n" if lines[s0].endswith("\r\n") else "\n"
    status_line = f"{head.group(0)}{status_label}{eol}"
    if caveats is None:
        edits = [(s0, s1, [status_line])]
    else:
        label = f"{indent}Caveats:"
        caveats_line = f"{label}{' ' * max(1, col - len(label))}{caveats}{eol}"
        existing = [f for f in flds if f[0] == "Caveats"]
        if replace_caveats and existing:
            _, c0, c1 = existing[0]
            edits = [(s0, s1, [status_line]), (c0, c1, [caveats_line])]
        else:
            edits = [(s0, s1, [status_line, caveats_line])]
    out = lines[:]
    for a, b, repl in sorted(edits, reverse=True):
        out[a:b] = repl
    return "".join(out)
