#!/usr/bin/env bash
# Guard: `catalog.sh prune` removes exactly what `catalog.sh list` reports as
# unused in the LOCAL layer, and nothing else.
#
#   1. --dry-run prints the plan and changes nothing; without a terminal and
#      without -y it refuses and changes nothing (these files are gitignored, so
#      git can't bring them back)
#   2. -y removes: an attached variant no slug uses (its extension and slug
#      survive), an orphaned side-effect engine, a model with no slugs, composes
#      no entry uses; and keeps: an engine registered on purpose, a file carried
#      beside a compose, a shadowed entry; the catalog still loads; a second run
#      finds nothing
#   3. an extension no slug is attached to goes as a whole file
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
L="$R/scripts/lib/profiles-local"
cat_() { bash scripts/catalog.sh "$@" --root "$R" 2>&1; }
loads() { (cd "$R" && python3 -c "
from scripts.lib.profiles.compose_registry import get_registry; get_registry()
from scripts.lib.profiles.compat import load_profiles; load_profiles()" >/dev/null 2>&1); }
layer_sum() { (cd "$L" && find . -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum); }
CM=qwen3.6-27b

# fixtures
mkdir -p "$R/w/own" "$R/w/byo"
echo '{"hidden_size": 64}' > "$R/w/own/config.json"
echo '{"model_type": "qwen3", "hidden_size": 2048, "num_hidden_layers": 24, "num_attention_heads": 16, "num_key_value_heads": 4, "max_position_embeddings": 32768}' > "$R/w/byo/config.json"
cp "models/$CM/vllm/compose/dual/autoround-int4/fp8-mtp.yml" "$R/sib.yml"
ATT="vllm/$CM-dual-own"
cat_ register --compose "$R/sib.yml" --engine vllm-stable --model "$CM" --weights-variant own --weights "$R/w/own" -y >/dev/null
cat_ update --slug "$ATT" --set weights_variant=autoround-int4 -y >/dev/null      # leaves variant `own` unused
cat > "$R/byo.yml" <<'YML'
# Profile (at-a-glance):
#   Model:     test
#   Status:    🐣 Incubating
#   Caveats:   test fixture
services:
  engine:
    image: ghcr.io/someone/side-engine:v1
    container_name: vllm-byo-test
    ports:
      - "${PORT:-20205}:8000"
    command: >
      vllm serve /models/byo --served-model-name byo-model --kv-cache-dtype fp8_e5m2
YML
cat_ register --compose "$R/byo.yml" --engine side-engine --engine-type vllm --weights "$R/w/byo" -y >/dev/null
cat_ register-engine --id vllm-kept --type vllm --image "ghcr.io/o/e@sha256:$(printf 'd%.0s' {1..64})" --min-sm 8.6 -y >/dev/null
# Orphan the byo model the way an older checkout could: its entry gone, its model
# profile, compose and side-effect engine left behind.
python3 - "$L/registry.local.json" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p))
del d["side-engine/byo-model"]
d["vllm/dual"] = dict(d[f"vllm/qwen3.6-27b-dual-own"])     # a shadowed entry
json.dump(d, open(p, "w"), indent=2)
PY
mkdir -p "$L/composes/$CM/vllm/patches/x" && echo "kind: carried" > "$L/composes/$CM/vllm/patches/x/config.yml"
mkdir -p "$L/composes/stray/y/compose/single/z" && echo "services: {}" > "$L/composes/stray/y/compose/single/z/base.yml"
for f in "$L/engines.d/side-engine.yml" "$L/models.d/byo-model.yml" "$L/engines.d/vllm-kept.yml" \
         "$L/composes/byo-model/side-engine/compose/single/safetensors/base.yml"; do
  [[ -f "$f" ]] || fail "fixture missing: ${f#$R/}"
done

# 1. --dry-run, and no terminal without -y
before="$(layer_sum)"
out="$(cat_ prune --dry-run)"; code=$?
if [[ $code -eq 0 && "$out" == *"remove variant 'own'"* && "$out" == *"side-engine.yml"* \
      && "$out" == *"needs a decision"* && "$(layer_sum)" == "$before" ]]; then
  ok "--dry-run prints the plan (and what needs a decision) and changes nothing"
else fail "dry-run (rc=$code): $out"; fi
out="$(cat_ prune < /dev/null)"; code=$?
[[ $code -eq 3 && "$out" == *"pass -y"* && "$(layer_sum)" == "$before" ]] \
  && ok "without a terminal and without -y it refuses and changes nothing" || fail "no-tty (rc=$code): $out"

# 2. prune -y
out="$(cat_ prune -y)"; code=$?
gone=( "$L/engines.d/side-engine.yml" "$L/models.d/byo-model.yml"
       "$L/composes/byo-model/side-engine/compose/single/safetensors/base.yml"
       "$L/composes/stray/y/compose/single/z/base.yml" )
kept=( "$L/engines.d/vllm-kept.yml" "$L/composes/$CM/vllm/patches/x/config.yml" "$L/extends.d/$CM.yml" )
bad=""
for f in "${gone[@]}"; do [[ -e "$f" ]] && bad="$bad still-there:${f#$L/}"; done
for f in "${kept[@]}"; do [[ -e "$f" ]] || bad="$bad removed:${f#$L/}"; done
variants="$(python3 -c "import yaml; print(sorted((yaml.safe_load(open('$L/extends.d/$CM.yml')) or {}).get('weights') or {}))")"
shadow="$(python3 -c "import json; print('vllm/dual' in json.load(open('$L/registry.local.json')))")"
if [[ $code -eq 0 && "$out" == *"PRUNE_OK"* && -z "$bad" && "$variants" == "[]" && "$shadow" == "True" ]] && loads \
   && head -1 "$L/extends.d/$CM.yml" | command grep -q '^# Attaches local slugs'; then
  ok "prune -y removes the unused items, keeps the purposeful ones and the shadowed entry; the catalog loads"
else fail "prune (rc=$code):$bad variants=$variants shadow=$shadow / $out"; fi
[[ ! -d "$L/composes/stray" && ! -d "$L/composes/byo-model" ]] && ok "the directories it emptied go too" \
  || fail "empty directories left behind"
out="$(cat_ prune -y)"
[[ "$out" == *"nothing to prune"* ]] && ok "a second run finds nothing to prune" || fail "second run: $out"

# 3. an extension with no slug attached goes as a whole file
cat_ unregister --slug "$ATT" -y >/dev/null
python3 - "$L/registry.local.json" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p)); d.pop("vllm/dual", None); json.dump(d, open(p, "w"), indent=2)
PY
out="$(cat_ prune -y)"; code=$?
[[ $code -eq 0 && ! -e "$L/extends.d/$CM.yml" ]] && loads && ok "an extension no slug is attached to is deleted" \
  || fail "extension removal (rc=$code): $out"

[[ $rc -eq 0 ]] && echo "PASS: catalog.sh prune" || echo "FAIL: test-catalog-prune"
exit $rc
