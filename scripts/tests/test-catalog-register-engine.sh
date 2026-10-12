#!/usr/bin/env bash
# Guard: `catalog.sh register-engine` registers an engine on its own in the LOCAL
# layer (scripts/lib/profiles-local/engines.d/<id>.yml), before any slug uses it.
#
#   1. a full registration loads, normalises the type, and passes the catalog
#      rules' required engine checks offline
#   2. it refuses what would break a launch or a load, writing nothing: a curated
#      id, an existing local id, an unknown type, a non-concrete image, an
#      image_env the launchers don't allowlist, an unknown KV format, a lineage
#      the loader rejects
#   3. a slug registered on it reuses the profile untouched, and with the
#      capabilities stated, fits() raises no C4 / C10 / C14 for it; without them
#      it does (the negative control)
#   4. unregister keeps an engine registered on purpose, and still removes one
#      `register` wrote as a side effect, found by the entry's engine, not the
#      slug prefix
#   5. unregister-engine removes a local engine no slug runs on, and refuses a
#      curated one, an unknown one, and one a slug still runs on (naming the
#      `unregister` commands); --dry-run removes nothing
set -uo pipefail
export CLUB3090_CONFIG_DIR=/nonexistent/club-3090-test-config   # tests never read your real settings (#1466)
export PYTHONUTF8="${PYTHONUTF8:-1}"
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
rc=0
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
ok()   { echo "  ok   $*"; }
fail() { echo "  FAIL $*"; rc=1; }

R="$TMP/root"
mkdir -p "$R/scripts" "$R/tools"
cp -r scripts/lib "$R/scripts/"
# The image_env allowlists live in each launcher's export_variant_engine_pin().
# Stub launchers holding only that function, extracted from the real ones: the
# allowlist under test is the shipped one, and nothing here can boot a model.
python3 - "$R" <<'PY'
import re, sys
from pathlib import Path
for name in ("switch", "launch"):
    text = Path(f"scripts/{name}.sh").read_text(encoding="utf-8")
    fn = re.search(r"export_variant_engine_pin\(\)\s*\{.*?\n\}", text, re.S).group(0)
    Path(sys.argv[1], "scripts", f"{name}.sh").write_text(
        "#!/usr/bin/env bash\n# test stub: the image allowlist only\n" + fn + "\n", encoding="utf-8")
PY
rm -rf "$R/scripts/lib/profiles-local/"{models.d,composes,engines.d,extends.d,registry.local.json}
cp -r tools/tui-core "$R/tools/"
ENG="$R/scripts/lib/profiles-local/engines.d"
DIGEST="ghcr.io/maintainer/vllm-myfork@sha256:$(printf 'a%.0s' {1..64})"
REF="0123456789abcdef0123456789abcdef01234567"
reg_engine() { bash scripts/catalog.sh register-engine --root "$R" -y "$@" 2>&1; }
none_written() { [[ ! -e "$ENG/$1.yml" ]]; }

# 0. usage lists it
bash scripts/catalog.sh --help 2>&1 | command grep -q "register-engine --id" \
  && ok "catalog.sh --help lists register-engine" || fail "--help does not list register-engine"

# 1. a full registration
out="$(reg_engine --id vllm-myfork --type vllm --image "$DIGEST" --display-name "vLLM my fork" \
       --min-sm 8.6 --kv-formats fp8_e5m2 --families qwen --weight-formats safetensors \
       --relation fork --base vllm --base-version 0.31.0 \
       --source https://github.com/maintainer/vllm-myfork --source-ref "$REF")"; code=$?
if [[ $code -eq 0 && "$out" == *"ENGINE_OK vllm-myfork"* && -f "$ENG/vllm-myfork.yml" ]]; then
  ok "a full registration writes engines.d/vllm-myfork.yml"
else fail "full registration (rc=$code): $out"; fi
res="$(cd "$R" && python3 -c "
from scripts.lib.profiles.compat import load_profiles
e = load_profiles().engines['vllm-myfork']
print(e.type, list(e.supported_kv_formats), e.install.get('method'), e.lineage['relation'], e.display_name)
" 2>&1 | command grep -v '^\[compat\]')"
[[ "$res" == "vllm ['fp8_e5m2'] docker_image fork vLLM my fork" ]] && ok "it loads with the stated fields" \
  || fail "loaded engine: $res"
python3 scripts/lib/profiles/catalog_rules.py --engine vllm-myfork --strict --root "$R" >/dev/null 2>&1 \
  && ok "catalog_rules --engine --strict passes offline (lineage, digest pin, family)" \
  || fail "catalog_rules --engine --strict failed: $(python3 scripts/lib/profiles/catalog_rules.py --engine vllm-myfork --root "$R" 2>&1 | tail -5)"

