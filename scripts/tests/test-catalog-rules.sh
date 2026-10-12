#!/usr/bin/env bash
#
# Guard: scripts/lib/profiles/catalog_rules.py, the one checker behind
# `catalog.sh register` (advisory), `preflight-add-model.sh` and
# `export_pr.py --check` (blocking). docs/ADDING_ENGINES.md and
# docs/ADDING_MODELS.md ("Which case is this?") are the contract.
#
#   1. every live curated slug passes every required rule, and every curated
#      engine does too apart from the listed exceptions
#   2. the exception lists only shrink, and each entry still applies
#   3. each required rule fails on a fixture built to break it and passes on one
#      that meets it, all in ONE batched call, so a row that drifts out of line
#      in the bash helper run shows up as the wrong verdict on its neighbour
#   4. same-model-defaults: advisory, and "the registry doesn't say" is not "same"
#   5. network rules are three-state with injected fakes: pass, fail, unchecked;
#      unchecked never passes and blocks only when the network was asked for
#   6. CLI exit codes, and the no-PyYAML path skips instead of crashing
#
# The delivery paths are guarded where they live: test-catalog-cli (register
# prints the rules and still succeeds), test-preflight-add-model (a curated slug
# the rules cannot check is a failure), test_export_pr.py (a required failure
# refuses the export).
set -uo pipefail
export CLUB3090_CONFIG_DIR=/nonexistent/club-3090-test-config   # tests never read your real settings (#1466)
export PYTHONUTF8="${PYTHONUTF8:-1}"
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
NAME="test-catalog-rules"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

python3 - "$ROOT" "$TMP" <<'PY' || { echo "FAIL: $NAME" >&2; exit 1; }
import sys
from pathlib import Path

root, tmp = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(root / "scripts/lib/profiles"))
sys.path.insert(0, str(root))
import catalog_rules as cr  # noqa: E402
from compose_registry import COMPOSE_REGISTRY  # noqa: E402

fails = []
def check(cond, msg):
    if not cond:
        fails.append(msg)

def nonpass(findings, *, skip=("weights-download", "image-anonymous")):
    return {(f.rule, f.level, f.state) for f in findings if f.state != cr.PASS and f.rule not in skip}

# ── 1. the curated catalog meets its own rules ──────────────────────────────
registry = dict(COMPOSE_REGISTRY)
live = {s: e for s, e in registry.items() if e.get("status") != "deprecated"}
results = cr.check_slugs(root, live, registry=registry)
for slug, findings in sorted(results.items()):
    for f in cr.blocking(findings, network=False):
        fails.append(f"{slug}: required rule {f.rule} fails: {f.message}")
check(len(results) == len(live), f"check_slugs answered {len(results)} of {len(live)} slugs")

engines = cr._engine_index(root)
core_ids = sorted(i for i, (_, _, is_core) in engines.items() if is_core)
check(core_ids, "no core engine profiles found")
for eid in core_ids:
    for f in cr.check_engine(root, eid):
        if f.level == cr.REQUIRED and f.state == cr.FAIL:
            fails.append(f"engine {eid}: required rule {f.rule} fails: {f.message}")
        if f.rule == "image-anonymous" and f.state == cr.PASS and "not a pinned docker image" not in f.message:
            fails.append(f"engine {eid}: image-anonymous passed without --network")

# ── 2. exception lists: shrink only, and each entry still applies ───────────
ALLOWED = {"LINEAGE_NO_SOURCE_REF": {"vllm-lmcache"}, "FILENAME_NOT_ID": {"llama-cpp-local"}}
for name, allowed in ALLOWED.items():
    extra = set(getattr(cr, name)) - allowed
    check(not extra, f"{name} grew ({sorted(extra)}): the exception lists may only shrink")
for eid in cr.FILENAME_NOT_ID:
    prof, path, is_core = cr._engine_profile(root, eid, engines)
    check(prof is not None and is_core, f"FILENAME_NOT_ID names {eid}, which has no core profile")
    if path is not None and path.stem == eid:
        fails.append(f"{eid}: its profile is now {path.name}; remove it from FILENAME_NOT_ID")
for eid in cr.LINEAGE_NO_SOURCE_REF:
    prof, _, _ = cr._engine_profile(root, eid, engines)
    if prof and (prof.get("lineage") or {}).get("source_ref"):
        fails.append(f"{eid}: has a source_ref now; remove it from LINEAGE_NO_SOURCE_REF")

