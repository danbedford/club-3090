# vLLM PR #40914 K+1-only overlay — manually rebased onto post-#41434 main

Vendored 2026-05-11 to unblock TQ3 + MTP on Qwen 3.6 27B (`dual/int8-tq3.yml`)
with proper spec-verify routing.

## Source

- Upstream PR: <https://github.com/vllm-project/vllm/pull/40914>
  ("[Bugfix][Spec-Decode] TurboQuant K+1 spec-verify routing (fixes #40880)")
- Upstream head SHA: `0ee9b859bbb2bbb6e33a461e7fd1fee1fa4792cc` (2026-04-29)
- State at vendor time: OPEN, MERGEABLE.

## Why this is a manual rebase

PR #40914 was forked from main BEFORE merged PR #41434 ("Eliminate
GPU↔CPU syncs in attention impls", merged 2026-05-08). Its full
`turboquant_attn.py` overlay would REVERT #41434's `query_start_loc_cpu`
/ `seq_lens_cpu` CPU-resident-metadata fixes and reintroduce the
`.tolist()` cudagraph crash family.

We need ONLY the additive K+1 spec-verify dispatch block (~86 lines)
inserted between `num_decode_tokens = attn_metadata.num_decode_tokens`
and `if not attn_metadata.is_prefill:`. All other diff hunks from PR
#40914 are reverts of #41434 and must be skipped.

## What this overlay contains

`turboquant_attn.py` = current `origin/main` (post-#41434) + ONLY the
additive K+1 dispatch block from PR #40914 inserted at the right location.

Result:
- `query_start_loc_cpu` / `seq_lens_cpu` fields and accessors: PRESERVED
- `.tolist()` → CPU-resident metadata fix: PRESERVED
- K+1 spec-verify dispatch from #40914: ADDED
- `buf_holder=layer` call site: matches nightly's `triton_turboquant_decode.py`
  signature (which post-#41434 accepts that kwarg)

## What this fixes

Bimodal MTP acceptance + "first word right then breaks" needle failure
on TQ3 + MTP. Symptom on the previous stack (PR #40798 partial only):

```
12:22:02  AL=3.73  accept 91.1%  per-position [0.962, 0.930, 0.841]  ← GOOD
12:22:32  AL=2.30  accept 43.4%  per-position [1.000, 0.303, 0.000]  ← BAD
12:23:42  AL=2.00  accept 33.2%  per-position [0.397, 0.306, 0.294]  ← VERY BAD
```

Verify-stress needle 10K/30K/60K/90K all failed with `expected 'golden
chinchilla 38' got 'golden '` pattern — first token recalled, then
attention diverged because the verify pass was attending only to
current-chunk K/V instead of prior cached compressed K/V.

This dispatch routes uniform K+1 spec-verify batches through the decode
kernel (which natively reads prior cached K/V), restoring correctness.

## Drop trigger

```
gh api repos/vllm-project/vllm/pulls/40914 --jq '.state, .merged_at'
```

reports `MERGED` AND the merge commit is post-#41434 (i.e., the PR was
rebased before merge so it doesn't revert #41434).

## Verified on

- vLLM nightly: `1acd67a7` (includes merged #41434)
- Compose: `dual/int8-tq3.yml`
- Qwen 3.6 27B AutoRound INT4, TQ3 KV, MTP n=3, 262K × 2 streams
- 2026-05-11
