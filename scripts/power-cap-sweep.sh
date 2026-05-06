#!/usr/bin/env bash
# power-cap-sweep.sh — Power-cap A/B sweep for cross-rig efficiency-knee data
#
# Why this exists:
#   3090 sweet spot is ~330W (5% TPS loss vs ~388W stock for ~15% power
#   reduction — see @syangsao's three-cap data on issue #58). For other GPU
#   classes (4090, 5090, A5000, A6000, modded variants) the knee differs and
#   has to be measured. This script automates the sweep so contributors can
#   produce comparable cross-rig numbers without hand-editing nvidia-smi
#   commands and bench invocations.
#
# Usage:
#   sudo bash scripts/power-cap-sweep.sh                          # comprehensive sweep at 10W increments (matches @laurimyllari resolution)
#   sudo bash scripts/power-cap-sweep.sh --step-size 20           # coarser sweep (~half runtime)
#   sudo bash scripts/power-cap-sweep.sh --caps 260,280,300       # explicit caps (overrides auto-derive)
#   sudo bash scripts/power-cap-sweep.sh --gpu 1                  # specific GPU index
#   sudo bash scripts/power-cap-sweep.sh --cooling water          # tag the run as water-cooled
#   sudo bash scripts/power-cap-sweep.sh --cooling air            # tag as air-cooled
#   sudo bash scripts/power-cap-sweep.sh --cooling aio            # tag as AIO/closed-loop
#   sudo bash scripts/power-cap-sweep.sh --no-reset               # leave at last cap (you reset manually)
#
# Default sweep behavior:
#   Without --caps, the script reads power.min_limit and power.max_limit and
#   generates caps at 10W increments across the entire envelope. This matches
#   @laurimyllari's reference resolution that produced the cleanest 4090 curve.
#
#   Each cap runs a reduced bench (WARMUPS=1 RUNS=2 with 500/400 max_tokens),
#   targeting ~30s/cap of sustained load — enough for the power sampler to
#   collect 50+ under-load samples for a stable median. Per-card estimates:
#
#     3090 (100-388W) →  30 caps  ~15 min
#     4090 (150-450W) →  31 caps  ~16 min
#     5090 (250-575W) →  33 caps  ~17 min
#     A5000 (100-230W) → 14 caps  ~7 min
#
#   At heavily-throttled caps (e.g. 100W on a 3090), bench runs slower and
#   the per-cap time can stretch to ~50-60s, so total runtime is ~20 min on
#   a typical sweep. For zooming into a known-good region, use --caps
#   260,280,300 explicitly. For coarser sweeps, --step-size 20.
#
# Output:
#   - Per-cap bench logs at /tmp/power-cap-N{wattage}.log
#   - Markdown summary at /tmp/power-cap-summary.md (paste into GitHub issue/discussion)
#
# Requires sudo for `nvidia-smi -pl`. Auto-detects running container + URL +
# MODEL via the same logic as bench.sh.
#
# Why --cooling matters:
#   Air-cooled cards thermal-throttle around 80-83°C, capping effective
#   sustained power at ~310-340W on a 3090 regardless of the software cap.
#   Water-cooled / AIO cards hold lower temps (~50-65°C) and sustain full
#   board power. Same software cap on different cooling produces different
#   real curves — recording the cooling class is essential for cross-rig
#   comparison. The script does NOT auto-detect this; you must specify.

set -euo pipefail

# Defaults — override via flags
GPU_INDEX=0
CAPS=""              # empty → auto-derive from card's min/max power limits at STEP_SIZE granularity
RESET=1              # 1 = reset to stock at end; 0 = leave at last cap
COOLING="unspecified" # air|water|aio|unspecified — affects how to read the data
STEP_SIZE=10          # increment in W between caps when --caps not specified (10W matches @laurimyllari's resolution)

while [ $# -gt 0 ]; do
  case "$1" in
    --gpu)        GPU_INDEX="$2"; shift 2 ;;
    --caps)       CAPS="$2"; shift 2 ;;
    --cooling)    COOLING="$2"; shift 2 ;;
    --step-size)  STEP_SIZE="$2"; shift 2 ;;
    --no-reset)   RESET=0; shift ;;
    -h|--help)
      sed -n '1,/^set -euo/p' "$0" | grep '^#' | sed 's/^# \?//'
      exit 0 ;;
    *)            echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

