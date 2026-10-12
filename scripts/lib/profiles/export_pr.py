#!/usr/bin/env python3
"""Community-loop completion: export what a contributor validated in the LOCAL
layer as a ready-to-commit CORE PR bundle.

The local layer (``scripts/lib/profiles-local/``, gitignored) is where a slug is
staged and validated before it is offered to the curated catalog. This tool reads
it and writes, into an OUTPUT directory (default ``/tmp``), what the PR needs,
translated to the CORE layout. The spec says what to export:

``{"model_id": "<id>"}`` for a model that only the local layer has (case 3 in
docs/ADDING_MODELS.md → "Which case is this?"):

    <out>/models/<id>.yml                       the ModelProfile (models.d/<id>.yml)
    <out>/models/<id>/<engine-dir>/compose/<topology>/<quant>/<serving>.yml
    <out>/registry-entry.yaml                   the registry ENTRY FILE
    <out>/PR_BODY.md                            .github/PULL_REQUEST_TEMPLATE/slug.md, filled in

``{"model_id": "<id>"}`` for a CURATED model the local layer attaches slugs to
(cases 1 and 2). The attaching takes an ``extends.d/<id>.yml``
(local_extensions.py), which holds any new weights variants:

    <out>/models/<id>/<engine-dir>/compose/…    one per slug
    <out>/registry-entry.yaml
    <out>/weights-snippet-<id>.yaml             only the NEW variants the slugs use
    <out>/PR_BODY.md                            slug.md, with case 1 or 2 ticked per slug

``{"engine_id": "<id>"}`` for an engine only the local layer has
(``engines.d/``, docs/ADDING_ENGINES.md):

    <out>/scripts/lib/profiles/engines/<id>.yml
    plus the composes, entries and weights snippets of the engine's slugs on
    curated models (its first slugs); slugs on local models are left out, since
    a new model goes in its own PR after the engine's
    <out>/PR_BODY.md                            engine.md, filled in

An optional ``"slugs": [...]`` limits the export to those slugs.

The catalog is DATA (registry.yaml; compose_registry.py is the loader shim), so
the registry artifact is an entry FILE, merged on the PR branch with the command
inside it. Nothing in THIS repo is touched: the export writes only under --out.

Where things land in core. An engine's composes go in the directory core already
uses for that engine id (``vllm-stable`` → ``vllm/``), and its slugs keep the
prefix core uses there; an engine core doesn't have yet keeps the directory the
local layer used. A relative mount is copied as written when it resolves from
the core location; one written for the local layer's depth (three directories
deeper) is re-pointed at the same target and reported, never silently.

Refusals (exit 3, nothing written): what a maintainer would bounce the PR for,
the required catalog rules (catalog_rules.py; --network adds the Hugging Face
download and the anonymous image pull), and anything that would overwrite
curated content. ``--check`` validates and writes nothing; ``--dry-run`` prints
the plan and writes nothing.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

# Repo-root default: this file lives at <root>/scripts/lib/profiles/export_pr.py.
_DEFAULT_ROOT = Path(__file__).resolve().parents[3]

_REGISTRY_YAML_REL = "scripts/lib/profiles/registry.yaml"
_CORE_MODELS_REL = "scripts/lib/profiles/models"
_CORE_ENGINES_REL = "scripts/lib/profiles/engines"
_LOCAL_DIR_REL = "scripts/lib/profiles-local"
_LOCAL_MODELS_REL = f"{_LOCAL_DIR_REL}/models.d"
_LOCAL_EXTENDS_REL = f"{_LOCAL_DIR_REL}/extends.d"
_LOCAL_ENGINES_REL = f"{_LOCAL_DIR_REL}/engines.d"
_LOCAL_COMPOSES_REL = f"{_LOCAL_DIR_REL}/composes"
_LOCAL_REGISTRY_REL = f"{_LOCAL_DIR_REL}/registry.local.json"
_TEMPLATES_REL = ".github/PULL_REQUEST_TEMPLATE"
_LOCAL_SLUG_PREFIX = "local/"

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_MODEL_ID_RE = _ID_RE

EXIT_COLLISION = 3   # refusal — nothing written
EXIT_INTERNAL = 2

NEW_MODEL, CORE_MODEL = "new-model", "core-model"


class Refusal(Exception):
    """A bad-shape / incomplete-id refusal → exit 3, nothing written."""


class TemplateDrift(Exception):
    """A PR template no longer has a line this tool fills → exit 2."""


def _info(msg: str) -> None:
    print(f"[export-pr] {msg}")


def _is_real_str(v: Any) -> bool:
    """A filled-in human value — not empty, not a leftover ``<...>`` scaffold
    placeholder."""
    return isinstance(v, str) and bool(v.strip()) and "<" not in v


def registry_module(root: Path):
    """Import the TARGET repo's compose_registry (the loader surface) — used
    BOTH for the ``_entry`` dry-wrap validation and for rendering the entry
    file with the loader's own YAML-subset emitters (never a second writer
    that can drift)."""
    sys.path.insert(0, str(root))
    try:
        import scripts.lib.profiles.compose_registry as reg_mod  # noqa: E402
    except Exception as exc:  # pragma: no cover - import failure surfaced later
        raise Refusal(f"compose_registry.py did not import: {exc}")
    return reg_mod


def _catalog_rules():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import catalog_rules
    finally:
        sys.path.pop(0)
    return catalog_rules


def _yaml():
    try:
        import yaml  # noqa: E402
    except Exception as exc:  # pragma: no cover - env guard
        raise Refusal(f"PyYAML unavailable, cannot validate the profile: {exc}")
    return yaml


def _load_yaml(path: Path) -> dict:
    try:
        data = _yaml().safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise Refusal(f"unreadable: {path}: {exc}")
    except Exception as exc:
        raise Refusal(f"{path} does not parse as YAML: {exc}")
    if not isinstance(data, dict):
        raise Refusal(f"{path}: expected a YAML mapping")
    return data


def _local_registry(root: Path) -> dict:
    reg_path = root / _LOCAL_REGISTRY_REL
    if not reg_path.exists():
        return {}
    try:
        raw = json.loads(reg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refusal(f"{reg_path} is unreadable: {exc}")
    if not isinstance(raw, dict):
        raise Refusal(f"{reg_path}: expected a JSON object of {{slug: kwargs}}")
    return raw


def _refuse_local_namespace(entries: list[dict]) -> None:
    for e in entries:
        if str(e["slug"]).startswith(_LOCAL_SLUG_PREFIX):
            # #1205 inverted this condition (the prefix went from REQUIRED to
            # REFUSED) but left the old wording, so the message said "lacks the
            # 'local/' namespace" about a slug that plainly had it. Say what
            # actually happened, and name the fix.
            raise Refusal(
                f"registry slug {e['slug']!r} uses the {_LOCAL_SLUG_PREFIX!r} "
                "namespace, which was removed — local slugs are '<engine>/<name>' "
                f"(provenance is the 'origin' field). Re-register it as "
                f"'<engine>/{str(e['slug'])[len(_LOCAL_SLUG_PREFIX):]}'."
            )


def _select(entries: list[dict], slugs: Optional[list[str]], what: str) -> list[dict]:
    if slugs is None:
        return entries
    have = {e["slug"] for e in entries}
    unknown = [s for s in slugs if s not in have]
    if unknown:
        raise Refusal(f"spec.slugs {unknown} are not local slugs of {what} "
                      f"(it has: {sorted(have) or 'none'})")
    return [e for e in entries if e["slug"] in slugs]


def load_local_state(root: Path, mid: str, slugs: Optional[list[str]] = None) -> dict:
    """Read EVERYTHING the export needs for one model from the LOCAL layer (disk
    truth, not the scaffold preview). A model only the local layer has is case 3;
    a curated model with local slugs attached through extends.d is cases 1 and 2.
    Raises Refusal when there is nothing to export."""
    profile_path = root / _LOCAL_MODELS_REL / f"{mid}.yml"
    core_path = root / _CORE_MODELS_REL / f"{mid}.yml"
    raw = _local_registry(root)
    entries = [{"slug": slug, "kwargs": (kwargs or {})} for slug, kwargs in raw.items()
               if isinstance(kwargs, dict) and kwargs.get("model") == mid]
    reg_name = Path(_LOCAL_REGISTRY_REL).name
    if profile_path.exists():
        try:
            profile_text = profile_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise Refusal(f"unreadable: {profile_path}: {exc}")
        state = {"kind": NEW_MODEL, "model_id": mid, "profile_path": profile_path,
                 "profile_text": profile_text, "profile": _load_yaml(profile_path)}
    elif core_path.exists():
        ext_path = root / _LOCAL_EXTENDS_REL / f"{mid}.yml"
        ext = _load_yaml(ext_path) if ext_path.exists() else {}
        state = {"kind": CORE_MODEL, "model_id": mid, "core_profile": _load_yaml(core_path),
                 "extension": ext, "extension_path": ext_path}
        if not entries:
            raise Refusal(
                f"{mid} is a curated model and no {reg_name} entries attach a slug to "
                f"it — there is nothing local to export. A slug on a curated model "
                f"needs {_LOCAL_EXTENDS_REL}/{mid}.yml and a local registry entry "
                f"(docs/ADDING_MODELS.md → the local layer)."
            )
    else:
        raise Refusal(f"no LOCAL model profile: {profile_path}, and {mid!r} is not a "
                      "curated model either — nothing to export")
    if not entries:
        raise Refusal(
            f"no {reg_name} entries reference model {mid!r} — "
            "promote it into the LOCAL layer first (c3 ⑤ Promote)"
        )
    _refuse_local_namespace(entries)
    state["entries"] = _select(entries, slugs, mid)
    return state


def load_local_engine(root: Path, eid: str, slugs: Optional[list[str]] = None) -> dict:
    """The local engine profile and the local slugs that run on it, grouped by
    model. Slugs on curated models travel with the engine PR; slugs on local
    models need their own (case 3) PR after it."""
    rules = _catalog_rules()
    engines = rules._engine_index(root)
    found = engines.get(eid)
    if found and found[2]:
        raise Refusal(f"engine {eid!r} is already curated ({found[1].name}) — "
                      "nothing local to export")
    if not found:
        raise Refusal(f"no local engine profile with id {eid!r} in {_LOCAL_ENGINES_REL}/")
    profile, path, _ = found
    core_file = root / _CORE_ENGINES_REL / f"{eid}.yml"
    if core_file.exists():
        raise Refusal(f"{_CORE_ENGINES_REL}/{eid}.yml already exists in the curated catalog; "
                      "the export would overwrite it")
    raw = _local_registry(root)
    on_engine = [{"slug": s, "kwargs": kw} for s, kw in raw.items()
                 if isinstance(kw, dict) and kw.get("engine") == eid]
    _refuse_local_namespace(on_engine)
    on_engine = _select(on_engine, slugs, f"engine {eid}")
    models: dict[str, list[str]] = {}
    left_out: list[str] = []
    for e in on_engine:
        mid = str(e["kwargs"].get("model") or "")
        if (root / _CORE_MODELS_REL / f"{mid}.yml").exists():
            models.setdefault(mid, []).append(e["slug"])
        else:
            left_out.append(e["slug"])
    states = [load_local_state(root, mid, sl) for mid, sl in sorted(models.items())]
    return {"engine_id": eid, "path": path, "text": path.read_text(encoding="utf-8"),
            "profile": profile, "states": states, "left_out": sorted(left_out)}


# ── where core keeps things ─────────────────────────────────────────────────

def _layout(reg_mod) -> tuple[dict[str, set], dict[str, set]]:
    """engine id → {(compose dir, slug prefix)}, compose dir → {slug prefix}, from the
    curated registry (core_layout.py, shared with catalog.sh register)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import core_layout
    finally:
        sys.path.pop(0)
    return core_layout.engine_layout(reg_mod.COMPOSE_REGISTRY)


