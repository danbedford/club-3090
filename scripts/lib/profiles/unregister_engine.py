#!/usr/bin/env python3
"""catalog.sh unregister-engine — remove an engine registered in the LOCAL layer.

    catalog.sh unregister-engine --id <id> [--root DIR] [--dry-run] [-y]

Removes ``scripts/lib/profiles-local/engines.d/<file>.yml`` (the file whose ``id:``
is <id>). A curated engine is git-tracked and git is its removal tool, so it is
refused, as is an engine any local slug still runs on: a slug whose engine is
gone fails the profile loader's cross-reference check, which takes every launcher
down with it. The profile set is re-loaded after the removal; a failure puts the
file back.

    ENGINE_REMOVED <id>
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

ROOT_DEFAULT = Path(__file__).resolve().parents[3]
LOCAL_REGISTRY_REL = "scripts/lib/profiles-local/registry.local.json"
EXIT_REFUSED, EXIT_INTERNAL = 3, 2


class Refusal(Exception):
    pass


def plan(root: Path, eid: str) -> Path:
    """The local profile file to remove. Raises Refusal when it must stay."""
    sys.path.insert(0, str(root / "scripts/lib/profiles"))
    try:
        import catalog_rules
    finally:
        sys.path.pop(0)
    found = catalog_rules._engine_index(root).get(eid)
    if not found:
        raise Refusal(f"no engine {eid!r} is registered in the local layer "
                      "(scripts/lib/profiles-local/engines.d/)")
    _prof, path, is_core = found
    if is_core:
        raise Refusal(f"{eid!r} is a curated engine ({path.name}): it is git-tracked, and git "
                      "is its removal tool. This command only removes local engines.")
    reg = root / LOCAL_REGISTRY_REL
    raw = {}
    if reg.is_file():
        try:
            raw = json.loads(reg.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise Refusal(f"{reg} is unreadable — fix or remove it first: {exc}")
    users = sorted(s for s, e in raw.items() if isinstance(e, dict) and e.get("engine") == eid)
    if users:
        raise Refusal(f"{len(users)} local slug(s) still run on {eid}; a slug whose engine is "
                      "gone stops the profile loader for every launcher. Unregister them first:\n"
                      + "\n".join(f"  bash scripts/catalog.sh unregister --slug {s}" for s in users))
    return path


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="catalog.sh unregister-engine",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("--id", required=True)
    ap.add_argument("--root", default=str(ROOT_DEFAULT))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-y", "--yes", action="store_true")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        path = plan(root, args.id)
    except Refusal as exc:
        print(f"[unregister-engine] REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    rel = path.relative_to(root)
    print(f"[unregister-engine] plan: remove {rel} (no local slug runs on {args.id})")
    if args.dry_run:
        print("[unregister-engine] dry-run — nothing removed.")
        return 0
    if not args.yes and sys.stdin.isatty():
        try:
            reply = input("[unregister-engine] remove it? [Y/n] ").strip().lower()
        except EOFError:
            reply = ""
        if reply not in ("", "y", "yes"):
            print("[unregister-engine] aborted — nothing removed", file=sys.stderr)
            return EXIT_REFUSED

    data = path.read_bytes()
    path.unlink()
    chk = subprocess.run(
        [sys.executable, "-c", "from scripts.lib.profiles.compat import load_profiles; load_profiles()"],
        cwd=str(root), capture_output=True, text=True, encoding="utf-8",
        env=dict(os.environ, PYTHONUTF8=os.environ.get("PYTHONUTF8", "1")))
    if chk.returncode != 0:
        path.write_bytes(data)
        print("[unregister-engine] ERROR: the profiles no longer load without it, so it was put "
              "back:\n" + (chk.stderr.strip() or chk.stdout.strip())[-800:], file=sys.stderr)
        return EXIT_INTERNAL
    print(f"[unregister-engine] removed {rel}")
    print(f"ENGINE_REMOVED {args.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
