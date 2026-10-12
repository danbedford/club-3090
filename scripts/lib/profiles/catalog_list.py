#!/usr/bin/env python3
"""catalog.sh list — what your LOCAL layer holds, and what in it is broken or unused.

    catalog.sh list [--json] [--root DIR]

`switch.sh --local` lists local slugs. This lists the whole layer
(scripts/lib/profiles-local/): engines, models, the variants attached to curated
models, slugs, and the problems among them:

  broken   an entry whose compose, engine, model or weights variant is missing; a
           file that doesn't parse. The profile loader refuses these, and with them
           every launcher.
  shadowed a local slug a curated one now shares; core wins the lookup
           (`catalog.sh rename` gives yours its name back).
  unused   an engine, model, attached variant or compose nothing uses.

It reads the files directly rather than through the profile loader, so it still
answers when the layer is broken. It writes nothing. Exit 0, even with problems.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

ROOT_DEFAULT = Path(__file__).resolve().parents[3]
LOCAL = "scripts/lib/profiles-local"
REGISTER_SIDE_EFFECT_MARK = "(local, registered from a compose)"   # register_engine.py


def _yaml_load(path: Path, problems: list) -> Optional[dict]:
    import yaml
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        problems.append(("broken", f"{path.name}: does not parse ({str(exc).splitlines()[0]})"))
        return None
    if not isinstance(data, dict):
        problems.append(("broken", f"{path.name}: not a YAML mapping"))
        return None
    return data


def _core(root: Path) -> tuple[dict, set, dict]:
    """(curated registry, curated engine ids, curated model id -> weights variants)."""
    reg: dict = {}
    sys.path.insert(0, str(root))
    try:
        from scripts.lib.profiles.compose_registry import COMPOSE_REGISTRY
        reg = dict(COMPOSE_REGISTRY)
    except Exception:
        pass
    finally:
        sys.path.pop(0)
    import yaml
    engines: set = set()
    for p in sorted((root / "scripts/lib/profiles/engines").glob("*.yml")):
        try:
            engines.add(str((yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("id")))
        except Exception:
            continue
    models: dict = {}
    for p in sorted((root / "scripts/lib/profiles/models").glob("*.yml")):
        try:
            models[p.stem] = set(((yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("weights") or {}))
        except Exception:
            continue
    return reg, engines, models


def inventory(root: Path) -> dict[str, Any]:
    local = root / LOCAL
    problems: list[tuple[str, str]] = []
    core_reg, core_engines, core_models = _core(root)

    entries: dict = {}
    reg_path = local / "registry.local.json"
    if reg_path.is_file():
        try:
            raw = json.loads(reg_path.read_text(encoding="utf-8"))
            entries = {s: e for s, e in raw.items() if isinstance(e, dict)} if isinstance(raw, dict) else {}
            if not isinstance(raw, dict):
                problems.append(("broken", "registry.local.json: not a JSON object"))
        except (OSError, ValueError) as exc:
            problems.append(("broken", f"registry.local.json: does not parse ({exc})"))

    engines: dict = {}
    for p in sorted((local / "engines.d").glob("*.yml")):
        d = _yaml_load(p, problems)
        if d is None:
            continue
        eid = str(d.get("id") or p.stem)
        engines[eid] = {
            "file": p.name, "type": d.get("type"),
            "registered": "with register (side effect)"
                          if REGISTER_SIDE_EFFECT_MARK in str(d.get("display_name") or "")
                          else "on its own (register-engine)",
            "image": (d.get("install") or {}).get("spec"),
            "lineage": bool(d.get("lineage")),
        }

    models: dict = {}
    for p in sorted((local / "models.d").glob("*.yml")):
        d = _yaml_load(p, problems)
        if d is None:
            continue
        models[str(d.get("id") or p.stem)] = {"file": p.name, "family": d.get("family"),
                                              "variants": sorted(d.get("weights") or {})}

    attached: dict = {}
    for p in sorted((local / "extends.d").glob("*.yml")):
        d = _yaml_load(p, problems)
        if d is None:
            continue
        attached[str(d.get("extends") or p.stem)] = {"file": p.name,
                                                     "variants": sorted(d.get("weights") or {})}

    slugs: dict = {}
    used_composes = set()
    for slug, e in sorted(entries.items()):
        model, engine = str(e.get("model") or ""), str(e.get("engine") or "")
        cpath = str(e.get("compose_path") or "")
        used_composes.add(cpath)
        flags = []
        if slug in core_reg:
            flags.append("shadowed")
            problems.append(("shadowed", f"{slug}: a curated slug has the same name and wins the "
                                         f"lookup; catalog.sh rename --slug {slug} --to <engine>/<name>"))
        if not cpath or not (root / cpath).is_file():
            flags.append("broken")
            problems.append(("broken", f"{slug}: compose {cpath or '(none)'} is missing"))
        if engine not in core_engines and engine not in engines:
            flags.append("broken")
            problems.append(("broken", f"{slug}: engine {engine!r} has no profile"))
        if model in models:
            variants = set(models[model]["variants"])
        elif model in core_models:
            variants = core_models[model] | set((attached.get(model) or {}).get("variants") or [])
        else:
            variants = None
        if variants is None:
            flags.append("broken")
            problems.append(("broken", f"{slug}: model {model!r} has no profile"))
        elif model in core_models and model not in attached:
            flags.append("broken")
            problems.append(("broken", f"{slug}: model {model!r} is curated, and no "
                                       f"extends.d/{model}.yml attaches local slugs to it"))
        else:
            for v in [e.get("weights_variant")] + list(e.get("weights_companions") or []):
                if v and v not in variants:
                    flags.append("broken")
                    problems.append(("broken", f"{slug}: weights variant {v!r} is not in {model}"))
        slugs[slug] = {"model": model, "engine": engine, "variant": e.get("weights_variant"),
                       "status": e.get("status"), "flags": sorted(set(flags))}

    def users(pred):
        return sorted(s for s, e in entries.items() if pred(e))

    for eid, info in engines.items():
        info["slugs"] = users(lambda e, i=eid: e.get("engine") == i)
        if not info["slugs"]:
            what = ("unregister-engine --id " + eid) if info["registered"].startswith("on its own") \
                else f"no slug uses it; remove {LOCAL}/engines.d/{info['file']}"
            problems.append(("unused", f"engine {eid}: no local slug runs on it ({what})"))
    for mid, info in models.items():
        info["slugs"] = users(lambda e, m=mid: e.get("model") == m)
        if not info["slugs"]:
            problems.append(("unused", f"model {mid}: no slug uses it ({LOCAL}/models.d/{info['file']})"))
    for mid, info in attached.items():
        info["slugs"] = users(lambda e, m=mid: e.get("model") == m)
        used = {v for s in info["slugs"] for v in
                [entries[s].get("weights_variant")] + list(entries[s].get("weights_companions") or [])}
        info["unused_variants"] = [v for v in info["variants"] if v not in used]
        if not info["slugs"]:
            problems.append(("unused", f"extends.d/{info['file']}: no slug is attached to {mid}"))
        for v in info["unused_variants"]:
            problems.append(("unused", f"extends.d/{info['file']}: variant {v!r} is used by no slug"))
    comp_root = local / "composes"
    if comp_root.is_dir():
        for p in sorted(comp_root.rglob("*.yml")):
            rel = str(p.relative_to(root))
            if rel not in used_composes:
                problems.append(("unused", f"{rel}: no entry uses this compose"))

    return {"layer": LOCAL, "engines": engines, "models": models, "attached": attached,
            "slugs": slugs, "problems": [{"kind": k, "what": w} for k, w in problems]}


def render(inv: dict) -> str:
    out = [f"[catalog] local layer: {inv['layer']}/"]
    if not (inv["engines"] or inv["models"] or inv["attached"] or inv["slugs"] or inv["problems"]):
        out.append("  Nothing is registered here. Start with: catalog.sh register --compose <file>")
        return "\n".join(out)

    def section(title, rows):
        out.append(f"\n{title} ({len(rows)})")
        out.extend(rows or ["  (none)"])

    section("Engines", [
        f"  {eid:28s} {str(i['type']):10s} {len(i['slugs'])} slug(s)   registered {i['registered']}"
        + ("" if i["lineage"] else "   · no lineage")
        for eid, i in inv["engines"].items()])
    section("Models", [
        f"  {mid:28s} family {str(i['family']):18s} variants: {', '.join(i['variants']) or '-'}"
        f"   {len(i['slugs'])} slug(s)" for mid, i in inv["models"].items()])
    section("Attached to curated models", [
        f"  {mid:28s} variants: {', '.join(i['variants']) or '(reuses curated ones)'}"
        f"   {len(i['slugs'])} slug(s)" for mid, i in inv["attached"].items()])
    section("Slugs", [
        f"  {s:44s} {i['model']:22s} {i['engine']:22s} {str(i['variant']):18s} {i['status']}"
        + (f"   [{', '.join(i['flags'])}]" if i["flags"] else "") for s, i in inv["slugs"].items()])
    probs = inv["problems"]
    if probs:
        out.append(f"\nProblems ({len(probs)})")
        for kind in ("broken", "shadowed", "unused"):
            out.extend(f"  {kind:8s} {p['what']}" for p in probs if p["kind"] == kind)
    else:
        out.append("\nNo problems found.")
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="catalog.sh list", description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--root", default=str(ROOT_DEFAULT))
    args = ap.parse_args(argv)
    os.environ.setdefault("PYTHONUTF8", "1")
    try:
        import yaml  # noqa: F401
    except ImportError:
        print("[catalog] list needs PyYAML (pip install pyyaml)", file=sys.stderr)
        return 2
    inv = inventory(Path(args.root).resolve())
    print(json.dumps(inv, indent=1) if args.json else render(inv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