def _local_compose_parts(slug: str, cpath: str) -> tuple[str, str, str]:
    """composes/<model>/<engine-dir>/compose/<rest> → (model, engine-dir, rest)."""
    rel = cpath[len(_LOCAL_COMPOSES_REL):].lstrip("/") if cpath.startswith(
        f"{_LOCAL_COMPOSES_REL}/") else ""
    parts = rel.split("/")
    if len(parts) < 4 or parts[2] != "compose":
        raise Refusal(f"{slug}: compose_path {cpath!r} is not "
                      f"{_LOCAL_COMPOSES_REL}/<model>/<engine-dir>/compose/…")
    return parts[0], parts[1], "/".join(parts[3:])


def core_compose_rel(reg_mod, slug: str, kwargs: dict) -> str:
    """The CORE path for a local compose: models/<model>/<engine-dir>/compose/…,
    in the directory core keeps this engine's composes, and only for a slug whose
    prefix core uses there."""
    mid = str(kwargs.get("model"))
    seg, local_dir, rest = _local_compose_parts(slug, str(kwargs.get("compose_path") or ""))
    if seg != mid:
        raise Refusal(f"{slug}: its compose sits under composes/{seg}/, but the entry's "
                      f"model is {mid!r}")
    engine = str(kwargs.get("engine"))
    prefix = slug.split("/", 1)[0]
    by_engine, by_dir = _layout(reg_mod)
    pairs = by_engine.get(engine, set())
    mismatch = (f"slug {slug!r} does not match the namespace implied by its compose "
                "path")
    if pairs:
        dirs = sorted({d for d, _ in pairs})
        if local_dir in dirs:
            core_dir = local_dir
        elif local_dir in by_dir:
            raise Refusal(f"{mismatch}: the compose sits in {local_dir}/, which core uses "
                          f"for other engines; {engine} composes live in "
                          f"{', '.join(d + '/' for d in dirs)}")
        elif len(dirs) == 1:
            core_dir = dirs[0]
        else:
            raise Refusal(f"{slug}: {engine} composes live in "
                          f"{', '.join(d + '/' for d in dirs)} in core; move the local "
                          "compose under the one it belongs to")
        want = sorted({p for d, p in pairs if d == core_dir})
        if prefix not in want:
            raise Refusal(f"{mismatch}: core slugs on {engine} in {core_dir}/ are "
                          f"{', '.join(p + '/…' for p in want)}")
    else:
        core_dir = local_dir
        if local_dir in by_dir and prefix not in by_dir[local_dir]:
            raise Refusal(f"{mismatch}: core slugs in {local_dir}/ are "
                          f"{', '.join(p + '/…' for p in sorted(by_dir[local_dir]))}")
    return f"models/{mid}/{core_dir}/compose/{rest}"


