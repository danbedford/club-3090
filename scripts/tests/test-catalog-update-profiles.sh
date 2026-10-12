#!/usr/bin/env bash
# Guard: `catalog.sh update-weights` and `update-engine` edit LOCAL profiles in
# place — a weights variant in models.d/ or extends.d/, an engine in engines.d/ —
# and nothing curated.
#
#   1. update-weights sets what export_pr.py needs on a variant attached to a
#      curated model (files:, hf_repo), keeps the file's header, and the profiles
#      still load; the same on a local model's variant; `KEY=` removes a key
#   2. it refuses a curated variant, a curated model with nothing local, an
#      unknown key, and a non-number size; --dry-run writes nothing
#   3. update-engine widens and narrows lists, adds lineage, repins the image as
#      one unquoted spec token, and the result passes the catalog rules
#   4. it refuses a curated engine, an unknown KV format, an image_env the
#      launchers don't allowlist, --add on a scalar, and a lineage the loader
#      rejects; --dry-run writes nothing
set -uo pipefail
export CLUB3090_CONFIG_DIR=/nonexistent/club-3090-test-config   # tests never read your real settings (#1466)
export PYTHONUTF8="${PYTHONUTF8:-1}"
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
rc=0
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
ok()   { echo "  ok   $*"; }
fail() { echo "  FAIL $*"; rc=1; }

R="$TMP/root"
mkdir -p "$R/scripts" "$R/tools" "$R/models"
cp -r scripts/lib "$R/scripts/"
rm -rf "$R/scripts/lib/profiles-local/"{models.d,composes,engines.d,extends.d,registry.local.json}
cp -r tools/tui-core "$R/tools/"
cp scripts/detect_nvlink.sh "$R/scripts/"
cp -r models/qwen3.6-27b "$R/models/"
# The image_env allowlists live in each launcher's export_variant_engine_pin().
# Stubs holding only that function, extracted from the real ones (nothing here
# can boot a model).
python3 - "$R" <<'PY'
import re, sys
from pathlib import Path
for name in ("switch", "launch"):
    text = Path(f"scripts/{name}.sh").read_text(encoding="utf-8")
    fn = re.search(r"export_variant_engine_pin\(\)\s*\{.*?\n\}", text, re.S).group(0)
    Path(sys.argv[1], "scripts", f"{name}.sh").write_text(
        "#!/usr/bin/env bash\n# test stub: the image allowlist only\n" + fn + "\n", encoding="utf-8")
PY
L="$R/scripts/lib/profiles-local"
cat_() { bash scripts/catalog.sh "$@" --root "$R" -y 2>&1; }
loads() { (cd "$R" && python3 -c "from scripts.lib.profiles.compat import load_profiles; load_profiles()" >/dev/null 2>&1); }
CM=qwen3.6-27b
DIGEST="ghcr.io/maintainer/vllm-myfork@sha256:$(printf 'a%.0s' {1..64})"
DIGEST2="ghcr.io/maintainer/vllm-myfork@sha256:$(printf 'b%.0s' {1..64})"

# fixtures: a variant attached to a curated model, and a local model
mkdir -p "$R/w/own" "$R/w/byo"
echo '{"hidden_size": 64}' > "$R/w/own/config.json"
echo '{"model_type": "qwen3", "hidden_size": 2048, "num_hidden_layers": 24, "num_attention_heads": 16, "num_key_value_heads": 4, "max_position_embeddings": 32768}' > "$R/w/byo/config.json"
cp "models/$CM/vllm/compose/dual/autoround-int4/fp8-mtp.yml" "$R/sib.yml"
cat_ register --compose "$R/sib.yml" --engine vllm-stable --model "$CM" --weights-variant own \
     --weights "$R/w/own" >/dev/null
cat > "$R/byo.yml" <<'YML'
# Profile (at-a-glance):
#   Model:     test
#   Status:    🐣 Incubating
#   Caveats:   test fixture
services:
  engine:
    image: vllm/vllm-openai:v0.31.0
    container_name: vllm-byo-test
    ports:
      - "${PORT:-20203}:8000"
    command: >
      vllm serve /models/byo --served-model-name byo-model --kv-cache-dtype fp8_e5m2
YML
cat_ register --compose "$R/byo.yml" --engine vllm-stable --weights "$R/w/byo" >/dev/null
[[ -f "$L/extends.d/$CM.yml" && -f "$L/models.d/byo-model.yml" ]] || fail "fixtures were not registered"

# 1. update-weights
out="$(cat_ update-weights --model "$CM" --variant own --set "files=*.safetensors,*.json" --set hf_repo=org/own)"; code=$?
res="$(cd "$R" && python3 -c "
import yaml
d = yaml.safe_load(open('scripts/lib/profiles-local/extends.d/$CM.yml'))['weights']['own']
print(d.get('files'), d.get('hf_repo'))")"
if [[ $code -eq 0 && "$out" == *"UPDATE_OK"* && "$res" == "['*.safetensors', '*.json'] org/own" ]] \
   && head -1 "$L/extends.d/$CM.yml" | command grep -q '^# Attaches local slugs' && loads; then
  ok "update-weights sets files: and hf_repo on an attached variant; header kept; profiles load"
