#!/usr/bin/env python3
"""catalog.sh register-engine — register an engine on its own in the LOCAL layer.

`catalog.sh register` writes an engine profile only as a side effect of
registering a slug, from what one compose shows. This writes
``scripts/lib/profiles-local/engines.d/<id>.yml`` directly, from what you state,
so an engine can be registered first and its slugs after
(docs/ADDING_ENGINES.md for what each field means):

    catalog.sh register-engine --id <id> --type <vllm|llama.cpp|sglang|exllamav3>
        --image <ref> [--display-name S] [--min-sm N] [--image-env VAR]
        [--kv-formats a,b] [--families a,b] [--weight-formats a,b]
        [--relation R --base B --base-version V --source URL --source-ref REF]
        [--root DIR] [--dry-run] [-y]

The profile is validated by the loader's own engine factory before anything is
written, and the profile set is re-loaded after; a failure removes the file.
It also holds what `catalog.sh register` uses to write its engine profiles
(``normalize_type``, ``render_engine_yaml``), so the two cannot drift apart.

    ENGINE_OK <id>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional

ROOT_DEFAULT = Path(__file__).resolve().parents[3]
LOCAL_ENGINES_REL = "scripts/lib/profiles-local/engines.d"
EXIT_REFUSED, EXIT_INTERNAL = 3, 2

# What an engine behaves like. `type` is not validated by the loader, and the
# consumers compare it as an exact string, so aliases are folded here.
_TYPES = {
    "vllm": "vllm",
    "llama.cpp": "llama.cpp", "llama-cpp": "llama.cpp", "llamacpp": "llama.cpp",
    "ik-llama": "llama.cpp", "ik_llama": "llama.cpp",
    "sglang": "sglang",
    "exllamav3": "exllamav3", "exl3": "exllamav3",
}
ENGINE_TYPES = ("vllm", "llama.cpp", "sglang", "exllamav3")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_IMAGE_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# The marker `catalog.sh register` puts in the display name of a profile it writes
# as a SIDE EFFECT of registering a slug. `unregister` removes only those once no
# slug uses them; one registered on purpose (register-engine) is kept.
REGISTER_SIDE_EFFECT_MARK = "(local, registered from a compose)"


class Refusal(Exception):
    pass


def normalize_type(value: str) -> Optional[str]:
    """`llamacpp` / `llama-cpp` / `ik-llama` → `llama.cpp`, etc.; None if unknown."""
    return _TYPES.get(str(value or "").strip().lower())


def _scalar(v) -> str:
    """A YAML scalar: plain when a block-context plain scalar reads back as the same
    string, otherwise JSON-quoted (also valid YAML)."""
    s = str(v)
    plain = (s and s == s.strip() and s[0] not in "-?:,[]{}#&*!|>'\"%@`"
             and ": " not in s and " #" not in s and not s.endswith(":")
             and not re.fullmatch(r"[-+.\d_]+|true|false|null|yes|no|on|off|~", s, re.I))
    return s if plain else json.dumps(s)


def render_engine_yaml(*, eid: str, display_name: str, etype: str, min_sm,
                       image: str = "", image_env: str = "",
                       kv_formats: Iterable[str] = (), families: Iterable[str] = (),
                       weight_formats: Iterable[str] = (), lineage: Optional[dict] = None,
                       notes: Iterable[str] = ()) -> str:
    """An engine profile's YAML in the curated files' key order. The image is one
    unquoted token on one `spec:` line, which engine-pin-bump.sh and
    resolved_config.py parse that way (docs/ADDING_ENGINES.md)."""
    lines = [
        "schema_version: 1",
        f"id: {eid}",
        f"display_name: {_scalar(display_name)}",
        f"type: {etype}",
        "stability: experimental",
        f"min_sm: {min_sm}",
    ]
    if image_env:
        lines.append(f"image_env: {image_env}")
    if image:
        lines += ["install:", "  method: docker_image", f"  spec: {image}"]
    for key, values in (("supported_kv_formats", kv_formats),
                        ("supported_model_families", families),
                        ("supported_weight_formats", weight_formats)):
        values = [v for v in values if v]
        if values:
            lines += [f"{key}:"] + [f"  - {_scalar(v)}" for v in values]
    if lineage:
        lines.append("lineage:")
        for k, v in lineage.items():
            lines.append(f"  {k}: {json.dumps(str(v))}")
    notes = list(notes)
    if notes:
        lines += ["notes: >"] + [f"  {n}" for n in notes]
    return "\n".join(lines) + "\n"


def _csv(value: Optional[str]) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _hardware_kv_formats(root: Path) -> set[str]:
    import yaml
    out: set[str] = set()
    for p in (root / "scripts/lib/profiles/hardware").glob("*.yml"):
        try:
            out.update((yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("supported_kv_formats") or [])
        except Exception:
            continue
    return out


def _known_values(root: Path, key: str) -> set[str]:
    """Values a field already takes somewhere in the catalog (core + local)."""
    import yaml
    out: set[str] = set()
    dirs = [root / "scripts/lib/profiles/engines", root / "scripts/lib/profiles/models",
            root / "scripts/lib/profiles-local/engines.d", root / "scripts/lib/profiles-local/models.d"]
    for d in dirs:
        for p in d.glob("*.yml") if d.is_dir() else ():
            try:
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            v = data.get(key)
            if isinstance(v, list):
                out.update(str(x) for x in v)
            elif v:
                out.add(str(v))
            if key == "family" or key == "format":
                for w in (data.get("weights") or {}).values():
                    if isinstance(w, dict) and key == "format" and w.get("format"):
                        out.add(str(w["format"]))
    return out


def _min_sm_default() -> str:
    """The card the engine demonstrably runs on, like `catalog.sh register`."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"],
                             capture_output=True, text=True, encoding="utf-8", timeout=10)
        first = (out.stdout.strip().splitlines() or [""])[0].strip()
        return first or "7.5"
    except Exception:
        return "7.5"


