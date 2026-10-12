#!/usr/bin/env bash
# Guard: scripts/deprecate-slug.sh marks a curated slug deprecated in every
# place the deprecation checklist names, refuses what would leave a dead
# default behind, and restores every file when a check fails after writing.
#
#   1. the header rewrite, run over EVERY curated compose: the result parses
#      back as deprecated, carries the reason as Caveats, and keeps every other
#      field and every line outside the profile block
#   2. refusals, nothing written: the maintainer gate, an empty reason, an
#      unknown slug, a local slug, a slug whose compose another live slug runs
#      (one header, one Status), a defaults target with no decision, a
#      --repoint that isn't a functional sibling (incl. an alias on the same
#      compose), a decision flag that doesn't apply, a gateway slug with no
#      decision
#   3. a real deprecation in a throwaway checkout: vllm/dual with its alias,
#      header, registry, defaults repointed, gateway kept and annotated, the
#      drift guard and litellm-emit --check green, hand-written mentions
#      listed; a second run is a no-op
#   4. --drop-default + --drop-gateway: the row and the route go, and the
#      resolver and recommended-model warning say so
#   5. a check failing after the write puts every file back byte for byte
set -uo pipefail
export CLUB3090_CONFIG_DIR=/nonexistent/club-3090-test-config   # tests never read your real settings (#1466)
export PYTHONUTF8="${PYTHONUTF8:-1}"
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
rc=0
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
ok()   { echo "  ok   $*"; }
fail() { echo "  FAIL $*"; rc=1; }
unset C3_ALLOW_CORE_PROMOTE

# 1. the pure header rewrite over every curated compose
python3 - <<'PY' || rc=1
import re, sys
from pathlib import Path
sys.path.insert(0, ".")
from scripts.lib.profiles.compose_registry import compose_header_status, get_registry
from scripts.lib.profiles.deprecate_slug import _fields, _schema_span, rewrite_header

REASON = "Superseded by vllm/other: same weights, faster drafter (#1234)."
bad = []
n = 0
for slug, e in sorted(get_registry().items()):
    if (e or {}).get("origin") == "local":
        continue
    text = Path(e["compose_path"]).read_text(encoding="utf-8")
    new = rewrite_header(text, REASON, "🗑️ Deprecated")
    n += 1
    if compose_header_status(new) != "deprecated":
        bad.append(f"{slug}: reads back as {compose_header_status(new)!r}"); continue
    old_l, new_l = text.splitlines(), new.splitlines()
    (o0, o1), (n0, n1) = _schema_span(old_l), _schema_span(new_l)
    if old_l[:o0] != new_l[:n0] or old_l[o1:] != new_l[n1:]:
        bad.append(f"{slug}: a line outside the profile block changed"); continue
    def named(lines, span):
        return [(name, lines[a:b]) for name, a, b in _fields(lines, *span)]
    before, after = named(old_l, (o0, o1)), named(new_l, (n0, n1))
    cav = [f for f in after if f[0] == "Caveats"][0]
    if len(cav[1]) != 1 or not re.match(r"^#\s*Caveats:\s+" + re.escape(REASON) + r"$", cav[1][0]):
        bad.append(f"{slug}: Caveats is {cav[1]!r}"); continue
    keep = lambda fs: [f for f in fs if f[0] not in ("Status", "Caveats")]
    if keep(before) != keep(after):
        bad.append(f"{slug}: another header field changed"); continue
    # a second Caveats field (a few headers have two) survives untouched
    if [f for f in before if f[0] == "Caveats"][1:] != [f for f in after if f[0] == "Caveats"][1:]:
        bad.append(f"{slug}: a later Caveats field changed")
for b in bad[:10]:
    print(f"  FAIL {b}")
print(f"  {'FAIL' if bad else 'ok  '} header rewrite over {n} curated composes ({len(bad)} bad)")
sys.exit(1 if bad else 0)
PY

# throwaway checkout
R="$TMP/root"
mkdir -p "$R/scripts/tests" "$R/tools" "$R/services"
cp -r scripts/lib "$R/scripts/"
rm -rf "$R/scripts/lib/profiles-local/"{models.d,composes,engines.d,extends.d,registry.local.json}
cp scripts/deprecate-slug.sh scripts/catalog.sh scripts/detect_nvlink.sh "$R/scripts/"
cp scripts/tests/test-compose-status-drift.sh "$R/scripts/tests/"
cp -r models "$R/"
cp -r services/litellm "$R/services/"
cp -r tools/tui-core "$R/tools/"
mkdir -p "$R/docs"; cp docs/UPSTREAM.md "$R/docs/"
REG="$R/scripts/lib/profiles/registry.yaml"
DUAL_COMPOSE="$R/models/qwen3.6-27b/vllm/compose/dual/autoround-int4/fp8-mtp.yml"
dep() { bash "$R/scripts/deprecate-slug.sh" "$@" 2>&1 | command grep -v '^\[compat\]'; return "${PIPESTATUS[0]}"; }
state() { cat "$REG" "$DUAL_COMPOSE" "$R/services/litellm/config.yaml" | sha256sum; }
entry() { (cd "$R" && python3 -c "import sys; from scripts.lib.profiles.compose_registry import get_registry; print(get_registry()[sys.argv[1]].get(sys.argv[2]))" "$1" "$2"); }
dflt() { (cd "$R" && python3 -c "import sys; from scripts.lib.profiles.compose_registry import DEFAULTS; print(DEFAULTS.get(tuple(sys.argv[1:])))" "$@"); }
WHY="Superseded by vllm/qwen-27b-dual-max on every measured axis (#1234)."
OK_ARGS=(--with-aliases --repoint vllm/qwen-27b-dual-max --keep-gateway)

