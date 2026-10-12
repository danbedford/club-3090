#!/usr/bin/env python3
"""catalog.sh prune — remove what nothing in your LOCAL layer uses.

    catalog.sh prune [--dry-run] [-y] [--root DIR]

Removes exactly what `catalog.sh list` reports as unused (catalog_list.inventory,
so the two cannot disagree):

  variant    a weights variant in extends.d/<id>.yml that no slug uses
  extension  an extends.d/<id>.yml no slug is attached to
  compose    a compose under composes/<model>/<engine-dir>/compose/ no entry uses
  engine     an engine profile `register` wrote as a side effect, that no slug uses
  model      a models.d/<id>.yml no slug uses

Never an engine registered on purpose (unregister-engine removes those), and
nothing broken or shadowed: those need a decision, not a cleanup.

This layer is gitignored, so git cannot bring a deleted file back. prune prints
every path first, refuses to run without a terminal unless -y says so, and puts
every file back if the catalog no longer loads afterwards.

    PRUNE_OK <count>
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

ROOT_DEFAULT = Path(__file__).resolve().parents[3]
LOCAL = "scripts/lib/profiles-local"
EXIT_REFUSED, EXIT_INTERNAL = 3, 2


def _split_comments(text: str) -> tuple[str, str]:
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines) and (lines[i].startswith("#") or not lines[i].strip()):
        i += 1
    return "".join(lines[:i]), "".join(lines[i:])


def describe(item: dict) -> str:
    k = item["kind"]
    if k == "variant":
        return f"remove variant {item['variant']!r} from {item['path']}"
    if k == "extension":
        return f"delete {item['path']} (no slug is attached to {item['id']})"
    if k == "engine":
        return f"delete {item['path']} (engine {item['id']}, written by register, no slug runs on it)"
    if k == "model":
        return f"delete {item['path']} (model {item['id']}, no slug uses it)"
    return f"delete {item['path']} (no entry uses this compose)"


def _reload_error(root: Path) -> Optional[str]:
    chk = subprocess.run(
        [sys.executable, "-c",
         "from scripts.lib.profiles.compose_registry import get_registry; get_registry(); "
         "from scripts.lib.profiles.compat import load_profiles; load_profiles()"],
        cwd=str(root), capture_output=True, text=True, encoding="utf-8",
        env=dict(os.environ, PYTHONUTF8=os.environ.get("PYTHONUTF8", "1")))
    return None if chk.returncode == 0 else (chk.stderr.strip() or chk.stdout.strip())[-800:]


def apply(root: Path, items: list[dict]) -> None:
    import yaml
    whole = {i["path"] for i in items if i["kind"] != "variant"}
    variants: dict[str, list[str]] = {}
    for i in items:
        if i["kind"] == "variant" and i["path"] not in whole:
            variants.setdefault(i["path"], []).append(i["variant"])
    for rel, names in variants.items():
        path = root / rel
        header, _ = _split_comments(path.read_text(encoding="utf-8"))
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        data["weights"] = {k: v for k, v in (data.get("weights") or {}).items() if k not in names}
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True,
                                               default_flow_style=False, width=1000),
                       encoding="utf-8")
        os.replace(tmp, path)
    stop = root / LOCAL
    for rel in sorted(whole):
        path = root / rel
        if path.exists():
            path.unlink()
        parent = path.parent                  # and the directories that are now empty
        while parent != stop and stop in parent.parents and parent.is_dir() \
                and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="catalog.sh prune", description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-y", "--yes", action="store_true")
    ap.add_argument("--root", default=str(ROOT_DEFAULT))
    args = ap.parse_args(argv)
    os.environ.setdefault("PYTHONUTF8", "1")
    root = Path(args.root).resolve()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import catalog_list

    inv = catalog_list.inventory(root)
    items = inv["unused"]
    kept = [p for p in inv["problems"] if p["kind"] in ("broken", "shadowed")]
    if not items:
        print("[prune] nothing to prune in the local layer.")
        for p in kept:
            print(f"[prune]   (not pruned, needs a decision) {p['kind']}: {p['what']}")
        return 0
    print(f"[prune] plan ({len(items)}):")
    for i in items:
        print(f"[prune]   {describe(i)}")
    for p in kept:
        print(f"[prune]   (not pruned, needs a decision) {p['kind']}: {p['what']}")
    if args.dry_run:
        print("[prune] dry-run — nothing removed.")
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            print("[prune] REFUSED: these files are gitignored, so git can't bring them back. "
                  "Run it in a terminal to confirm, or pass -y.", file=sys.stderr)
            return EXIT_REFUSED
        try:
            reply = input("[prune] remove all of these? [y/N] ").strip().lower()
        except EOFError:
            reply = ""
        if reply not in ("y", "yes"):
            print("[prune] aborted — nothing removed", file=sys.stderr)
            return EXIT_REFUSED

    snapshot = {i["path"]: (root / i["path"]).read_bytes() for i in items if (root / i["path"]).is_file()}
    apply(root, items)
    err = _reload_error(root)
    if err:
        for rel, data in snapshot.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(data)
        print(f"[prune] ERROR: the catalog no longer loads after pruning, so every file was put "
              f"back:\n{err}", file=sys.stderr)
        return EXIT_INTERNAL
    print(f"PRUNE_OK {len(items)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
