#!/usr/bin/env python3
"""deprecate-slug — mark a CURATED slug 🗑️ Deprecated, every place at once.

    bash scripts/deprecate-slug.sh <slug> --reason "<why — the failure, not 'old'>"
        [--repoint <sibling> | --drop-default] [--keep-gateway | --drop-gateway]
        [--with-aliases] [--dry-run] [--root DIR]

Maintainer-only: it writes the tracked catalog, so it needs C3_ALLOW_CORE_PROMOTE=1
(exported, or saved with settings.sh, exactly as `promote.py --layer core` reads
it). --dry-run needs nothing and writes nothing.

What it changes, together:

  compose header   Status: 🗑️ Deprecated, and Caveats: <reason>. The old Caveats
                   field is replaced (it described a config people were running;
                   git keeps it).
  registry.yaml    status: deprecated, and a status_note that starts with the
                   date and the reason (an earlier note is kept after it). The
                   entry and its hardware fields stay: deprecated is not deleted.
  defaults:        a slug that is a defaults target needs a decision. --repoint
                   to a functional sibling (same model, engine and topology), or
                   --drop-default so the resolver says "pick explicitly". Left
                   alone, <engine>/default would hand users a dead config.
  aliases          slugs that run the same compose share its one Status line, so
                   they go together: --with-aliases, or a refusal naming them.
  gateway          a gateway=True slug needs a decision: --keep-gateway leaves the
                   route up (annotated `# status: deprecated`), --drop-gateway
                   removes it. The generated block of services/litellm/config.yaml
                   is regenerated when it changes.

Then it checks the result: registry.yaml is canonical, the merged registry loads,
the header parses back as deprecated, and litellm-emit --check is green. If any
of that fails, every file goes back as it was.

What it cannot do, it lists: hand-written mentions of the slug (launcher hints,
docs, tests), the docs/UPSTREAM.md row, the per-model CHANGELOG entry, and the
guards to run. Local slugs are refused: `catalog.sh unregister` removes those.

Exit: 0 done / dry-run / already deprecated · 3 refused, nothing written ·
2 a check failed after writing, everything restored.
"""

from __future__ import annotations

import argparse
import datetime
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

ROOT_DEFAULT = Path(__file__).resolve().parents[3]
REGISTRY_REL = "scripts/lib/profiles/registry.yaml"
LITELLM_REL = "services/litellm/config.yaml"
GATE_ENV = "C3_ALLOW_CORE_PROMOTE"
EXIT_REFUSED, EXIT_INTERNAL = 3, 2

# A schema field line: `#` + 1-4 spaces + `Key:`. Continuation lines are indented
# deeper or don't look like a key, so a field never swallows the next one.
_KEY = re.compile(r"^#(\s{1,4})([A-Z][A-Za-z0-9 ._()/-]*?):(\s|$)")


class Refusal(Exception):
    """Nothing written → exit 3."""


class HeaderError(Refusal):
    """The compose has no profile header this tool can rewrite safely."""


def _info(msg: str = "") -> None:
    print(f"[deprecate] {msg}" if msg else "")


# ── compose header ────────────────────────────────────────────────────────────

def _schema_span(lines: list[str]) -> Optional[tuple[int, int]]:
    """[start, end) of the `# Profile (at-a-glance):` block, bounded the way
    compose_header_status reads it (up to the `# --` separator)."""
    start = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if start is None:
            if s.startswith("# Profile (at-a-glance):"):
                start = i + 1
            continue
        if s.startswith("# --") or s.startswith("#--"):
            return start, i
    return None if start is None else (start, len(lines))


def _fields(lines: list[str], start: int, end: int) -> list[tuple[str, int, int]]:
    """(name, first line, end line) per field; the range covers its continuations."""
    out = []
    i = start
    while i < end:
        m = _KEY.match(lines[i])
        if not m:
            i += 1
            continue
        j = i + 1
        while (j < end and lines[j].startswith("#") and lines[j].lstrip("#").strip()
               and not _KEY.match(lines[j])):
            j += 1
        out.append((m.group(2), i, j))
        i = j
    return out


