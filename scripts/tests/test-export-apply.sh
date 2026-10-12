#!/usr/bin/env bash
# Guard: `export_pr.py --apply --into <checkout>` puts a bundle into another
# club-3090 checkout the way a maintainer would by hand, and leaves the target as
# it was when it can't.
#
#   1. the weights snippet goes into every curated model profile as TEXT: the
#      result parses to the old profile plus the variant (and valid_tp), and no
#      comment is lost; a variant the profile already has is refused; the disk
#      counts are left alone when the target's are already off
#   2. refusals, target untouched: no gate, this same checkout, a directory that
#      isn't the top of a checkout, uncommitted changes, a file already there, a
#      target that doesn't ship the model, and the flag combinations argparse owns
#   3. --dry-run lists the changes and writes nothing (no gate needed)
#   4. a real apply: the compose added at its core path, the entry merged as a
#      core entry, the variant added with the profile's comments intact, the
#      disk counts raised, registry.yaml canonical, nothing else touched; a
#      header/status disagreement shows as a new test-compose-status-drift FAIL
#   5. a guard already red before the apply is reported as such, not as new
#   6. a failed check after the write puts the target back exactly
set -uo pipefail
export CLUB3090_CONFIG_DIR=/nonexistent/club-3090-test-config   # tests never read your real settings (#1466)
export PYTHONUTF8="${PYTHONUTF8:-1}"
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
rc=0
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
ok()   { echo "  ok   $*"; }
fail() { echo "  FAIL $*"; rc=1; }
unset C3_ALLOW_CORE_PROMOTE

CM=qwen3.6-27b
SLUG="vllm/$CM-dual-own-awq"
COMPOSE="models/$CM/vllm/compose/dual/own-awq/base.yml"
PROFILE="scripts/lib/profiles/models/$CM.yml"

# 1. the text edits, over the real curated profiles
python3 - <<'PY' || rc=1
import sys
from pathlib import Path
sys.path.insert(0, ".")
from scripts.lib.profiles.export_pr import Refusal, bump_disk_counts, insert_weights
import yaml

variant = {"zz-apply-test": {"path": "x", "hf_repo": "org/x", "engine": "vllm",
                             "files": ["*.safetensors", "*.json"], "size_gb": 1.5}}
bad, n = [], 0
for p in sorted(Path("scripts/lib/profiles/models").glob("*.yml")):
    text = p.read_text(encoding="utf-8")
    if "weights" not in (yaml.safe_load(text) or {}):
        continue
    n += 1
    try:
        new = insert_weights(text, variant, [3])
    except Refusal as exc:
        bad.append(f"{p.name}: {exc}"); continue
    comments = lambda t: [ln.strip() for ln in t.splitlines() if ln.lstrip().startswith("#")]
    if comments(text) != comments(new):
        bad.append(f"{p.name}: a comment was lost or moved")
    d = yaml.safe_load(new)
    if d["weights"].get("zz-apply-test") != variant["zz-apply-test"] or 3 not in d.get("valid_tp", []):
        bad.append(f"{p.name}: variant or valid_tp missing")
print(f"  {'FAIL' if bad else 'ok  '} weights snippet added as text to {n} curated profiles ({len(bad)} bad)")
for b in bad[:8]:
    print(f"  FAIL {b}")
try:
    insert_weights(Path("scripts/lib/profiles/models/qwen3.6-27b.yml").read_text(encoding="utf-8"),
                   {"autoround-int4": {"path": "x"}}, [])
    bad.append("clash"); print("  FAIL a variant the profile already has was not refused")
except Refusal:
    print("  ok   a variant the profile already has is refused")
disk = Path("scripts/tests/test-compose-registry-disk.sh").read_text(encoding="utf-8")
if bump_disk_counts(disk, (1, 2), (1, 1)) is None:
    print("  ok   disk counts left alone when the target's are already off")
else:
    bad.append("bump"); print("  FAIL disk counts bumped although they didn't match")
sys.exit(1 if bad else 0)
PY

# source: a throwaway root whose local layer attaches one slug, with a new variant
R="$TMP/src"
mkdir -p "$R/scripts" "$R/tools" "$R/models" "$R/.github"
cp -r scripts/lib "$R/scripts/"
rm -rf "$R/scripts/lib/profiles-local/"{models.d,composes,engines.d,extends.d,registry.local.json}
cp -r tools/tui-core "$R/tools/"
cp scripts/detect_nvlink.sh "$R/scripts/"
cp -r "models/$CM" "$R/models/"
cp -r .github/PULL_REQUEST_TEMPLATE "$R/.github/"
mkdir -p "$R/w/own-awq"; echo '{"hidden_size": 64}' > "$R/w/own-awq/config.json"
cp "models/$CM/vllm/compose/dual/autoround-int4/fp8-mtp.yml" "$R/my.yml"
bash scripts/catalog.sh register --compose "$R/my.yml" --engine vllm-stable --root "$R" -y \
  --model "$CM" --weights-variant own-awq --weights "$R/w/own-awq" --hf-repo org/own-awq >/dev/null 2>&1
