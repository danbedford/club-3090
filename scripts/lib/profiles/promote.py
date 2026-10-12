#!/usr/bin/env python3
"""Catalog-promotion executor — writes a NEW model into the catalog.

Plan contract C4-rev (c3 ⑤ Promote-to-catalog): the cockpit's gated write plan
runs this script with the promotion spec in the environment, then chains
``diagnose-profile.sh <slug>`` + ``preflight-add-model.sh <slug>``::

    python3 scripts/lib/profiles/promote.py --spec-env C3_PROMOTE_SPEC

The spec is the JSON dict the c3 scaffold computed (``compute_promote_scaffold``
→ ``PromoteScaffold.spec``), with the user's inline edits applied::

    {
      "model_id": "...",            # [a-z0-9][a-z0-9._-]*  — <id>.yml stem
      "display_name": "...",        # REQUIRED non-empty
      "family": "...",              # REQUIRED non-empty family tag
      "arch": {...},                # optional ModelProfile arch dims (may be partial)
      "weights": {"<variant>": {...}},          # REQUIRED — the weights MAP
      "default_weight_variant": "...",          # REQUIRED — key into weights
      "compatible_drafters": [...],             # optional
      "vision_capable": true|false|null,        # null → omitted
      "compose": {"path": "...", "content": "<yaml text>"},
      "registry_entry": {"slug": "<engine>/...", "kwargs": {...}},
      "setup": {...}                            # optional C1 `setup:` block
    }

A slug ATTACHED to a curated model (catalog.sh register --model <curated id>)
sends a different spec: the curated ``model_id``, no profile fields, and

    {"attach": {"weights": {"<new variant>": {...}},   # only what it adds; may be {}
                "valid_tp_add": [3]}}                    # optional

It writes ``extends.d/<model_id>.yml`` (created, or merged with the variants already
there) instead of a model profile, LOCAL layer only. The merged extension is
validated before any write, since a broken one stops load_profiles() for every
launcher; a failed post-write check puts every file back.

TWO write targets (--layer, DEFAULT **local**):

**local** (the community path — C4-rev).  Writes ONLY inside the gitignored
``scripts/lib/profiles-local/`` layer; NO core catalog file is ever touched:
  1. ``scripts/lib/profiles-local/models.d/<model_id>.yml``
  2. the compose file at ``spec.compose.path`` (MUST live under
     ``scripts/lib/profiles-local/composes/``)
  3. a JSON MERGE into ``scripts/lib/profiles-local/registry.local.json``
     (plain-dict ``_entry`` kwargs — never a python-source edit).
The slug is ``<engine>/<name>``, like a curated one (the old ``local/`` prefix is
refused, #1202), and the entry is written with ``status="incubating"`` (hidden from
``switch.sh --list``, ``--force`` to launch — see compose_registry.STATUS_VALUES).

**core** (maintainer-only).  Today's curated-catalog behavior:
  1. ``scripts/lib/profiles/models/<model_id>.yml``
  2. the compose file anywhere under ``models/``
  3. the entry merged into ``scripts/lib/profiles/registry.yaml`` (a canonical
     rewrite of the data file; compose_registry.py is only its loader).
Double-gated: requires ``--layer core`` AND ``C3_ALLOW_CORE_PROMOTE=1`` in the
environment. Anything else refuses BEFORE writing — community users can never
reach the core catalog through this tool.

Refuses (exit 3, writes NOTHING) on any collision: existing model yml, existing
compose path, existing registry slug (core OR local), a bad model-id/slug shape,
or a namespace violation. Internal failures AFTER a write started exit 2 — there
is NO rollback; git is the rollback for core writes and "delete the file(s)" for
local ones (stated in every run's output). ``--dry-run`` prints the plan and
writes nothing.

Success marker (the c3 parser hook)::

    PROMOTE_OK <slug>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import zlib
import sys
from pathlib import Path
from typing import Any, Optional

# Repo-root default: this file lives at <root>/scripts/lib/profiles/promote.py.
_DEFAULT_ROOT = Path(__file__).resolve().parents[3]

_REGISTRY_REL = "scripts/lib/profiles/compose_registry.py"
_REGISTRY_YAML_REL = "scripts/lib/profiles/registry.yaml"
_MODELS_DIR_REL = "scripts/lib/profiles/models"
_EMIT_REL = "scripts/lib/registry-emit.sh"

# The C4-rev LOCAL layer (gitignored except its README — see its README.md).
_LOCAL_DIR_REL = "scripts/lib/profiles-local"
_LOCAL_MODELS_REL = f"{_LOCAL_DIR_REL}/models.d"
_LOCAL_COMPOSES_REL = f"{_LOCAL_DIR_REL}/composes"
_LOCAL_REGISTRY_REL = f"{_LOCAL_DIR_REL}/registry.local.json"
_LOCAL_EXTENDS_REL = f"{_LOCAL_DIR_REL}/extends.d"
_LOCAL_SLUG_PREFIX = "local/"
# Local entries ALWAYS land here (the enum's pre-experimental rung: hidden from
# switch.sh --list, --force to launch). Not user-selectable — promote up the
# enum by editing registry.local.json once the model validates.
_LOCAL_FORCED_STATUS = "incubating"
# Maintainer gate for the CORE write path (C4-rev): both the flag AND --layer
# core must be present, or the core catalog is untouchable.
_CORE_GATE_ENV = "C3_ALLOW_CORE_PROMOTE"

# #1142 / #1466: settings (club-3090 config, then the repo-root .env) through the ONE
# loader. Dual import so this file works both as a script (`python3
# scripts/lib/profiles/promote.py` — own dir on sys.path) and as a package module
# (`from scripts.lib.profiles import promote`, as the tests do).
try:  # package context
    from scripts.lib.club_config import load as load_config
except ImportError:  # direct-script context
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from club_config import load as load_config
try:  # package context
    from scripts.lib.profiles import compose_header
except ImportError:  # direct-script context: this directory is on sys.path
    import compose_header


_MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*/[a-z0-9][a-z0-9._-]*$")

EXIT_COLLISION = 3
EXIT_INTERNAL = 2


class Refusal(Exception):
    """A collision / bad-shape refusal → exit 3, nothing written."""


class InternalError(Exception):
    """A post-write failure → exit 2 (git is the rollback)."""


def _info(msg: str) -> None:
    print(f"[promote] {msg}")




def _yaml_scalar(v: Any) -> str:
    """Render a scalar as a YAML token. json.dumps produces valid YAML for the
    string/number/bool shapes the spec carries (UTF-8 output, double-quoted
    escapes YAML accepts)."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return json.dumps(v, ensure_ascii=False)