def rewrite_header(text: str, reason: str, status_token: str) -> str:
    """The compose text with Status → `status_token` and Caveats → `reason`.

    Pure: the test runs it over every curated compose. Raises HeaderError when
    there is no profile block or not exactly one Status field in it."""
    lines = text.splitlines(keepends=True)
    span = _schema_span(lines)
    if span is None:
        raise HeaderError("the compose has no `# Profile (at-a-glance):` header block")
    fields = _fields(lines, *span)
    status = [f for f in fields if f[0] == "Status"]
    if len(status) != 1:
        raise HeaderError(f"the profile header has {len(status)} Status fields, expected 1")
    _, s0, s1 = status[0]
    head = re.match(r"^(#\s*)Status:\s*", lines[s0])
    indent, col = head.group(1), len(head.group(0))
    eol = "\r\n" if lines[s0].endswith("\r\n") else "\n"
    status_line = f"{head.group(0)}{status_token}{eol}"
    label = f"{indent}Caveats:"
    caveats_line = f"{label}{' ' * max(1, col - len(label))}{reason}{eol}"
    caveats = [f for f in fields if f[0] == "Caveats"]
    if caveats:
        _, c0, c1 = caveats[0]
        edits = [(s0, s1, [status_line]), (c0, c1, [caveats_line])]
    else:
        edits = [(s0, s1, [status_line, caveats_line])]
    out = lines[:]
    for a, b, repl in sorted(edits, reverse=True):
        out[a:b] = repl
    return "".join(out)


# ── registry ──────────────────────────────────────────────────────────────────

def _topology(compose_path: str) -> Optional[str]:
    if "/compose/" not in compose_path:
        return None
    return compose_path.split("/compose/", 1)[1].split("/", 1)[0]


def _default_keys(defaults: dict, slug: str) -> list[tuple[str, str, str]]:
    return [(m, e, t) for m, engines in defaults.items() for e, topos in engines.items()
            for t, s in topos.items() if s == slug]


def _siblings(entries: dict, key: tuple[str, str, str], exclude: set, functional) -> list[str]:
    """Functional slugs that could take over a defaults row: same model, engine
    and topology, and not one of the slugs on the compose being deprecated."""
    model, engine, topo = key
    return sorted(k for k, e in entries.items()
                  if k not in exclude and e.get("model") == model and k.split("/", 1)[0] == engine
                  and _topology(e.get("compose_path", "")) == topo
                  and e.get("status") in functional)


def _lookup(defaults: dict, key: tuple[str, str, str]) -> str:
    return defaults[key[0]][key[1]][key[2]]


def _drop_default(defaults: dict, key: tuple[str, str, str]) -> None:
    model, engine, topo = key
    del defaults[model][engine][topo]
    if not defaults[model][engine]:
        del defaults[model][engine]
    if not defaults[model]:
        del defaults[model]


# ── what it can't do: mentions to look at by hand ─────────────────────────────

_TEXT_SUFFIXES = {".sh", ".py", ".md", ".yml", ".yaml", ".json", ".txt", ".toml", ".tcss"}
_SKIP_DIRS = {".venv", "__pycache__", "node_modules", "profiles-local", ".git", ".pytest_cache"}


def mentions(root: Path, slug: str, skip: set[str]) -> dict[str, list[str]]:
    """Hand-written mentions, grouped: scripts/tools (launcher hints and the
    like), tests, docs, and models/ (sibling compose header tables, model
    READMEs). Generated or rewritten files are in `skip`; CHANGELOGs are history."""
    pat = re.compile(r"(?<![\w/.-])" + re.escape(slug) + r"(?![\w-]|\.\w)")
    groups: dict[str, list[str]] = {"code": [], "tests": [], "docs": [], "models": []}
    bases = [root / "scripts", root / "tools", root / "docs", root / "models", root / "README.md"]
    for base in bases:
        files = [base] if base.is_file() else sorted(base.rglob("*")) if base.is_dir() else []
        for f in files:
            rel = f.relative_to(root).as_posix()
            if (not f.is_file() or f.suffix not in _TEXT_SUFFIXES or rel in skip
                    or f.name == "CHANGELOG.md"
                    or _SKIP_DIRS.intersection(f.relative_to(root).parts)):
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if slug not in text:
                continue
            group = ("tests" if rel.startswith("scripts/tests/") or "/tests/" in rel
                     else "models" if rel.startswith("models/")
                     else "docs" if rel.startswith("docs/") or rel.endswith(".md") else "code")
            for n, line in enumerate(text.splitlines(), 1):
                if pat.search(line):
                    groups[group].append(f"{rel}:{n}: {line.strip()[:110]}")
    return groups