# 2. refusals, each writing nothing
refuse() {   # <label> <expected text> <id> <args...>
  local label="$1" want="$2" id="$3"; shift 3
  local o c; o="$(reg_engine --id "$id" "$@")"; c=$?
  if [[ $c -eq 3 && "$o" == *"$want"* ]] && { [[ "$id" == vllm-myfork || "$id" == vllm-stable ]] || none_written "$id"; }; then
    ok "refused: $label"
  else fail "$label (rc=$c): $o"; fi
}
refuse "a curated id"             "register a slug on it instead" vllm-stable --type vllm --image "$DIGEST"
refuse "an existing local id"     "already registered"            vllm-myfork --type vllm --image "$DIGEST"
refuse "an unknown type"          "one of vllm, llama.cpp"        x-one --type cuda --image "$DIGEST"
refuse "a \${…} image"            "concrete image reference"      x-two --type vllm --image '${VLLM_IMAGE:-vllm/vllm-openai:v1}'
refuse "an image_env the launchers don't allowlist" "unexpected engine pin export" x-three --type vllm \
       --image "$DIGEST" --image-env MYFORK_IMAGE
refuse "an unknown KV format"     "not in any hardware profile"   x-four --type vllm --image "$DIGEST" --kv-formats fp9_magic
refuse "a lineage the loader rejects" "would not load"            x-five --type vllm --image "$DIGEST" --relation cousin

# 2b. aliases fold; an allowlisted image_env is accepted; --dry-run writes nothing
out="$(reg_engine --id x-six --type llamacpp --image ghcr.io/o/llama@sha256:00 --image-env VLLM_IMAGE --dry-run)"; code=$?
if [[ $code -eq 0 && "$out" == *"| type: llama.cpp"* && "$out" == *"| image_env: VLLM_IMAGE"* ]] && none_written x-six; then
  ok "--type llamacpp is written as llama.cpp; an allowlisted image_env passes; --dry-run writes nothing"
else fail "alias / dry-run (rc=$code): $out"; fi
out="$(reg_engine --id vllm-bare --type vllm --image "$DIGEST" --min-sm 8.6)"; code=$?
if [[ $code -eq 0 && "$out" == *"export_pr.py needs relation"* && "$out" == *"update-engine --id vllm-bare"* && "$out" == *"fail C10"* ]]; then
  ok "without lineage or capabilities it registers and says what that costs"
else fail "bare registration (rc=$code): $out"; fi

# 3. slugs on it: the profile is reused, and capabilities decide fits()
mkdir -p "$R/w/m"
echo '{"model_type": "qwen3", "hidden_size": 2048, "num_hidden_layers": 24, "num_attention_heads": 16, "num_key_value_heads": 4, "max_position_embeddings": 32768}' > "$R/w/m/config.json"
compose() {   # <file> <engine id> <served name>
  cat > "$1" <<YML
# Profile (at-a-glance):
#   Model:     test
#   Status:    🐣 Incubating
#   Caveats:   test fixture
services:
  engine:
    image: $DIGEST
    container_name: $2-test
    ports:
      - "\${PORT:-20201}:8000"
    command: >
      vllm serve /models/m --served-model-name $3 --kv-cache-dtype fp8_e5m2
      --tensor-parallel-size 1 --max-model-len 32768
YML
}
before="$(sha256sum "$ENG/vllm-myfork.yml")"
compose "$R/fork.yml" vllm-myfork fork-model
out="$(bash scripts/catalog.sh register --compose "$R/fork.yml" --engine vllm-myfork --weights "$R/w/m" --root "$R" -y 2>&1)"; code=$?
[[ $code -eq 0 && "$(sha256sum "$ENG/vllm-myfork.yml")" == "$before" ]] \
  && ok "a slug registered on it leaves the engine profile untouched" || fail "slug on the engine (rc=$code): $(echo "$out" | tail -4)"
compose "$R/bare.yml" vllm-bare bare-model
bash scripts/catalog.sh register --compose "$R/bare.yml" --engine vllm-bare --weights "$R/w/m" --root "$R" -y >/dev/null 2>&1
res="$(cd "$R" && python3 -c "
from scripts.lib.profiles.compat import load_profiles
from scripts.lib.profiles.compose_registry import get_registry
from scripts.lib.profiles.diagnose_profile_cli import fits_for_entry
p = load_profiles(); reg = get_registry()
hw = [p.hardware['rtx-3090']]
for slug in ('vllm-myfork/fork-model', 'vllm-bare/bare-model'):
    r = fits_for_entry(reg[slug], p, hw, False, project_vram=False)
    bad = sorted({x.split(':')[0] for x in r.reasons if x.split(':')[0] in ('C4', 'C10', 'C14')})
    print(slug, bad)