# Validate --cooling value
case "$COOLING" in
  air|water|aio|unspecified) ;;
  *) echo "[error] --cooling must be one of: air, water, aio (or omit for 'unspecified')" >&2; exit 1 ;;
esac

if [ "$COOLING" = "unspecified" ]; then
  echo "[warn] --cooling not specified. Cooling class is essential context for interpreting"
  echo "[warn] the efficiency knee (air-cooled cards thermal-throttle, water-cooled don't)."
  echo "[warn] Consider re-running with: --cooling air|water|aio"
  echo
fi

# Sanity checks
if [ "$EUID" -ne 0 ]; then
  echo "[error] must run as root (nvidia-smi -pl requires sudo)" >&2
  echo "[hint]  rerun with: sudo bash scripts/power-cap-sweep.sh ..." >&2
  exit 1
fi

if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "[error] nvidia-smi not found in PATH" >&2; exit 1
fi

# Determine paths — script may be invoked from anywhere
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BENCH="$REPO_ROOT/scripts/bench.sh"
if [ ! -x "$BENCH" ]; then
  echo "[error] expected $BENCH" >&2; exit 1
fi

# Auto-detect URL/CONTAINER/MODEL from the running engine.
# This must happen BEFORE we exec bench.sh under our sudo context — bench.sh's
# own autodetect doesn't reliably fire when re-invoked under sudo (env vars
# get stripped, defaults kick in, wrong MODEL → HTTP 404 against the server).
if [ -z "${CONTAINER:-}" ] || [ -z "${URL:-}" ]; then
  if [[ -f "$REPO_ROOT/scripts/preflight.sh" ]]; then
    # shellcheck source=preflight.sh
    source "$REPO_ROOT/scripts/preflight.sh"
    preflight_autodetect_endpoint || true
  fi
fi

# preflight_autodetect_endpoint only sets URL + CONTAINER, not MODEL.
# Query the live /v1/models endpoint to derive the served model name.
if [ -z "${MODEL:-}" ] && [ -n "${URL:-}" ]; then
  MODEL=$(curl -sf --max-time 5 "${URL}/v1/models" 2>/dev/null \
    | python3 -c "import sys, json; print(json.load(sys.stdin)['data'][0]['id'])" 2>/dev/null || echo "")
fi

# CONTAINER is OPTIONAL — host engine builds (e.g. llama.cpp host server, see
# club-3090#85, #87) have no container. URL + MODEL are the only hard
# requirements. If CONTAINER is unset we mark it "none" for display, which is
# also the value bench.sh expects to skip its docker-log scrape cleanly.
if [ -z "${CONTAINER:-}" ]; then
  CONTAINER="none"
fi

if [ -z "${URL:-}" ] || [ -z "${MODEL:-}" ]; then
  echo "[error] could not auto-detect a running URL + MODEL." >&2
  echo "[hint]  start a model server first (bash scripts/switch.sh <variant>)" >&2
  echo "[hint]  or pass URL=http://... MODEL=name as env vars" >&2
  echo "[hint]  CONTAINER is optional — set CONTAINER=none for host builds" >&2
  echo "[got]   URL='${URL:-}' CONTAINER='${CONTAINER:-}' MODEL='${MODEL:-}'" >&2
  exit 1
fi
export URL CONTAINER MODEL
echo "[setup] target:   container=$CONTAINER url=$URL model=$MODEL"

# Capture card's power envelope (so we can reset cleanly + auto-derive sweep range)
STOCK_TDP=$(nvidia-smi --query-gpu=power.default_limit --format=csv,noheader,nounits -i "$GPU_INDEX" | head -1 | tr -d ' ')
MIN_LIMIT=$(nvidia-smi --query-gpu=power.min_limit     --format=csv,noheader,nounits -i "$GPU_INDEX" | head -1 | tr -d ' ')
MAX_LIMIT=$(nvidia-smi --query-gpu=power.max_limit     --format=csv,noheader,nounits -i "$GPU_INDEX" | head -1 | tr -d ' ')
GPU_NAME=$(nvidia-smi --query-gpu=name                  --format=csv,noheader            -i "$GPU_INDEX" | head -1)
GPU_VRAM=$(nvidia-smi --query-gpu=memory.total          --format=csv,noheader,nounits     -i "$GPU_INDEX" | head -1 | tr -d ' ')