def _engine_types(root: Path) -> dict[str, str]:
    return {i: str(p.get("type") or "") for i, (p, _, _) in
            _catalog_rules()._engine_index(root).items()}


def translated_entry(state: dict, entry: dict, reg_mod=None, root: Optional[Path] = None) -> dict:
    """One LOCAL registry entry → its CORE ``_entry(...)`` kwargs (pure copy —
    the local JSON is never mutated)."""
    reg_mod = reg_mod or registry_module(root or _DEFAULT_ROOT)
    kw = dict(entry["kwargs"])
    kw["compose_path"] = core_compose_rel(reg_mod, str(entry["slug"]), kw)
    if not kw.get("kvcalc_key"):
        kw["kvcalc_key"] = "SKIP"  # non-vLLM default; vLLM was gated in completeness_gaps
    # PUBLISH IS NOT A RENAME (#1202 P3): local slugs already ARE
    # '<engine>/<name>', so the core slug is the slug the user has been running,
    # and every reference they hold survives publication.
    return {"slug": str(entry["slug"]), "kwargs": kw, "local_compose": entry["kwargs"]["compose_path"]}


# ── what a maintainer would bounce the PR for ───────────────────────────────

def _variant_gaps(where: str, w: Any) -> list[str]:
    """A weights variant the PR adds: setup.sh must be able to fetch it."""
    if not isinstance(w, dict):
        return [f"{where}: not a mapping"]
    gaps = []
    if not _is_real_str(w.get("hf_repo")):
        gaps.append(f"{where}: hf_repo must be a real Org/Repo")
    files = w.get("files")
    if not isinstance(files, list) or not files:
        gaps.append(f"{where}: files must be a non-empty list "
                    "(no files: = the WHOLE repo is fetched — ADDING_MODELS 'Two silent traps')")
    if str(w.get("format") or "") == "gguf" and not str(w.get("verify_glob") or "").strip():
        gaps.append(f"{where}: GGUF entries REQUIRE verify_glob ('*.gguf') — "
                    "the safetensors default matches nothing")
    return gaps


def _referenced_variants(kwargs: dict) -> list[str]:
    names = [str(kwargs.get("weights_variant") or "")]
    names += [str(c) for c in (kwargs.get("weights_companions") or [])]
    return [n for n in names if n]


def new_variants(state: dict) -> dict[str, dict]:
    """CORE_MODEL: the extension variants the exported slugs use (what the PR adds
    to models/<id>.yml). NEW_MODEL: none — the whole profile ships."""
    if state["kind"] != CORE_MODEL:
        return {}
    ext_w = state["extension"].get("weights") or {}
    used = {n for e in state["entries"] for n in _referenced_variants(e["kwargs"])}
    return {n: ext_w[n] for n in sorted(used) if n in ext_w}


def new_tp(state: dict) -> list[int]:
    """CORE_MODEL: tp values the slugs need that the curated valid_tp lacks."""
    if state["kind"] != CORE_MODEL:
        return []
    core = set(state["core_profile"].get("valid_tp") or [])
    add = set(state["extension"].get("valid_tp_add") or [])
    need = {int(e["kwargs"]["tp"]) for e in state["entries"]
            if isinstance(e["kwargs"].get("tp"), int)}
    return sorted(t for t in need if t not in core and t in add)


REQUIRED_KWARGS = (
    "model", "weights_variant", "workload", "engine", "kv_format", "tp",
    "max_ctx", "max_num_seqs", "mem_util", "compose_path", "default_port",
    "status",
)