# 2. refusals — nothing written
S0="$(state)"
refused() {  # <label> <expected text> -- <args…>
  local label="$1" want="$2"; shift 3
  out="$(dep "$@")"; code=$?
  [[ $code -eq 3 && "$out" == *"$want"* && "$(state)" == "$S0" ]] \
    && ok "$label" || fail "$label (rc=$code): $out"
}
refused "no gate, no write" "maintainer-gated" -- vllm/dual --reason "$WHY" "${OK_ARGS[@]}"
export C3_ALLOW_CORE_PROMOTE=1
refused "empty reason" "--reason is empty" -- vllm/dual --reason "  " "${OK_ARGS[@]}"
refused "unknown slug, with a suggestion" "did you mean vllm/dual" -- vllm/duall --reason "$WHY"
refused "a shared compose needs --with-aliases" "also the compose of vllm/qwen-27b-dual-fast" -- vllm/dual --reason "$WHY" --repoint vllm/qwen-27b-dual-max --keep-gateway
refused "defaults target needs a decision, siblings listed" "functional siblings: vllm/qwen-27b-dual-max, vllm/qwen-27b-dual-nvfp4" -- vllm/dual --reason "$WHY" --with-aliases --keep-gateway
refused "--repoint to an alias on the same compose" "same compose" -- vllm/dual --reason "$WHY" --with-aliases --repoint vllm/qwen-27b-dual-fast --keep-gateway
refused "--repoint to another topology is not a sibling" "not a functional sibling" -- vllm/dual --reason "$WHY" --with-aliases --repoint vllm/minimal --keep-gateway
refused "--repoint to a deprecated slug is not a sibling" "not a functional sibling" -- vllm/dual --reason "$WHY" --with-aliases --repoint vllm/qwen-27b-dual-balanced --keep-gateway
refused "gateway slug needs a decision" "on the LAN gateway" -- vllm/dual --reason "$WHY" --with-aliases --repoint vllm/qwen-27b-dual-max
NONDEF="$(cd "$R" && python3 - <<'PY'
from scripts.lib.profiles.compose_registry import DEFAULTS, FUNCTIONAL_STATUSES, get_registry
d = set(DEFAULTS.values())
print(next(k for k, e in sorted(get_registry().items())
           if k not in d and not e.get("gateway") and e.get("status") in FUNCTIONAL_STATUSES))
PY
)"
refused "--drop-default on a slug that is no default" "not a defaults target" -- "$NONDEF" --reason "$WHY" --drop-default
refused "--keep-gateway on a slug that is not on it" "not on the gateway" -- "$NONDEF" --reason "$WHY" --keep-gateway
refused "--with-aliases on a compose no other slug runs" "drop --with-aliases" -- "$NONDEF" --reason "$WHY" --with-aliases

# a local slug: catalog.sh unregister owns those
mkdir -p "$R/w/own"; echo '{"hidden_size": 64}' > "$R/w/own/config.json"
cp "$DUAL_COMPOSE" "$R/sib.yml"
bash "$R/scripts/catalog.sh" register --compose "$R/sib.yml" --engine vllm-stable --model qwen3.6-27b \
  --weights-variant own --weights "$R/w/own" --root "$R" -y >/dev/null 2>&1
LOCAL_SLUG="vllm/qwen3.6-27b-dual-own"
if [[ "$(entry "$LOCAL_SLUG" origin)" == "local" ]]; then
  refused "a local slug points at catalog.sh unregister" "catalog.sh unregister $LOCAL_SLUG" -- "$LOCAL_SLUG" --reason "$WHY"
else
  fail "fixture: local slug $LOCAL_SLUG was not registered"
fi
rm -rf "$R/scripts/lib/profiles-local/"{models.d,composes,engines.d,extends.d,registry.local.json} "$R/sib.yml"

# dry-run: the diff, and still nothing written
out="$(C3_ALLOW_CORE_PROMOTE= dep vllm/dual --reason "$WHY" "${OK_ARGS[@]}" --dry-run)"; code=$?
[[ $code -eq 0 && "$out" == *"+#   Status:    🗑️ Deprecated"* && "$out" == *"+      dual: vllm/qwen-27b-dual-max"* \
   && "$out" == *"dry-run — nothing written"* && "$(state)" == "$S0" ]] \
  && ok "--dry-run shows the diff without the gate and writes nothing" || fail "dry-run (rc=$code): $out"

