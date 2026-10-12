#!/usr/bin/env bash
# Guard: `catalog.sh register --model <curated id>` attaches a slug to a model the
# catalog already ships, through scripts/lib/profiles-local/extends.d/<id>.yml
# (local_extensions.py), instead of creating a second model.
#
# The extension is SHARED STATE: every slug attached to that model reads it, and a
# broken one stops load_profiles() for every launcher, curated slugs included. So
# besides the happy paths this pins the refusals that must fire BEFORE a write,
# the rollback when a post-write check fails, and that the slug it names exports
# without a rename (the reason the feature exists).
set -uo pipefail
export CLUB3090_CONFIG_DIR=/nonexistent/club-3090-test-config   # tests never read your real settings (#1466)
export PYTHONUTF8="${PYTHONUTF8:-1}"
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
ROOT="$PWD"
rc=0
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
ok()   { echo "  ok   $*"; }
fail() { echo "  FAIL $*"; rc=1; }

CM=qwen3.6-27b
SIBLING="models/$CM/vllm/compose/dual/autoround-int4/fp8-mtp.yml"

fresh() {   # a throwaway root: the tooling, the curated model's tree, an empty local layer
  local r="$TMP/$1"
  mkdir -p "$r/scripts" "$r/tools" "$r/models" "$r/.github"
  cp -r scripts/lib "$r/scripts/"
  rm -rf "$r/scripts/lib/profiles-local/"{models.d,composes,engines.d,extends.d,registry.local.json}
  cp -r tools/tui-core "$r/tools/"
  cp scripts/detect_nvlink.sh "$r/scripts/"
  cp -r "models/$CM" "$r/models/"
  cp -r .github/PULL_REQUEST_TEMPLATE "$r/.github/"
  mkdir -p "$r/w/own-awq"; echo '{"hidden_size": 64}' > "$r/w/own-awq/config.json"
  cp "$SIBLING" "$r/my.yml"
  echo "$r"
}
reg() { local r="$1"; shift; bash scripts/catalog.sh register --compose "$r/my.yml" --engine vllm-stable --root "$r" -y "$@" 2>&1; }
ext()  { echo "$1/scripts/lib/profiles-local/extends.d/$CM.yml"; }
local_json() { echo "$1/scripts/lib/profiles-local/registry.local.json"; }
nothing_written() { [[ ! -e "$(ext "$1")" && ! -e "$(local_json "$1")" && ! -d "$1/scripts/lib/profiles-local/composes" ]]; }
py() { local r="$1"; shift; (cd "$r" && CLUB3090_CONFIG_DIR=/nonexistent/x python3 -c "$@" 2>&1 | command grep -v '^\[compat\]'); }

# 1. refusals that fire before anything is written
R="$(fresh refuse)"
out="$(reg "$R" --model "$CM")"; code=$?
if [[ $code -ne 0 && "$out" == *"--weights-variant"* && "$out" == *"autoround-int4"* ]] && nothing_written "$R"; then
  ok "no --weights-variant: refused, curated variants listed, nothing written"
else fail "no --weights-variant (rc=$code): $out"; fi
out="$(reg "$R" --model "$CM" --weights-variant brand-new)"; code=$?
if [[ $code -ne 0 && "$out" == *"--weights"* ]] && nothing_written "$R"; then
  ok "a new variant without --weights: refused, nothing written"
else fail "new variant without --weights (rc=$code): $out"; fi
# The curated sibling itself serves `qwen3.6-27b` (its first --served-model-name).
out="$(bash scripts/catalog.sh register --compose "$R/my.yml" --engine vllm-stable --root "$R" -y 2>&1)"; code=$?
if [[ $code -ne 0 && "$out" == *"--model $CM --weights-variant"* ]] && nothing_written "$R"; then
  ok "a served name equal to a curated id is not attached silently; the fix is named"
else fail "served name = curated id (rc=$code): $out"; fi

# 2. --dry-run writes nothing, the extension included
out="$(reg "$R" --model "$CM" --weights-variant own-awq --weights "$R/w/own-awq" --dry-run)"; code=$?
if [[ $code -eq 0 && "$out" == *"attach to the curated model $CM"* ]] && nothing_written "$R"; then
  ok "--dry-run shows the attach plan and writes nothing"
else fail "--dry-run (rc=$code): $out"; fi