else fail "attached variant (rc=$code): $out / $res"; fi
out="$(cat_ update-weights --model byo-model --variant local --set hf_repo=org/byo --set size_gb=4.5)"; code=$?
res="$(cd "$R" && python3 -c "
import yaml
d = yaml.safe_load(open('scripts/lib/profiles-local/models.d/byo-model.yml'))['weights']['local']
print(d.get('hf_repo'), d.get('size_gb'))")"
[[ $code -eq 0 && "$res" == "org/byo 4.5" ]] && loads && ok "update-weights edits a local model's variant" \
  || fail "local model variant (rc=$code): $out / $res"
cat_ update-weights --model byo-model --variant local --set hf_repo= >/dev/null
(cd "$R" && python3 -c "
import yaml, sys
d = yaml.safe_load(open('scripts/lib/profiles-local/models.d/byo-model.yml'))['weights']['local']
sys.exit('hf_repo' in d)") && ok "KEY= removes the key" || fail "hf_repo= did not remove the key"

# 2. update-weights refusals; --dry-run
refuse() {   # <label> <expected text> <args...>
  local label="$1" want="$2"; shift 2
  local o c; o="$(cat_ "$@")"; c=$?
  [[ $c -eq 3 && "$o" == *"$want"* ]] && ok "refused: $label" || fail "$label (rc=$c): $o"
}
refuse "a curated variant"           "curated variant"          update-weights --model "$CM" --variant autoround-int4 --set hf_repo=x/y
refuse "a curated model with nothing local" "curated model"     update-weights --model gemma-4-12b --variant x --set hf_repo=x/y
refuse "an unknown key"              "isn't editable"           update-weights --model "$CM" --variant own --set hf_repoz=x
refuse "a non-number size"           "expected a number"        update-weights --model "$CM" --variant own --set size_gb=big
before="$(sha256sum "$L/extends.d/$CM.yml")"
out="$(cat_ update-weights --model "$CM" --variant own --set revision=main --dry-run)"; code=$?
[[ $code -eq 0 && "$out" == *"weights.own.revision"* && "$(sha256sum "$L/extends.d/$CM.yml")" == "$before" ]] \
  && ok "update-weights --dry-run shows the change and writes nothing" || fail "dry-run (rc=$code): $out"

# 3. update-engine
cat_ register-engine --id vllm-myfork --type vllm --image "$DIGEST" --min-sm 8.6 >/dev/null
out="$(cat_ update-engine --id vllm-myfork --add kv_formats=fp8_e5m2,bf16 --add families=qwen3-next-hybrid \
       --add weight_formats=autoround --set relation=fork --set base=vllm --set base_version=0.31.0 \
       --set source=https://github.com/maintainer/vllm-myfork --set source_ref=0123456789abcdef0123456789abcdef01234567 \
       --set image="$DIGEST2")"; code=$?
res="$(cd "$R" && python3 -c "
from scripts.lib.profiles.compat import load_profiles
e = load_profiles().engines['vllm-myfork']
print(list(e.supported_kv_formats), list(e.supported_model_families), e.lineage['relation'], e.lineage['base_version'], e.install['spec'][-8:])
" 2>&1 | command grep -v '^\[compat\]')"
if [[ $code -eq 0 && "$res" == "['fp8_e5m2', 'bf16'] ['qwen3-next-hybrid'] fork 0.31.0 bbbbbbbb" ]] \
   && [[ "$(command grep -c '^ *spec:' "$L/engines.d/vllm-myfork.yml")" == 1 ]] \
   && python3 scripts/lib/profiles/catalog_rules.py --engine vllm-myfork --strict --root "$R" >/dev/null 2>&1; then
  ok "update-engine widens lists, adds lineage, repins the image; catalog rules pass"
else fail "update-engine (rc=$code): $out / $res"; fi
cat_ update-engine --id vllm-myfork --remove kv_formats=bf16 >/dev/null
res="$(cd "$R" && python3 -c "
from scripts.lib.profiles.compat import load_profiles
print(list(load_profiles().engines['vllm-myfork'].supported_kv_formats))" 2>&1 | command grep -v '^\[compat\]')"
[[ "$res" == "['fp8_e5m2']" ]] && ok "--remove narrows a list" || fail "--remove: $res"

# 4. update-engine refusals; --dry-run
refuse "a curated engine"            "curated engine"           update-engine --id vllm-stable --add kv_formats=bf16
refuse "an unknown KV format"        "hardware profile"         update-engine --id vllm-myfork --add kv_formats=fp9_magic
refuse "an image_env not allowlisted" "unexpected engine pin export" update-engine --id vllm-myfork --set image_env=MYFORK_IMAGE
refuse "--add on a scalar"           "works on lists"           update-engine --id vllm-myfork --add display_name=x
refuse "a lineage the loader rejects" "would not load"          update-engine --id vllm-myfork --set relation=cousin
before="$(sha256sum "$L/engines.d/vllm-myfork.yml")"
out="$(cat_ update-engine --id vllm-myfork --add kv_formats=bf16 --dry-run)"; code=$?
[[ $code -eq 0 && "$(sha256sum "$L/engines.d/vllm-myfork.yml")" == "$before" ]] \
  && ok "update-engine --dry-run writes nothing" || fail "engine dry-run (rc=$code): $out"

[[ $rc -eq 0 ]] && echo "PASS: catalog.sh update-weights / update-engine" || echo "FAIL: test-catalog-update-profiles"
exit $rc
