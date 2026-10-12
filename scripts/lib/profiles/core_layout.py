"""Where the curated catalog keeps each engine's composes, and the slug prefix it
uses there — read from the live registry, never from a hand-kept map.

Two tools need the same answer and must not drift apart:
  catalog.sh register  — names a slug attached to a curated model and files its
                         compose where core keeps that engine's composes, so the
                         slug exports without a rename;
  export_pr.py         — places a local compose in the core layout and refuses a
                         slug whose prefix core does not use for that engine.

Stdlib-only: catalog.sh runs on the launcher's no-extra-deps path.
"""

from __future__ import annotations

from typing import Optional


def engine_layout(registry: dict) -> tuple[dict[str, set], dict[str, set]]:
    """From the curated registry's live (non-deprecated) entries: engine id →
    {(compose dir, slug prefix)}, and compose dir → {slug prefix}."""
    by_engine: dict[str, set] = {}
    by_dir: dict[str, set] = {}
    for slug, e in registry.items():
        if e.get("status") == "deprecated":
            continue
        parts = str(e.get("compose_path") or "").split("/")
        if len(parts) < 4 or parts[0] != "models":
            continue
        prefix = str(slug).split("/", 1)[0]
        by_engine.setdefault(str(e.get("engine")), set()).add((parts[2], prefix))
        by_dir.setdefault(parts[2], set()).add(prefix)
    return by_engine, by_dir


def pick_pair(by_engine: dict[str, set], engine: str,
              dir_hint: str = "") -> tuple[Optional[tuple[str, str]], list[tuple[str, str]]]:
    """(the (dir, prefix) pair to use for a new slug on `engine`, every pair core
    has for it). None when core has no slug on the engine, or when it has several
    and `dir_hint` (the compose's own engine directory, e.g. `llama-cpp` vs
    `ik-llama`) picks none of them."""
    pairs = sorted(by_engine.get(engine, set()))
    if len(pairs) == 1:
        return pairs[0], pairs
    hinted = [p for p in pairs if p[0] == dir_hint]
    return (hinted[0] if len(hinted) == 1 else None), pairs