# 3. a new variant: extension written, no model profile, the curated layout
out="$(reg "$R" --model "$CM" --weights-variant own-awq --weights "$R/w/own-awq" --hf-repo org/own-awq)"; code=$?
SLUG="vllm/$CM-dual-own-awq"
if [[ $code -eq 0 && "$out" == *"PROMOTE_OK $SLUG"* ]]; then
  ok "a new variant attaches as $SLUG (the prefix core uses for vllm-stable)"
else fail "new variant (rc=$code): $out"; fi
[[ ! -e "$R/scripts/lib/profiles-local/models.d/$CM.yml" ]] && ok "no models.d profile for a curated model" \
  || fail "a models.d/$CM.yml was written"
[[ -f "$R/scripts/lib/profiles-local/composes/$CM/vllm/compose/dual/own-awq/base.yml" ]] \
  && ok "compose filed where core keeps vllm-stable composes (vllm/)" || fail "compose not under composes/$CM/vllm/"
res="$(py "$R" "
import json
from scripts.lib.profiles.compat import load_profiles
from scripts.lib.profiles.compose_registry import get_registry
p = load_profiles()
m = p.models['$CM']
e = get_registry()['$SLUG']
print('variant' if 'own-awq' in m.weights else 'no-variant', e['model'], e['weights_variant'], e['origin'])
# What the PR branch checks: the KV format is one the engine and every hardware
# profile can name (a raw \${KV_CACHE_DTYPE:-…} would be in neither), and the new
# variant names the engine DIRECTORY, as curated profiles do.
eng = p.engines['vllm-stable']
print('kv-ok' if e['kv_format'] in (eng.supported_kv_formats or []) else 'kv-bad:' + str(e['kv_format']),
      'engine-dir:' + str(m.weights['own-awq'].get('engine')))
")"
[[ "$res" == "variant $CM own-awq local"$'\n'"kv-ok engine-dir:vllm" ]] \
  && ok "profiles load with the variant; the entry names the curated model; kv_format and engine: are catalog values" \
  || fail "loaded state: $res"

# 4. a second slug reusing a curated variant merges into the same extension
out="$(reg "$R" --model "$CM" --weights-variant autoround-int4 --slug "vllm/$CM-dual-mine")"; code=$?
res="$(py "$R" "
import yaml
from scripts.lib.profiles.compose_registry import get_registry
reg = get_registry()
d = yaml.safe_load(open('scripts/lib/profiles-local/extends.d/$CM.yml'))
print(sorted(d['weights']), '$SLUG' in reg, 'vllm/$CM-dual-mine' in reg)
")"
if [[ $code -eq 0 && "$res" == "['own-awq'] True True" ]]; then
  ok "a second slug reuses a curated variant; the extension keeps the first slug's variant"
else fail "second slug (rc=$code): $res / $out"; fi

# 5. the same variant name for other weights is refused, and the file is untouched
before="$(sha256sum "$(ext "$R")")"
mkdir -p "$R/w/other"; echo '{}' > "$R/w/other/config.json"
out="$(reg "$R" --model "$CM" --weights-variant own-awq --weights "$R/w/other" --slug "vllm/$CM-dual-clash")"; code=$?
if [[ $code -ne 0 && "$out" == *"already has variant 'own-awq'"* && "$(sha256sum "$(ext "$R")")" == "$before" ]]; then
  ok "a variant name already used for other weights is refused; the extension is unchanged"
else fail "variant clash (rc=$code): $out"; fi

# 6. unregister removes the slug and keeps the extension other slugs need
bash scripts/catalog.sh unregister --slug "vllm/$CM-dual-mine" --root "$R" -y >/dev/null 2>&1
res="$(py "$R" "
from scripts.lib.profiles.compose_registry import get_registry
r = get_registry(); print('vllm/$CM-dual-mine' in r, '$SLUG' in r)")"
[[ "$res" == "False True" && -f "$(ext "$R")" ]] && ok "unregister removed one slug, kept the extension and the other slug" \
  || fail "unregister: $res"

# 7. the round trip: the slug it named exports without a rename
bash scripts/catalog.sh update --slug "$SLUG" --set "kvcalc_key=$CM:dual" --root "$R" >/dev/null 2>&1
py "$R" "
import yaml
p = 'scripts/lib/profiles-local/extends.d/$CM.yml'
d = yaml.safe_load(open(p)); d['weights']['own-awq']['files'] = ['*.safetensors', '*.json']
open(p, 'w').write(yaml.safe_dump(d, sort_keys=False))" >/dev/null
echo "{\"model_id\": \"$CM\"}" > "$TMP/spec.json"
out="$(python3 scripts/lib/profiles/export_pr.py --spec-file "$TMP/spec.json" --root "$R" --check 2>&1)"; code=$?
if [[ $code -eq 0 && "$out" == *"CHECK_OK $CM"* && "$out" == *"models/$CM/vllm/compose/dual/own-awq/base.yml"* ]]; then
  ok "export_pr --check passes on the attached slug, compose and prefix unchanged"
