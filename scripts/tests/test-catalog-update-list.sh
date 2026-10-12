#!/usr/bin/env bash
# Guard: `catalog.sh update` edits weights_variant / chat_template / status_note
# and validates every edit before writing; `catalog.sh list` reports the whole
# local layer and what in it is broken, shadowed or unused.
#
#   1. update: a curated variant is accepted on an attached slug, a missing one
#      is refused with the model's variants listed; `8` stays a string; chat
#      template and status note set and clear; an invalid status, which used to
#      be written and then fail get_registry() for every launcher, is refused
#      before the write
#   2. list: the empty layer; engines (registered on their own vs by register),
#      models, attached variants and slugs; unused, broken and shadowed items;
#      a stray compose; a file that doesn't parse; --json; and it still answers
#      when registry.local.json itself is broken
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
cat_() { bash scripts/catalog.sh "$@" --root "$R" -y 2>&1; }
list_() { bash scripts/catalog.sh list --root "$R" "$@" 2>&1 | command grep -v '^\[compat\]'; }
CM=qwen3.6-27b
DIGEST="ghcr.io/maintainer/vllm-solo@sha256:$(printf 'c%.0s' {1..64})"

# 0. the empty layer
out="$(list_)"
[[ "$out" == *"Nothing is registered here"* ]] && ok "list on an empty layer says so" || fail "empty list: $out"

# fixtures
mkdir -p "$R/w/own" "$R/w/byo"
echo '{"hidden_size": 64}' > "$R/w/own/config.json"
echo '{"model_type": "qwen3", "hidden_size": 2048, "num_hidden_layers": 24, "num_attention_heads": 16, "num_key_value_heads": 4, "max_position_embeddings": 32768}' > "$R/w/byo/config.json"
cp "models/$CM/vllm/compose/dual/autoround-int4/fp8-mtp.yml" "$R/sib.yml"
cat_ register --compose "$R/sib.yml" --engine vllm-stable --model "$CM" --weights-variant own --weights "$R/w/own" >/dev/null
ATT="vllm/$CM-dual-own"
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
      - "${PORT:-20204}:8000"
    command: >
      vllm serve /models/byo --served-model-name byo-model --kv-cache-dtype fp8_e5m2
YML
cat_ register --compose "$R/byo.yml" --engine side-engine --engine-type vllm --weights "$R/w/byo" >/dev/null
cat_ register-engine --id vllm-solo --type vllm --image "$DIGEST" --min-sm 8.6 >/dev/null
[[ -f "$L/extends.d/$CM.yml" && -f "$L/models.d/byo-model.yml" && -f "$L/engines.d/vllm-solo.yml" ]] \
  || fail "fixtures were not registered"

# 1. update
entry() { python3 -c "import json,sys; print(json.load(open('$L/registry.local.json'))['$1'].get('$2'))"; }
out="$(cat_ update --slug "$ATT" --set weights_variant=autoround-int4)"; code=$?
[[ $code -eq 0 && "$(entry "$ATT" weights_variant)" == "autoround-int4" ]] \
  && ok "update weights_variant to a curated variant of the attached model" || fail "curated variant (rc=$code): $out"
before="$(sha256sum "$L/registry.local.json")"
out="$(cat_ update --slug "$ATT" --set weights_variant=nope)"; code=$?
[[ $code -eq 3 && "$out" == *"no weights variant 'nope'"* && "$out" == *"autoround-int4"* \
   && "$(sha256sum "$L/registry.local.json")" == "$before" ]] \
  && ok "a missing variant is refused, naming the model's variants; nothing written" || fail "missing variant (rc=$code): $out"
out="$(cat_ update --slug "$ATT" --set weights_variant=8)"; code=$?
[[ $code -eq 3 && "$out" == *"no weights variant '8'"* ]] && ok "a numeric-looking variant stays a string" \
  || fail "numeric variant (rc=$code): $out"
