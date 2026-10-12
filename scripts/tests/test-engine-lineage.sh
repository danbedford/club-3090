#!/usr/bin/env bash
#
# Guard: every curated engine profile says where it comes from, and our tooling
# can classify it.
#
# docs/ADDING_ENGINES.md ("CONTRACT — engine lineage"): an engine declares
# `lineage:` (relation stock | patched | derivative | fork | new, its base, and
# the public source and commit it was built from). Lineage decides what the
# engine inherits from our tooling, so it is data, not prose in `notes:`.
#
#   1. every core profile declares `lineage`
#   2. anything that isn't stock names a public `source` and a `source_ref`;
#      the few that can't yet are listed in NO_SOURCE_REF, which may only shrink
#   3. a non-new engine's `base` is the same family as its `type`
#   4. every core engine id resolves to a family in scripts/lib/engine-kind.sh
#   5. the loader rejects a malformed `lineage` (negative controls)
#
# Core profiles only: the local layer (profiles-local/engines.d) differs on every
# rig, and local engines are allowed to predate the field.
set -uo pipefail
export CLUB3090_CONFIG_DIR=/nonexistent/club-3090-test-config   # tests never read your real settings (#1466)
export PYTHONUTF8="${PYTHONUTF8:-1}"
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
NAME="test-engine-lineage"

python3 - "$ROOT" <<'PY' || { echo "FAIL: $NAME" >&2; exit 1; }
import sys, subprocess, tempfile
from pathlib import Path

import yaml

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from scripts.lib.profiles import compat  # noqa: E402

# Non-stock engines with no verifiable source commit yet. May only SHRINK: an
# entry that gains a `source_ref` must be removed from here.
NO_SOURCE_REF = {
    # LMCache's own image; the v0.4.7-cu129 tag was re-pushed and the image
    # carries no revision label, so no commit can be claimed.
    "vllm-lmcache",
}
TYPE_FAMILY = {"vllm": "vllm", "sglang": "sglang", "exllamav3": "exllamav3",
               "llama.cpp": "llamacpp", "llama-cpp": "llamacpp", "llamacpp": "llamacpp"}

fails = []
profiles = []
for path in sorted((root / "scripts/lib/profiles/engines").glob("*.yml")):
    profiles.append((path, compat._engine(yaml.safe_load(path.read_text(encoding="utf-8")), path)))

for path, e in profiles:
    lin = e.lineage
    if lin is None:
        fails.append(f"{path.name}: no `lineage:` block")
        continue
    rel = lin["relation"]
    if rel != "stock":
        if not lin.get("source"):
            fails.append(f"{e.id}: relation {rel} needs a public `source`")
        if not lin.get("source_ref") and e.id not in NO_SOURCE_REF:
            fails.append(f"{e.id}: relation {rel} needs a `source_ref` (commit or release tag)")
    if e.id in NO_SOURCE_REF and lin.get("source_ref"):
        fails.append(f"{e.id}: has a source_ref now; remove it from NO_SOURCE_REF")
    if rel != "new":
        base_family = TYPE_FAMILY.get((lin.get("base") or "").lower())
        type_family = TYPE_FAMILY.get(e.type.lower())
        if base_family is None or base_family != type_family:
            fails.append(f"{e.id}: lineage.base {lin.get('base')!r} is not the family of type {e.type!r}")
print(f"  checked lineage on {len(profiles)} core engine profiles")

ids = [e.id for _, e in profiles]
out = subprocess.run(
    ["bash", "-c", 'source "$1/scripts/lib/engine-kind.sh"; shift; for i in "$@"; do echo "$i $(engine_kind_from_engine_id "$i")"; done',
     "_", str(root), *ids],
    capture_output=True, text=True, encoding="utf-8", check=False)
for line in out.stdout.splitlines():
    eid, kind = line.rsplit(" ", 1)
    if kind == "unknown":
        fails.append(f"{eid}: engine-kind.sh resolves it to unknown")
if len(out.stdout.splitlines()) != len(ids):
    fails.append(f"engine-kind.sh answered for {len(out.stdout.splitlines())} of {len(ids)} ids: {out.stderr.strip()}")

# Negative controls: the loader must refuse these, loudly.
bad = {
    "unknown relation": {"relation": "vendored", "base": "vllm"},
    "numeric base_version": {"relation": "stock", "base": "vllm", "base_version": 0.31},
    "unknown key": {"relation": "stock", "base": "vllm", "sauce": "x"},
    "chain not a list": {"relation": "fork", "base": "llama.cpp", "chain": "llama.cpp"},
}
import logging
logging.disable(logging.CRITICAL)   # the loader logs each refusal; they're expected here
with tempfile.TemporaryDirectory() as tmp:
    for label, lineage in bad.items():
        doc = {"schema_version": 1, "id": "neg", "display_name": "neg", "type": "vllm",
               "stability": "experimental", "min_sm": 7.5, "lineage": lineage}
        p = Path(tmp) / "neg.yml"
        p.write_text(yaml.safe_dump(doc), encoding="utf-8")
        try:
            compat._engine(doc, p)
        except compat.ProfileError:
            continue
        fails.append(f"loader accepted a lineage with {label}")
logging.disable(logging.NOTSET)

if fails:
    for f in fails:
        print(f"FAIL: {f}", file=sys.stderr)
    sys.exit(1)
print("  every core engine declares lineage; non-stock ones name source and commit; bases match types")
print("  every core engine id resolves to a family; malformed lineage is refused")
PY
echo "PASS: $NAME"