def completeness_gaps(root: Path, state: dict) -> list[str]:
    """Every reason a maintainer would bounce this PR — the REQUIRED core
    fields. Empty list == exportable."""
    gaps: list[str] = []
    mid = state["model_id"]

    if state["kind"] == NEW_MODEL:
        p = state["profile"]
        for key in ("display_name", "family"):
            if not _is_real_str(p.get(key)):
                gaps.append(
                    f"models/{mid}.yml: {key} must be a real value "
                    f"(got {p.get(key)!r} — fill it before exporting)"
                )
        weights = p.get("weights")
        if not isinstance(weights, dict) or not weights:
            gaps.append(f"models/{mid}.yml: weights must be a non-empty map")
            weights = {}
        dwv = p.get("default_weight_variant")
        if dwv not in weights:
            gaps.append(
                f"models/{mid}.yml: default_weight_variant {dwv!r} is not a weights key"
            )
        for name, w in sorted(weights.items()):
            gaps += _variant_gaps(f"weights.{name}", w)
    else:
        core_w = state["core_profile"].get("weights") or {}
        ext_w = state["extension"].get("weights") or {}
        for e in state["entries"]:
            for name in _referenced_variants(e["kwargs"]):
                if name not in core_w and name not in ext_w:
                    gaps.append(f"{e['slug']}: weights variant {name!r} is in neither "
                                f"models/{mid}.yml nor {_LOCAL_EXTENDS_REL}/{mid}.yml")
        for name, w in new_variants(state).items():
            gaps += _variant_gaps(f"{_LOCAL_EXTENDS_REL}/{mid}.yml: weights.{name}", w)

    reg_mod = registry_module(root)
    types = _engine_types(root)
    for e in state["entries"]:
        slug, kw = e["slug"], e["kwargs"]
        for key in REQUIRED_KWARGS:
            if kw.get(key) in (None, ""):
                gaps.append(f"{slug}: registry kwarg {key!r} is required")
        if slug in reg_mod.COMPOSE_REGISTRY:
            gaps.append(f"{slug}: the curated catalog already has this slug")
        cpath = str(kw.get("compose_path") or "")
        if not cpath:
            continue
        if not cpath.startswith(f"{_LOCAL_COMPOSES_REL}/"):
            gaps.append(
                f"{slug}: compose_path {cpath!r} does not live under "
                f"{_LOCAL_COMPOSES_REL}/"
            )
            continue
        comp_abs = root / cpath
        if not comp_abs.exists():
            gaps.append(f"{slug}: compose file missing on disk: {comp_abs}")
            continue
        try:
            comp_text = comp_abs.read_text(encoding="utf-8")
        except OSError as exc:
            gaps.append(f"{slug}: unreadable compose {comp_abs}: {exc}")
            continue
        if "# Profile (at-a-glance):" not in comp_text or "Status:" not in comp_text:
            gaps.append(
                f"{slug}: compose lacks the '# Profile (at-a-glance):' header "
                "with a 'Status:' line (test-compose-status-drift fails CI)"
            )
        # vLLM-type engines MUST carry a kvcalc_key (kv-calc projection); the
        # others legitimately default to "SKIP".
        if types.get(str(kw.get("engine"))) == "vllm" and not str(kw.get("kvcalc_key") or "").strip():
            gaps.append(
                f"{slug}: vLLM entries require kvcalc_key \"<model>:<profile>\" "
                "(kv-calc projection; llama-family may omit it → SKIP)"
            )

    # ── Dry-wrap the TRANSLATED kwargs exactly like the loader will — a kwarg
    # missing from _entry's signature (e.g. `pp`) or rejected by its
    # validation refuses HERE, before any bundle is written.
    if not gaps:
        for e in state["entries"]:
            t = translated_entry(state, e, reg_mod)
            if (root / t["kwargs"]["compose_path"]).exists():
                gaps.append(f"{e['slug']}: {t['kwargs']['compose_path']} already exists "
                            "in the curated catalog; the export would overwrite it")
            try:
                reg_mod._entry(**t["kwargs"])
            except TypeError as exc:
                gaps.append(f"{e['slug']}: not valid _entry kwargs: {exc}")
            except ValueError as exc:
                gaps.append(f"{e['slug']}: rejected by _entry: {exc}")
    return gaps


def engine_gaps(root: Path, eng: dict, *, network: bool) -> tuple[list[str], list, list[str]]:
    """(gaps, findings, still-to-do) for an engine export."""
    rules = _catalog_rules()
    eid, prof = eng["engine_id"], eng["profile"]
    gaps: list[str] = []
    sys.path.insert(0, str(root))
    try:
        from scripts.lib.profiles import compat  # noqa: E402
        compat._engine(prof, eng["path"])
    except Exception as exc:
        gaps.append(f"engines/{eid}.yml does not load: {exc}")
    finally:
        sys.path.pop(0)
    install = prof.get("install") or {}
    if install.get("method") != "docker_image":
        gaps.append(f"engines/{eid}.yml: install.method must be docker_image with the "
                    f"maintainer's image as install.spec (got {install.get('method')!r}; "
                    "catalog.sh register wrote `docker` before it learned this)")
    findings = rules.check_engine(root, eid, network=network)
    for f in rules.blocking(findings, network):
        hint = ""
        if f.rule == "engine-lineage":
            hint = (" — fill lineage: relation (stock | patched | derivative | fork | new), "
                    "base, base_version, source, source_ref (docs/ADDING_ENGINES.md → "
                    "CONTRACT — engine lineage)")
        gaps.append(f"engines/{eid}.yml: {f.rule}: {f.message}{hint}")
    todo: list[str] = []
    image_env = prof.get("image_env")
    if not image_env:
        todo.append("set `image_env` so the launchers inject install.spec, and allowlist it "
                    "in both launchers")
    else:
        for launcher, envs in rules.launcher_image_envs(root).items():
            if image_env not in envs:
                todo.append(f"add {image_env} to the case arm in "
                            f"`export_variant_engine_pin()` in {launcher}")
    if not eng["states"]:
        todo.append("an engine PR carries its first slugs: register one on a model we "
                    "serve and export again")
    if "(local" in str(prof.get("display_name") or ""):
        todo.append(f"give engines/{eid}.yml a real display_name (it still says it was "
                    "registered locally)")
    if "Registered by catalog.sh" in str(prof.get("notes") or ""):
        todo.append(f"replace the catalog.sh `notes:` in engines/{eid}.yml with what the "
                    "engine is and where it comes from")
    return gaps, findings, todo


def catalog_rule_gaps(root: Path, state: dict, *, network: bool = False,
                      findings_out: Optional[dict] = None) -> list[str]:
    """The catalog rules (scripts/lib/profiles/catalog_rules.py) as export gaps: a
    required rule that fails, and with --network one that could not be checked.
    Advisory findings print but never refuse."""
    rules = _catalog_rules()
    if rules.yaml is None:
        return ["catalog rules need PyYAML (pip install pyyaml)"]
    registry = registry_module(root).get_registry(str(root))
    gaps: list[str] = []
    for e in state["entries"]:
        try:
            findings = rules.check_slug(root, e["slug"], dict(e["kwargs"]),
                                        network=network, registry=registry)
        except Exception as exc:  # a rule that cannot run refuses; it never passes
            gaps.append(f"{e['slug']}: the catalog rules could not run: {exc}")
            continue
        if findings_out is not None:
            findings_out[e["slug"]] = findings
        for f in rules.blocking(findings, network):
            gaps.append(f"{e['slug']}: {f.rule}: {f.message} ({f.doc})")
        for f in findings:
            if f.level == rules.ADVISORY and f.state == rules.FAIL:
                _info(f"advisory, {e['slug']}: {f.rule}: {f.message}")
    return gaps


# ── mounts: the local layer sits three directories deeper than core ─────────

_INNER_DEFAULT = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-([^${}]*)\}")