else fail "export round trip (rc=$code): $(echo "$out" | command grep -v '^\[compat\]' | tail -6)"; fi

# 8. a reuse with no prior extension still writes one (the loader needs the file)
R2="$(fresh reuse)"
out="$(reg "$R2" --model "$CM" --weights-variant autoround-int4)"; code=$?
res="$(py "$R2" "
import yaml
from scripts.lib.profiles.compose_registry import get_registry
d = yaml.safe_load(open('scripts/lib/profiles-local/extends.d/$CM.yml'))
print(d.get('extends'), d.get('weights'), 'vllm/$CM-dual-autoround-int4' in get_registry())")"
[[ $code -eq 0 && "$res" == "$CM {} True" ]] && ok "reusing a curated variant writes extends.d/$CM.yml with no weights" \
  || fail "reuse without an extension (rc=$code): $res / $out"

# 9. an extension that would not load is refused before any write
R3="$(fresh broken)"
mkdir -p "$(dirname "$(ext "$R3")")"
printf 'schema_version: 1\nextends: %s\nweights: {}\nbogus_key: 1\n' "$CM" > "$(ext "$R3")"
before="$(sha256sum "$(ext "$R3")")"
out="$(reg "$R3" --model "$CM" --weights-variant autoround-int4)"; code=$?
if [[ $code -ne 0 && "$out" == *"would not load"* && "$(sha256sum "$(ext "$R3")")" == "$before" \
      && ! -e "$(local_json "$R3")" ]]; then
  ok "an extension that would not load is refused before anything is written"
else fail "broken extension (rc=$code): $out"; fi

# 10. a post-write failure puts every file back
R4="$(fresh rollback)"
printf '#!/usr/bin/env bash\necho "emit broken for the test" >&2\nexit 1\n' > "$R4/scripts/lib/registry-emit.sh"
out="$(reg "$R4" --model "$CM" --weights-variant own-awq --weights "$R4/w/own-awq")"; code=$?
if [[ $code -eq 2 && "$out" == *"ROLLED BACK"* ]] && nothing_written "$R4"; then
  ok "a failed post-write check rolls back the extension, compose and entry"
else fail "rollback (rc=$code): $(echo "$out" | tail -4)"; ls -R "$R4/scripts/lib/profiles-local" 2>/dev/null | head; fi

# 11. llama-cpp-local keeps composes in two directories; the compose picks one
LL="$(python3 - <<'PY' 2>/dev/null
import sys; sys.path.insert(0, ".")
from scripts.lib.profiles.compose_registry import COMPOSE_REGISTRY as R
for s, e in sorted(R.items()):
    if e.get("engine") == "llama-cpp-local" and e.get("status") != "deprecated" \
            and e["compose_path"].split("/")[2] == "llama-cpp":
        print(e["model"], e["weights_variant"], e["compose_path"]); break
PY
)"
read -r LM LV LC <<<"$LL"
if [[ -n "${LC:-}" ]]; then
  R5="$(fresh llama)"; cp -r "models/$LM" "$R5/models/" 2>/dev/null; cp "$LC" "$R5/ll.yml"
  out="$(bash scripts/catalog.sh register --compose "$R5/ll.yml" --engine llama-cpp-local --model "$LM" \
         --weights-variant "$LV" --slug "llamacpp/$LM-attach-test" --root "$R5" -y 2>&1)"; code=$?
  if [[ $code -eq 0 && -d "$R5/scripts/lib/profiles-local/composes/$LM/llama-cpp" ]]; then
    ok "llama-cpp-local: the compose's own engine picks llama-cpp/ of its two core directories"
  else fail "llama-cpp-local attach (rc=$code): $(echo "$out" | tail -4)"; fi
else fail "fixture: no live llama-cpp-local slug in llama-cpp/"; fi

[[ $rc -eq 0 ]] && echo "PASS: catalog.sh register attaches slugs to curated models" \
  || echo "FAIL: test-catalog-attach"
exit $rc
