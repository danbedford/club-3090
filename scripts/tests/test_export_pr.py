#!/usr/bin/env python3
"""export_pr.py tests — LOCAL layer → ready-to-commit CORE PR bundle.

Runs the CLI as a SUBPROCESS against a throwaway repo copy (the same harness
as test_promote.py): a synthetic profiles-local/ layer is seeded in tmp, the
bundle lands under a second tmp dir, and NOTHING inside the repo root may be
touched by an export.

registry-as-data branch: artifact 3 is a registry ENTRY FILE
(``registry-entry.yaml`` — the ``entries:`` map in exactly the registry.yaml
DATA subset), NOT a compose_registry.py source patch.  The real-merge proof
merges the exported entry into a tmp copy of THIS branch's registry.yaml via
the loader's own load → update → dump_registry_yaml path (what promote.py
--layer core does) and loads the result through ``get_registry()``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

# The throwaway repo needs everything the compose_registry import + the merge
# proof (get_registry()) need: the whole lib tree (loader shim + registry.yaml).
_COPY_TREES = ("scripts/lib", ".github/PULL_REQUEST_TEMPLATE")


@pytest.fixture()
def root(tmp_path):
    # The throwaway repo lives in a SIBLING dir of tmp_path so the --out
    # bundle (tmp_path/bundle) is provably outside the exported repo.
    repo = tmp_path / "repo"
    repo.mkdir()
    for rel in _COPY_TREES:
        shutil.copytree(
            REPO / rel,
            repo / rel,
            # #1142: skip root-owned container torch_compile caches under
            # models/ — copytree dies on them (Permission denied) on any
            # rig that has served a model. Pure build artifact.
            #
            # ⚠️⚠️ `profiles-local` is skipped for a DIFFERENT and sharper reason
            # (#1316 follow-up): it is the REAL local layer of whoever is running
            # the suite. Copying it in makes these tests inherit that machine's
            # promoted models — so they pass on a clean checkout and fail on any
            # rig that has ever run `promote.py --layer local`, which is the very
            # feature under test. Every test here seeds its own synthetic layer;
            # the real one must never be visible.
            ignore=shutil.ignore_patterns("__pycache__", "cache", "profiles-local"),
        )
    return repo


MID = "my-model"
QUANT = "autoround-int4"
CORE_SLUG = f"vllm/{MID}-dual-{QUANT}"
# ⭐ PUBLISH IS NO LONGER A RENAME (#1205). `local/` was hard-cut, so a local slug
# already IS '<engine>/<name>' — the core slug is the slug the user has been
# running, and every reference they hold survives publication. These are
# deliberately the SAME string; that identity is the contract under test.
LOCAL_SLUG = CORE_SLUG
LOCAL_COMPOSE_REL = (
    f"scripts/lib/profiles-local/composes/{MID}/vllm/compose/dual/{QUANT}/base.yml"
)
CORE_COMPOSE_REL = f"models/{MID}/vllm/compose/dual/{QUANT}/base.yml"

COMPOSE_TEXT = (
    "# Profile (at-a-glance):\n"
    "#   Model:     My Model\n"
    "#   Status:    🐣 Incubating\n"
    "#   Caveats:   nothing boot-validated yet\n"
    "services:\n"
    "  vllm:\n"
    "    image: vllm/vllm-openai:v0.27.1\n"
    "    container_name: vllm-my-model-dual\n"
    '    ports:\n'
    '      - "${PORT:-20242}:8000"\n'
)

# The same compose for a llama.cpp slug. The container name has to read as the
# slug's engine family (catalog_rules container-family, docs/ADDING_ENGINES.md
# → CONTRACT — naming), so the llama fixture cannot reuse the vllm one.
LLAMA_COMPOSE_TEXT = (
    COMPOSE_TEXT.replace("  vllm:\n", "  llamacpp:\n")
    .replace("vllm-my-model-dual", "llamacpp-my-model-dual")
    .replace('"${PORT:-20242}:8000"', '"${PORT:-20242}:8080"')
)


def _profile_text(**over) -> str:
    parts = [
        "schema_version: 1",
        f"id: {MID}",
        over.get("display_name", "display_name: My Model"),
        over.get("family", "family: generic-dense"),
        "num_hidden_layers: 40",
        "num_kv_heads: 4",
        "num_attention_heads: 24",
        "head_dim: 128",
        "weights:",
        f"  {QUANT}:",
        f"    path: {MID}-{QUANT}",
        f"    local_subdir: {MID}-{QUANT}",
        "    size_gb: 12.5",
        '    format: "autoround"',
        "    status: incubating",
        '    hf_repo: "org/model"',
        '    files: ["*.safetensors"]',
        '    engine: "vllm"',
        '    kind: "main"',
        '    verify_glob: "*.safetensors"',
        f"default_weight_variant: {QUANT}",
        "compatible_drafters: []",
        "vision_capable: false",
    ]
    return "\n".join(parts) + "\n"


def _entry_kwargs() -> dict:
    return {
        "model": MID,
        "weights_variant": QUANT,
        "workload": "long-ctx-single",
        "engine": "vllm-stable",
        "drafter": None,
        "kv_format": "fp8_e5m2",
        "tp": 2,
        "max_ctx": 131072,
        "max_num_seqs": 2,
        "mem_util": 0.92,
        "compose_path": LOCAL_COMPOSE_REL,
        "default_port": 20242,
        "kvcalc_key": f"{MID}:dual",
        "status": "incubating",
        "status_note": "community loop entry",
    }


def _seed_local(root: Path, *, profile_text=None, entry_kwargs=None, slug=None):
    """Seed the gitignored LOCAL layer exactly as promote.py --layer local
    writes it."""
    (root / "scripts/lib/profiles-local/models.d").mkdir(parents=True, exist_ok=True)
    (root / "scripts/lib/profiles-local/models.d" / f"{MID}.yml").write_text(
        profile_text or _profile_text(), encoding="utf-8"
    )
    comp_rel = (
        (entry_kwargs or {}).get("compose_path") or LOCAL_COMPOSE_REL
    )
    comp = root / comp_rel
    comp.parent.mkdir(parents=True, exist_ok=True)
    comp.write_text(COMPOSE_TEXT, encoding="utf-8")
    raw = {}
    reg = root / "scripts/lib/profiles-local/registry.local.json"
    if reg.exists():
        raw = json.loads(reg.read_text(encoding="utf-8"))
    raw[slug or LOCAL_SLUG] = entry_kwargs or _entry_kwargs()
    reg.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")


def _spec_file(root: Path) -> Path:
    f = root / "export-spec.json"
    payload = json.dumps({"model_id": MID})
    # Idempotent — a rewrite would bump the mtime the no-repo-touches test
    # snapshots around.
    if not f.exists() or f.read_text(encoding="utf-8") != payload + "\n":
        f.write_text(payload + "\n", encoding="utf-8")
    return f


def _run_cli(root, out, *extra, env_extra=None):
    env = dict(os.environ)
    env.pop("C3_ALLOW_CORE_PROMOTE", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts/lib/profiles/export_pr.py"),
            "--spec-file", str(_spec_file(root)),
            "--root", str(root),
            "--out", str(out),
            *extra,
        ],
        cwd=str(root),
        capture_output=True,
        text=True,
        env=env,
    )


def _merge_entry_file(repo: Path, entry_yaml: Path):
    """The contributor step: run the CANONICAL merge command exactly as
    documented inside registry-entry.yaml — parse the entry file with the
    loader's own reader, refuse slug collisions, merge + rewrite registry.yaml
    canonically (the same load → update → dump_registry_yaml rewrite promote.py
    --layer core performs)."""
    import re

    text = entry_yaml.read_text(encoding="utf-8")
    m = re.search(r'^#\s+python3 -c "(.+)"', text, re.MULTILINE)
    assert m, "registry-entry.yaml must carry the canonical python3 -c merge"
    return subprocess.run(
        [sys.executable, "-c", m.group(1), str(entry_yaml)],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )



class TestHappyPath:
    def test_exports_three_core_artifacts(self, root, tmp_path):
        _seed_local(root)
        out = tmp_path / "bundle"
        res = _run_cli(root, out)
        assert res.returncode == 0, res.stderr + res.stdout
        assert f"EXPORT_OK {out}" in res.stdout

        # Artifact 1 — the ModelProfile, copied verbatim to the CORE path.
        prof = out / "models" / f"{MID}.yml"
        assert prof.exists()
        assert prof.read_text(encoding="utf-8") == _profile_text()

        # Artifact 2 — the compose, translated to the CORE layout path
        # (profiles-local/composes/<rest> → models/<rest>).
        comp = out / CORE_COMPOSE_REL
        assert comp.exists(), sorted(p.relative_to(out) for p in out.rglob("*"))
        assert comp.read_text(encoding="utf-8") == COMPOSE_TEXT

        # Artifact 3 — the registry ENTRY FILE: the core slug (identical to the
        # local one since #1205 — publish is not a rename), the TRANSLATED
        # compose_path, and the setup-block notes + merge command as comments.
        entry = out / "registry-entry.yaml"
        assert entry.exists()
        text = entry.read_text(encoding="utf-8")
        assert f"{CORE_SLUG}:" in text
        assert f"compose_path: {CORE_COMPOSE_REL}" in text
        assert LOCAL_COMPOSE_REL not in text      # no LOCAL path leaks
        assert "setup:" in text                   # setup-block notes present
        assert "incubating" in text               # status guidance present
        assert "dump_registry_yaml" in text       # canonical merge command named
        # The entry file is in EXACTLY the registry.yaml DATA subset — the
        # branch's own stdlib parser reads it without guessing.
        sys.path.insert(0, str(root))
        from scripts.lib.profiles.compose_registry import parse_registry_text

        parsed = parse_registry_text(text, source="registry-entry.yaml")
        kw = parsed["entries"][CORE_SLUG]
        assert kw["model"] == MID and kw["default_port"] == 20242
        assert kw["mem_util"] == 0.92 and kw["drafter"] is None

    def test_entry_file_merges_and_get_registry_loads_it(self, root, tmp_path):
        """REAL MERGE PROOF: the exported entry merges into a tmp copy of THIS
        branch's registry.yaml via the documented canonical command, and the
        merged catalog loads through get_registry() carrying the new slug."""
        _seed_local(root)
        out = tmp_path / "bundle"
        res = _run_cli(root, out)
        assert res.returncode == 0, res.stderr + res.stdout

        merged = _merge_entry_file(root, out / "registry-entry.yaml")
        assert merged.returncode == 0, merged.stderr + merged.stdout
        # registry.yaml carries the row; the loader shim is untouched.
        reg_text = (root / "scripts/lib/profiles/registry.yaml").read_text()
        assert f"{CORE_SLUG}:" in reg_text
        assert MID not in (root / "scripts/lib/profiles/compose_registry.py").read_text()

        # The mutated catalog imports + carries the new slug (fresh process,
        # cwd = the tmp repo → ITS OWN registry.yaml).
        # A PR branch carries ONLY models/ + the merged registry entry — the
        # gitignored LOCAL layer stays behind (and its ids must not collide
        # with core once promoted).
        shutil.rmtree(root / "scripts/lib/profiles-local")

        # The mutated catalog imports + carries the new slug (fresh process,
        # cwd = the tmp repo → ITS OWN registry.yaml).
        chk = subprocess.run(
            [
                sys.executable, "-c",
                "from scripts.lib.profiles.compose_registry import get_registry;"
                f"e = get_registry()['{CORE_SLUG}'];"
                "assert e['model'] == 'my-model' and e['pp'] == 1;"
                "assert e['kvcalc_key'] == 'my-model:dual';"
                "assert e['status'] == 'incubating'",
            ],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
        assert chk.returncode == 0, chk.stderr

        # And the canonical post-edit gate passes on the merged catalog.
        mig = subprocess.run(
            [sys.executable,
             "scripts/lib/profiles/migrate_registry_to_yaml.py", "--check"],
            cwd=str(root), capture_output=True, text=True,
        )
        assert mig.returncode == 0, mig.stderr + mig.stdout

    def test_merge_refuses_slug_collision(self, root, tmp_path):
        """Merging twice must refuse loudly (promote.py semantics) — never
        silently overwrite an existing curated slug."""
        _seed_local(root)
        out = tmp_path / "bundle"
        res = _run_cli(root, out)
        assert res.returncode == 0, res.stderr + res.stdout
        first = _merge_entry_file(root, out / "registry-entry.yaml")
        assert first.returncode == 0, first.stderr
        again = _merge_entry_file(root, out / "registry-entry.yaml")
        assert again.returncode != 0
        assert "slug collision" in again.stderr

    def test_export_touches_nothing_inside_the_repo(self, root, tmp_path):
        _seed_local(root)
        _spec_file(root)   # pre-write: the CLI must not touch it either
        out = tmp_path / "bundle"
        def _snap():
            return {
                p.relative_to(root): p.stat().st_mtime
                for p in root.rglob("*")
                if p.is_file() and "__pycache__" not in p.parts
            }

        before = _snap()
        res = _run_cli(root, out)
        assert res.returncode == 0, res.stderr
        after = _snap()
        # registry.local.json is READ, never rewritten; registry.yaml /
        # compose_registry.py are never edited by the export itself.
        changed = {k for k in after if before.get(k) != after[k]}
        assert not changed, changed

    def test_llama_family_defaults_kvcalc_skip(self, root, tmp_path):
        kw = _entry_kwargs()
        kw["engine"] = "llama-cpp-local"
        kw.pop("kvcalc_key")
        local_compose = (
            f"scripts/lib/profiles-local/composes/{MID}/llama-cpp/compose/single/"
            "unsloth-q4km/base.yml"
        )
        kw["compose_path"] = local_compose
        # ⚠️ The slug's engine slot must AGREE with the compose path's engine dir.
        # Since #1205 publish no longer renames (it used to strip `local/` and
        # re-derive the prefix from the path), so a `vllm/` slug on a llama-cpp
        # compose is now a packaging mistake the exporter refuses — see
        # test_slug_must_match_its_compose_path_namespace below. Seed the
        # matching slug: llama.cpp FILESYSTEM dir → `llamacpp` slug prefix
        # (ADDING_MODELS Step 3).
        llama_slug = f"llamacpp/{MID}-dual-{QUANT}"
        _seed_local(root, entry_kwargs=kw, slug=llama_slug)
        (root / local_compose).write_text(LLAMA_COMPOSE_TEXT, encoding="utf-8")
        out = tmp_path / "bundle"
        res = _run_cli(root, out)
        assert res.returncode == 0, res.stderr + res.stdout
        text = (out / "registry-entry.yaml").read_text(encoding="utf-8")
        assert "kvcalc_key: SKIP" in text
        assert f"{llama_slug}:" in text
        assert (out / "models" / MID / "llama-cpp/compose/single"
                / "unsloth-q4km/base.yml").exists()


class TestRefusals:
    def test_slug_must_match_its_compose_path_namespace(self, root, tmp_path):
        # #1205 made publish a no-op on the slug, so the engine slot is now
        # load-bearing: it is what the core catalog will carry. A slug whose
        # engine disagrees with the engine dir its compose lives in is a
        # packaging mistake, and the exporter says so rather than silently
        # publishing one of the two.
        kw = _entry_kwargs()
        kw["compose_path"] = (
            f"scripts/lib/profiles-local/composes/{MID}/llama-cpp/compose/single/"
            "unsloth-q4km/base.yml"
        )
        _seed_local(root, entry_kwargs=kw, slug=f"vllm/{MID}-dual-{QUANT}")
        (root / kw["compose_path"]).write_text(COMPOSE_TEXT, encoding="utf-8")
        res = _run_cli(root, tmp_path / "bundle")
        assert res.returncode == 3
        assert "does not match the namespace implied by its compose path" in res.stderr
        assert not (tmp_path / "bundle").exists()

    def test_container_from_another_engine_family_refused(self, root, tmp_path):
        # A llama.cpp slug whose compose still names its container `vllm-…` —
        # the copy-a-sibling mistake. Discovery and engine-kind would read the
        # running container as vLLM, so the export refuses it as a required
        # catalog rule (catalog_rules container-family).
        kw = _entry_kwargs()
        kw["engine"] = "llama-cpp-local"
        kw.pop("kvcalc_key")
        kw["compose_path"] = (
            f"scripts/lib/profiles-local/composes/{MID}/llama-cpp/compose/single/"
            "unsloth-q4km/base.yml"
        )
        _seed_local(root, entry_kwargs=kw, slug=f"llamacpp/{MID}-dual-{QUANT}")
        (root / kw["compose_path"]).write_text(COMPOSE_TEXT, encoding="utf-8")
        res = _run_cli(root, tmp_path / "bundle")
        assert res.returncode == 3
        assert "container-family" in res.stderr
        assert "vllm-my-model-dual" in res.stderr
        assert not (tmp_path / "bundle").exists()

    def test_removed_local_namespace_refused_with_the_fix_named(self, root, tmp_path):
        # A user holding a pre-#1205 registry.local.json meets this. The message
        # must describe what is wrong (it USES `local/`) — it previously said the
        # slug "lacks" that namespace, which was the inverse of the truth.
        _seed_local(root, slug=f"local/{MID}-dual-{QUANT}")
        res = _run_cli(root, tmp_path / "bundle")
        assert res.returncode == 3
        assert "was removed" in res.stderr
        assert "lacks" not in res.stderr
        assert f"'<engine>/{MID}-dual-{QUANT}'" in res.stderr

    def test_incomplete_entry_refused_and_writes_nothing(self, root, tmp_path):
        # The scaffold's leftover <...> family placeholder — the exact state a
        # contributor exports too early.
        _seed_local(root, profile_text=_profile_text(family="family: <family-tag>"))
        out = tmp_path / "bundle"
        res = _run_cli(root, out)
        assert res.returncode == 3
        assert "REFUSED" in res.stderr
        assert "family" in res.stderr
        assert not out.exists() or not any(out.iterdir())

    def test_missing_local_model_refused(self, root, tmp_path):
        res = _run_cli(root, tmp_path / "bundle")   # nothing seeded
        assert res.returncode == 3
        assert "no LOCAL model profile" in res.stderr

    def test_vllm_without_kvcalc_key_refused(self, root, tmp_path):
        kw = _entry_kwargs()
        del kw["kvcalc_key"]
        _seed_local(root, entry_kwargs=kw)
        res = _run_cli(root, tmp_path / "bundle")
        assert res.returncode == 3
        assert "kvcalc_key" in res.stderr

    def test_gguf_without_verify_glob_refused(self, root, tmp_path):
        prof = _profile_text().replace(
            'verify_glob: "*.safetensors"', ""
        ).replace('format: "autoround"', 'format: "gguf"').replace(
            'files: ["*.safetensors"]', 'files: ["model-q4km.gguf"]'
        )
        _seed_local(root, profile_text=prof)
        res = _run_cli(root, tmp_path / "bundle")
        assert res.returncode == 3
        assert "verify_glob" in res.stderr

    def test_compose_without_status_header_refused(self, root, tmp_path):
        _seed_local(root)
        (root / LOCAL_COMPOSE_REL).write_text(
            "services:\n  vllm:\n    image: x\n", encoding="utf-8"
        )
        res = _run_cli(root, tmp_path / "bundle")
        assert res.returncode == 3
        assert "Status:" in res.stderr

    def test_missing_compose_on_disk_refused(self, root, tmp_path):
        _seed_local(root)
        (root / LOCAL_COMPOSE_REL).unlink()
        res = _run_cli(root, tmp_path / "bundle")
        assert res.returncode == 3
        assert "missing on disk" in res.stderr

    def test_no_local_entries_for_model_refused(self, root, tmp_path):
        _seed_local(root)
        reg = root / "scripts/lib/profiles-local/registry.local.json"
        reg.unlink()
        res = _run_cli(root, tmp_path / "bundle")
        assert res.returncode == 3
        assert "no registry.local.json entries" in res.stderr


class TestModes:
    def test_dry_run_writes_nothing(self, root, tmp_path):
        _seed_local(root)
        out = tmp_path / "bundle"
        res = _run_cli(root, out, "--dry-run")
        assert res.returncode == 0, res.stderr
        assert "dry-run — nothing written." in res.stdout
        assert "EXPORT_OK" not in res.stdout
        assert not out.exists() or not any(out.iterdir())

    def test_check_validates_without_writing(self, root, tmp_path):
        _seed_local(root)
        out = tmp_path / "bundle"
        res = _run_cli(root, out, "--check")
        assert res.returncode == 0, res.stderr
        assert f"CHECK_OK {MID}" in res.stdout
        assert not out.exists() or not any(out.iterdir())

    def test_check_lists_every_gap(self, root, tmp_path):
        _seed_local(
            root,
            profile_text=_profile_text(
                display_name="display_name: <Human-readable name>",
                family="family: <family-tag>",
            ),
        )
        res = _run_cli(root, tmp_path / "bundle", "--check")
        assert res.returncode == 3
        assert "display_name" in res.stderr and "family" in res.stderr

    def test_spec_env_input(self, root, tmp_path):
        _seed_local(root)
        env_extra = {"C3_EXPORT_SPEC": json.dumps({"model_id": MID})}
        res = subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts/lib/profiles/export_pr.py"),
                "--spec-env", "C3_EXPORT_SPEC",
                "--root", str(root),
                "--out", str(tmp_path / "bundle"),
            ],
            cwd=str(root),
            capture_output=True,
            text=True,
            env={**os.environ, **env_extra},
        )
        assert res.returncode == 0, res.stderr + res.stdout


# ── Cases 1 and 2: slugs a contributor attached to a CURATED model ──────────
# docs/ADDING_MODELS.md "Which case is this?". The local layer attaches them via
# extends.d/<core-id>.yml (local_extensions.py); the bundle carries composes,
# entries and only the NEW weights variants, never the curated models/<id>.yml.

CM = "qwen3.6-27b"
EXT = "test-ext-awq"
SIBLING = "models/qwen3.6-27b/vllm/compose/dual/autoround-int4/fp8-mtp.yml"
LOCAL_DEPTH = "../../../../../../../../../"   # composes/<id>/<dir>/compose/<t>/<q>/ → root
CORE_DEPTH = "../../../../../../"             # models/<id>/<dir>/compose/<t>/<q>/ → root


@pytest.fixture()
def core_root(root):
    # The curated model's own tree, so mounts into models/qwen3.6-27b resolve.
    shutil.copytree(REPO / "models" / CM, root / "models" / CM,
                    ignore=shutil.ignore_patterns("__pycache__", "cache"))
    (root / "models" / CM / "vllm" / "cache").mkdir()
    # The repo-level file its composes mount.
    shutil.copy2(REPO / "scripts/detect_nvlink.sh", root / "scripts/detect_nvlink.sh")
    return root


def _ext_variant(**over):
    w = {"path": "test-ext", "local_subdir": "test-ext", "size_gb": 15.0, "format": "awq",
         "status": "community-experimental", "hf_repo": "org/qwen36-awq",
         "files": ["*.safetensors", "*.json"], "verify_glob": "*.safetensors",
         "engine": "vllm", "kind": "main"}
    w.update(over)
    return w


def _attach(root, slugs: dict, *, variants=None, tp_add=None):
    """extends.d + composes + registry.local.json for slugs on the curated model.
    slugs: {slug: (kwargs overrides, compose text, local compose rel)}."""
    ext = {"schema_version": 1, "extends": CM,
           "weights": variants if variants is not None else {EXT: _ext_variant()}}
    if tp_add:
        ext["valid_tp_add"] = tp_add
    d = root / "scripts/lib/profiles-local/extends.d"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{CM}.yml").write_text(json.dumps(ext, indent=2), encoding="utf-8")
    raw = {}
    for slug, (over, text, rel) in slugs.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
        kw = {"model": CM, "weights_variant": EXT, "workload": "long-ctx-single",
              "engine": "vllm-stable", "drafter": "qwen-mtp-builtin", "kv_format": "fp8_e4m3",
              "tp": 2, "max_ctx": 131072, "max_num_seqs": 2, "mem_util": 0.92,
              "compose_path": rel, "default_port": 20251, "kvcalc_key": "qwen3.6-27b:dual",
              "status": "incubating", "status_note": "community entry"}
        kw.update(over)
        raw[slug] = kw
    reg = root / "scripts/lib/profiles-local/registry.local.json"
    reg.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")


def _sibling(local_depth=False):
    """The curated sibling compose; with local_depth, rewritten the way a working
    LOCAL copy has to be (its relative mounts reach three directories further)."""
    text = (REPO / SIBLING).read_text(encoding="utf-8")
    if local_depth:
        # Repo-level mounts first (6 → 9 levels), then the model's own patches,
        # which the local copy must reach in the curated tree.
        text = re.sub(r"(?<![./])(\.\./){6}", LOCAL_DEPTH, text)
        text = re.sub(r"(?<![./])\.\./\.\./\.\./patches/",
                      LOCAL_DEPTH + "models/qwen3.6-27b/vllm/patches/", text)
    return text


def _local_rel(dir_="vllm", quant=EXT, name="fp8-mtp.yml", topo="dual"):
    return f"scripts/lib/profiles-local/composes/{CM}/{dir_}/compose/{topo}/{quant}/{name}"


def _run_model(root, out, *extra, mid=CM, slugs=None):
    spec = {"model_id": mid}
    if slugs is not None:
        spec["slugs"] = slugs
    return _run_spec(root, out, spec, *extra)


def _run_spec(root, out, spec, *extra):
    f = root / "export-spec.json"
    f.write_text(json.dumps(spec) + "\n", encoding="utf-8")
    env = dict(os.environ)
    env.pop("C3_ALLOW_CORE_PROMOTE", None)
    return subprocess.run(
        [sys.executable, str(REPO / "scripts/lib/profiles/export_pr.py"),
         "--spec-file", str(f), "--root", str(root), "--out", str(out), *extra],
        capture_output=True, text=True, encoding="utf-8", env=env)


def _assert_clean_body(body: str, root: Path, out: Path):
    # It is the contributor's PR: no machine paths, no AI attribution.
    for leak in (str(root), str(out), "/home/", "/opt/", "/mnt/"):
        assert leak not in body, leak
    for attribution in ("Co-Authored-By", "Claude", "Generated with"):
        assert attribution not in body, attribution
    assert body.count("## Export check") == 1   # the template's section, filled, not a second one
    assert "fills this section" not in body      # its placeholder comment was replaced
    assert "<prefix>/<name>" not in body and "<model id>" not in body


class TestCuratedModelSlugs:
    def test_case1_bundle_has_no_model_profile_and_a_weights_snippet(self, core_root, tmp_path):
        slug = "vllm/qwen36-test-ext-dual"
        _attach(core_root, {slug: ({}, _sibling(local_depth=True), _local_rel())})
        out = tmp_path / "bundle"
        res = _run_model(core_root, out)
        assert res.returncode == 0, res.stderr + res.stdout
        assert not (out / "models" / f"{CM}.yml").exists()   # never the curated profile
        comp = out / "models" / CM / "vllm/compose/dual" / EXT / "fp8-mtp.yml"
        assert comp.exists()
        snippet = (out / f"weights-snippet-{CM}.yaml").read_text(encoding="utf-8")
        assert EXT in snippet and "org/qwen36-awq" in snippet
        assert f"compose_path: models/{CM}/vllm/compose/dual/{EXT}/fp8-mtp.yml" in \
            (out / "registry-entry.yaml").read_text(encoding="utf-8")
        body = (out / "PR_BODY.md").read_text(encoding="utf-8")
        assert "- [x] **Case 1:**" in body and "- [ ] **Case 2:**" in body
        assert "## Case 2:" not in body and "## Case 3:" not in body
        assert f"- **Slug(s):** `{slug}`" in body
        assert "registered in `scripts/lib/profiles/engines/`" in body
        _assert_clean_body(body, core_root, out)

    def test_mounts_written_for_the_local_depth_are_rebased_and_reported(self, core_root, tmp_path):
        slug = "vllm/qwen36-test-ext-dual"
        _attach(core_root, {slug: ({}, _sibling(local_depth=True), _local_rel())})
        out = tmp_path / "bundle"
        res = _run_model(core_root, out)
        assert res.returncode == 0, res.stderr + res.stdout
        text = (out / "models" / CM / "vllm/compose/dual" / EXT / "fp8-mtp.yml").read_text(encoding="utf-8")
        # Exactly the curated sibling again: every mount points where it did in core.
        assert text == _sibling(), "rebased compose differs from the curated sibling"
        assert "mount rebased" in res.stdout
        assert "Mounts re-pointed for the core location" in (out / "PR_BODY.md").read_text(encoding="utf-8")

    def test_mounts_already_right_for_core_are_copied_as_written(self, core_root, tmp_path):
        slug = "vllm/qwen36-test-ext-dual"
        _attach(core_root, {slug: ({}, _sibling(), _local_rel())})
        out = tmp_path / "bundle"
        res = _run_model(core_root, out)
        assert res.returncode == 0, res.stderr + res.stdout
        text = (out / "models" / CM / "vllm/compose/dual" / EXT / "fp8-mtp.yml").read_text(encoding="utf-8")
        assert text == _sibling()
        assert "mount rebased" not in res.stdout

    def test_a_mount_that_resolves_nowhere_is_refused(self, core_root, tmp_path):
        bad = _sibling().replace("../../../patches/froggeric-chat-template/",
                                 "../../../patches/no-such-patch/")
        _attach(core_root, {"vllm/qwen36-test-ext-dual": ({}, bad, _local_rel())})
        res = _run_model(core_root, tmp_path / "bundle")
        assert res.returncode == 3
        assert "no-such-patch" in res.stderr and "resolves to nothing" in res.stderr
        assert not (tmp_path / "bundle").exists()

    def test_a_mount_into_the_gitignored_local_layer_is_refused(self, core_root, tmp_path):
        # It exists on the contributor's machine, so it "resolves" here, and on no
        # one else's: a curated compose must never point into profiles-local.
        shared = core_root / "scripts/lib/profiles-local/engines.d/shared.jinja"
        shared.parent.mkdir(parents=True, exist_ok=True)
        shared.write_text("x\n", encoding="utf-8")
        bad = _sibling().replace(
            "../../../patches/froggeric-chat-template/chat_template.jinja",
            LOCAL_DEPTH + "scripts/lib/profiles-local/engines.d/shared.jinja")
        _attach(core_root, {"vllm/qwen36-test-ext-dual": ({}, bad, _local_rel())})
        res = _run_model(core_root, tmp_path / "bundle")
        assert res.returncode == 3, res.stdout
        assert "shared.jinja" in res.stderr and "resolves to nothing" in res.stderr

    def test_a_patch_only_the_local_layer_has_is_carried(self, core_root, tmp_path):
        # The compose mounts a patch directory beside it in the local layer; core
        # has none, so the bundle carries it to the same place in the curated tree.
        new = _sibling().replace("../../../patches/froggeric-chat-template/",
                                 "../../../patches/new-template/")
        _attach(core_root, {"vllm/qwen36-test-ext-dual": ({}, new, _local_rel())})
        patch = core_root / f"scripts/lib/profiles-local/composes/{CM}/vllm/patches/new-template"
        patch.mkdir(parents=True)
        (patch / "chat_template.jinja").write_text("{{ messages }}\n", encoding="utf-8")
        out = tmp_path / "bundle"
        res = _run_model(core_root, out)
        assert res.returncode == 0, res.stderr + res.stdout
        carried = out / "models" / CM / "vllm/patches/new-template/chat_template.jinja"
        assert carried.read_text(encoding="utf-8") == "{{ messages }}\n"
        assert "carries models/qwen3.6-27b/vllm/patches/new-template/chat_template.jinja" in res.stdout

    def test_case2_engine_new_to_the_model_names_its_existing_slugs(self, core_root, tmp_path):
        # Every llama.cpp qwen3.6-27b slug in core is deprecated, so this is case 2.
        slug = "llamacpp/qwen36-test-gguf-single"
        compose = LLAMA_COMPOSE_TEXT.replace("llamacpp-my-model-dual", "llama-cpp-qwen36-test")
        variants = {"test-gguf": _ext_variant(format="gguf", files=["*Q4_K_M.gguf"],
                                              verify_glob="*.gguf", engine="llama-cpp")}
        _attach(core_root, {slug: ({"engine": "llama-cpp-local", "weights_variant": "test-gguf",
                                    "drafter": None, "kv_format": "q8_0", "tp": 1,
                                    "kvcalc_key": None},
                                   compose, _local_rel("llama-cpp", "test-gguf", "base.yml", "single"))},
                variants=variants)
        out = tmp_path / "bundle"
        res = _run_model(core_root, out)
        assert res.returncode == 0, res.stderr + res.stdout
        body = (out / "PR_BODY.md").read_text(encoding="utf-8")
        assert "- [x] **Case 2:**" in body and "- [ ] **Case 1:**" in body
        assert "## Case 2:" in body and "## Case 3:" not in body
        assert "`vllm/dual`" in body          # the model's existing slug, from the registry
        assert "`<slug>`" not in body
        assert (out / "models" / CM / "llama-cpp/compose/single/test-gguf/base.yml").exists()
        _assert_clean_body(body, core_root, out)

    def test_compose_under_the_engine_id_moves_to_the_core_directory(self, core_root, tmp_path):
        # catalog.sh register files composes under the engine ID; core keeps
        # vllm-stable composes in vllm/.
        _attach(core_root, {"vllm/qwen36-test-ext-dual":
                            ({}, _sibling(local_depth=True), _local_rel("vllm-stable"))})
        out = tmp_path / "bundle"
        res = _run_model(core_root, out)
        assert res.returncode == 0, res.stderr + res.stdout
        assert (out / "models" / CM / "vllm/compose/dual" / EXT / "fp8-mtp.yml").exists()

    def test_slug_prefix_core_does_not_use_for_the_engine_is_refused(self, core_root, tmp_path):
        _attach(core_root, {"vllm-stable/qwen36-test-ext-dual": ({}, _sibling(), _local_rel())})
        res = _run_model(core_root, tmp_path / "bundle")
        assert res.returncode == 3
        assert "does not match the namespace implied by its compose path" in res.stderr
        assert "vllm/…" in res.stderr

    def test_new_variant_without_files_is_refused(self, core_root, tmp_path):
        _attach(core_root, {"vllm/qwen36-test-ext-dual": ({}, _sibling(), _local_rel())},
                variants={EXT: _ext_variant(files=None)})
        res = _run_model(core_root, tmp_path / "bundle")
        assert res.returncode == 3
        assert f"weights.{EXT}: files must be a non-empty list" in res.stderr

    def test_reusing_a_curated_variant_adds_no_snippet(self, core_root, tmp_path):
        _attach(core_root, {"vllm/qwen36-test-ext-dual":
                            ({"weights_variant": "autoround-int4"}, _sibling(),
                             _local_rel(quant="autoround-int4", name="fp8-mtp-test.yml"))},
                variants={})
        out = tmp_path / "bundle"
        res = _run_model(core_root, out)
        assert res.returncode == 0, res.stderr + res.stdout
        assert not list(out.glob("weights-snippet-*.yaml"))

    def test_overwriting_a_curated_compose_is_refused(self, core_root, tmp_path):
        _attach(core_root, {"vllm/qwen36-test-ext-dual":
                            ({"weights_variant": "autoround-int4"}, _sibling(),
                             _local_rel(quant="autoround-int4"))}, variants={})
        res = _run_model(core_root, tmp_path / "bundle")
        assert res.returncode == 3
        assert "already exists in the curated catalog" in res.stderr

    def test_spec_slugs_limits_the_export(self, core_root, tmp_path):
        a, b = "vllm/qwen36-test-ext-dual", "vllm/qwen36-test-ext-dual-b"
        _attach(core_root, {a: ({}, _sibling(), _local_rel()),
                            b: ({"default_port": 20252}, _sibling(),
                                _local_rel(name="fp8-mtp-b.yml"))})
        out = tmp_path / "bundle"
        res = _run_model(core_root, out, slugs=[b])
        assert res.returncode == 0, res.stderr + res.stdout
        entry = (out / "registry-entry.yaml").read_text(encoding="utf-8")
        assert f"{b}:" in entry and f"  {a}:\n" not in entry
        res = _run_model(core_root, tmp_path / "b2", slugs=["vllm/nope"])
        assert res.returncode == 3 and "not local slugs" in res.stderr


# ── An engine only the local layer has (docs/ADDING_ENGINES.md) ─────────────

ENG = "vllm-testfork"
DIGEST = "ghcr.io/maintainer/vllm-testfork@sha256:" + "a" * 64


def _engine_profile(**over) -> str:
    lin = over.pop("lineage", {"relation": "fork", "base": "vllm", "base_version": "0.31.0",
                               "source": "https://github.com/maintainer/vllm-testfork",
                               "source_ref": "0123456789abcdef0123456789abcdef01234567"})
    spec = over.pop("spec", DIGEST)
    method = over.pop("method", "docker_image")
    lines = ["schema_version: 1", f"id: {ENG}", "display_name: vLLM test fork", "type: vllm",
             "stability: experimental", "min_sm: 8.0", "image_env: VLLM_TESTFORK_IMAGE",
             "install:", f"  method: {method}", f"  spec: {spec}",
             "supported_kv_formats:", "  - fp8_e4m3"]
    if lin is not None:
        lines += ["lineage:"] + [f"  {k}: {json.dumps(v)}" for k, v in lin.items()]
    return "\n".join(lines) + "\n"


def _seed_engine(root, **over):
    d = root / "scripts/lib/profiles-local/engines.d"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ENG}.yml").write_text(_engine_profile(**over), encoding="utf-8")


class TestEngineExport:
    def _slug_on_fork(self, root):
        compose = _sibling(local_depth=True).replace("vllm-qwen36-27b-dual", "vllm-qwen36-testfork")
        _attach(root, {"vllm/qwen36-testfork-dual": ({"engine": ENG}, compose,
                                                     _local_rel(ENG))})

    def test_engine_bundle_with_its_first_slug(self, core_root, tmp_path):
        _seed_engine(core_root)
        self._slug_on_fork(core_root)
        # A slug on a model core doesn't have rides in a later PR.
        _seed_local(core_root, entry_kwargs=dict(_entry_kwargs(), engine=ENG,
                                                 compose_path=LOCAL_COMPOSE_REL.replace("/vllm/", f"/{ENG}/")),
                    slug=f"vllm/{MID}-testfork")
        out = tmp_path / "bundle"
        res = _run_spec(core_root, out, {"engine_id": ENG})
        assert res.returncode == 0, res.stderr + res.stdout
        assert (out / "scripts/lib/profiles/engines" / f"{ENG}.yml").read_text(encoding="utf-8") \
            == _engine_profile()
        assert (out / "models" / CM / ENG / "compose/dual" / EXT / "fp8-mtp.yml").exists()
        body = (out / "PR_BODY.md").read_text(encoding="utf-8")
        assert f"- **Engine id:** `{ENG}`" in body
        assert "relation `fork` · base `vllm 0.31.0`" in body
        assert f"- **Image:** `{DIGEST}`" in body
        assert "`vllm/qwen36-testfork-dual`" in body
        assert f"`vllm/{MID}-testfork`" in body and "left out" in body
        # image_env isn't allowlisted in the launchers yet: listed, not refused.
        assert "VLLM_TESTFORK_IMAGE" in body and "export_variant_engine_pin" in body
        assert not (out / "models" / f"{MID}.yml").exists()
        _assert_clean_body(body, core_root, out)

    def test_engine_without_lineage_names_the_fields(self, core_root, tmp_path):
        _seed_engine(core_root, lineage=None)
        res = _run_spec(core_root, tmp_path / "bundle", {"engine_id": ENG})
        assert res.returncode == 3
        assert "engine-lineage" in res.stderr and "source_ref" in res.stderr

    def test_fork_on_a_tag_is_refused(self, core_root, tmp_path):
        _seed_engine(core_root, spec="ghcr.io/maintainer/vllm-testfork:v1")
        res = _run_spec(core_root, tmp_path / "bundle", {"engine_id": ENG})
        assert res.returncode == 3
        assert "image-pin" in res.stderr and "digest" in res.stderr

    def test_method_docker_is_refused(self, core_root, tmp_path):
        _seed_engine(core_root, method="docker")
        res = _run_spec(core_root, tmp_path / "bundle", {"engine_id": ENG})
        assert res.returncode == 3
        assert "install.method must be docker_image" in res.stderr

    def test_catalog_sh_leftovers_are_listed_to_fix(self, core_root, tmp_path):
        _seed_engine(core_root)
        p = core_root / "scripts/lib/profiles-local/engines.d" / f"{ENG}.yml"
        p.write_text(p.read_text(encoding="utf-8").replace(
            "display_name: vLLM test fork",
            "display_name: vllm-testfork (local, registered from a compose)")
            + "notes: >\n  Registered by catalog.sh from x.yml.\n", encoding="utf-8")
        self._slug_on_fork(core_root)
        out = tmp_path / "bundle"
        res = _run_spec(core_root, out, {"engine_id": ENG})
        assert res.returncode == 0, res.stderr + res.stdout
        body = (out / "PR_BODY.md").read_text(encoding="utf-8")
        assert "real display_name" in body and "catalog.sh `notes:`" in body

    def test_engine_file_already_in_core_is_refused(self, core_root, tmp_path):
        _seed_engine(core_root)
        (core_root / "scripts/lib/profiles/engines" / f"{ENG}.yml").write_text(
            "# placeholder with another id\nid: something-else\n", encoding="utf-8")
        res = _run_spec(core_root, tmp_path / "bundle", {"engine_id": ENG})
        assert res.returncode == 3 and "already exists in the curated catalog" in res.stderr

    def test_curated_engine_is_refused(self, core_root, tmp_path):
        res = _run_spec(core_root, tmp_path / "bundle", {"engine_id": "vllm-stable"})
        assert res.returncode == 3 and "already curated" in res.stderr

    def test_spec_names_exactly_one_target(self, core_root, tmp_path):
        res = _run_spec(core_root, tmp_path / "bundle", {"engine_id": ENG, "model_id": CM})
        assert res.returncode == 3 and "exactly one of model_id or engine_id" in res.stderr


class TestPrBody:
    def test_case3_body_ticks_case3_and_keeps_its_section(self, root, tmp_path):
        _seed_local(root)
        out = tmp_path / "bundle"
        res = _run_cli(root, out)
        assert res.returncode == 0, res.stderr + res.stdout
        body = (out / "PR_BODY.md").read_text(encoding="utf-8")
        assert "- [x] **Case 3:**" in body and "## Case 3:" in body
        assert "## Case 2:" not in body
        assert f"- **Model:** `{MID}`" in body
        assert "Network rules not run" in body and "export again with `--network`" in body
        _assert_clean_body(body, root, out)

    def test_template_without_an_export_check_section_gets_one_before_evidence(self, root, tmp_path):
        _seed_local(root)
        tpl = root / ".github/PULL_REQUEST_TEMPLATE/slug.md"
        text = tpl.read_text(encoding="utf-8")
        start, end = text.index("## Export check"), text.index("## Evidence")
        tpl.write_text(text[:start] + text[end:], encoding="utf-8")
        out = tmp_path / "bundle"
        res = _run_cli(root, out)
        assert res.returncode == 0, res.stderr + res.stdout
        body = (out / "PR_BODY.md").read_text(encoding="utf-8")
        assert body.count("## Export check") == 1
        assert body.index("## Export check") < body.index("## Evidence")

    def test_template_drift_fails_loudly(self, root, tmp_path):
        _seed_local(root)
        tpl = root / ".github/PULL_REQUEST_TEMPLATE/slug.md"
        tpl.write_text(tpl.read_text(encoding="utf-8").replace("- **Model:**", "- **The model:**"),
                       encoding="utf-8")
        res = _run_cli(root, tmp_path / "bundle")
        assert res.returncode == 2
        assert "- **Model:**" in res.stderr and "template" in res.stderr
        assert not (tmp_path / "bundle").exists()