# ── 3. one passing and one failing fixture per required rule, one batch ─────
HEADER = "# Profile (at-a-glance):\n#   Model:     Fixture\n#   Status:    🐣 Incubating\n"
def compose(name, *, header=True, container="vllm-fixture", port="8000", cache=False,
            user=True, umask=True, restart=True, image=True):
    lines = [HEADER if header else "# no profile header\n", "services:\n  engine:\n"]
    if image:
        lines.append("    image: vllm/vllm-openai:v0.31.0\n")
    if container is not None:
        lines.append(f"    container_name: ${{CONTAINER_NAME:-{container}}}\n")
    lines.append(f'    ports:\n      - "${{PORT:-20299}}:{port}"\n')
    if restart:
        lines.append("    restart: ${CLUB3090_RESTART:-unless-stopped}\n")
    if cache:
        if user:
            lines.append('    user: "0:${DOCKER_GID:-1000}"\n')
        lines.append("    volumes:\n      - ${CLUB3090_ENGINE_CACHE_DIR:-~/.cache/club-3090}/vllm:/root/.cache\n")
        lines.append("    entrypoint: [\"bash\", \"-c\", \"" + ("umask 0002 && " if umask else "") + "exec vllm serve\"]\n")
    path = tmp / f"{name}.yml"
    path.write_text("".join(lines), encoding="utf-8")
    return str(path)

def entry(compose_path, engine="vllm-stable", **over):
    # One model per fixture, so same-model-defaults (arm 4) stays out of the way.
    e = {"model": f"fixture-{Path(compose_path).stem}", "engine": engine,
         "compose_path": compose_path, "status": "incubating"}
    e.update(over)
    return e

FIX = {  # slug -> (entry, the non-pass findings expected, network rules aside)
    "fx/good": (entry(compose("good", cache=True)), set()),
    # An empty container next to a vLLM image, mid-batch: if the helper run's
    # separator collapsed empty fields, the image would land in the container
    # column, read as vllm, and this would wrongly pass container-family.
    "fx/no-container": (entry(compose("nocontainer", container=None)),
                        {("container-family", "required", "fail")}),
    "fx/unknown-engine": (entry(compose("mystery", container="mystery-fixture", image=False),
                                engine="mystery-engine"),
                          {("engine-family", "required", "fail"), ("container-family", "required", "fail")}),
    "fx/wrong-family-container": (entry(compose("llama-name", container="llamacpp-fixture")),
                                  {("container-family", "required", "fail")}),
    "fx/undiscoverable-port": (entry(compose("port", port="9999")),
                               {("discoverable", "required", "fail")}),
    "fx/no-header": (entry(compose("header", header=False)), {("status-header", "required", "fail")}),
    "fx/cache-root-owned": (entry(compose("cache", cache=True, user=False, umask=False, restart=False)),
                            {("cache-ownership", "required", "fail"), ("cache-umask", "advisory", "fail"),
                             ("restart-knob", "advisory", "fail")}),
    "fx/good-again": (entry(compose("good2")), set()),
}
fx_registry = {s: e for s, (e, _) in FIX.items()}
batch = cr.check_slugs(root, fx_registry, registry=fx_registry)
for slug, (_, want) in FIX.items():
    got = nonpass(batch[slug])
    check(got == want, f"{slug}: expected {sorted(want)}, got {sorted(got)}")
    for f in batch[slug]:
        if f.rule in ("weights-download", "image-anonymous") and f.state == cr.PASS and "not a pinned" not in f.message:
            fails.append(f"{slug}: {f.rule} passed without --network")
# The helper run itself, column by column: empty fields mid-row must stay empty.
# (No finding reads the image column, so the fixtures above cannot see a shift there.)
rows = [("vllm-stable", "", "vllm/vllm-openai:v0.31.0", ["8000"]),
        ("mystery-engine", "", "", []),
        ("vllm-stable", "llamacpp-x", "", ["9999"])]
got = [(f["id"], f["container"], f["image"], f["discovered"]) for f in cr._tooling_facts_many(root, rows)]
want = [("vllm", "unknown", "vllm", "8000"), ("unknown", "unknown", "unknown", ""),
        ("vllm", "llamacpp", "unknown", "")]
check(got == want, f"helper facts misaligned: {got} != {want}")
check(not cr.blocking(batch["fx/good"], network=False), "fx/good blocks")
check(not any(f.level == cr.ADVISORY for f in cr.blocking(batch["fx/cache-root-owned"], network=True)),
      "an advisory finding blocked")