out="$(cat_ update --slug "$ATT" --set chat_template=froggeric --set "status_note=waiting on files")"; code=$?
[[ $code -eq 0 && "$(entry "$ATT" chat_template)" == "froggeric" && "$(entry "$ATT" status_note)" == "waiting on files" ]] \
  && ok "chat_template and status_note set" || fail "template/note (rc=$code): $out"
out="$(cat_ update --slug "$ATT" --set chat_template=my-own-template)"; code=$?
[[ $code -eq 0 && "$out" == *"not used by any curated slug"* ]] && ok "an unfamiliar chat_template is accepted with a note" \
  || fail "unfamiliar template (rc=$code): $out"
cat_ update --slug "$ATT" --set status_note= >/dev/null
[[ "$(entry "$ATT" status_note)" == "None" ]] && ok "status_note= clears it" || fail "status_note not cleared"
before="$(sha256sum "$L/registry.local.json")"
out="$(cat_ update --slug "$ATT" --set status=bogus)"; code=$?
[[ $code -eq 3 && "$out" == *"would not load"* && "$(sha256sum "$L/registry.local.json")" == "$before" ]] \
  && ok "an invalid status is refused before the write" || fail "invalid status (rc=$code): $out"

# 2. list
out="$(list_)"
for want in "vllm-solo" "on its own (register-engine)" "side-engine" "with register (side effect)" \
            "byo-model" "Attached to curated models" "$ATT" \
            "engine vllm-solo: no local slug runs on it" "variant 'own' is used by no slug"; do
  [[ "$out" == *"$want"* ]] || { fail "list is missing: $want"; echo "$out" | sed 's/^/      /'; break; }
done
[[ "$out" == *"variant 'own' is used by no slug"* ]] && ok "list shows engines, models, attachments, slugs and unused items"
# break things: a missing compose, a shadowed slug, a stray compose, an unparseable engine file
rm "$L/composes/byo-model/side-engine/compose/single/safetensors/base.yml"
python3 - "$L/registry.local.json" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p))
d["vllm/dual"] = dict(d["side-engine/byo-model"])     # a name the curated catalog uses
json.dump(d, open(p, "w"), indent=2)
PY
mkdir -p "$L/composes/stray/x/compose/single/y" && echo "services: {}" > "$L/composes/stray/x/compose/single/y/base.yml"
printf 'id: [unclosed\n' > "$L/engines.d/broken.yml"
out="$(list_)"
checks=(
  "broken   side-engine/byo-model: compose scripts/lib/profiles-local/composes/byo-model/side-engine/compose/single/safetensors/base.yml is missing"
  "shadowed vllm/dual: a curated slug has the same name"
  "composes/stray/x/compose/single/y/base.yml: no entry uses this compose"
  "broken   broken.yml: does not parse"
)
miss=0
for want in "${checks[@]}"; do [[ "$out" == *"$want"* ]] || { fail "list did not report: $want"; miss=1; }; done
[[ $miss -eq 0 ]] && ok "list reports a missing compose, a shadowed slug, a stray compose and an unparseable file"
json_ok="$(bash scripts/catalog.sh list --json --root "$R" 2>/dev/null | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(sorted(d) == ['attached', 'engines', 'layer', 'models', 'problems', 'slugs', 'unused'] and any(p['kind'] == 'shadowed' for p in d['problems']))")"
[[ "$json_ok" == "True" ]] && ok "--json carries every section and the problems" || fail "--json: $json_ok"
echo '{not json' > "$L/registry.local.json"
out="$(list_)"; code=$?
[[ $code -eq 0 && "$out" == *"registry.local.json: does not parse"* ]] \
  && ok "list still answers when registry.local.json is broken" || fail "broken registry (rc=$code): $out"

[[ $rc -eq 0 ]] && echo "PASS: catalog.sh update fields + list" || echo "FAIL: test-catalog-update-list"
exit $rc