def _interpolate_unset(source: str) -> str:
    """What a raw `docker compose up` with nothing set mounts (as
    test-compose-mounts-resolve reads it): every ${VAR:-default} → its default."""
    prev = None
    while prev != source:
        prev, source = source, _INNER_DEFAULT.sub(lambda m: m.group(1), source)
    return source


def _volume_source(v: Any) -> str:
    if isinstance(v, dict):
        return str(v.get("source") or "")
    s, depth, i = str(v), 0, 0
    while i < len(s):  # the first ':' outside ${…} ends the source
        if s.startswith("${", i):
            depth, i = depth + 1, i + 2
            continue
        if s[i] == "}" and depth:
            depth -= 1
        elif s[i] == ":" and depth == 0:
            return s[:i]
        i += 1
    return s


def _relative_tokens(source: str) -> list[str]:
    """The relative path tokens a source is built from: the innermost default of a
    ${…} chain, or the source itself."""
    toks = [m.group(1) for m in _INNER_DEFAULT.finditer(source)]
    if not toks and "${" not in source:
        toks = [source]
    return [t for t in toks if t.startswith(".")]


def _resolves(root: Path, model_tree: Path, path: Path) -> str:
    """By test-compose-mounts-resolve's criteria: 'ok'; 'cache' (the engine-cache
    fallback inside the model's tree, a directory the PR adds); 'tree' (anything
    else inside the model's tree that core lacks); or 'bad'."""
    lexical = Path(os.path.abspath(path))
    if not str(lexical).startswith(str(root) + os.sep):
        return "bad"
    local_layer = root / _LOCAL_DIR_REL
    if lexical == local_layer or local_layer in lexical.parents:
        return "bad"   # gitignored: it exists here and on no other user's machine
    if "models-cache" in lexical.parts:
        return "ok" if (lexical == root / "models-cache" or root / "models-cache" in lexical.parents) else "bad"
    if lexical.exists():
        return "ok"
    in_tree = lexical == model_tree or model_tree in lexical.parents
    if "cache" in lexical.parts:
        cache_root = Path(*lexical.parts[:lexical.parts.index("cache") + 1])
        if cache_root.exists():
            return "ok"
        if in_tree:
            return "cache"
    return "tree" if in_tree else "bad"


def rebase_mounts(root: Path, local_rel: str, core_rel: str, text: str
                  ) -> tuple[str, list[tuple[str, str]], list[str], list[str], dict[str, bytes]]:
    """(text for the core location, rebased, must-add, gaps, carried files). A
    source that resolves from the core location is kept. One inside the model's
    tree that core lacks is carried from the local layer (a patch directory, a
    template). One that only resolves from the local location is re-pointed at the
    same target. Anything else is a gap."""
    try:
        data = _yaml().safe_load(text) or {}
    except Exception:
        return text, [], [], [f"{local_rel}: does not parse as YAML"]
    core_dir, local_dir = (root / core_rel).parent, (root / local_rel).parent
    model_tree = root / "/".join(core_rel.split("/")[:2])
    local_tree = root / "/".join(local_rel.split("/")[:5])
    rebased, ships, gaps = [], [], []
    carry: dict[str, bytes] = {}
    replace: dict[str, str] = {}
    for svc in (data.get("services") or {}).values():
        for v in (svc or {}).get("volumes") or []:
            source = _volume_source(v)
            plain = _interpolate_unset(source)
            if not plain.startswith("."):
                continue
            verdict = _resolves(root, model_tree, core_dir / plain)
            if verdict == "ok":
                continue
            core_target = Path(os.path.abspath(core_dir / plain))
            if verdict == "cache":
                cache_root = Path(*core_target.parts[:core_target.parts.index("cache") + 1])
                ships.append(os.path.relpath(cache_root, root) + "/")
                continue
            if verdict == "tree":
                local_target = Path(os.path.abspath(local_dir / plain))
                files = ([local_target] if local_target.is_file() else
                         sorted(f for f in local_target.rglob("*") if f.is_file())
                         if local_target.is_dir() else [])
                if files:
                    for f in files:
                        dest = core_target if f == local_target else core_target / f.relative_to(local_target)
                        carry[os.path.relpath(dest, root)] = f.read_bytes()
                    continue
            for tok in _relative_tokens(source):
                target = Path(os.path.abspath(local_dir / tok))
                inside = target == local_tree or local_tree in target.parents
                if inside or not str(target).startswith(str(root) + os.sep):
                    continue
                new = os.path.relpath(target, core_dir)
                if _resolves(root, model_tree, core_dir / plain.replace(tok, new, 1)) != "bad":
                    replace[tok] = new
            if not any(t in replace for t in _relative_tokens(source)):
                gaps.append(f"{local_rel}: mount {source!r} resolves to nothing from "
                            f"{core_rel} (nor from the local layer)")
    for old, new in sorted(replace.items(), key=lambda kv: -len(kv[0])):
        text = re.sub(r"(?<![\w./])" + re.escape(old) + r"(?=[}:\"'\s]|$)", new, text, flags=re.M)
        rebased.append((old, new))
    return text, rebased, sorted(set(ships)), gaps, carry


# ── artifacts ───────────────────────────────────────────────────────────────

_MERGE_SNIPPET = (
    "import sys; from pathlib import Path; "
    "from scripts.lib.profiles.compose_registry import ("
    "load_registry_data, parse_registry_text, dump_registry_yaml, _entry, "
    "_DERIVED_ENTRY_KEYS); "
    "reg = Path('scripts/lib/profiles/registry.yaml'); "
    "d = load_registry_data(reg); "
    "new = parse_registry_text(Path(sys.argv[1]).read_text(encoding='utf-8'), "
    "source=sys.argv[1])['entries']; "
    "clash = sorted(set(new) & set(d['entries'])); "
    "assert not clash, f'slug collision: {clash}'; "
    "d['entries'].update({slug: {k: v for k, v in _entry(**kw).items() "
    "if k not in _DERIVED_ENTRY_KEYS} for slug, kw in new.items()}); "
    "tmp = reg.with_name(reg.name + '.tmp'); "
    "tmp.write_text(dump_registry_yaml(d), encoding='utf-8'); tmp.replace(reg)"
)