one = cr.check_slug(root, "fx/wrong-family-container", registry=fx_registry)
check(nonpass(one) == FIX["fx/wrong-family-container"][1], "check_slug and check_slugs disagree")
try:
    cr.check_slug(root, "fx/not-there", registry=fx_registry)
    fails.append("an unknown slug did not raise LookupError")
except LookupError:
    pass

# ── 4. same-model-defaults ──────────────────────────────────────────────────
def smd(mine, others):
    reg = {f"o/{i}": {"model": "m", "engine": eng, "chat_template": t, "status": st}
           for i, (eng, t, st) in enumerate(others)}
    e = {"model": "m", "engine": "vllm-stable", "chat_template": mine}
    (f,) = cr._same_model_defaults("me", e, reg)
    check(f.level == cr.ADVISORY, "same-model-defaults is not advisory")
    return f.state, f.message
check(smd("a", [])[0] == cr.PASS, "no other engine should pass")
check(smd("a", [("sglang-stable", "a", "production")])[0] == cr.PASS, "same template should pass")
check(smd("b", [("sglang-stable", "a", "production")])[0] == cr.FAIL, "a different template should be flagged")
st, msg = smd(None, [("sglang-stable", "a", "production")])
check(st == cr.FAIL and "doesn't say" in msg, "an unrecorded template must not read as the same")
check(smd("b", [("sglang-stable", "a", "deprecated")])[0] == cr.PASS, "a deprecated slug set expectations")
check(smd("b", [("vllm-stable", "a", "production")])[0] == cr.PASS, "a same-engine slug set expectations")

# ── 5. network rules, three states, with fakes ──────────────────────────────
def boom(*_):
    raise AssertionError("a network fake was called without --network")
from scripts.lib.profiles.compat import load_profiles  # noqa: E402
models = load_profiles(root / "scripts/lib/profiles").models

def weights_entry(pred):
    for mid, m in sorted(models.items()):
        for var, w in sorted(m.weights.items()):
            if isinstance(w, dict) and w.get("hf_repo") and pred(w):
                return {"model": mid, "weights_variant": var}, w
    raise SystemExit("no weights variant fits a network-rule fixture")

def siblings_for(w):
    names = [g.replace("*", "x").replace("?", "x") for g in (w.get("files") or [])]
    if w.get("verify_glob"):
        names.append(str(w["verify_glob"]).replace("*", "x").replace("?", "x"))
    return [{"rfilename": n} for n in names]

def weights(e, hf):
    return [f for f in cr._weights_rules(root, e, True, hf) if f.rule == "weights-download"]

whole, w_whole = weights_entry(lambda w: not w.get("files") and w.get("verify_glob"))
globbed, w_globbed = weights_entry(lambda w: w.get("files") and w.get("verify_glob"))
cases = {  # label: (entry, hf fake, expected state)
    "whole repo, model files present": (whole, lambda r, v: (200, {"siblings": siblings_for(w_whole)}), cr.PASS),
    "whole repo, no model files": (whole, lambda r, v: (200, {"siblings": [{"rfilename": "README.md"}]}), cr.FAIL),
    "files: globs match": (globbed, lambda r, v: (200, {"siblings": siblings_for(w_globbed)}), cr.PASS),
    "files: glob matches nothing": (globbed, lambda r, v: (200, {"siblings": [{"rfilename": "README.md"}]}), cr.FAIL),
    "404": (whole, lambda r, v: (404, None), cr.FAIL),
    "401": (whole, lambda r, v: (401, None), cr.UNCHECKED),
    "unreachable": (whole, lambda r, v: (None, None), cr.UNCHECKED),
}
for label, (e, hf, want) in cases.items():
    got = {f.state for f in weights(e, hf)}
    check(got == {want}, f"weights-download {label} ({e['model']}/{e['weights_variant']}): "
                         f"expected {want}, got {sorted(got)}")
gated = weights(whole, lambda r, v: (200, {"siblings": siblings_for(w_whole), "gated": "manual"}))
check(gated and all(f.state == cr.PASS and "gated" in f.message for f in gated),
      "a gated repo should pass and say so")
check(not weights({"model": "no-such-model", "weights_variant": "x"}, boom)[0].state == cr.PASS,
      "a model with no profile passed weights-download")
check({f.state for f in cr._weights_rules(root, whole, False, boom)} == {cr.UNCHECKED},
      "weights-download without --network should be unchecked")