def _yaml_dump(data: Any, indent: int = 0) -> list[str]:
    """Minimal deterministic YAML emitter for the profile skeleton's shape
    (scalars / string-lists / one-level string→dict maps / one-level
    string→string maps). Key order = insertion order."""
    pad = " " * indent
    lines: list[str] = []
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict) and v:
                lines.append(f"{pad}{k}:")
                lines.extend(_yaml_dump(v, indent + 2))
            elif isinstance(v, list) and v:
                lines.append(f"{pad}{k}:")
                for item in v:
                    lines.append(f"{pad}  - {_yaml_scalar(item)}")
            else:
                lines.append(f"{pad}{k}: {_yaml_scalar(v)}")
    elif isinstance(data, list):
        for item in data:
            lines.append(f"{pad}- {_yaml_scalar(item)}")
    return lines


def render_profile_yaml(spec: dict) -> str:
    """The ModelProfile YAML for the spec — canonical ModelProfile schema keys
    (compat.ModelProfile: ``num_attn_heads`` / ``head_dim_attn`` /
    ``max_ctx_supported``). Only arch dims WITH values are emitted; the
    remaining family-specific extras are added by hand afterwards (same as
    every shipped profile)."""
    mid = spec["model_id"]
    arch = spec.get("arch") or {}
    weights = spec["weights"]

    root: dict[str, Any] = {
        "schema_version": 1,
        "id": mid,
        "display_name": spec["display_name"],
        "family": spec["family"],
    }
    # Arch dims — canonical schema names, only when the fact exists.
    for spec_key, yml_key in (
        ("hidden_size", "hidden_size"),
        ("num_hidden_layers", "num_hidden_layers"),
        ("num_attn_heads", "num_attn_heads"),
        ("num_kv_heads", "num_kv_heads"),
        ("head_dim_attn", "head_dim_attn"),
        ("max_ctx_supported", "max_ctx_supported"),
        # Required by compat.load_profiles for the CORE catalog — a core write
        # whose spec omits it fails the post-write registry-emit gate (exit 2),
        # which is the correct outcome: an incomplete profile never lands in
        # the curated catalog. Local-layer profiles bypass load_profiles.
        ("attention_k_eq_v", "attention_k_eq_v"),
    ):
        if arch.get(spec_key) is not None:
            root[yml_key] = arch[spec_key]
    if arch.get("valid_tp"):
        root["valid_tp"] = list(arch["valid_tp"])
    # compat.ModelProfile REQUIRES attention_k_eq_v — emit the conservative
    # default when the spec doesn't carry the fact (corrected by hand / flagged
    # by the post-write diagnose + kv-calc gates during validation).
    root["attention_k_eq_v"] = bool(arch.get("attention_k_eq_v"))

    root["weights"] = weights
    root["default_weight_variant"] = spec["default_weight_variant"]
    root["compatible_drafters"] = list(spec.get("compatible_drafters") or [])
    if spec.get("vision_capable") is not None:
        root["vision_capable"] = bool(spec["vision_capable"])
    if spec.get("setup"):
        root["setup"] = spec["setup"]

    where = (
        f"{_LOCAL_MODELS_REL}/{mid}.yml"
        if spec.get("_layer") == "local"
        else f"{_MODELS_DIR_REL}/{mid}.yml"
    )
    header = (
        f"# {spec['display_name']} — scaffolded by c3 Promote-to-catalog\n"
        f"# ({where} via scripts/lib/profiles/promote.py).\n"
        "# INCUBATING: fill family-specific arch extras + notes by hand, then\n"
        "# promote the status enum as the model validates (ADDING_MODELS.md).\n"
    )
    return header + "\n".join(_yaml_dump(root)) + "\n"




def _import_compose_registry(root: Path, *, merged: bool):
    """Import the CURRENT registry (pre-write) for the collision checks.

    merged=True returns the C4-rev merged view (core + local layer) so a local
    write refuses a slug that exists ANYWHERE; merged=False is the core dict
    (used by the core path, which additionally checks the local file below)."""
    sys.path.insert(0, str(root))
    try:
        if merged:
            from scripts.lib.profiles.compose_registry import get_registry  # noqa: E402

            return get_registry()
        from scripts.lib.profiles.compose_registry import COMPOSE_REGISTRY  # noqa: E402

        return COMPOSE_REGISTRY
    except Exception as exc:
        raise Refusal(f"compose_registry.py does not import: {exc}")


def _load_local_raw(root: Path) -> dict:
    """The RAW registry.local.json dict ({} when absent). Bad JSON is a
    refusal — we never overwrite a file we cannot parse."""
    path = root / _LOCAL_REGISTRY_REL
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refusal(f"{path} is unreadable — fix or remove it first: {exc}")
    if not isinstance(raw, dict):
        raise Refusal(f"{path}: expected a JSON object of {{slug: kwargs}}")
    return raw


