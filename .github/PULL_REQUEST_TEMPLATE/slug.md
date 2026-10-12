<!--
Adding a slug: one model, on one engine, in one topology, as one compose plus its registry entry.
A new engine needs an engine proposal first, then ?template=engine.md.
Which case this is, and what each case needs: docs/ADDING_MODELS.md → "Which case is this?".
Why each gate exists: CONTRIBUTING.md → "Submitting a new compose variant".
Tick what applies, delete the sections that don't, and explain any N/A.
-->

## Slug

- **Slug(s):** `<prefix>/<name>`
- **Model:** `<model id>`
- **Engine:** `<engine id>`, registered in `scripts/lib/profiles/engines/` (or depends on engine PR #)
- **Weights variant / topology / KV:**
- **Status:** `🐣` / `🧪` / `⚠️` / `✅` (a `Caveats:` line unless 🧪 or ✅)

**This PR adds** (tick one):
- [ ] **Case 1:** a new slug for a model and engine we already pair
- [ ] **Case 2:** a model we serve, on an engine that doesn't serve it yet. Model's existing slug(s): `<slug>`
- [ ] **Case 3:** a model we don't have. Model proposal (approved): #

## Every case

- [ ] Compose at `models/<model>/<engine-dir>/compose/<topology>/<quant>/<serving>.yml`, with the `Profile (at-a-glance)` header and the `ESTATE_*` hooks
- [ ] `weights:` entry (plus companions) for anything new
- [ ] Registry entry; `default_port` equals the compose's `${PORT:-NNNN}`; no `gateway` or `DEFAULTS` change unless that's the point of the PR
- [ ] `bash scripts/preflight-add-model.sh <slug>` green
- [ ] The catalog rules pass, network checks included: `python3 scripts/lib/profiles/catalog_rules.py --slug <slug> --network --strict`, output under **Export check**
- [ ] Users can set it up: `bash scripts/setup.sh <slug>` downloads the weights and every companion from Hugging Face on a machine that doesn't have them, and the engine image pulls without credentials (`DOCKER_CONFIG=$(mktemp -d) docker buildx imagetools inspect <image>`). A gated repo is named as such, with its license, in the status note
- [ ] CPU-offload (moe-cache) slug: gates M1–M11 in ADDING_MODELS.md, with the pool census in the status note

## Case 2: a model we serve, on a new engine for it

- [ ] The engine profile covers the model: `family`, weights `format`, KV format, drafter `spec_method`
- [ ] Compose copied from the nearest validated compose *for this engine*, not the model's compose for another engine
- [ ] Same behaviour as the model's other slugs, or the difference is in `Caveats:`:
  - [ ] chat template (registry `chat_template`, and `patches.yml` `load_bearing_when` if it's ours)
  - [ ] default reasoning effort and thinking budgets, and a request's own `reasoning_effort` is honoured (#1439)
  - [ ] `sampler_profiles`
  - [ ] tool-call parser and served model name
- [ ] 8-pack at the same reasoning effort as the model's existing slug; both results quoted

## Case 3: a model we don't have

- [ ] `scripts/lib/profiles/models/<id>.yml` per the ModelProfile contract (architecture facts from `config.json` and the model card)
- [ ] The family added to each engine profile its slugs use
- [ ] kv-calc support (or `kv_calc_supported: false` and `kvcalc_key: SKIP`); calibration anchors for vLLM-type slugs
- [ ] `CLUB3090_CHECK_HF_REPOS=1 bash scripts/tests/test-hf-repos-resolve.sh` passes
- [ ] Starts at 🐣 Incubating; `models/<id>/CHANGELOG.md` created; a BENCHMARKS section
- [ ] One model in this PR

## Export check

<!-- From your local layer, `export_pr.py --network` fills this section (docs/BRING_YOUR_OWN.md §5).
Otherwise paste `python3 scripts/lib/profiles/catalog_rules.py --slug <slug> --network` for each slug. -->

## Evidence

- [ ] Booted with `bash scripts/switch.sh <slug>` from this branch, not a hand-run `docker run`. Image: `docker inspect --format '{{.Image}}' <container>` →
- [ ] `verify-full.sh` PASS · `verify-stress.sh` 7/7 · `SOAK_MODE=continuous` soak (required for single-card)
- [ ] `bench.sh` (3 warm + 5 measured), with the power cap stated
- [ ] Quality: the wrapper's line pasted verbatim, with the reasoning effort it ran at
- [ ] BENCHMARKS row; `models/<model>/CHANGELOG.md` entry
- [ ] Full `scripts/tests` sweep: `N/N` (any failure baselined against master)

`bash scripts/rebench-full.sh --with-8pack-thinking=both` covers the verify, stress, soak, bench and quality items in one run.

## N/A justifications

<!-- e.g. "N/A — dual-card only, so the single-card soak gate doesn't apply" -->
