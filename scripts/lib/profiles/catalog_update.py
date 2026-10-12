#!/usr/bin/env python3
"""catalog.sh update-weights / update-engine — edit LOCAL profiles in place.

`catalog.sh update` edits a slug's registry entry. These edit what the slug runs:

    catalog.sh update-weights --model M --variant V --set KEY=VALUE [--set …]
        a weights variant in models.d/<M>.yml (a model only your rig has) or in
        extends.d/<M>.yml (a variant you added to a curated model). `export_pr.py`
        needs `files:` and `hf_repo` on a variant it ships, and `register` says so.

    catalog.sh update-engine --id ID [--set KEY=VALUE] [--add KEY=a,b] [--remove KEY=a,b]
        an engine in engines.d/: widen what it supports once measured, add lineage,
        repin its image.

Both refuse a curated profile (git-tracked; git and a PR are its edit tools), show
what changes before writing, validate the result with the loader's own factories,
write through a temp file, re-load the profiles after, and put the file back if
they no longer load. `KEY=` with no value removes the key. --dry-run writes
nothing; -y skips the confirm.

    UPDATE_OK <file>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

ROOT_DEFAULT = Path(__file__).resolve().parents[3]
LOCAL = "scripts/lib/profiles-local"
EXIT_REFUSED, EXIT_INTERNAL = 3, 2

# Weights-variant keys a user states (a subset of compat.WEIGHTS_VARIANT_KEYS).
# `hf_repos` is derived by the loader, and the setup_* keys belong to setup.sh's
# dispatch policy, not to one local variant.
WEIGHT_KEYS = {
    "files": "list", "hf_repo": "str", "revision": "str", "verify_glob": "str",
    "path": "str", "local_subdir": "str", "size_gb": "float", "format": "str",
    "status": "str", "kind": "str", "engine": "str", "quant_label": "str",
    "manual_note": "str",
}
# Engine keys: name -> (where it lives, type). Lists take --add / --remove too.
ENGINE_KEYS = {
    "display_name": (("display_name",), "str"),
    "min_sm": (("min_sm",), "float"),
    "type": (("type",), "type"),
    "stability": (("stability",), "str"),
    "image": (("install", "spec"), "image"),
    "image_env": (("image_env",), "str"),
    "kv_formats": (("supported_kv_formats",), "list"),
    "families": (("supported_model_families",), "list"),
    "weight_formats": (("supported_weight_formats",), "list"),
    "drafters": (("supported_drafters",), "list"),
    "relation": (("lineage", "relation"), "str"),
    "base": (("lineage", "base"), "str"),
    "base_version": (("lineage", "base_version"), "str"),
    "source": (("lineage", "source"), "str"),
    "source_ref": (("lineage", "source_ref"), "str"),
}


class Refusal(Exception):
    pass


def _yaml():
    import yaml
    return yaml


def _split_comments(text: str) -> tuple[str, str]:
    """(the leading comment block, the rest), so a rewrite keeps the header."""
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines) and (lines[i].startswith("#") or not lines[i].strip()):
        i += 1
    return "".join(lines[:i]), "".join(lines[i:])


def _dump(header: str, data: dict) -> str:
    return header + _yaml().safe_dump(data, sort_keys=False, allow_unicode=True,
                                      default_flow_style=False, width=1000)


def _parse_kv(items: list[str], flag: str) -> list[tuple[str, str]]:
    out = []
    for kv in items:
        if "=" not in kv:
            raise Refusal(f"{flag} expects KEY=VALUE, got {kv!r}")
        k, v = kv.split("=", 1)
        out.append((k.strip(), v.strip()))
    return out


def _csv(v: str) -> list[str]:
    return [x.strip() for x in v.split(",") if x.strip()]


def _coerce(key: str, kind: str, value: str):
    if kind == "list":
        return _csv(value)
    if kind == "float":
        try:
            return float(value)
        except ValueError:
            raise Refusal(f"{key}={value!r}: expected a number")
    return value


def _changes(before: dict, after: dict, prefix: str = "") -> list[str]:
    lines = []
    for k in list(dict.fromkeys([*before, *after])):
        b, a = before.get(k), after.get(k)
        if isinstance(b, dict) or isinstance(a, dict):
            lines += _changes(b or {}, a or {}, f"{prefix}{k}.")
        elif b != a:
            lines.append(f"{prefix}{k}: {json.dumps(b)} -> {json.dumps(a)}"
                         if a is not None else f"{prefix}{k}: {json.dumps(b)} -> (removed)")
    return lines


def _reload_ok(root: Path) -> Optional[str]:
    chk = subprocess.run(
        [sys.executable, "-c", "from scripts.lib.profiles.compat import load_profiles; load_profiles()"],
        cwd=str(root), capture_output=True, text=True, encoding="utf-8",
        env=dict(os.environ, PYTHONUTF8=os.environ.get("PYTHONUTF8", "1")))
    return None if chk.returncode == 0 else (chk.stderr.strip() or chk.stdout.strip())[-800:]


def _compat(root: Path):
    sys.path.insert(0, str(root))
    try:
        from scripts.lib.profiles import compat, local_extensions
    finally:
        sys.path.pop(0)
    return compat, local_extensions


# ── update-weights ───────────────────────────────────────────────────────────

def plan_weights(root: Path, model: str, variant: str, sets: list[tuple[str, str]]):
    """(path, new text, change lines). Raises Refusal before anything is written."""
    yaml = _yaml()
    local_model = root / LOCAL / "models.d" / f"{model}.yml"
    ext = root / LOCAL / "extends.d" / f"{model}.yml"
    core = root / "scripts/lib/profiles/models" / f"{model}.yml"
    if local_model.is_file():
        path, kind = local_model, "model"
    elif ext.is_file():
        path, kind = ext, "extension"
    elif core.is_file():
        raise Refusal(f"{model} is a curated model and the local layer adds no variants to it "
                      f"(no {LOCAL}/extends.d/{model}.yml); curated weights change in a PR")
    else:
        raise Refusal(f"no local model {model!r} ({LOCAL}/models.d/ or extends.d/)")
    text = path.read_text(encoding="utf-8")
    header, _ = _split_comments(text)
    data = yaml.safe_load(text) or {}
    weights = data.get("weights") or {}
    if variant not in weights:
        curated = ""
        if kind == "extension" and core.is_file():
            if variant in ((yaml.safe_load(core.read_text(encoding="utf-8")) or {}).get("weights") or {}):
                curated = f" ({variant} is a curated variant of {model}; it changes in a PR)"
        raise Refusal(f"{path.relative_to(root)} has no weights variant {variant!r}{curated}. "
                      f"Its variants: {', '.join(sorted(weights)) or 'none'}")
    if not sets:
        raise Refusal(f"nothing to change: pass --set KEY=VALUE ({', '.join(sorted(WEIGHT_KEYS))})")
    new_meta = dict(weights[variant])
    for k, v in sets:
        if k not in WEIGHT_KEYS:
            raise Refusal(f"weights key {k!r} isn't editable here. Editable: {', '.join(sorted(WEIGHT_KEYS))}")
        if v == "":
            new_meta.pop(k, None)
        else:
            new_meta[k] = _coerce(k, WEIGHT_KEYS[k], v)
    new = dict(data)
    new["weights"] = {**weights, variant: new_meta}

    compat, ext_mod = _compat(root)
    try:
        if kind == "model":
            compat._model(new, path)
        else:
            core_data = yaml.safe_load(core.read_text(encoding="utf-8")) or {}
            core_ids = {p.stem for p in (root / "scripts/lib/profiles/models").glob("*.yml")}
            ext_mod.validate(new, path, core_ids, core_data.get("weights") or {},
                             compat.WEIGHTS_VARIANT_KEYS)
    except Exception as exc:
        raise Refusal(f"the result would not load: {exc}")
    changes = [f"weights.{variant}.{c}" for c in _changes(weights[variant], new_meta)]
    return path, _dump(header, new), changes


# ── update-engine ────────────────────────────────────────────────────────────

def _get(d: dict, where: tuple):
    for k in where[:-1]:
        d = d.get(k) or {}
    return d.get(where[-1])


def _put(d: dict, where: tuple, value) -> None:
    for k in where[:-1]:
        d = d.setdefault(k, {})
    if value is None or value == [] or value == "":
        d.pop(where[-1], None)
    else:
        d[where[-1]] = value


def plan_engine(root: Path, eid: str, sets, adds, removes):
    yaml = _yaml()
    sys.path.insert(0, str(root / "scripts/lib/profiles"))
    try:
        import catalog_rules
        import register_engine
    finally:
        sys.path.pop(0)
    found = catalog_rules._engine_index(root).get(eid)
    if not found:
        raise Refusal(f"no engine {eid!r} in the local layer ({LOCAL}/engines.d/)")
    _prof, path, is_core = found
    if is_core:
        raise Refusal(f"{eid!r} is a curated engine ({path.name}); it changes in a PR "
                      "(scripts/engine-pin-bump.sh for its image pin)")
    if not (sets or adds or removes):
        raise Refusal(f"nothing to change: pass --set / --add / --remove ({', '.join(ENGINE_KEYS)})")
    text = path.read_text(encoding="utf-8")
    header, _ = _split_comments(text)
    data = yaml.safe_load(text) or {}
    new = json.loads(json.dumps(data))           # a deep copy of plain YAML data

    for flag, pairs in (("--set", sets), ("--add", adds), ("--remove", removes)):
        for k, v in pairs:
            if k not in ENGINE_KEYS:
                raise Refusal(f"engine key {k!r} isn't editable here. Editable: {', '.join(ENGINE_KEYS)}")
            where, kind = ENGINE_KEYS[k]
            if flag != "--set" and kind != "list":
                raise Refusal(f"{flag} works on lists ({', '.join(x for x, (_, t) in ENGINE_KEYS.items() if t == 'list')}); "
                              f"use --set {k}=…")
            if flag == "--set":
                if kind == "type":
                    t = register_engine.normalize_type(v)
                    if not t:
                        raise Refusal(f"type {v!r}: one of {', '.join(register_engine.ENGINE_TYPES)}")
                    value = t
                elif kind == "image":
                    if not v or "${" in v or any(c.isspace() or c in "\"'" for c in v):
                        raise Refusal("image must be the concrete image reference the engine runs")
                    value = v
                    new.setdefault("install", {})["method"] = "docker_image"
                else:
                    value = _coerce(k, kind, v) if v != "" else None
                _put(new, where, value)
            else:
                cur = list(_get(new, where) or [])
                vals = _csv(v)
                cur = (cur + [x for x in vals if x not in cur]) if flag == "--add" \
                    else [x for x in cur if x not in vals]
                _put(new, where, cur)

    kv = _get(new, ENGINE_KEYS["kv_formats"][0]) or []
    known_kv = register_engine._hardware_kv_formats(root)
    bad = [k for k in kv if k not in known_kv and k not in (_get(data, ENGINE_KEYS["kv_formats"][0]) or [])]
    if bad:
        raise Refusal(f"kv format(s) {', '.join(bad)} aren't in any hardware profile's "
                      "supported_kv_formats; a new KV format needs a hardware-profile change in a PR")
    image_env = new.get("image_env")
    if image_env and image_env != data.get("image_env"):
        allowed = catalog_rules.launcher_image_envs(root)
        missing = [launcher for launcher, envs in allowed.items() if image_env not in envs]
        if missing:
            raise Refusal(f"image_env {image_env}: {', '.join(missing)} would stop every launch of "
                          "this engine's slugs with 'unexpected engine pin export'")
    compat, _ = _compat(root)
    try:
        compat._engine(new, path)
    except Exception as exc:
        raise Refusal(f"the result would not load: {exc}")
    return path, _dump(header, new), _changes(data, new)


# ── shared: plan, confirm, write, verify ─────────────────────────────────────

def apply(root: Path, path: Path, text: str, changes: list[str], *, dry_run: bool, yes: bool,
          tag: str) -> int:
    rel = path.relative_to(root)
    if not changes:
        print(f"[{tag}] {rel}: nothing changes.")
        return 0
    print(f"[{tag}] plan: update {rel}")
    for c in changes:
        print(f"[{tag}]   {c}")
    old = path.read_text(encoding="utf-8")
    if "#" in _split_comments(old)[1]:
        print(f"[{tag}]   note: comments after the file's header are not kept")
    if dry_run:
        print(f"[{tag}] dry-run — nothing written.")
        return 0
    if not yes and sys.stdin.isatty():
        try:
            reply = input(f"[{tag}] write it? [Y/n] ").strip().lower()
        except EOFError:
            reply = ""
        if reply not in ("", "y", "yes"):
            print(f"[{tag}] aborted — nothing written", file=sys.stderr)
            return EXIT_REFUSED
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    err = _reload_ok(root)
    if err:
        path.write_text(old, encoding="utf-8")
        print(f"[{tag}] ERROR: the profiles no longer load with this change, so the file was "
              f"put back:\n{err}", file=sys.stderr)
        return EXIT_INTERNAL
    print(f"UPDATE_OK {rel}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("weights", "engine"):
        print("usage: catalog_update.py weights|engine …", file=sys.stderr)
        return 64
    what = argv.pop(0)
    tag = f"update-{what}"
    ap = argparse.ArgumentParser(prog=f"catalog.sh {tag}")
    if what == "weights":
        ap.add_argument("--model", required=True)
        ap.add_argument("--variant", required=True)
    else:
        ap.add_argument("--id", required=True)
        ap.add_argument("--add", action="append", default=[], metavar="KEY=a,b")
        ap.add_argument("--remove", action="append", default=[], metavar="KEY=a,b")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument("--root", default=str(ROOT_DEFAULT))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-y", "--yes", action="store_true")
    args = ap.parse_args(argv)
    os.environ.setdefault("PYTHONUTF8", "1")
    root = Path(args.root).resolve()
    try:
        if what == "weights":
            path, text, changes = plan_weights(root, args.model, args.variant,
                                               _parse_kv(args.set, "--set"))
        else:
            path, text, changes = plan_engine(root, args.id, _parse_kv(args.set, "--set"),
                                              _parse_kv(args.add, "--add"),
                                              _parse_kv(args.remove, "--remove"))
    except Refusal as exc:
        print(f"[{tag}] REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    return apply(root, path, text, changes, dry_run=args.dry_run, yes=args.yes, tag=tag)


if __name__ == "__main__":
    raise SystemExit(main())