# ── writing, checking, restoring ─────────────────────────────────────────────

def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".deprecate.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    shutil.copymode(path, tmp)
    os.replace(tmp, path)


def _run(root: Path, cmd: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONUTF8=os.environ.get("PYTHONUTF8", "1"))
    env.pop("LITELLM_CONFIG", None)
    env.pop("LITELLM_EMIT_REGISTRY_JSON", None)
    return subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                          encoding="utf-8", env=env)


_CHECK = r"""
import json, sys
from pathlib import Path
from scripts.lib.profiles.compose_registry import (
    RECOMMENDED_DEFAULT_MODELS, compose_header_status, curated_default_target, get_registry)
slugs, topos = json.loads(sys.argv[1]), json.loads(sys.argv[2])
for slug in slugs:
    e = get_registry()[slug]
    if e.get("status") != "deprecated":
        sys.exit(f"{slug}: registry status reads back as {e.get('status')!r}")
h = compose_header_status(Path(e["compose_path"]).read_text(encoding="utf-8"))
if h != "deprecated":
    sys.exit(f"compose header Status reads back as {h!r}")
print(json.dumps({"defaults": {t: curated_default_target(e["model"], t) for t in topos},
                  "recommended": e["model"] in RECOMMENDED_DEFAULT_MODELS}))
"""