def build(root: Path, args) -> tuple[str, dict, list[str]]:
    """(yaml text, parsed data, notes). Raises Refusal before anything is written."""
    sys.path.insert(0, str(root / "scripts/lib/profiles"))
    try:
        import catalog_rules
    finally:
        sys.path.pop(0)
    notes: list[str] = []

    eid = args.id
    if not _ID_RE.match(eid or ""):
        raise Refusal(f"--id {eid!r}: expected [a-z0-9][a-z0-9._-]*")
    found = catalog_rules._engine_index(root).get(eid)
    if found and found[2]:
        raise Refusal(f"{eid!r} is a curated engine ({found[1].name}): register a slug on it "
                      f"instead (catalog.sh register --compose … --engine {eid})")
    if found:
        raise Refusal(f"a local engine {eid!r} is already registered ({found[1]}); edit that "
                      "file, or unregister its slugs and remove it first")
    if (root / LOCAL_ENGINES_REL / f"{eid}.yml").exists():
        raise Refusal(f"{LOCAL_ENGINES_REL}/{eid}.yml already exists (under another id)")

    etype = normalize_type(args.type)
    if not etype:
        raise Refusal(f"--type {args.type!r}: one of {', '.join(ENGINE_TYPES)} "
                      "(what the engine behaves like; aliases such as llamacpp are folded)")

    image = (args.image or "").strip()
    if not image or "${" in image or any(c.isspace() or c in "\"'" for c in image):
        raise Refusal("--image must be the concrete image reference the engine runs, e.g. "
                      "ghcr.io/org/engine@sha256:… (a published image; docs/ADDING_ENGINES.md)")

    image_env = (args.image_env or "").strip()
    if image_env:
        if not _IMAGE_ENV_RE.match(image_env):
            raise Refusal(f"--image-env {image_env!r}: expected an env var name like FOO_IMAGE")
        allowed = catalog_rules.launcher_image_envs(root)
        missing = [launcher for launcher, envs in allowed.items() if image_env not in envs]
        if missing:
            raise Refusal(
                f"--image-env {image_env}: {', '.join(missing)} would stop every launch of this "
                f"engine's slugs with 'unexpected engine pin export', because its "
                f"export_variant_engine_pin() case arm doesn't list it. Leave --image-env out "
                f"(each compose's own image: is used), or reuse one it lists "
                f"({', '.join(sorted(set.intersection(*allowed.values()))) if allowed else 'none'})")

    kv = _csv(args.kv_formats)
    known_kv = _hardware_kv_formats(root)
    bad = [k for k in kv if k not in known_kv]
    if bad:
        raise Refusal(f"--kv-formats {', '.join(bad)}: not in any hardware profile's "
                      f"supported_kv_formats, so no slug could use it here. A new KV format "
                      f"needs a hardware-profile change in a PR (known: {', '.join(sorted(known_kv))})")
    families = _csv(args.families)
    unknown_f = [f for f in families if f not in _known_values(root, "family")]
    if unknown_f:
        notes.append(f"families {', '.join(unknown_f)} are not used by any model profile yet")
    wfmts = _csv(args.weight_formats)
    unknown_w = [w for w in wfmts if w not in _known_values(root, "format")
                 and w not in _known_values(root, "supported_weight_formats")]
    if unknown_w:
        notes.append(f"weight formats {', '.join(unknown_w)} are not used by any weights entry yet")
    for flag, vals, check in (("--kv-formats", kv, "C4"), ("--families", families, "C10"),
                              ("--weight-formats", wfmts, "C14")):
        if not vals:
            notes.append(f"no {flag}: diagnose-profile and preflight will fail {check} for every "
                         f"slug on this engine until the profile lists them (catalog.sh "
                         f"update-engine --id {eid} --add {flag[2:].replace('-', '_')}=…)")

    lineage = None
    lin_args = {"relation": args.relation, "base": args.base, "base_version": args.base_version,
                "source": args.source, "source_ref": args.source_ref}
    if any(lin_args.values()):
        lineage = {k: v for k, v in lin_args.items() if v}
    else:
        notes.append("no lineage: fine locally, but export_pr.py needs relation, base, "
                     "base_version, source and source_ref (docs/ADDING_ENGINES.md → CONTRACT — "
                     f"engine lineage); add them later with catalog.sh update-engine --id {eid} "
                     "--set relation=…")

    text = render_engine_yaml(
        eid=eid, display_name=args.display_name or f"{eid} (local)", etype=etype,
        min_sm=args.min_sm or _min_sm_default(), image=image, image_env=image_env,
        kv_formats=kv, families=families, weight_formats=wfmts, lineage=lineage,
        notes=["Registered by catalog.sh register-engine. Capabilities are what was stated",
               "at registration, not measured; widen or correct them once measured."])

    # Validate with the loader's own factory, exactly as load_profiles will.
    import yaml
    data = yaml.safe_load(text)
    sys.path.insert(0, str(root))
    try:
        from scripts.lib.profiles import compat
        compat._engine(data, root / LOCAL_ENGINES_REL / f"{eid}.yml")
    except Exception as exc:
        raise Refusal(f"the profile would not load: {exc}")
    finally:
        sys.path.pop(0)
    return text, data, notes


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="catalog.sh register-engine",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("--id", required=True)
    ap.add_argument("--type", required=True, help=f"one of {', '.join(ENGINE_TYPES)}")
    ap.add_argument("--image", required=True, help="the image the engine runs")
    ap.add_argument("--display-name")
    ap.add_argument("--min-sm", help="lowest compute capability (default: this rig's card)")
    ap.add_argument("--image-env", help="the env var launchers inject the image as")
    ap.add_argument("--kv-formats", help="comma-separated")
    ap.add_argument("--families", help="model families, comma-separated")
    ap.add_argument("--weight-formats", help="comma-separated")
    ap.add_argument("--relation", help="stock | patched | derivative | fork | new")
    ap.add_argument("--base")
    ap.add_argument("--base-version")
    ap.add_argument("--source", help="public source repository")
    ap.add_argument("--source-ref", help="commit or release tag")
    ap.add_argument("--root", default=str(ROOT_DEFAULT))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-y", "--yes", action="store_true")
    args = ap.parse_args(argv)
    os.environ.setdefault("PYTHONUTF8", "1")

    root = Path(args.root).resolve()
    path = root / LOCAL_ENGINES_REL / f"{args.id}.yml"
    try:
        text, _data, notes = build(root, args)
    except Refusal as exc:
        print(f"[register-engine] REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    print(f"[register-engine] plan: write {LOCAL_ENGINES_REL}/{args.id}.yml (local layer only)")
    for ln in text.rstrip("\n").splitlines():
        print(f"[register-engine]   | {ln}")
    for n in notes:
        print(f"[register-engine]   note: {n}")
    if args.dry_run:
        print("[register-engine] dry-run — nothing written.")
        return 0
    if not args.yes and sys.stdin.isatty():
        try:
            reply = input("[register-engine] write it? [Y/n] ").strip().lower()
        except EOFError:
            reply = ""
        if reply not in ("", "y", "yes"):
            print("[register-engine] aborted — nothing written", file=sys.stderr)
            return EXIT_REFUSED

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        print(f"[register-engine] ERROR: could not write {path}: {exc}", file=sys.stderr)
        return EXIT_INTERNAL
    chk = subprocess.run(
        [sys.executable, "-c", "from scripts.lib.profiles.compat import load_profiles; load_profiles()"],
        cwd=str(root), capture_output=True, text=True, encoding="utf-8",
        env=dict(os.environ, PYTHONUTF8=os.environ.get("PYTHONUTF8", "1")))
    if chk.returncode != 0:
        path.unlink(missing_ok=True)
        print("[register-engine] ERROR: the profiles no longer load with it, so it was removed:\n"
              + (chk.stderr.strip() or chk.stdout.strip())[-800:], file=sys.stderr)
        return EXIT_INTERNAL
    print(f"[register-engine] wrote {path}")

    sys.path.insert(0, str(root / "scripts/lib/profiles"))
    try:
        import catalog_rules
        print("[register-engine] Would it pass the central registry? (advisory)")
        print(catalog_rules.render(f"engine {args.id}", catalog_rules.check_engine(root, args.id),
                                   network=False))
    except Exception as exc:  # advice only; the registration stands
        print(f"[register-engine] (catalog rules could not run: {exc})")
    finally:
        sys.path.pop(0)
    print(f"[register-engine] next: catalog.sh register --compose <compose> --engine {args.id}")
    print(f"ENGINE_OK {args.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