# If --caps not specified, derive a sweep at STEP_SIZE-W increments across the
# card's operating range. 10W default matches @laurimyllari's reference
# resolution that produced the cleanest 4090 curve. Works on any card class:
#   3090 (100-388W) →  30 caps  (~60 min runtime at 2 min/cap)
#   4090 (150-450W) →  31 caps  (~62 min runtime)
#   5090 (250-575W) →  33 caps  (~66 min runtime)
#   A5000 (100-230W) → 14 caps  (~30 min runtime)
# For a quicker first-look use --step-size 20 (cuts runtime in half) or
# --caps 260,280,300 (zoom into a known-good region).
if [ -z "$CAPS" ]; then
  CAPS=$(python3 -c "
min_l = int(float('${MIN_LIMIT%.*}'))
max_l = int(float('${MAX_LIMIT%.*}'))
step = max(1, int('${STEP_SIZE}'))
# Round min UP to nearest step boundary, max DOWN — keeps caps clean multiples of step.
start = ((min_l + step - 1) // step) * step
end   = (max_l // step) * step
caps = list(range(start, end + 1, step))
# Always include the exact max_limit at the end if rounding clipped it (so we
# capture the stock-or-near anchor).
if caps[-1] != max_l:
    caps.append(max_l)
print(','.join(str(c) for c in caps))
")
  AUTO_DERIVED=1
else
  AUTO_DERIVED=0
fi
NUM_CAPS=$(echo "$CAPS" | tr ',' '\n' | wc -l | tr -d ' ')
# ~30s/cap including settle + bench (1 warmup + 2 runs × 500+400 tokens).
EST_MIN=$(( (NUM_CAPS * 30 + 59) / 60 ))

echo "[setup] GPU $GPU_INDEX: $GPU_NAME ($GPU_VRAM MiB)"
echo "[setup] power envelope: ${MIN_LIMIT}W (min) → ${STOCK_TDP}W (default) → ${MAX_LIMIT}W (max)"
echo "[setup] cooling:   $COOLING"
if [ "$AUTO_DERIVED" -eq 1 ]; then
  echo "[setup] sweep caps: $NUM_CAPS caps in ${STEP_SIZE}W increments (override via --caps or --step-size)"
  echo "[setup]            $CAPS W"
else
  echo "[setup] sweep caps: $NUM_CAPS caps (user-specified)"
  echo "[setup]            $CAPS W"
fi
echo "[setup] estimated runtime: ~${EST_MIN} min (${NUM_CAPS} caps × ~30s/cap; reduced bench WARMUPS=1 RUNS=2)"
echo "[setup] reset at end: $([ $RESET -eq 1 ] && echo yes || echo no)"
echo

# Persistence mode (one-time; idempotent)
nvidia-smi -pm 1 -i "$GPU_INDEX" >/dev/null 2>&1 || true

# Sweep
RESULTS_FILE=/tmp/power-cap-summary.md
{
  echo "# Power-cap sweep — $GPU_NAME (GPU $GPU_INDEX)"
  echo ""
  echo "**GPU:** $GPU_NAME &nbsp; **VRAM:** ${GPU_VRAM} MiB &nbsp; **Stock TDP:** ${STOCK_TDP}W &nbsp; **Cooling:** ${COOLING}"
  echo "**Model:** \`${MODEL}\` &nbsp; **Engine:** \`${CONTAINER}\` &nbsp; **Endpoint:** ${URL}"
  echo "**Date:** $(date -u +%Y-%m-%dT%H:%M:%S)Z"
  echo ""
  if [ "$COOLING" = "unspecified" ]; then
    echo "> ⚠️  Cooling class not specified at run time. Add **air / water / AIO** when posting"
    echo "> this data — water-cooled cards sustain full board power; air-cooled thermal-throttle"
    echo "> at ~80-83 °C and may cap below the software limit regardless of \`-pl\` setting."
    echo ""
  fi
  echo "> Cross-rig comparisons require **matching model + engine class** — TPS scales with"
  echo "> model size and quant (e.g. Qwen3.6-27B-AutoRound at 30 TPS, Gemma-4-31B-AutoRound +"
  echo "> MTP at 100 TPS). The *shape* of the efficiency knee is the cross-rig signal; absolute"
  echo "> numbers only compare like-to-like."
  echo ""
  echo "| Cap (W) | Narr wall TPS | Code wall TPS | Actual power (W) | GPU temp (°C) | TPS/W (narr) |"
  echo "|--------:|--------------:|--------------:|-----------------:|--------------:|-------------:|"
} > "$RESULTS_FILE"

IFS=',' read -ra CAP_ARRAY <<< "$CAPS"
for CAP in "${CAP_ARRAY[@]}"; do
  CAP=$(echo "$CAP" | tr -d ' ')
  echo "================================================"
  echo "=== Cap: ${CAP}W (GPU $GPU_INDEX) ==="
  echo "================================================"

  # Apply cap
  if ! nvidia-smi -pl "$CAP" -i "$GPU_INDEX" 2>&1 | tail -1; then
    echo "[warn] failed to set ${CAP}W — skipping"
    continue
  fi

  # Verify cap applied
  ACTUAL_LIMIT=$(nvidia-smi --query-gpu=power.limit --format=csv,noheader,nounits -i "$GPU_INDEX" | head -1 | tr -d ' ')
  echo "[verify] limit set to: ${ACTUAL_LIMIT}W"
  echo

  # Brief settle (let driver re-clock)
  sleep 3

  # Start background power-draw sampler at 0.5s intervals.
  # Capturing under-load power requires sampling DURING bench runs — bench.sh's
  # final "GPU state" line samples after all runs complete and may catch the
  # card mid-idle (~40W) instead of under load (~330W). The sampler writes
  # CSV: index, utilization%, power.draw_W, temp_C — we post-process for
  # median power across samples where utilization > 50% (under-load median).
  SAMPLE_FILE="/tmp/power-cap-N${CAP}-samples.csv"
  (
    while true; do
      nvidia-smi --query-gpu=index,utilization.gpu,power.draw,temperature.gpu \
        --format=csv,noheader,nounits -i "$GPU_INDEX" 2>/dev/null | head -1
      sleep 0.5
    done
  ) > "$SAMPLE_FILE" &
  SAMPLER_PID=$!
  trap "kill $SAMPLER_PID 2>/dev/null || true" EXIT

  # Run bench at reduced precision for sweep efficiency.
  # Canonical bench.sh uses WARMUPS=3 RUNS=5 + 1000/800 max_tokens =
  # 8 × (1000+800) tokens = ~14k tokens × ~10ms/token = ~2 min/cap.
  # For sweep purposes we don't need ±0.5% TPS precision — we need the curve
  # shape and stable under-load power readings. With WARMUPS=1 RUNS=2 +
  # 500/400 max_tokens = 3 × 900 tokens = 2,700 tokens → ~25-35s/cap on a
  # mid-range card, ~50s on a heavily-power-starved cap. That's enough
  # sustained load for the sampler to collect 50+ under-load samples for
  # a stable median. TPS std/CV will be higher (n=2) but the knee position
  # is unaffected, which is what the sweep is for.
  LOG_FILE="/tmp/power-cap-N${CAP}.log"
  echo "[bench] running bench.sh @ ${CAP}W cap (output: $LOG_FILE; sampling power)"
  if ! WARMUPS=1 RUNS=2 MAX_TOKENS_NARR=500 MAX_TOKENS_CODE=400 \
       bash "$BENCH" 2>&1 | tee "$LOG_FILE" | tail -8; then
    kill $SAMPLER_PID 2>/dev/null || true
    echo "[warn] bench.sh failed at ${CAP}W"
    continue
  fi
  kill $SAMPLER_PID 2>/dev/null || true
  trap - EXIT
  echo

  # Extract bench TPS metrics from the log
  NARR_TPS=$(grep -A1 "summary \[narrative\]" "$LOG_FILE" | grep "wall_TPS" | head -1 | grep -oE 'mean= *[0-9]+\.[0-9]+' | head -1 | grep -oE '[0-9]+\.[0-9]+' || echo "?")
  CODE_TPS=$(grep -A1 "summary \[code\]"      "$LOG_FILE" | grep "wall_TPS" | head -1 | grep -oE 'mean= *[0-9]+\.[0-9]+' | head -1 | grep -oE '[0-9]+\.[0-9]+' || echo "?")

  # Compute median under-load power and peak temp from the sampler.
  # Filter to samples where GPU utilization > 50% (i.e. actively decoding).
  # Falls back to bench.sh's GPU-state line if sampler captured no under-load
  # samples (rare; only happens if bench.sh failed silently or finished before
  # the sampler took its first reading).
  if [ -s "$SAMPLE_FILE" ]; then
    UNDER_LOAD_STATS=$(python3 -c "
import sys
samples = []
with open('$SAMPLE_FILE') as f:
    for line in f:
        try:
            idx, util, power, temp = [x.strip() for x in line.strip().split(',')]
            if int(util) > 50:
                samples.append((float(power), int(temp)))
        except Exception:
            continue
if not samples:
    print('? ?')
else:
    powers = sorted(s[0] for s in samples)
    temps  = [s[1] for s in samples]
    median_power = powers[len(powers)//2]
    peak_temp    = max(temps)
    print(f'{median_power:.2f} {peak_temp}')
" 2>/dev/null || echo "? ?")
    ACTUAL_POWER=$(echo "$UNDER_LOAD_STATS" | awk '{print $1}')
    GPU_TEMP=$(echo "$UNDER_LOAD_STATS"      | awk '{print $2}')
  else
    ACTUAL_POWER="?"; GPU_TEMP="?"
  fi

  # Fallback to bench.sh GPU-state line if sampler returned ?
  if [ "$ACTUAL_POWER" = "?" ]; then
    GPU_STATE_LINE=$(grep -A2 "GPU state" "$LOG_FILE" | grep ",$GPU_INDEX," | head -1 || grep -A2 "GPU state" "$LOG_FILE" | grep "^${GPU_INDEX}," | head -1 || echo "")
    ACTUAL_POWER=$(echo "$GPU_STATE_LINE" | awk -F', ' '{print $5}' | grep -oE '[0-9]+\.?[0-9]*' | head -1 || echo "?")
    GPU_TEMP=$(echo "$GPU_STATE_LINE"     | awk -F', ' '{print $6}' | tr -d ' ' || echo "?")
  fi

  # TPS/W efficiency calc (if both numeric)
  if [[ "$NARR_TPS" =~ ^[0-9]+\.[0-9]+$ && "$ACTUAL_POWER" =~ ^[0-9]+\.?[0-9]*$ && "$ACTUAL_POWER" != "0" ]]; then
    EFFICIENCY=$(awk "BEGIN{printf \"%.3f\", $NARR_TPS / $ACTUAL_POWER}")
  else
    EFFICIENCY="?"
  fi

  printf "[result] %sW cap → %s narr / %s code TPS @ %sW actual draw, %s°C, eff %s TPS/W\n\n" \
    "$CAP" "$NARR_TPS" "$CODE_TPS" "$ACTUAL_POWER" "$GPU_TEMP" "$EFFICIENCY"

  printf "| %s | %s | %s | %s | %s | %s |\n" \
    "$CAP" "$NARR_TPS" "$CODE_TPS" "$ACTUAL_POWER" "$GPU_TEMP" "$EFFICIENCY" \
    >> "$RESULTS_FILE"
done

# Reset
if [ "$RESET" -eq 1 ]; then
  echo "[reset] restoring GPU $GPU_INDEX to stock TDP (${STOCK_TDP}W)"
  nvidia-smi -pl "$STOCK_TDP" -i "$GPU_INDEX" 2>&1 | tail -1
else
  echo "[reset] --no-reset specified; GPU $GPU_INDEX left at last cap"
fi

# Append context to results file
{
  echo ""
  echo "**Reset:** $([ $RESET -eq 1 ] && echo "auto-reset to ${STOCK_TDP}W stock" || echo "left at last cap (--no-reset)")"
  echo ""
  echo "**Notes:**"
  echo "- Each row: 1 warm + 2 measured runs of canonical narr (500-tok essay) + code (400-tok quicksort)."
  echo "- Actual power = **median** of 0.5s samples taken DURING bench where util > 50% (i.e. under-load)."
  echo "- GPU temp = **peak** during workload (not a single post-bench point sample)."
  echo "- TPS/W efficiency lets you spot the knee — typically the highest cap before efficiency drops."
  echo "- If actual power < cap consistently, the workload is **compute-saturated** for this hardware:"
  echo "  the card can't use the extra power because it's not the bottleneck (smaller models on bigger"
  echo "  GPUs commonly land here). The lowest cap where TPS plateaus is the real efficiency setting."
  echo "- **Cooling class affects interpretation:** air-cooled cards thermal-throttle at ~80-83 °C, capping"
  echo "  effective sustained power below the software limit. Water-cooled / AIO cards stay at lower temps"
  echo "  and sustain the full software cap. Cross-rig comparisons should match cooling class for fairness."
} >> "$RESULTS_FILE"

echo
echo "================================================"
echo "Sweep complete. Summary at: $RESULTS_FILE"
echo "Raw bench logs at: /tmp/power-cap-N*.log"
echo "================================================"
echo
cat "$RESULTS_FILE"