def render_registry_entry_yaml(reg_mod, translated: list[dict], what: str,
                               kinds: set[str]) -> str:
    """The registry ENTRY FILE: notes + the canonical merge command (comments are
    valid registry.yaml DATA) + one ``entries:`` row per translated entry, emitted
    with the LOADER'S OWN dump helpers so the file is byte-style identical to what
    dump_registry_yaml writes — the stdlib reader can never fail to re-parse it."""
    lines = [
        f"# ── Community PR import: {what} (exported from the LOCAL layer) ──",
        "#",
        "# Merge this ENTRY FILE into scripts/lib/profiles/registry.yaml on your",
        "# PR branch. The catalog is DATA — never hand-edit and never patch",
        "# compose_registry.py (it is the loader shim). Canonical merge, from",
        "# your PR-branch repo root (parse → validate through _entry →",
        "# canonical dump_registry_yaml rewrite — promote.py's own path):",
        "#",
        f"#   python3 -c \"{_MERGE_SNIPPET}\" <path to this registry-entry.yaml>",
        "#",
        "# Then prove the merged catalog still loads:",
        "#",
        "#   python3 scripts/lib/profiles/migrate_registry_to_yaml.py --check",
        "#",
    ]
    if NEW_MODEL in kinds:
        lines += [
            "# New model (models/<id>.yml `setup:`):",
            "#   * ships NO setup: block — the default dispatch policy applies",
            "#     (primary fetch = default_weight_variant, no aliases/drafters).",
            "#     Add one ONLY if the fetch policy differs "
            "(ADDING_MODELS.md Step 4c).",
            "#   * NEW MODELS START AT status=\"incubating\" (hidden from switch.sh",
            "#     --list, --force to launch) — promote up the enum as the FULL",
            "#     gate clears.",
            "#   * vLLM rows: author calibration/<id>.yml after 4+ measured boots.",
        ]
    if CORE_MODEL in kinds:
        lines += [
            "# Slugs on a curated model:",
            "#   * new weights variants are in weights-snippet-<id>.yaml: add them",
            "#     under `weights:` in scripts/lib/profiles/models/<id>.yml.",
            "#   * no `gateway` or DEFAULTS change unless that is the PR's point.",
        ]
    lines += [
        "#   * bump test-compose-registry-disk.sh's catalog count for every",
        "#     compose added.",
        "#   * Gateway-reachable? Add services/litellm/config.yaml route → ",
        "#     <default_port> (test-litellm-ports-resolve.sh gates the port).",
        "",
        "entries:",
    ]
    for t in translated:
        lines.append(f"  {reg_mod._yaml_key(t['slug'])}:")
        for k, v in t["kwargs"].items():
            reg_mod._dump_node(lines, k, v, 4)
    return "\n".join(lines) + "\n"


def render_weights_snippet(state: dict) -> str:
    mid = state["model_id"]
    lines = [
        f"# New weights variants for scripts/lib/profiles/models/{mid}.yml, from",
        f"# {_LOCAL_EXTENDS_REL}/{mid}.yml. Add each block under `weights:`.",
    ]
    tp = new_tp(state)
    if tp:
        lines.append(f"# Also add {', '.join(map(str, tp))} to `valid_tp:`.")
    body = _yaml().safe_dump({"weights": new_variants(state)}, sort_keys=False,
                             allow_unicode=True, default_flow_style=False)
    return "\n".join(lines) + "\n" + body


# ── the PR body, filled from the repo's own template ────────────────────────

def _template(root: Path, name: str) -> str:
    path = root / _TEMPLATES_REL / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TemplateDrift(f"cannot read the PR template {_TEMPLATES_REL}/{name}: {exc}")


def _fill(text: str, anchor: str, value: str, template: str) -> str:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(anchor):
            lines[i] = f"{anchor} {value}"
            return "\n".join(lines) + "\n"
    raise TemplateDrift(f"{template} has no line starting {anchor!r}; "
                        "export_pr.py needs updating to match the template")


def _drop_section(text: str, heading: str) -> str:
    out, skip = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            skip = line.startswith(heading)
        if not skip:
            out.append(line)
    return "\n".join(out) + "\n"


def _insert_before(text: str, heading: str, block: str, template: str) -> str:
    idx = text.find("\n" + heading)
    if idx < 0:
        raise TemplateDrift(f"{template} has no {heading!r} section")
    return text[:idx + 1] + block.rstrip() + "\n\n" + text[idx + 1:]


def _put_export_check(text: str, block: str, template: str) -> str:
    """The template's own `## Export check` section, replaced by the export's;
    a template without one gets it before Evidence."""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.strip() == "## Export check"), None)
    if start is None:
        return _insert_before(text, "## Evidence", block, template)
    end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
    return "\n".join(lines[:start] + block.rstrip().splitlines() + [""] + lines[end:]) + "\n"


def _compose_status(root: Path, entry: dict) -> str:
    """The registry status, and the compose header's Status line beside it, so a
    disagreement (test-compose-status-drift fails on one) shows in the PR."""
    reg = f"`{entry['kwargs'].get('status') or '?'}`"
    try:
        text = (root / entry["kwargs"]["compose_path"]).read_text(encoding="utf-8")
    except OSError:
        return reg
    m = re.search(r"(?m)^#\s+Status:\s*(.+?)\s*$", text)
    return f"{reg} (compose header: {m.group(1)})" if m else reg


def slug_cases(reg_mod, state: dict) -> dict[str, tuple[int, list[str]]]:
    """slug → (case, the model's existing curated slugs on other engines)."""
    if state["kind"] == NEW_MODEL:
        return {e["slug"]: (3, []) for e in state["entries"]}
    live = {s: e for s, e in reg_mod.COMPOSE_REGISTRY.items()
            if e.get("model") == state["model_id"] and e.get("status") != "deprecated"}
    out = {}
    for e in state["entries"]:
        eng = e["kwargs"].get("engine")
        same = [s for s, c in live.items() if c.get("engine") == eng]
        out[e["slug"]] = (1, []) if same else (2, sorted(live))
    return out


def _export_check_block(findings: dict, network: bool, extra: list[str]) -> str:
    rules = _catalog_rules()
    when = datetime.date.today().isoformat()
    flag = " --network" if network else ""
    lines = ["## Export check", "",
             f"`export_pr.py --check{flag}`, {when}. "
             + ("Network rules ran (Hugging Face download, anonymous image pull)."
                if network else
                "**Network rules not run:** export again with `--network` before opening the "
                "PR, so this section records them."),
             "", "```text"]
    for subject, fs in findings.items():
        lines.append(rules.render(subject, fs, network))
    lines.append("```")
    if extra:
        lines += [""] + extra
    return "\n".join(lines) + "\n"