" 2>&1 | command grep -v '^\[compat\]')"
if [[ "$res" == *"vllm-myfork/fork-model []"* && "$res" == *"vllm-bare/bare-model ['C10', 'C14', 'C4']"* ]]; then
  ok "with capabilities stated fits() raises no C4/C10/C14; without, all three (negative control)"
else fail "fits(): $res"; fi

# 4. unregister: keep the engine registered on purpose; remove a side-effect one
out="$(bash scripts/catalog.sh unregister --slug vllm-myfork/fork-model --root "$R" -y 2>&1)"
if [[ -f "$ENG/vllm-myfork.yml" && "$out" == *"kept engines.d/vllm-myfork.yml"* ]]; then
  ok "unregistering its last slug keeps an engine registered on its own"
else fail "register-engine profile after unregister: $(echo "$out" | tail -3)"; fi
cat > "$R/side.yml" <<YML
# Profile (at-a-glance):
#   Model:     test
#   Status:    🐣 Incubating
#   Caveats:   test fixture
services:
  engine:
    image: ghcr.io/someone/side-engine:v1
    container_name: vllm-side-test
    ports:
      - "\${PORT:-20202}:8000"
    command: >
      vllm serve /models/m --served-model-name side-model --kv-cache-dtype fp8_e5m2
YML
bash scripts/catalog.sh register --compose "$R/side.yml" --engine side-engine --engine-type vllm \
     --weights "$R/w/m" --slug custom/side-model --root "$R" -y >/dev/null 2>&1
[[ -f "$ENG/side-engine.yml" ]] || fail "fixture: register wrote no side-effect engine profile"
bash scripts/catalog.sh unregister --slug custom/side-model --root "$R" -y >/dev/null 2>&1
[[ ! -e "$ENG/side-engine.yml" ]] \
  && ok "a side-effect profile is removed with its last slug, though the slug prefix (custom/) isn't the engine" \
  || fail "side-effect engine profile leaked after unregister"

# 5. unregister-engine
unreg_engine() { bash scripts/catalog.sh unregister-engine --root "$R" -y "$@" 2>&1; }
out="$(unreg_engine --id vllm-stable)"; code=$?
[[ $code -eq 3 && "$out" == *"curated engine"* ]] && ok "unregister-engine refuses a curated engine" \
  || fail "curated engine (rc=$code): $out"
out="$(unreg_engine --id no-such-engine)"; code=$?
[[ $code -eq 3 && "$out" == *"no engine 'no-such-engine'"* ]] && ok "unregister-engine refuses an unknown id" \
  || fail "unknown id (rc=$code): $out"
out="$(unreg_engine --id vllm-bare)"; code=$?
if [[ $code -eq 3 && "$out" == *"catalog.sh unregister --slug vllm-bare/bare-model"* && -f "$ENG/vllm-bare.yml" ]]; then
  ok "unregister-engine refuses while a slug runs on it, naming the command for that slug"
else fail "engine in use (rc=$code): $out"; fi
out="$(unreg_engine --id vllm-myfork --dry-run)"; code=$?
[[ $code -eq 0 && -f "$ENG/vllm-myfork.yml" ]] && ok "unregister-engine --dry-run removes nothing" \
  || fail "dry-run (rc=$code): $out"
out="$(unreg_engine --id vllm-myfork)"; code=$?
res="$(cd "$R" && python3 -c "
from scripts.lib.profiles.compat import load_profiles
print('vllm-myfork' in load_profiles().engines)" 2>&1 | command grep -v '^\[compat\]')"
if [[ $code -eq 0 && "$out" == *"ENGINE_REMOVED vllm-myfork"* && ! -e "$ENG/vllm-myfork.yml" && "$res" == "False" ]]; then
  ok "unregister-engine removes an engine no slug runs on; the profiles still load"
else fail "removal (rc=$code): $out / $res"; fi
bash scripts/catalog.sh unregister --slug vllm-bare/bare-model --root "$R" -y >/dev/null 2>&1
out="$(unreg_engine --id vllm-bare)"; code=$?
[[ $code -eq 0 && ! -e "$ENG/vllm-bare.yml" ]] && ok "once its last slug is unregistered, the engine goes too" \
  || fail "after its slug (rc=$code): $out"

[[ $rc -eq 0 ]] && echo "PASS: catalog.sh register-engine" || echo "FAIL: test-catalog-register-engine"
exit $rc
