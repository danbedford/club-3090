#!/usr/bin/env bash
# deprecate-slug — mark a CURATED slug 🗑️ Deprecated everywhere it has to change:
# the compose header (Status + Caveats), its registry.yaml entry, its defaults row
# (--repoint to a functional sibling or --drop-default) and its gateway route.
# Then checks the catalog still loads and puts every file back if it doesn't.
#
# Maintainer-only (writes the tracked catalog): needs C3_ALLOW_CORE_PROMOTE=1.
# Local slugs are yours, not the catalog's: `catalog.sh unregister` removes those.
#
# Usage:
#   scripts/deprecate-slug.sh <slug> --reason "<why: the failure, or what supersedes it>"
#       [--repoint <sibling> | --drop-default] [--keep-gateway | --drop-gateway]
#       [--with-aliases] [--dry-run]
#
#   --with-aliases  also deprecate the slugs that run the same compose (its header
#                   has one Status line, so they can't differ)
#   --dry-run       print the plan and the diff, write nothing (no gate needed)
#
# It lists what it can't change for you: hand-written mentions of the slug
# (launcher hints, docs, tests), the docs/UPSTREAM.md row, the per-model
# CHANGELOG entry, and the guards to run.
#
# Exit: 0 done / dry-run / already deprecated; 3 refused, nothing written;
#       2 a check failed after writing, every file restored.
set -euo pipefail

# Force Python's UTF-8 mode (PEP 540) for every python3 this script runs.
# Repo sources are full of unicode (— × → ⚠), and without this a rig on a real
# non-UTF-8 locale (de_DE.iso88591 and friends) decodes reads, stdout AND argv
# with the locale codec, which crashes the launcher/emit paths (#779). Python
# already auto-enables UTF-8 mode for the C/POSIX locale, so this covers the
# case it does NOT: a genuine non-UTF-8, non-C locale. Exported, so child
# processes and nested scripts inherit it. Guarded by test-locale-utf8.sh.
export PYTHONUTF8="${PYTHONUTF8:-1}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
case "${1:-}" in
  -h|--help) command grep -E '^#( |$)' "${BASH_SOURCE[0]}" | cut -c3-; exit 0 ;;
  "") command grep -E '^#( |$)' "${BASH_SOURCE[0]}" | cut -c3- >&2; exit 3 ;;
esac
branch="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
if [[ "$branch" == "master" || "$branch" == "main" ]]; then
  echo "[deprecate] note: you are on $branch — a status change goes in by PR, so branch first." >&2
fi
exec python3 "$ROOT/scripts/lib/profiles/deprecate_slug.py" --root "$ROOT" "$@"