def slug_pr_body(root: Path, reg_mod, states: list[dict], translated: list[dict],
                 findings: dict, network: bool, extra: list[str]) -> str:
    name = "slug.md"
    text = _template(root, name)
    entries = [(st, e) for st in states for e in st["entries"]]
    cases: dict[str, tuple[int, list[str]]] = {}
    for st in states:
        cases.update(slug_cases(reg_mod, st))
    core_engines = {i for i, v in _catalog_rules()._engine_index(root).items() if v[2]}
    topo = {t["slug"]: t["kwargs"]["compose_path"].split("/compose/", 1)[-1].split("/")[0]
            for t in translated}
    engines = sorted({str(e["kwargs"].get("engine")) for _, e in entries})
    text = _fill(text, "- **Slug(s):**", ", ".join(f"`{e['slug']}`" for _, e in entries), name)
    text = _fill(text, "- **Model:**", ", ".join(f"`{st['model_id']}`" for st in states), name)
    text = _fill(text, "- **Engine:**", "; ".join(
        f"`{eng}`, registered in `scripts/lib/profiles/engines/`" if eng in core_engines
        else f"`{eng}`, added by its engine PR (send that first)" for eng in engines), name)
    text = _fill(text, "- **Weights variant / topology / KV:**", "; ".join(
        f"`{e['kwargs'].get('weights_variant')}` / {topo.get(e['slug'], '?')} / "
        f"`{e['kwargs'].get('kv_format')}`" for _, e in entries), name)
    text = _fill(text, "- **Status:**", "; ".join(
        f"`{e['slug']}` {_compose_status(root, e)}" for _, e in entries), name)
    present = {c for c, _ in cases.values()}
    for n in (1, 2, 3):
        anchor = f"- [ ] **Case {n}:**"
        if anchor not in text:
            raise TemplateDrift(f"{name} has no {anchor!r} line")
        if n in present:
            text = text.replace(anchor, f"- [x] **Case {n}:**", 1)
    if 2 in present:
        existing = sorted({s for c, ex in cases.values() if c == 2 for s in ex})
        old = "Model's existing slug(s): `<slug>`"
        if old not in text:
            raise TemplateDrift(f"{name} has no {old!r}")
        text = text.replace(old, "Model's existing slug(s): "
                            + ", ".join(f"`{s}`" for s in existing), 1)
    else:
        text = _drop_section(text, "## Case 2:")
    if 3 not in present:
        text = _drop_section(text, "## Case 3:")
    return _put_export_check(text, _export_check_block(findings, network, extra), name)


def engine_pr_body(root: Path, eng: dict, translated: list[dict], findings: dict,
                   network: bool, extra: list[str]) -> str:
    name = "engine.md"
    text = _template(root, name)
    prof = eng["profile"]
    lin = prof.get("lineage") or {}
    spec = (prof.get("install") or {}).get("spec") or "?"
    text = _fill(text, "- **Engine id:**", f"`{eng['engine_id']}`. Profile: "
                 f"`scripts/lib/profiles/engines/{eng['engine_id']}.yml`", name)
    base = " ".join(str(x) for x in (lin.get("base", "?"), lin.get("base_version")) if x)
    text = _fill(text, "- **Lineage:**",
                 f"relation `{lin.get('relation', '?')}` · base `{base}` · "
                 f"source `{lin.get('source', '?')}@{lin.get('source_ref', '?')}`", name)
    text = _fill(text, "- **Image:**", f"`{spec}`", name)
    slugs = [t["slug"] for t in translated]
    first = ", ".join(f"`{s}`" for s in slugs) if slugs else "none in this bundle"
    if eng["left_out"]:
        first += (" (left out, on models the catalog doesn't have yet: "
                  + ", ".join(f"`{s}`" for s in eng["left_out"]) + "; they follow in a model PR)")
    text = _fill(text, "- **First slugs:**", first, name)
    return _put_export_check(text, _export_check_block(findings, network, extra), name)


# ── plan + write ────────────────────────────────────────────────────────────

def build(root: Path, *, model_states: list[dict], engine: Optional[dict], network: bool
          ) -> tuple[dict[str, Any], list[str], list[str]]:
    """Everything the bundle will contain, as {relative path: text}, plus the plan's
    notes and the refusal gaps. Writes nothing."""
    reg_mod = registry_module(root)
    files: dict[str, str] = {}
    notes: list[str] = []
    gaps: list[str] = []
    findings: dict[str, list] = {}
    translated: list[dict] = []
    todo: list[str] = []
    rebased_md: list[str] = []
    carried_md: list[str] = []

    if engine is not None:
        egaps, efindings, etodo = engine_gaps(root, engine, network=network)
        gaps += egaps
        findings[f"engine {engine['engine_id']}"] = efindings
        files[f"{_CORE_ENGINES_REL}/{engine['engine_id']}.yml"] = engine["text"]
        if engine["left_out"]:
            notes.append("left out (their models aren't curated yet; a model PR follows "
                         "this one): " + ", ".join(engine["left_out"]))
        todo += etodo

    for st in model_states:
        gaps += completeness_gaps(root, st)
    if gaps:
        return files, notes, gaps
    for st in model_states:
        gaps += catalog_rule_gaps(root, st, network=network, findings_out=findings)
        mid = st["model_id"]
        if st["kind"] == NEW_MODEL:
            files[f"models/{mid}.yml"] = st["profile_text"]
        elif new_variants(st):
            files[f"weights-snippet-{mid}.yaml"] = render_weights_snippet(st)
        for e in st["entries"]:
            t = translated_entry(st, e, reg_mod)
            translated.append(t)
            local_text = (root / t["local_compose"]).read_text(encoding="utf-8")
            text, rebased, ships, mgaps, carry = rebase_mounts(
                root, t["local_compose"], t["kwargs"]["compose_path"], local_text)
            gaps += mgaps
            files[t["kwargs"]["compose_path"]] = text
            for rel in carry:
                notes.append(f"{t['slug']}: carries {rel}, which the compose mounts")
                carried_md.append(f"- `{rel}` (mounted by `{t['slug']}`)")
            files.update(carry)
            for old, new in rebased:
                notes.append(f"{t['slug']}: mount rebased for the core location: {old} → {new}")
                rebased_md.append(f"- `{t['slug']}`: `{old}` → `{new}`")
            for s in ships:
                item = (f"add `{s}`, the raw-compose cache fallback `{t['slug']}` mounts "
                        "(copy a sibling's `cache/.gitignore` and `README.md`)")
                notes.append(f"still to do: {item}")
                todo.append(item)
    if translated:
        kinds = {st["kind"] for st in model_states}
        what = (f"engine {engine['engine_id']}" if engine is not None
                else ", ".join(st["model_id"] for st in model_states))
        files["registry-entry.yaml"] = render_registry_entry_yaml(reg_mod, translated, what, kinds)
    if gaps:
        return files, notes, gaps
    extra: list[str] = []
    for title, items in (
        ("**Still to do in this PR:**", [f"- [ ] {t}" for t in todo]),
        ("**Mounts re-pointed for the core location** (the local layer sits three "
         "directories deeper than `models/`):", rebased_md),
        ("**Files carried from the local layer** (the composes mount them):", carried_md),
    ):
        if items:
            extra += ([""] if extra else []) + [title, ""] + items
    if engine is not None:
        files["PR_BODY.md"] = engine_pr_body(root, engine, translated, findings, network, extra)
    else:
        files["PR_BODY.md"] = slug_pr_body(root, reg_mod, model_states, translated,
                                           findings, network, extra)
    for t in translated:
        notes.append(f"entry: {t['slug']}  port {t['kwargs'].get('default_port')}"
                     f"  status {t['kwargs'].get('status')}")
    return files, notes, gaps


