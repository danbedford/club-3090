<!--
Adding a new engine to the curated catalog. Start with an engine proposal issue and wait for the
go-ahead: docs/ADDING_ENGINES.md → "Propose first". Each box below maps to a section of that doc.
Tick what applies, and explain N/A where it doesn't.
-->

## Engine

- **Proposal (approved):** #
- **Engine id:** `<family>-<name>`. Profile: `scripts/lib/profiles/engines/<id>.yml`; the filename equals the id
- **Lineage:** relation `stock | patched | derivative | fork | new` · base `<engine> <version>` · source `<repo>@<commit>`
- **Image:** `<ref>@sha256:…`, published by the engine's own maintainer
- **First slugs:** `<prefix>/<slug>`, …, preferably on a model we already serve, so the engine is measured against an existing slug. A model we don't have also needs an approved model proposal: #

## Catalog

- [ ] Engine profile, with every field its slugs need (KV formats, `spec_method`s, weight formats, model families) and `lineage:`
- [ ] Naming: engine id, slug prefix, compose directory, container name and internal port follow "CONTRACT — naming"
- [ ] The image pulls without credentials: `DOCKER_CONFIG=$(mktemp -d) docker buildx imagetools inspect <spec>`
- [ ] `image_env` set, and allowlisted in **both** launchers (`export_variant_engine_pin` in `switch.sh` and `launch.sh`)
- [ ] Hardware profiles' `supported_kv_formats`; for `type: vllm`, the kv-calc tables; `test-profiles-compat` engine count bumped
- [ ] Patches we carry are in `patches.yml` with their upstream status ([PATCH_POLICY.md](https://github.com/noonghunna/club-3090/blob/master/docs/PATCH_POLICY.md))

## Tooling

- [ ] The engine resolves to its family from the id, the container name, the image and `owned_by` (`scripts/lib/engine-kind.sh`; probes added to `test-engine-kind-resolver` for a new family)
- [ ] Found without `URL=`: the family's standard internal port, or every mirrored port set updated (`test-engine-port-set-drift`)
- [ ] On a live boot, `bench.sh` and `health.sh` show throughput and spec-decode acceptance
- [ ] Our launch knobs (`REASONING_EFFORT`, `THINKING_BUDGET_*`, `SPEC_N`, …) are mapped, or listed as unsupported in `Caveats:`; hardware-injection names mapped
- [ ] Chat template, default reasoning effort and thinking budgets match the model's other slugs, or the difference is in `Caveats:`; the registry `chat_template` names what is actually served
- [ ] The gateway can derive the served name (`--served-model-name` / `--alias`, or `served_name=`)

## Composes

- [ ] The conventions in "CONTRACT — composes on a new engine", including the ones no guard checks under a new engine directory: restart knob, cache mounts with the `user:` GID line and `umask 0002`, GPU selection
- [ ] Registry entries for the first slugs ([ADDING_MODELS.md](https://github.com/noonghunna/club-3090/blob/master/docs/ADDING_MODELS.md)); `bash scripts/preflight-add-model.sh <slug>` green for each

## Evidence (per slug)

- [ ] Booted with `bash scripts/switch.sh <slug>` from this branch, not a hand-run `docker run`. Image: `docker inspect --format '{{.Image}}' <container>` →
- [ ] `verify-full.sh` PASS
- [ ] `rebench-full.sh --with-8pack-thinking=both` report attached
- [ ] Quality lines pasted verbatim from the wrapper, with the reasoning effort they ran at
- [ ] Power cap stated for the bench rows
- [ ] Full `scripts/tests` sweep: `N/N` (any failure baselined against master); c3 suite if c3 changed

## Docs

- [ ] `docs/engines/<ENGINE>.md`, plus a row in `docs/engines/README.md` and a link in `docs/README.md`
- [ ] `AGENTS.md` engine-pinning table; `docs/UPSTREAM.md` pinned-images row and upstream section; `docs/NIGHTLY_BUMP_RUNBOOK.md` engine section
- [ ] BENCHMARKS rows; `models/<model>/CHANGELOG.md` entry

## N/A justifications

<!-- e.g. "N/A — no spec-decode on this engine, so no acceptance line to parse" -->