def _append_registry_entry(root: Path, slug: str, kwargs: dict) -> None:
    """Merge the new entry into <root>'s registry.yaml (canonical rewrite).

    The old CORE path did an anchored textual insert into compose_registry.py
    — fragile by construction (brace anchors, comma repair, source parsing).
    Now: load the DATA file through the module's own reader, append the
    `_entry(**kwargs)` row, re-dump with the ONE shared deterministic writer,
    and atomically replace (temp sibling + os.replace — a failed encode can
    never truncate the catalog). Import-time validation of the result happens
    in the post-write checks; a kwargs problem surfaces THERE as exit 2."""
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from scripts.lib.profiles.compose_registry import (  # noqa: E402
            dump_registry_yaml,
            load_registry_data,
        )
    except Exception as exc:
        raise InternalError(f"compose_registry.py does not import: {exc}")
    path = root / _REGISTRY_YAML_REL
    data = load_registry_data(path)
    if slug in data["entries"]:
        raise Refusal(f"registry slug already exists in {_REGISTRY_YAML_REL}: {slug}")
    data["entries"][slug] = dict(kwargs)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(dump_registry_yaml(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise InternalError(f"could not rewrite {_REGISTRY_YAML_REL}: {exc}")


def _run(cmd: list[str], *, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)


def _post_write_checks(root: Path, spec: dict, profile_path: Path) -> None:
    """Import-sanity + registry-emit re-check. Any failure exits 2 — the write
    already happened, so the message points at the rollback."""
    slug = spec["registry_entry"]["slug"]
    mid = spec["model_id"]
    rollback_hint = (
        f"revert with git: git checkout -- {_REGISTRY_YAML_REL}"
        if spec.get("_layer") == "core"
        else f"delete the written files ({_LOCAL_DIR_REL}/…)"
    )
    no_rollback = "NO ROLLBACK — "
    if spec.get("_attach") is not None:   # main() puts every file back
        rollback_hint, no_rollback = "the files are put back", ""

    # 1) The mutated registry imports and carries the new slug (merged view —
    #    covers BOTH layers; the local JSON merge is visible via get_registry).
    chk = _run(
        [
            "python3",
            "-c",
            "from scripts.lib.profiles.compose_registry import get_registry; "
            f"assert {slug!r} in get_registry()",
        ],
        cwd=root,
    )
    if chk.returncode != 0:
        raise InternalError(
            "registry failed import-sanity after write:\n"
            + (chk.stderr.strip() or chk.stdout.strip())
            + f"\n[promote] {no_rollback}{rollback_hint}"
        )

    # 2) The written profile parses as YAML with the right id.
    try:
        import yaml  # noqa: E402
    except Exception as exc:
        raise InternalError(f"PyYAML unavailable, cannot sanity-check the profile: {exc}")
    try:
        data = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
        key = "extends" if spec.get("_attach") is not None else "id"
        assert data.get(key) == mid, f"profile {key} {data.get(key)!r} != {mid!r}"
    except Exception as exc:
        raise InternalError(
            f"written profile failed sanity: {exc}\n"
            f"[promote] {no_rollback}" + ("the files are put back" if not no_rollback else f"delete {profile_path}")
        )
    # An attached slug: the whole profile set must still load. A broken extension
    # stops load_profiles() for every launcher, curated slugs included.
    if spec.get("_attach") is not None:
        lp = _run(["python3", "-c", "from scripts.lib.profiles.compat import load_profiles; "
                   "load_profiles()"], cwd=root)
        if lp.returncode != 0:
            raise InternalError("the profiles no longer load with the extension:\n"
                                + (lp.stderr.strip() or lp.stdout.strip())[-800:])

    # 3) The shared emitter sees the new slug end-to-end.
    emit = _run(["bash", _EMIT_REL, "--json", str(root)], cwd=root)
    if emit.returncode != 0:
        raise InternalError(
            "registry-emit.sh --json failed after write:\n"
            + (emit.stderr.strip() or emit.stdout.strip())[-800:]
            + f"\n[promote] {no_rollback}{rollback_hint}"
        )
    try:
        payload = json.loads(emit.stdout)
        slugs = {v.get("slug") for v in payload.get("variants") or []}
        assert slug in slugs, f"slug {slug!r} absent from registry-emit output"
    except Exception as exc:
        raise InternalError(
            f"registry-emit re-check failed: {exc}\n"
            f"[promote] {no_rollback}{rollback_hint}"
        )


def _check_common_shape(spec: Any) -> dict:
    """Spec-shape validation shared by BOTH layers. Raises Refusal."""
    if not isinstance(spec, dict):
        raise Refusal("spec is not a JSON object")

    mid = spec.get("model_id")
    if not isinstance(mid, str) or not _MODEL_ID_RE.match(mid or ""):
        raise Refusal(f"bad model_id {mid!r} — expected [a-z0-9][a-z0-9._-]*")
    for key in ("display_name", "family"):
        v = spec.get(key)
        if not isinstance(v, str) or not v.strip():
            raise Refusal(f"spec.{key} is required and must be a non-empty string")
    weights = spec.get("weights")
    if not isinstance(weights, dict) or not weights:
        raise Refusal("spec.weights must be a non-empty map")
    dwv = spec.get("default_weight_variant")
    if dwv not in weights:
        raise Refusal(f"spec.default_weight_variant {dwv!r} is not a weights key")

    _check_entry_shape(spec, mid)
    return spec


def _refuse_if_path_escapes(rel: str, base_rel: str, what: str) -> None:
    """Refuse a spec-supplied relative path that escapes (or sits outside) the
    given layer directory — no traversal out of scripts/lib/profiles-local/."""
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        raise Refusal(f"{what} must be a repo-relative path without '..': {rel!r}")
    if not p.is_relative_to(Path(base_rel)):
        raise Refusal(f"{what} must live under {base_rel}/ (got {rel!r})")


def _validate_local_entry(spec: dict, root: Path) -> None:
    """The checks every LOCAL registry entry gets, whether its model is new
    (models.d) or curated (attached through extends.d). Raises Refusal."""
    mid = spec["model_id"]
    slug = spec["registry_entry"]["slug"]
    kwargs = spec["registry_entry"]["kwargs"]
    cpath = spec["compose"]["path"]
    # ── Namespace + containment (C4-rev): local writes NEVER leave the layer.
    # HARD-CUT (#1202 P3): local slugs are '<engine>/<name>', same as core.
    if slug.startswith(_LOCAL_SLUG_PREFIX):
        raise Refusal(
            f"the {_LOCAL_SLUG_PREFIX!r} namespace was removed — local slugs "
            f"now use '<engine>/<name>' (provenance is the 'origin' field). "
            f"Use '<engine>/{slug[len(_LOCAL_SLUG_PREFIX):]}'."
        )
    if slug.count("/") != 1:
        raise Refusal(f"local slug {slug!r} must be '<engine>/<name>'")
    _refuse_if_path_escapes(cpath, _LOCAL_COMPOSES_REL, "spec.compose.path")
    if kwargs.get("compose_path") != cpath:
        raise Refusal(
            f"registry kwargs.compose_path {kwargs.get('compose_path')!r} != "
            f"compose.path {cpath!r}"
        )
    # Force the incubating rung — a local entry never ships functional.
    kwargs["status"] = _LOCAL_FORCED_STATUS

    compose_abs = root / cpath
    if compose_abs.exists():
        raise Refusal(f"compose path already exists: {compose_abs}")

    # Collide against EVERYTHING (core + local) via the merged accessor.
    registry = _import_compose_registry(root, merged=True)
    if slug in registry:
        raise Refusal(f"registry slug already exists: {slug}")
    if slug in _load_local_raw(root):
        raise Refusal(f"registry slug already exists in {_LOCAL_REGISTRY_REL}: {slug}")
    _check_local_engine_profile(root, spec)

    # Dry-wrap the kwargs EXACTLY like the loader will — a bad kwarg or a
    # bad status must refuse BEFORE anything is written.
    sys.path.insert(0, str(root))
    try:
        from scripts.lib.profiles.compose_registry import _entry  # noqa: E402

        _entry(**kwargs)
    except TypeError as exc:
        raise Refusal(f"registry kwargs are not valid _entry kwargs: {exc}")
    except ValueError as exc:
        raise Refusal(f"registry kwargs rejected by _entry: {exc}")
    # ── LOCAL: keep BYOM ports out of the curated band (#1142 follow-up) ──
    # The curated catalog occupies 8010-8199. A local slug landing on one of
    # those ports turns the repo's OWN test-compose-port-conflicts guard red
    # on the user's checkout, and its remediation text invites reassigning
    # "one side" — i.e. editing a curated entry they must never touch.
    # Refuse the collision here and hand them the deterministic 202xx
    # LOCAL-band port the c3 Promote scaffold already assigns, so the CLI and
    # the cockpit agree on one convention.
    want = kwargs.get("default_port")
    if want is not None:
        core_ports = {
            e.get("default_port")
            for e in _import_compose_registry(root, merged=False).values()
        }
        if want in core_ports:
            suggested = 20200 + (zlib.crc32(mid.encode("utf-8")) % 100)
            raise Refusal(
                f"default_port {want} is already used by the curated catalog. "
                f"LOCAL models live in the 202xx band so a `git pull` of new "
                f"curated slugs can never collide with yours — use {suggested} "
                f"(deterministic for model id {mid!r}; the same value c3's "
                f"Promote scaffold assigns) and set the compose's "
                f"${{PORT:-NNNN}} to match"
            )


def _check_entry_shape(spec: dict, mid: str) -> None:
    """The compose + registry_entry part of a spec, shared by both spec shapes."""
    compose = spec.get("compose") or {}
    cpath = compose.get("path")
    ccontent = compose.get("content")
    if not isinstance(cpath, str) or not cpath:
        raise Refusal("spec.compose.path is required")
    if not isinstance(ccontent, str) or not ccontent.strip():
        raise Refusal("spec.compose.content is required (the compose YAML text)")
    entry = spec.get("registry_entry") or {}
    slug = entry.get("slug")
    kwargs = entry.get("kwargs")
    if not isinstance(slug, str) or not _SLUG_RE.match(slug or ""):
        raise Refusal(f"bad registry slug {slug!r} — expected <engine>/<name>")
    if not isinstance(kwargs, dict) or "model" not in kwargs or "compose_path" not in kwargs:
        raise Refusal("spec.registry_entry.kwargs must be a dict incl. model + compose_path")
    if kwargs.get("model") != mid:
        raise Refusal(f"registry kwargs.model {kwargs.get('model')!r} != model_id {mid!r}")


def _validate_attach(spec: Any, root: Path, layer: str) -> dict:
    """A slug ATTACHED to a curated model (local_extensions.py): no models.d profile.
    ``spec.attach`` = {"weights": {<new variant>: {...}}, "valid_tp_add": [...]}, both
    optional. The merged ``extends.d/<id>.yml`` is validated here, before anything
    is written, because a broken extension stops load_profiles() for every launcher,
    curated slugs included."""
    if layer != "local":
        raise Refusal("attaching a slug to a curated model is LOCAL-layer only; in the "
                      "curated catalog, add the slug and its weights to "
                      "scripts/lib/profiles/models/<id>.yml in a PR")
    mid = spec.get("model_id")
    if not isinstance(mid, str) or not _MODEL_ID_RE.match(mid or ""):
        raise Refusal(f"bad model_id {mid!r} — expected [a-z0-9][a-z0-9._-]*")
    attach = spec.get("attach")
    if not isinstance(attach, dict):
        raise Refusal("spec.attach must be {weights: {...}, valid_tp_add: [...]}")
    new_w = attach.get("weights") or {}
    tp_add = attach.get("valid_tp_add") or []
    if not isinstance(new_w, dict) or not all(isinstance(v, dict) for v in new_w.values()):
        raise Refusal("spec.attach.weights must map variant -> mapping")
    if not isinstance(tp_add, list) or not all(isinstance(x, int) and x > 0 for x in tp_add):
        raise Refusal("spec.attach.valid_tp_add must be a list of positive integers")
    _check_entry_shape(spec, mid)
    spec["_layer"] = layer

    core_path = root / _MODELS_DIR_REL / f"{mid}.yml"
    if not core_path.is_file():
        raise Refusal(f"{mid!r} is not a curated model ({_MODELS_DIR_REL}/{mid}.yml) — "
                      "a model only your rig has gets its own profile instead")
    try:
        import yaml  # noqa: E402
        core = yaml.safe_load(core_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise Refusal(f"cannot read {core_path}: {exc}")
    core_w = core.get("weights") or {}
    core_tp = [int(x) for x in (core.get("valid_tp") or [])]

    ext_path = root / _LOCAL_EXTENDS_REL / f"{mid}.yml"
    old_text = None
    existing: dict = {}
    if ext_path.exists():
        try:
            old_text = ext_path.read_text(encoding="utf-8")
            existing = yaml.safe_load(old_text) or {}
        except Exception as exc:
            raise Refusal(f"{ext_path} is unreadable — fix or remove it first: {exc}")
        if not isinstance(existing, dict):
            raise Refusal(f"{ext_path}: expected a YAML mapping")
    merged = {"schema_version": 1, "extends": mid, **existing}
    merged_w = dict(merged.get("weights") or {})
    added: list[str] = []
    for name, meta in new_w.items():
        if name in core_w:
            raise Refusal(f"weights variant {name!r} is curated: reuse it as the slug's "
                          "weights variant instead of adding it")
        if name in merged_w:
            if merged_w[name].get("path") != meta.get("path"):
                raise Refusal(
                    f"{_LOCAL_EXTENDS_REL}/{mid}.yml already has variant {name!r} at path "
                    f"{merged_w[name].get('path')!r}; these weights are at "
                    f"{meta.get('path')!r}. Pick another variant name.")
            continue                       # the same weights: reuse the entry as it is
        merged_w[name] = meta
        added.append(name)
    merged["weights"] = merged_w
    have_tp = [int(x) for x in (merged.get("valid_tp_add") or [])]
    added_tp = [t for t in tp_add if t not in core_tp and t not in have_tp]
    if have_tp or added_tp:
        merged["valid_tp_add"] = have_tp + added_tp

    sys.path.insert(0, str(root))
    try:
        from scripts.lib.profiles import local_extensions as _ext  # noqa: E402
        from scripts.lib.profiles.compat import WEIGHTS_VARIANT_KEYS  # noqa: E402
        core_ids = {pth.stem for pth in (root / _MODELS_DIR_REL).glob("*.yml")}
        _ext.validate(merged, ext_path, core_ids, core_w, WEIGHTS_VARIANT_KEYS)
    except Refusal:
        raise
    except Exception as exc:
        raise Refusal(f"the extension would not load: {exc}")
    finally:
        sys.path.pop(0)

    kwargs = spec["registry_entry"]["kwargs"]
    for name in [kwargs.get("weights_variant")] + list(kwargs.get("weights_companions") or []):
        if name and name not in core_w and name not in merged_w:
            raise Refusal(f"weights variant {name!r} is neither curated in {mid} nor in "
                          f"spec.attach.weights")
    tp = kwargs.get("tp")
    if isinstance(tp, int) and tp not in core_tp and tp not in merged.get("valid_tp_add", []):
        raise Refusal(f"tp {tp} is not in {mid}'s valid_tp {core_tp}: add it to "
                      "spec.attach.valid_tp_add")

    _validate_local_entry(spec, root)
    spec["_attach"] = {
        "path": ext_path, "data": merged, "old_text": old_text, "added": added,
        "added_tp": added_tp,
        "action": "write" if old_text is None else ("update" if added or added_tp else "keep"),
    }
    return spec


def validate_spec(spec: Any, root: Path, layer: str) -> dict:
    """Shape + collision validation. Raises Refusal (exit 3) — nothing written."""
    if isinstance(spec, dict) and "attach" in spec:
        return _validate_attach(spec, root, layer)
    spec = _check_common_shape(spec)
    spec["_layer"] = layer
    mid = spec["model_id"]
    slug = spec["registry_entry"]["slug"]
    kwargs = spec["registry_entry"]["kwargs"]
    cpath = spec["compose"]["path"]

    if layer == "local":
        profile_path = root / _LOCAL_MODELS_REL / f"{mid}.yml"
        if profile_path.exists():
            raise Refusal(f"local model profile already exists: {profile_path}")
        # …and against core/local MODEL IDS (the loader refuses these loudly, so
        # refuse here where the message can name the exact fix).
        core_models = {e.get("model") for e in _import_compose_registry(root, merged=False).values()}
        if mid in core_models:
            raise Refusal(
                f"model_id {mid!r} collides with a CORE model — pick another id, or "
                f"attach a slug to it (catalog.sh register --model {mid} --weights-variant …)"
            )
        local_raw = _load_local_raw(root)
        if any(k.get("model") == mid for k in local_raw.values()):
            raise Refusal(f"local model id already exists in {_LOCAL_REGISTRY_REL}: {mid!r}")
        _validate_local_entry(spec, root)
        return spec

    # ── layer == "core": maintainer-gated curated-catalog write ──────────────
    if os.environ.get(_CORE_GATE_ENV) != "1":
        raise Refusal(
            f"core-catalog writes are maintainer-gated: re-run with --layer core "
            f"AND {_CORE_GATE_ENV}=1 — exported in the shell OR saved with "
            f"`bash scripts/settings.sh set {_CORE_GATE_ENV}=1` (the shell wins). Community "
            f"users: use the default --layer local"
        )
    if slug.startswith(_LOCAL_SLUG_PREFIX):
        raise Refusal(
            f"the {_LOCAL_SLUG_PREFIX!r} namespace belongs to the LOCAL layer — "
            "core slugs are <engine>/<name>"
        )
    # ── Symmetric to the LOCAL containment check above (#1205 follow-up) ─────
    # LOCAL writes may not leave the layer; CORE writes may not reach INTO it.
    # `profiles-local/` is GITIGNORED, so a curated row whose compose_path points
    # there would reference a file that exists on the maintainer's disk and
    # nowhere else — the registry would ship pointing at nothing. This used to be
    # unreachable by accident: every local-layer spec also carried a `local/`
    # slug, so the namespace check above refused first. #1205 removed that
    # namespace and with it the only thing standing in front of this.
    if Path(cpath).is_relative_to(Path(_LOCAL_DIR_REL)):
        raise Refusal(
            f"compose.path {cpath!r} is inside the LOCAL layer ({_LOCAL_DIR_REL}/), "
            f"which is gitignored — a curated entry must not point there. Move the "
            f"compose under models/{mid}/ for a core write, or use --layer local."
        )
    profile_path = root / _MODELS_DIR_REL / f"{mid}.yml"
    if profile_path.exists():
        raise Refusal(f"model profile already exists: {profile_path}")
    compose_abs = root / cpath
    if compose_abs.exists():
        raise Refusal(f"compose path already exists: {compose_abs}")
    registry = _import_compose_registry(root, merged=True)
    if slug in registry:
        raise Refusal(f"registry slug already exists: {slug}")
    local_raw = _load_local_raw(root)
    if slug in local_raw:
        raise Refusal(f"registry slug already exists in {_LOCAL_REGISTRY_REL}: {slug}")
    if spec.get("local_engine_profile"):
        raise Refusal("local_engine_profile is LOCAL-layer only — a curated engine "
                      "profile is a scripts/lib/profiles/engines/<id>.yml in a PR")
    return spec


_LOCAL_ENGINES_REL = f"{_LOCAL_DIR_REL}/engines.d"


def _check_local_engine_profile(root: Path, spec: dict) -> None:
    """Optional spec.local_engine_profile = {id, yaml}: a NEW local engine profile
    (catalog.sh sends one for an engine the catalog does not know). Validated here,
    written by _write_local — never before the plan / --dry-run / confirm. An
    existing profile is never overwritten (spec['_engine_profile_action'] = keep)."""
    ep = spec.get("local_engine_profile")
    if not ep:
        return
    if not isinstance(ep, dict) or not isinstance(ep.get("id"), str) or not isinstance(ep.get("yaml"), str):
        raise Refusal("spec.local_engine_profile must be {id: str, yaml: str}")
    eid = ep["id"]
    if not eid or "/" in eid or eid.startswith(".") or not eid.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise Refusal(f"spec.local_engine_profile.id {eid!r} is not a safe engine id")
    path = root / _LOCAL_ENGINES_REL / f"{eid}.yml"
    spec["_engine_profile_path"] = path
    spec["_engine_profile_action"] = "keep" if path.exists() else "write"


def plan_lines(root: Path, spec: dict) -> list[str]:
    """The dry-run plan (also printed before a real run)."""
    mid = spec["model_id"]
    slug = spec["registry_entry"]["slug"]
    if spec["_layer"] == "local":
        eng = []
        att = spec.get("_attach")
        if spec.get("_engine_profile_path") is not None:
            rel = f"{_LOCAL_ENGINES_REL}/{spec['_engine_profile_path'].name}"
            eng = [f"  write {rel} (new local ENGINE profile)"
                   if spec.get("_engine_profile_action") == "write"
                   else f"  keep  {rel} (already exists — not overwritten)"]
        if att is not None:
            rel = f"{_LOCAL_EXTENDS_REL}/{mid}.yml"
            what = []
            if att["added"]:
                what.append(f"variant(s) {', '.join(att['added'])}")
            if att["added_tp"]:
                what.append(f"valid_tp_add {att['added_tp']}")
            ext_line = {
                "write": f"  write {rel} (attaches local slugs to the curated model; "
                         f"{', '.join(what) or 'no new variants'})",
                "update": f"  update {rel}: add {', '.join(what)}"
                          + (" (rewritten; comments in it are not kept)"
                             if "#" in (att["old_text"] or "") else ""),
                "keep": f"  keep  {rel} (already has what this slug needs)",
            }[att["action"]]
            variant = spec["registry_entry"]["kwargs"].get("weights_variant")
            return [
                f"plan (layer LOCAL — attach to the curated model {mid}, root {root}):",
                *eng,
                ext_line,
                f"  write {spec['compose']['path']}",
                f"  merge entry into {_LOCAL_REGISTRY_REL} (JSON — no source edit)",
                f"  slug: {slug}  model: {mid} (curated)  variant: {variant}"
                f"  status: {_LOCAL_FORCED_STATUS}",
                "  then: load_profiles + get_registry() + registry-emit.sh --json re-checks;"
                " a failure puts every file back",
                f"  NO core file is touched. Undo later: catalog.sh unregister --slug {slug}"
                f" (it keeps {rel})",
            ]
        return [
            f"plan (layer LOCAL — gitignored {_LOCAL_DIR_REL}/, root {root}):",
            *eng,
            f"  write {_LOCAL_MODELS_REL}/{mid}.yml",
            f"  write {spec['compose']['path']}",
            f"  merge entry into {_LOCAL_REGISTRY_REL} (JSON — no source edit)",
            f"  slug: {slug}  model: {mid}  variant: {spec['default_weight_variant']}"
            f"  status: {_LOCAL_FORCED_STATUS}",
            "  then: get_registry() import-sanity + registry-emit.sh --json re-check",
            "  NO core file is touched. Rollback: delete the written files.",
        ]
    return [
        f"plan (layer CORE — maintainer, root {root}):",
        f"  write {_MODELS_DIR_REL}/{mid}.yml",
        f"  write {spec['compose']['path']}",
        f"  merge entry into {_REGISTRY_YAML_REL} (canonical rewrite — no source edit)",
        f"  slug: {slug}  model: {mid}  variant: {spec['default_weight_variant']}",
        "  then: import-sanity + registry-emit.sh --json re-check",
        "  NO ROLLBACK — git is the rollback (dry-run writes nothing).",
    ]


_EXT_HEADER = (
    "# Attaches local slugs to the curated model {mid} (local_extensions.py).\n"
    "# Additive only: new weights variants and valid_tp values, never an override.\n"
    "# Written by catalog.sh register / promote.py; safe to edit by hand.\n"
)


def _write_extension(att: dict) -> None:
    """extends.d/<id>.yml through a temp sibling + os.replace: other slugs may
    already depend on this file, so a half-written one must never be seen."""
    import yaml  # noqa: E402
    path = att["path"]
    text = _EXT_HEADER.format(mid=att["data"]["extends"]) + yaml.safe_dump(
        att["data"], sort_keys=False, allow_unicode=True, default_flow_style=False)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        _info(f"wrote {path}")
    except OSError as exc:
        raise InternalError(f"could not write {path}: {exc}")


def _snapshot(paths: list[Path]) -> dict[Path, Optional[bytes]]:
    return {pth: (pth.read_bytes() if pth.exists() else None) for pth in paths}


def _restore(snap: dict[Path, Optional[bytes]], stop: Path) -> list[str]:
    """Put every snapshotted file back as it was (absent → removed, along with the
    directories the write created for it, up to ``stop``)."""
    done = []
    for pth, data in snap.items():
        try:
            if data is None:
                if pth.exists():
                    pth.unlink()
                    done.append(f"removed {pth}")
                parent = pth.parent
                while parent != stop and stop in parent.parents and parent.is_dir() \
                        and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
            else:
                pth.write_bytes(data)
                done.append(f"restored {pth}")
        except OSError as exc:
            done.append(f"could not restore {pth}: {exc}")
    return done


_LOCAL_CAVEAT = ("registered in the local layer: incubating until it's validated on a rig "
                 "and exported (docs/BRING_YOUR_OWN.md)")


def _local_header(content: str) -> str:
    """A local entry is always written incubating, so its compose header has to say
    so: a copied sibling's `Status: ⚠️ …` would otherwise disagree with the entry, and
    test-compose-status-drift goes red once the slug is exported into a checkout. The
    compose's own Caveats stay (they still describe the config); ours goes in after
    Status. A compose with no profile header is left as written (export_pr asks for
    one)."""
    if compose_header.status_value(content) is None or compose_header.says(content, _LOCAL_FORCED_STATUS):
        return content
    try:
        return compose_header.set_status(content, compose_header.LABELS[_LOCAL_FORCED_STATUS],
                                         caveats=_LOCAL_CAVEAT)
    except compose_header.HeaderError:
        return content


def _write_local(root: Path, spec: dict) -> tuple[Path, Path]:
    """The THREE local-layer artifacts. Returns (profile_path, compose_path)."""
    mid = spec["model_id"]
    profile_path = root / _LOCAL_MODELS_REL / f"{mid}.yml"
    compose_abs = root / spec["compose"]["path"]

    # ── Write 0/3 (optional): a NEW local engine profile ────────────────────
    # First, so the registry entry written last never references an engine that
    # failed to land, and before the post-write checks, whose cross-reference
    # validation needs it.
    if spec.get("_engine_profile_action") == "write":
        ep_path = spec["_engine_profile_path"]
        try:
            ep_path.parent.mkdir(parents=True, exist_ok=True)
            ep_path.write_text(spec["local_engine_profile"]["yaml"], encoding="utf-8")
            _info(f"wrote {ep_path}")
        except OSError as exc:
            raise InternalError(f"could not write {ep_path}: {exc}")

    # ── Write 1/3: the ModelProfile, or for a curated model its extension ───
    att = spec.get("_attach")
    if att is not None:
        profile_path = att["path"]
        if att["action"] != "keep":
            _write_extension(att)
    else:
        try:
            profile_path.parent.mkdir(parents=True, exist_ok=True)
            profile_path.write_text(render_profile_yaml(spec), encoding="utf-8")
            _info(f"wrote {profile_path}")
        except OSError as exc:
            raise InternalError(f"could not write {profile_path}: {exc}")

    # ── Write 2/3: the compose ──────────────────────────────────────────────
    spec["compose"]["content"] = _local_header(spec["compose"]["content"])
    try:
        compose_abs.parent.mkdir(parents=True, exist_ok=True)
        compose_abs.write_text(spec["compose"]["content"], encoding="utf-8")
        _info(f"wrote {compose_abs}")
    except OSError as exc:
        raise InternalError(f"could not write {compose_abs}: {exc}")

    # ── Write 3/3: the JSON merge into registry.local.json ──────────────────
    reg_path = root / _LOCAL_REGISTRY_REL
    try:
        raw = _load_local_raw(root)
        raw[spec["registry_entry"]["slug"]] = spec["registry_entry"]["kwargs"]
        reg_path.parent.mkdir(parents=True, exist_ok=True)
        reg_path.write_text(
            json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        _info(f"merged entry into {reg_path}")
    except OSError as exc:
        raise InternalError(f"could not rewrite {reg_path}: {exc}")
    return profile_path, compose_abs


def _write_core(root: Path, spec: dict) -> tuple[Path, Path]:
    """Today's curated-catalog write (maintainer-gated). Returns paths."""
    mid = spec["model_id"]
    profile_path = root / _MODELS_DIR_REL / f"{mid}.yml"
    compose_abs = root / spec["compose"]["path"]

    # ── Write 1/3: the ModelProfile ─────────────────────────────────────────
    try:
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        profile_path.write_text(render_profile_yaml(spec), encoding="utf-8")
        _info(f"wrote {profile_path}")
    except OSError as exc:
        raise InternalError(f"could not write {profile_path}: {exc}")

    # ── Write 2/3: the compose ──────────────────────────────────────────────
    try:
        compose_abs.parent.mkdir(parents=True, exist_ok=True)
        compose_abs.write_text(spec["compose"]["content"], encoding="utf-8")
        _info(f"wrote {compose_abs}")
    except OSError as exc:
        raise InternalError(f"could not write {compose_abs}: {exc}")

    # ── Write 3/3: the registry.yaml entry merge (data, not source) ─────────
    try:
        _append_registry_entry(
            root, spec["registry_entry"]["slug"], spec["registry_entry"]["kwargs"]
        )
        _info(f"merged entry into {_REGISTRY_YAML_REL}")
    except OSError as exc:
        raise InternalError(f"could not rewrite {_REGISTRY_YAML_REL}: {exc}")
    return profile_path, compose_abs




def _apply_model_spec(spec: dict) -> dict:
    """M4 — a typed ModelSpec (``spec_version: 1`` under ``model_spec``) is the
    CANONICAL arch source: its Fact values override the loose ``arch`` dict.
    Legacy loose-only specs pass through untouched (from_plain_spec semantics
    stay available to callers).  Same-directory import: this script runs as
    ``python3 scripts/lib/profiles/promote.py``, so its dir is on sys.path."""
    ms_raw = spec.get("model_spec")
    if not isinstance(ms_raw, dict):
        return spec
    import model_spec as _ms
    ms = _ms.ModelSpec.from_dict(ms_raw)
    arch = spec.setdefault("arch", {})
    arch.update(ms.arch_dims())
    if spec.get("vision_capable") is None and ms.vision_capable is not None:
        spec["vision_capable"] = bool(ms.vision_capable.value)
    return spec


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="promote.py",
        description=(
            "Write a NEW model into the catalog (C4-rev executor). "
            "DEFAULT: the gitignored LOCAL layer — core is maintainer-gated."
        ),
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--spec-env", metavar="VAR", help="read the promotion spec JSON from this env var")
    g.add_argument("--spec-file", metavar="PATH", help="read the promotion spec JSON from this file")
    ap.add_argument(
        "--layer",
        choices=("local", "core"),
        default=None,          # None => "not chosen"; resolved to local below
        help="write target: local (default — scripts/lib/profiles-local/, never "
        "touches core) or core (curated catalog; needs " + _CORE_GATE_ENV + "=1)",
    )
    ap.add_argument(
        "--yes", action="store_true",
        help="skip the interactive layer confirm (implied when --layer is given, "
        "or when stdin is not a TTY)",
    )
    ap.add_argument("--root", default=str(_DEFAULT_ROOT), help="repo root (default: this checkout)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    args = ap.parse_args(argv)

    # #1142 item 6: `local` is the safe default, but it was taken SILENTLY — the
    # user never learned a choice existed. Pose it when the layer was defaulted
    # AND a human is present. c3 already does this (its Promote screen names all
    # three destinations); this makes the CLI agree. Skipped when --layer was
    # given (the cockpit always passes it and has its own confirm), under --yes,
    # under --dry-run, and whenever stdin is not a TTY — so scripts and tests are
    # untouched.
    layer_defaulted = args.layer is None
    args.layer = args.layer or "local"
    if layer_defaulted and not args.yes and not args.dry_run and sys.stdin.isatty():
        print(
            f"[promote] no --layer given → writing the LOCAL layer "
            f"(scripts/lib/profiles-local/, gitignored, never touches the curated "
            f"catalog).\n[promote]   --layer core   = curated catalog "
            f"(maintainer-only, needs {_CORE_GATE_ENV}=1)\n"
            f"[promote]   export_pr.py = turn this local model into a PR bundle later",
            file=sys.stderr,
        )
        try:
            reply = input("[promote] write the LOCAL layer? [Y/n] ").strip().lower()
        except EOFError:
            reply = ""
        if reply not in ("", "y", "yes"):
            print("[promote] aborted — nothing written", file=sys.stderr)
            return EXIT_COLLISION

    root = Path(args.root).resolve()
    # #1142: these tools are invoked directly (no wrapper in scripts/ runs them),
    # so a gate parked in the settings used to be a SILENT no-op — no error, no
    # effect. Fill UNSET keys through the one loader (#1466), and say so when the
    # maintainer gate is one of them: settings stop being silent BOTH ways.
    _from_config = load_config(root)
    if _CORE_GATE_ENV in _from_config:
        _where = f"{root}/.env" if _from_config[_CORE_GATE_ENV] == "repo .env" else _from_config[_CORE_GATE_ENV]
        print(f"[promote] {_CORE_GATE_ENV} read from {_where} "
              f"(export it in the shell to override)", file=sys.stderr)
    try:
        if args.spec_env:
            raw_spec = os.environ.get(args.spec_env)
            if not raw_spec:
                raise Refusal(f"env var {args.spec_env} is empty/unset — no spec to promote")
            spec = json.loads(raw_spec)
        else:
            spec = json.loads(Path(args.spec_file).read_text(encoding="utf-8"))
        spec = _apply_model_spec(spec)
        spec = validate_spec(spec, root, args.layer)
    except Refusal as exc:
        print(f"[promote] REFUSED: {exc}", file=sys.stderr)
        return EXIT_COLLISION
    except json.JSONDecodeError as exc:
        print(f"[promote] REFUSED: spec is not valid JSON: {exc}", file=sys.stderr)
        return EXIT_COLLISION

    slug = spec["registry_entry"]["slug"]

    for ln in plan_lines(root, spec):
        _info(ln)
    if args.dry_run:
        _info("dry-run — nothing written.")
        return 0

    # An attached slug shares extends.d/<id>.yml with every other slug on that
    # model, so a failed write must not leave it changed: snapshot every target
    # and put them all back if any post-write check fails.
    snap = None
    if spec.get("_attach") is not None:
        targets = [spec["_attach"]["path"], root / spec["compose"]["path"],
                   root / _LOCAL_REGISTRY_REL]
        if spec.get("_engine_profile_action") == "write":
            targets.append(spec["_engine_profile_path"])
        snap = _snapshot(targets)

    try:
        if spec["_layer"] == "local":
            profile_path, _compose_abs = _write_local(root, spec)
        else:
            profile_path, _compose_abs = _write_core(root, spec)

        # ── Post-write sanity ───────────────────────────────────────────────
        _post_write_checks(root, spec, profile_path)
    except InternalError as exc:
        if snap is None:
            raise
        undone = _restore(snap, root / _LOCAL_DIR_REL)
        raise InternalError(f"{exc}\n[promote] ROLLED BACK — "
                            + "; ".join(undone or ["nothing had been written"]))
    _info(f"import-sanity + registry-emit re-check passed for {slug}")
    # #1142 item 6: state the layer on every run — "PROMOTE_OK <slug>" alone left
    # the reader unable to tell whether their model went to their own checkout or
    # to the curated catalog.
    print(f"PROMOTE_OK {slug} [layer={args.layer}]")
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    try:
        raise SystemExit(main())
    except Refusal as exc:  # defensive: raised outside the validate window
        print(f"[promote] REFUSED: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_COLLISION)
    except InternalError as exc:
        print(f"[promote] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_INTERNAL)