def _diff(rel: str, before: str, after: str) -> str:
    return "".join(difflib.unified_diff(before.splitlines(keepends=True),
                                        after.splitlines(keepends=True),
                                        f"a/{rel}", f"b/{rel}", n=1))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="deprecate-slug.sh", description=__doc__.splitlines()[0])
    ap.add_argument("slug")
    ap.add_argument("--reason", required=True,
                    help="one line: why (the failure mechanism or what supersedes it)")
    dflt = ap.add_mutually_exclusive_group()
    dflt.add_argument("--repoint", metavar="SIBLING",
                      help="point its defaults row at this functional sibling")
    dflt.add_argument("--drop-default", action="store_true",
                      help="remove its defaults row (the resolver degrades)")
    gw = ap.add_mutually_exclusive_group()
    gw.add_argument("--keep-gateway", action="store_true",
                    help="keep its gateway route (annotated deprecated)")
    gw.add_argument("--drop-gateway", action="store_true", help="take it off the gateway")
    ap.add_argument("--with-aliases", action="store_true",
                    help="also deprecate the other slugs that run the same compose")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and diff, write nothing")
    ap.add_argument("--root", default=str(ROOT_DEFAULT))
    args = ap.parse_args(argv)
    os.environ.setdefault("PYTHONUTF8", "1")
    root = Path(args.root).resolve()
    sys.path.insert(0, str(root))
    from scripts.lib.profiles.compose_registry import (
        COMPOSE_STATUS_EMOJI, FUNCTIONAL_STATUSES, compose_header_status, dump_registry_yaml,
        get_registry, load_registry_data)
    try:
        return _deprecate(args, root, COMPOSE_STATUS_EMOJI, FUNCTIONAL_STATUSES,
                          compose_header_status, dump_registry_yaml, get_registry,
                          load_registry_data)
    except Refusal as exc:
        print(f"[deprecate] REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED


def _deprecate(args, root, emoji_map, functional, header_status, dump_yaml, get_registry,
               load_data) -> int:
    slug = args.slug
    reason = " ".join(args.reason.split())
    if not reason:
        raise Refusal("--reason is empty — say why: the failure, or what supersedes it")
    token = next(e for e, w in emoji_map.items() if w == "deprecated") + " Deprecated"

    if not args.dry_run:
        from scripts.lib.club_config import load as load_config
        src = load_config(root).get(GATE_ENV)
        if os.environ.get(GATE_ENV) != "1":
            raise Refusal(
                f"this writes the curated catalog, so it is maintainer-gated: set {GATE_ENV}=1 "
                f"(export it, or `bash scripts/settings.sh set {GATE_ENV}=1`). "
                f"--dry-run shows the change without it.")
        if src:
            _info(f"{GATE_ENV} read from {src} (export it in the shell to override)")

    reg_path = root / REGISTRY_REL
    data = load_data(reg_path)
    entries = data["entries"]
    if slug not in entries:
        merged = get_registry(root)
        if slug in merged and (merged[slug] or {}).get("origin") == "local":
            raise Refusal(f"{slug} is a LOCAL slug — it is yours, not the catalog's. "
                          f"`bash scripts/catalog.sh unregister {slug}` removes it.")
        near = difflib.get_close_matches(slug, list(entries), n=3)
        raise Refusal(f"no curated slug {slug!r}" + (f" — did you mean {', '.join(near)}?"
                                                      if near else ""))
    entry = entries[slug]
    compose_rel = entry["compose_path"]
    compose_path = root / compose_rel
    if not compose_path.is_file():
        raise Refusal(f"its compose {compose_rel} does not exist")
    old_compose = compose_path.read_text(encoding="utf-8")

    if entry.get("status") == "deprecated":
        if header_status(old_compose) == "deprecated":
            _info(f"{slug} is already deprecated — nothing to do.")
            return 0
        raise Refusal(f"{slug} is deprecated in registry.yaml but its compose header says "
                      f"{header_status(old_compose)!r} — fix the header by hand "
                      f"(test-compose-status-drift flags it)")

    # aliases: slugs that run this same compose. The header carries ONE Status for
    # all of them, so one can't be deprecated while another stays live.
    aliases = sorted(k for k, e in entries.items()
                     if k != slug and e.get("compose_path") == compose_rel
                     and e.get("status") != "deprecated")
    if aliases and not args.with_aliases:
        raise Refusal(
            f"{compose_rel} is also the compose of {', '.join(aliases)}, and its header "
            f"carries one Status for all of them — deprecating {slug} alone leaves the others "
            f"drifting (test-compose-status-drift). Pass --with-aliases to deprecate them "
            f"together; if only the name should go, remove that registry entry by PR instead.")
    if args.with_aliases and not aliases:
        raise Refusal(f"no other live slug runs {compose_rel} — drop --with-aliases")
    targets = [slug] + aliases
    same_compose = {k for k, e in entries.items() if e.get("compose_path") == compose_rel}

    # defaults: a decision, never a silent dead default
    defaults = data["defaults"]
    keys = [k for t in targets for k in _default_keys(defaults, t)]
    who = " / ".join(targets)
    if keys and not (args.repoint or args.drop_default):
        lines = []
        for key in keys:
            sib = _siblings(entries, key, same_compose, functional)
            lines.append(f"  defaults[{key[0]}][{key[1]}][{key[2]}] = "
                         f"{_lookup(defaults, key)}; functional siblings: "
                         f"{', '.join(sib) if sib else 'none — use --drop-default'}")
        raise Refusal(f"{who} is a defaults target, so <engine>/default would hand users a "
                      f"deprecated config. Pass --repoint <sibling> or --drop-default:\n"
                      + "\n".join(lines))
    if not keys and (args.repoint or args.drop_default):
        raise Refusal(f"{who} is not a defaults target — drop "
                      f"{'--repoint' if args.repoint else '--drop-default'}")
    if args.repoint:
        tgt = entries.get(args.repoint)
        if tgt is None:
            raise Refusal(f"--repoint {args.repoint}: no such curated slug")
        for key in keys:
            ok = _siblings(entries, key, same_compose, functional)
            if args.repoint not in ok:
                raise Refusal(
                    f"--repoint {args.repoint} is not a functional sibling for "
                    f"defaults[{key[0]}][{key[1]}][{key[2]}]: it needs the same model, engine "
                    f"and topology, a status in {sorted(functional)}, and a compose of its "
                    f"own (it is {tgt.get('model')}, {args.repoint.split('/', 1)[0]}, "
                    f"{_topology(tgt.get('compose_path', ''))}, {tgt.get('status')}"
                    f"{', same compose' if args.repoint in same_compose else ''}). "
                    f"Functional siblings: {', '.join(ok) if ok else 'none — use --drop-default'}")

    # gateway: same — a decision
    on_gateway = [t for t in targets if entries[t].get("gateway")]
    if on_gateway and not (args.keep_gateway or args.drop_gateway):
        raise Refusal(f"{', '.join(on_gateway)} is on the LAN gateway (gateway: true). Pass "
                      f"--keep-gateway to leave its route up (annotated `# status: deprecated`) "
                      f"or --drop-gateway to take it off")
    if not on_gateway and (args.keep_gateway or args.drop_gateway):
        raise Refusal(f"{who} is not on the gateway — drop "
                      f"{'--keep-gateway' if args.keep_gateway else '--drop-gateway'}")

    # build the new texts
    new_compose = rewrite_header(old_compose, reason, token)
    if header_status(new_compose) != "deprecated":
        raise HeaderError("the rewritten header does not parse back as deprecated")
    today = datetime.date.today().isoformat()
    was = {}
    for t in targets:
        e = entries[t]
        was[t] = e.get("status")
        old_note = e.get("status_note")
        e["status"] = "deprecated"
        e["status_note"] = (f"🗑️ DEPRECATED {today}: {reason}"
                            + (f" Earlier note: {old_note}" if old_note else ""))
        if args.drop_gateway:
            e["gateway"] = False
    for key in keys:
        if args.repoint:
            defaults[key[0]][key[1]][key[2]] = args.repoint
        else:
            _drop_default(defaults, key)
    old_reg = reg_path.read_text(encoding="utf-8")
    new_reg = dump_yaml(data)

    _info(f"plan for {who} ({entry['model']}, {compose_rel}):")
    for t in targets:
        _info(f"  {t}: status {was[t]} → deprecated")
    _info(f"  Caveats: {reason}")
    for key in keys:
        _info(f"  defaults[{key[0]}][{key[1]}][{key[2]}]: "
              + (f"→ {args.repoint}" if args.repoint else "removed"))
    if on_gateway:
        _info(f"  gateway ({', '.join(on_gateway)}): "
              + ("route kept, annotated deprecated" if args.keep_gateway else "route removed"))
    print(_diff(compose_rel, old_compose, new_compose) + _diff(REGISTRY_REL, old_reg, new_reg),
          end="")
    if args.dry_run:
        _info("dry-run — nothing written." + (
            f" The generated block of {LITELLM_REL} is regenerated on the real run if it "
            f"changes." if on_gateway or keys else ""))
        return 0

    litellm = root / LITELLM_REL
    emit = "scripts/lib/litellm-emit.sh"
    if litellm.is_file() and _run(root, ["bash", emit, "--check", str(root)]).returncode != 0:
        raise Refusal(f"the generated block of {LITELLM_REL} is already out of date "
                      f"(`bash {emit} --check` is red before this change), and regenerating it "
                      f"here would fold that unrelated change into the deprecation. Regenerate "
                      f"it in its own commit first: `bash {emit}`")
    snapshot = {p: p.read_bytes() for p in (compose_path, reg_path, litellm) if p.is_file()}

    def restore(why: str) -> int:
        for p, b in snapshot.items():
            p.write_bytes(b)
        print(f"[deprecate] ERROR: {why}\n[deprecate] every file was put back as it was.",
              file=sys.stderr)
        return EXIT_INTERNAL

    _write_atomic(compose_path, new_compose)
    _write_atomic(reg_path, new_reg)
    chk = _run(root, [sys.executable, "scripts/lib/profiles/migrate_registry_to_yaml.py",
                      "--check"])
    if chk.returncode != 0:
        return restore("registry.yaml is not canonical after the edit:\n"
                       + (chk.stderr or chk.stdout).strip()[-800:])
    topos = sorted({k[2] for k in keys} | {_topology(compose_rel) or "single"})
    chk = _run(root, [sys.executable, "-c", _CHECK, json.dumps(targets), json.dumps(topos)])
    if chk.returncode != 0:
        return restore("the catalog did not read back as intended:\n"
                       + (chk.stderr or chk.stdout).strip()[-800:])
    readback = json.loads(chk.stdout.strip().splitlines()[-1])
    regenerated = False
    if litellm.is_file():
        if _run(root, ["bash", emit, "--check", str(root)]).returncode != 0:
            gen = _run(root, ["bash", emit, str(root)])
            again = _run(root, ["bash", emit, "--check", str(root)])
            if gen.returncode != 0 or again.returncode != 0:
                return restore("the gateway config could not be regenerated:\n"
                               + (gen.stderr or again.stdout or gen.stdout).strip()[-800:])
            regenerated = True

    _info()
    _info(f"wrote {compose_rel}, {REGISTRY_REL}" + (f", {LITELLM_REL} (regenerated block)"
                                                    if regenerated else ""))
    for topo, tgt in readback["defaults"].items():
        _info(f"  {entry['model']} {topo} default now: "
              + (tgt or "none functional — the resolver says pick explicitly"))
        if tgt is None and readback["recommended"]:
            _info(f"  ⚠ {entry['model']} is in recommended_default_models and has no "
                  f"functional {topo} default now")

    skip = {REGISTRY_REL, compose_rel, LITELLM_REL}
    found: dict[str, list[str]] = {"code": [], "tests": [], "docs": [], "models": []}
    for t in targets:
        for group, hits in mentions(root, t, skip).items():
            found[group] += [h for h in hits if h not in found[group]]
    _info()
    _info("by hand — this tool doesn't edit these:")
    if found["code"]:
        _info("  hand-written mentions in scripts/ and tools/ (repoint any suggestion to use it):")
        for m in found["code"][:20]:
            _info(f"    {m}")
        if len(found["code"]) > 20:
            _info(f"    … and {len(found['code']) - 20} more")
    upstream = [m for m in found["docs"] if m.startswith("docs/UPSTREAM.md:")]
    other_docs = [m for m in found["docs"] if not m.startswith("docs/UPSTREAM.md:")]
    if upstream:
        _info("  docs/UPSTREAM.md tracks it — update the row's status:")
        for m in upstream:
            _info(f"    {m}")
    if other_docs:
        _info(f"  docs that name it ({len(other_docs)}): check that none recommends it")
        for m in other_docs[:10]:
            _info(f"    {m}")
        if len(other_docs) > 10:
            _info(f"    … and {len(other_docs) - 10} more")
    if found["models"]:
        _info(f"  under models/ ({len(found['models'])}): sibling compose header tables are kept "
              f"both ways, and a model README may recommend it")
        for m in found["models"][:10]:
            _info(f"    {m}")
        if len(found["models"]) > 10:
            _info(f"    … and {len(found['models']) - 10} more")
    if found["tests"]:
        _info(f"  tests that name it ({len(found['tests'])}); the guards below show which "
              f"still hold")
    if (root / "models" / entry["model"] / "CHANGELOG.md").is_file():
        _info(f"  a dated entry in models/{entry['model']}/CHANGELOG.md (append-only)")
    _info("  run: test-compose-status-drift, test-default-resolver, test-model-default-resolver, "
          "test-litellm-generate, then the full suite (a status change is catalog-shaped)")
    print(f"DEPRECATE_OK {' '.join(targets)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