for status, want in (("ok", cr.PASS), ("denied", cr.FAIL), ("error", cr.UNCHECKED)):
    f = cr._image_rule("ghcr.io/x/y:v1", "docker_image", True, lambda s, st=status: (st, "detail"))
    check(f.state == want, f"image-anonymous {status}: expected {want}, got {f.state}")
check(cr._image_rule("ghcr.io/x/y:v1", "docker_image", False, boom).state == cr.UNCHECKED,
      "image-anonymous without --network should be unchecked")
check(cr._image_rule("", "pip", True, boom).state == cr.PASS, "a non-image install should not need a pull")

unchecked = [cr.Finding("weights-download", cr.REQUIRED, cr.UNCHECKED, "", "")]
check(not cr.blocking(unchecked, network=False), "unchecked blocked without --network")
check(cr.blocking(unchecked, network=True), "unchecked passed with --network")

# ── 6a. CLI exit logic, with the checks stubbed ─────────────────────────────
import contextlib, io  # noqa: E402
def run_main(argv, findings):
    saved = cr.check_slug
    cr.check_slug = lambda *a, **k: findings
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return cr.main(argv)
    finally:
        cr.check_slug = saved
req_fail = [cr.Finding("status-header", cr.REQUIRED, cr.FAIL, "", "")]
adv_fail = [cr.Finding("restart-knob", cr.ADVISORY, cr.FAIL, "", "")]
for argv, findings, want in (
    (["--slug", "x"], req_fail, 0),
    (["--slug", "x", "--strict"], req_fail, 1),
    (["--slug", "x", "--strict"], adv_fail, 0),
    (["--slug", "x", "--strict"], unchecked, 0),
    (["--slug", "x", "--strict", "--network"], unchecked, 1),
):
    got = run_main(argv, findings)
    check(got == want, f"main({argv}) on {findings[0].level}/{findings[0].state}: exit {got}, want {want}")

if fails:
    for f in fails:
        print(f"  FAIL {f}")
    sys.exit(1)
print(f"  ok   {len(live)} live slugs and {len(core_ids)} engines meet the required rules; "
      f"{len(FIX)} fixtures, network fakes and exit logic behave")
PY

fail=0
RULES="$ROOT/scripts/lib/profiles/catalog_rules.py"

# ── 6b. the real CLI ─────────────────────────────────────────────────────────
python3 "$RULES" --slug vllm/minimal --strict >"$TMP/out" 2>&1; code=$?
if [[ $code -ne 0 ]] || ! command grep -q "^\[catalog-rules\] vllm/minimal:" "$TMP/out"; then
  echo "  FAIL --slug vllm/minimal --strict: exit $code"; sed 's/^/    /' "$TMP/out"; fail=1
else echo "  ok   a curated slug passes --strict"; fi

python3 "$RULES" --slug no-such/slug >"$TMP/out" 2>&1; code=$?
[[ $code -eq 2 ]] && echo "  ok   an unknown slug exits 2" || { echo "  FAIL unknown slug: exit $code (want 2)"; fail=1; }

if python3 "$RULES" --engine vllm-stable --json 2>/dev/null \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); assert {"rule","level","state"} <= set(d[0])'; then
  echo "  ok   --engine --json prints findings"
else echo "  FAIL --engine vllm-stable --json did not print findings"; fail=1; fi

# ── 6c. no PyYAML: skip, don't crash (catalog.sh register runs on any python3) ─
mkdir -p "$TMP/noyaml/yaml"
echo 'raise ImportError("PyYAML hidden by test-catalog-rules")' > "$TMP/noyaml/yaml/__init__.py"
PYTHONPATH="$TMP/noyaml" python3 "$RULES" --slug vllm/minimal >"$TMP/out" 2>&1; code=$?
if [[ $code -eq 0 ]] && command grep -q "skipped: needs PyYAML" "$TMP/out"; then
  echo "  ok   without PyYAML it skips (exit 0)"
else echo "  FAIL without PyYAML: exit $code"; sed 's/^/    /' "$TMP/out"; fail=1; fi
PYTHONPATH="$TMP/noyaml" python3 "$RULES" --slug vllm/minimal --strict >/dev/null 2>&1; code=$?
[[ $code -eq 2 ]] && echo "  ok   without PyYAML --strict exits 2" \
  || { echo "  FAIL without PyYAML --strict: exit $code (want 2)"; fail=1; }

if [[ $fail -ne 0 ]]; then echo "FAIL: $NAME" >&2; exit 1; fi
echo "PASS: $NAME"