bash scripts/catalog.sh update --slug "$SLUG" --set "kvcalc_key=$CM:dual" --root "$R" >/dev/null 2>&1
(cd "$R" && python3 -c "
import yaml
p = 'scripts/lib/profiles-local/extends.d/$CM.yml'
d = yaml.safe_load(open(p)); d['weights']['own-awq']['files'] = ['*.safetensors', '*.json']
open(p, 'w').write(yaml.safe_dump(d, sort_keys=False))")
echo "{\"model_id\": \"$CM\"}" > "$TMP/spec.json"

target() {   # a git checkout of this tree's HEAD, on a PR branch
  local t="$TMP/$1"; mkdir -p "$t"
  git archive HEAD | tar -x -C "$t"
  (cd "$t" && git -c init.defaultBranch=pr-branch init -q && git add -A \
    && git -c user.email=t@example.com -c user.name=t commit -qm base)
  echo "$t"
}
commit_in() { (cd "$1" && git add -A && git -c user.email=t@example.com -c user.name=t commit -qm "$2"); }
apply_() { local t="$1"; shift
  python3 "$R/scripts/lib/profiles/export_pr.py" --spec-file "$TMP/spec.json" --root "$R" \
    --out "$TMP/out-$(basename "$t")" --apply --into "$t" "$@" 2>&1 | command grep -v '^\[compat\]'
  return "${PIPESTATUS[0]}"; }
untouched() { [[ -z "$(git -C "$1" status --porcelain --untracked-files=all)" ]]; }

out="$(python3 "$R/scripts/lib/profiles/export_pr.py" --spec-file "$TMP/spec.json" --root "$R" --check 2>&1)" \
  || { fail "fixture: the attached slug does not export: $(echo "$out" | tail -5)"; exit 1; }

# 2. refusals
T="$(target t-refuse)"
out="$(apply_ "$T")"; code=$?
[[ $code -eq 3 && "$out" == *"maintainer-gated"* && ! -e "$TMP/out-t-refuse" ]] && untouched "$T" \
  && ok "no gate: refused, no bundle written, target untouched" || fail "no gate (rc=$code): $out"
export C3_ALLOW_CORE_PROMOTE=1
out="$(apply_ "$R")"; code=$?
[[ $code -eq 3 && "$out" == *"separate checkout"* ]] && ok "this same checkout is refused" || fail "same checkout (rc=$code): $out"
mkdir -p "$TMP/plain"
out="$(apply_ "$TMP/plain")"; code=$?
[[ $code -eq 3 && "$out" == *"not the top of a git checkout"* ]] && ok "a plain directory is refused" || fail "plain dir (rc=$code): $out"
out="$(apply_ "$T/models")"; code=$?
[[ $code -eq 3 && "$out" == *"not the top of a git checkout"* ]] && untouched "$T" \
  && ok "a subdirectory of a checkout is refused" || fail "subdir (rc=$code): $out"
echo "# local edit" >> "$T/README.md"
out="$(apply_ "$T")"; code=$?
[[ $code -eq 3 && "$out" == *"uncommitted changes"* && "$out" == *"README.md"* ]] \
  && ok "uncommitted changes are refused, named" || fail "dirty (rc=$code): $out"
git -C "$T" checkout -q -- README.md
mkdir -p "$(dirname "$T/$COMPOSE")"; echo "x: 1" > "$T/$COMPOSE"
out="$(apply_ "$T")"; code=$?
[[ $code -eq 3 && "$out" == *"$COMPOSE already exists"* ]] && ok "a file already in the target is refused" || fail "exists (rc=$code): $out"
rm -rf "$T/models/$CM/vllm/compose/dual/own-awq"
untouched "$T" && ok "the refusals left the target untouched" || fail "target changed: $(git -C "$T" status --porcelain)"
T2="$(target t-nomodel)"; git -C "$T2" rm -q "$PROFILE"; commit_in "$T2" drop
out="$(apply_ "$T2")"; code=$?
[[ $code -eq 3 && "$out" == *"doesn't ship $CM"* ]] && untouched "$T2" \
  && ok "a target without the model is refused" || fail "no model (rc=$code): $out"
python3 scripts/lib/profiles/export_pr.py --spec-file "$TMP/spec.json" --root "$R" --apply >/dev/null 2>&1; code=$?
python3 scripts/lib/profiles/export_pr.py --spec-file "$TMP/spec.json" --root "$R" --apply --into "$T" --check >/dev/null 2>&1; code2=$?
[[ $code -eq 2 && $code2 -eq 2 ]] && ok "--apply without --into, and with --check, are usage errors" || fail "flag combos rc=$code/$code2"

# 3. --dry-run
out="$(C3_ALLOW_CORE_PROMOTE= apply_ "$T" --dry-run)"; code=$?
[[ $code -eq 0 && "$out" == *"add    $COMPOSE"* && "$out" == *"merge  scripts/lib/profiles/registry.yaml: $SLUG"* \
   && "$out" == *"edit   $PROFILE"* && "$out" == *"edit   scripts/tests/test-compose-registry-disk.sh"* \
   && "$out" == *"dry-run — nothing written"* ]] && untouched "$T" \
  && ok "--dry-run lists every change, needs no gate, writes nothing" || fail "dry-run (rc=$code): $out"

# 4. the real thing
before_profile="$(cat "$T/$PROFILE")"
out="$(apply_ "$T")"; code=$?
[[ $code -eq 0 && "$out" == *"APPLY_OK $T"* && -f "$T/$COMPOSE" ]] && ok "applies; the compose lands at its core path" \
  || fail "apply (rc=$code): $out"
res="$(cd "$T" && python3 -c "
from scripts.lib.profiles.compat import load_profiles
from scripts.lib.profiles.compose_registry import get_registry
e = get_registry()['$SLUG']; m = load_profiles().models['$CM']
print(e['origin'], e['compose_path'], 'own-awq' in m.weights)" 2>&1 | command grep -v '^\[compat\]')"
[[ "$res" == "core $COMPOSE True" ]] && ok "the target loads it as a core entry with the new variant" || fail "loaded: $res"
python3 - "$before_profile" "$T/$PROFILE" <<'PY' && ok "the model profile keeps every line it had" || fail "model profile lines lost"
import sys
old = sys.argv[1].splitlines(); new = open(sys.argv[2], encoding="utf-8").read().splitlines()
it = iter(new); sys.exit(0 if all(ln in it for ln in old) else 1)
PY
command grep -q '== 171, f"registry expects 171 entries' "$T/scripts/tests/test-compose-registry-disk.sh" \
  && command grep -q '== 174, f"disk expects 174 compose files' "$T/scripts/tests/test-compose-registry-disk.sh" \
  && ok "disk counts raised by one entry and one compose" || fail "disk counts not raised"
(cd "$T" && python3 scripts/lib/profiles/migrate_registry_to_yaml.py --check >/dev/null 2>&1) \
  && ok "the target's registry.yaml is canonical" || fail "registry not canonical"
changed="$(git -C "$T" status --porcelain --untracked-files=all | sort | tr '\n' '|')"
want="$(printf '%s\n' " M $PROFILE" " M scripts/lib/profiles/registry.yaml" " M scripts/tests/test-compose-registry-disk.sh" "?? $COMPOSE" | sort | tr '\n' '|')"
[[ "$changed" == "$want" ]] && ok "exactly those four paths changed" || fail "changed: $changed"
[[ "$out" == *"ok   test-compose-registry-disk"* && "$out" == *"ok   test-model-weights-registry"* \
   && "$out" == *"ok   test-compose-mounts-resolve"* \
   && "$out" == *"FAIL test-compose-status-drift"* && "$out" == *"'incubating') consistent with compose header ('caveats')"* ]] \
  && ok "guards reported: the header/status disagreement shows as a new FAIL" || fail "guards: $out"
[[ -f "$TMP/out-t-refuse/PR_BODY.md" ]] && ok "the bundle (PR_BODY.md) is written beside it" || fail "no PR_BODY.md"

# 5. already red before: reported as such. The header now agrees with the entry.
LC="$R/scripts/lib/profiles-local/composes/$CM/vllm/compose/dual/own-awq/base.yml"
sed -i -E 's/^(#[[:space:]]+Status:[[:space:]]+).*/\1🐣 Incubating/' "$LC"
T3="$(target t-baseline)"; git -C "$T3" rm -rq kv-offload; commit_in "$T3" "drop kv-offload"
out="$(apply_ "$T3")"; code=$?
[[ $code -eq 0 && "$out" == *"--   test-compose-mounts-resolve: red before the apply too"* \
   && "$out" == *"ok   test-compose-status-drift"* && "$out" != *"red above"* ]] \
  && ok "a guard red before the apply is not blamed on it" || fail "baseline (rc=$code): $out"

# 6. a check failing after the write puts the target back
T4="$(target t-restore)"
printf 'import sys\nsys.exit("broken on purpose")\n' > "$T4/scripts/lib/profiles/migrate_registry_to_yaml.py"
commit_in "$T4" "break the canonical check"
out="$(apply_ "$T4")"; code=$?
[[ $code -eq 2 && "$out" == *"was put back as it was"* ]] && untouched "$T4" \
  && [[ ! -e "$T4/models/$CM/vllm/compose/dual/own-awq" ]] \
  && ok "a failed check restores the target: no change, no file, no directory left" \
  || fail "restore (rc=$code): $out / $(git -C "$T4" status --porcelain --untracked-files=all)"

[[ $rc -eq 0 ]] && echo "test-export-apply: ok" || echo "test-export-apply: FAILED"
exit $rc