def plan_lines(out: Path, files: dict[str, Any], notes: list[str]) -> list[str]:
    lines = [f"plan (CORE PR bundle → {out}):"]
    lines += [f"  write {out / rel}" for rel in files]
    lines += [f"  {n}" for n in notes]
    lines.append("  NOTHING in this repo is touched — copy the bundle onto your PR branch "
                 "and merge registry-entry.yaml (command inside the file).")
    return lines


def write_bundle(out: Path, files: dict[str, Any]) -> list[Path]:
    written: list[Path] = []
    for rel, content in files.items():
        abs_ = out / rel
        try:
            abs_.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                abs_.write_bytes(content)
            else:
                abs_.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise Refusal(f"could not write {abs_}: {exc}")
        written.append(abs_)
        _info(f"wrote {abs_}")
    return written


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
        prog="export_pr.py",
        description=(
            "Export what the LOCAL layer validated — a new model, slugs on a curated "
            "model, or an engine — as a ready-to-commit CORE PR bundle with a "
            "pre-filled PR_BODY.md. Writes ONLY under --out."
        ),
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--spec-env", metavar="VAR",
                   help="read the export spec JSON ({model_id: …} or {engine_id: …}) from this env var")
    g.add_argument("--spec-file", metavar="PATH",
                   help="read the export spec JSON from this file")
    ap.add_argument("--root", default=str(_DEFAULT_ROOT),
                    help="repo root (default: this checkout)")
    ap.add_argument("--out", default="/tmp",
                    help="bundle output directory (default: /tmp)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan, write nothing")
    ap.add_argument("--network", action="store_true",
                    help="also run the network catalog rules (weights download from "
                         "Hugging Face, anonymous image pull); required before a PR")
    ap.add_argument("--check", action="store_true",
                    help="validate completeness only — never writes anything")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    # #1142 / #1466: same as promote.py — no shell wrapper loads settings for this
    # tool, so do it here through the one loader (real environment wins). Matters
    # for --spec-env.
    try:  # package context
        from scripts.lib.club_config import load as load_config
    except ImportError:  # direct-script context
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from club_config import load as load_config
    load_config(root)
    out = Path(args.out).resolve()
    try:
        if args.spec_env:
            raw_spec = os.environ.get(args.spec_env)
            if not raw_spec:
                raise Refusal(f"env var {args.spec_env} is empty/unset — no spec")
            spec = json.loads(raw_spec)
        else:
            spec = json.loads(Path(args.spec_file).read_text(encoding="utf-8"))
            spec = _apply_model_spec(spec)
        if not isinstance(spec, dict):
            raise Refusal("spec is not a JSON object")
        slugs = spec.get("slugs")
        if slugs is not None and (not isinstance(slugs, list)
                                  or not all(isinstance(s, str) for s in slugs)):
            raise Refusal("spec.slugs must be a list of slug strings")
        mid, eid = spec.get("model_id"), spec.get("engine_id")
        if (mid is None) == (eid is None):
            raise Refusal("the spec names exactly one of model_id or engine_id")
        engine = None
        if eid is not None:
            if not isinstance(eid, str) or not _ID_RE.match(eid):
                raise Refusal(f"bad spec.engine_id {eid!r} — expected [a-z0-9][a-z0-9._-]*")
            engine = load_local_engine(root, eid, slugs)
            states = engine["states"]
            label = f"engine {eid}"
        else:
            if not isinstance(mid, str) or not _MODEL_ID_RE.match(mid or ""):
                raise Refusal(
                    f"bad spec.model_id {mid!r} — expected [a-z0-9][a-z0-9._-]*"
                )
            states = [load_local_state(root, mid, slugs)]
            label = mid
        files, notes, gaps = build(root, model_states=states, engine=engine,
                                   network=args.network)
        if gaps:
            print(f"[export-pr] REFUSED: {label} is not PR-ready — {len(gaps)} gap(s) "
                  "(required core fields, catalog rules; docs/ADDING_MODELS.md, "
                  "docs/ADDING_ENGINES.md):", file=sys.stderr)
            for gp in gaps:
                print(f"  - {gp}", file=sys.stderr)
            return EXIT_COLLISION
        if not args.network:
            _info("network rules not run (weights download from Hugging Face, anonymous "
                  "image pull): export again with --network before opening the PR, so "
                  "PR_BODY.md records them.")
    except Refusal as exc:
        print(f"[export-pr] REFUSED: {exc}", file=sys.stderr)
        return EXIT_COLLISION
    except json.JSONDecodeError as exc:
        print(f"[export-pr] REFUSED: spec is not valid JSON: {exc}",
              file=sys.stderr)
        return EXIT_COLLISION
    except TemplateDrift as exc:
        print(f"[export-pr] ERROR: {exc}", file=sys.stderr)
        return EXIT_INTERNAL

    for ln in plan_lines(out, files, notes):
        _info(ln)
    if args.check:
        _info(f"CHECK_OK {label} — all required core fields present.")
        return 0
    if args.dry_run:
        _info("dry-run — nothing written.")
        return 0

    write_bundle(out, files)
    _info(f"bundle ready: copy it onto your PR branch, merge {out / 'registry-entry.yaml'} "
          "with the command inside it, run the full suite (docs/ADDING_MODELS.md "
          f"Steps 4b–7), then open the PR with: gh pr create --body-file {out / 'PR_BODY.md'}")
    print(f"EXPORT_OK {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    try:
        raise SystemExit(main())
    except Refusal as exc:  # defensive: raised outside the validate window
        print(f"[export-pr] REFUSED: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_COLLISION)