# 3. the real thing
out="$(dep vllm/dual --reason "$WHY" "${OK_ARGS[@]}")"; code=$?
[[ $code -eq 0 && "$out" == *"DEPRECATE_OK vllm/dual vllm/qwen-27b-dual-fast"* ]] && ok "deprecates vllm/dual and its alias" || fail "deprecate (rc=$code): $out"
[[ "$(entry vllm/dual status)" == "deprecated" && "$(entry vllm/dual status_note)" == "🗑️ DEPRECATED "*": $WHY" \
   && "$(entry vllm/qwen-27b-dual-fast status)" == "deprecated" ]] \
  && ok "registry: both deprecated, dated note with the reason" || fail "registry entry: $(entry vllm/dual status) / $(entry vllm/dual status_note)"
[[ "$(entry vllm/dual compose_path)" == *"fp8-mtp.yml" && "$(entry vllm/dual tp)" == "2" && "$(entry vllm/dual gateway)" == "True" ]] \
  && ok "the entry, its hardware fields and gateway flag are kept" || fail "entry fields lost"
[[ "$(dflt qwen3.6-27b vllm dual)" == "vllm/qwen-27b-dual-max" ]] && ok "defaults row repointed" || fail "defaults: $(dflt qwen3.6-27b vllm dual)"
command grep -qF "#   Caveats:   $WHY" "$DUAL_COMPOSE" && ! command grep -q "OPEN vllm#50021 (GDN spec-decode crash" "$DUAL_COMPOSE" \
  && ok "header: the old Caveats field replaced by the reason" || fail "header caveats"
bash "$R/scripts/tests/test-compose-status-drift.sh" >/dev/null 2>&1 && ok "test-compose-status-drift green on the result" || fail "drift guard red"
(cd "$R" && python3 scripts/lib/profiles/migrate_registry_to_yaml.py --check >/dev/null 2>&1) && ok "registry.yaml canonical" || fail "registry not canonical"
(cd "$R" && bash scripts/lib/litellm-emit.sh --check "$R" >/dev/null 2>&1) && ok "litellm-emit --check green" || fail "litellm-emit --check red"
command grep -q "# status: deprecated" "$R/services/litellm/config.yaml" && ok "gateway route kept, annotated deprecated" || fail "no deprecated annotation in the gateway config"
[[ "$out" == *"qwen3.6-27b dual default now: vllm/qwen-27b-dual-max"* && "$out" == *"hand-written mentions in scripts/ and tools/"* \
   && "$out" == *"models/qwen3.6-27b/CHANGELOG.md"* ]] && ok "reports the resolver result, mentions and CHANGELOG" || fail "report: $out"
S1="$(state)"
out="$(dep vllm/dual --reason "$WHY")"; code=$?
[[ $code -eq 0 && "$out" == *"already deprecated"* && "$(state)" == "$S1" ]] && ok "a second run is a no-op" || fail "rerun (rc=$code): $out"

# 4. drop the default and the route
G=vllm/gemma-31b-dual
out="$(dep "$G" --reason "Test: no functional sibling." --drop-default --drop-gateway)"; code=$?
[[ $code -eq 0 && "$(dflt gemma-4-31b vllm dual)" == "None" && "$(entry "$G" gateway)" == "False" ]] \
  && ok "--drop-default removes the row, --drop-gateway the flag" || fail "drop (rc=$code): $out"
[[ "$out" == *"gemma-4-31b dual default now: none functional"* && "$out" == *"recommended_default_models"* ]] \
  && ok "says the resolver now degrades, and warns for a recommended model" || fail "drop report: $out"
(cd "$R" && bash scripts/lib/litellm-emit.sh --check "$R" >/dev/null 2>&1) && ! command grep -q "gemma-31b-dual" "$R/services/litellm/config.yaml" \
  && ok "gateway block regenerated without the route" || fail "gateway still routes $G"

# 5. a failure after the write restores everything
F=vllm/qwen-35b-a3b-dual
cp "$R/scripts/lib/litellm-emit.sh" "$TMP/emit.bak"
printf '#!/usr/bin/env bash\necho "emit broke" >&2; exit 7\n' > "$R/scripts/lib/litellm-emit.sh"
F_COMPOSE="$R/$(entry "$F" compose_path)"
before="$(cat "$REG" "$F_COMPOSE" "$R/services/litellm/config.yaml" | sha256sum)"
out="$(dep "$F" --reason "Test: restore." --drop-default --keep-gateway)"; code=$?
after="$(cat "$REG" "$F_COMPOSE" "$R/services/litellm/config.yaml" | sha256sum)"
[[ $code -eq 2 && "$out" == *"every file was put back"* && "$before" == "$after" ]] \
  && ok "a failed check restores every file" || fail "restore (rc=$code): $out"
cp "$TMP/emit.bak" "$R/scripts/lib/litellm-emit.sh"
ls "$R"/scripts/lib/profiles/*.deprecate.tmp "$R"/models/*/*/compose/*/*/*.deprecate.tmp >/dev/null 2>&1 \
  && fail "temp files left behind" || ok "no temp files left behind"

[[ $rc -eq 0 ]] && echo "test-deprecate-slug: ok" || echo "test-deprecate-slug: FAILED"
exit $rc
