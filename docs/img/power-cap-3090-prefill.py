"""Generate 3090 prefill-heavy power-cap efficiency chart from @noonghunna's air-cooled rig.

Source data: 2026-05-07 sweep, 1× 3090 air-cooled (GPU 0).
Engine: mainline llama.cpp + Qwen3.6-27B Q3_K_XL.
Methodology: power-cap-sweep --load-mode prefill-heavy with adaptive prompt
calibration (probe at highest cap, size prompt for ~10s prefill at high cap,
use across sweep). Total wall: 5m36s for 21 caps.

Companion to power-cap-3090-qwen36.png (decode-single curve from the same rig).
The two charts together tell the cross-workload story: same 3090 has different
sweet-spot at 290W for decode vs 250W for prefill.
"""
import matplotlib.pyplot as plt

# (cap_W, prefill_TPS, actual_W, eff_TPS_per_W)
data = [
    (190, 543.18, 189.77, 2.862),
    (200, 607.34, 199.62, 3.042),
    (210, 671.29, 209.68, 3.201),
    (220, 737.69, 219.55, 3.360),
    (230, 795.53, 229.69, 3.463),
    (240, 851.79, 239.61, 3.555),
    (250, 901.07, 249.15, 3.617),  # ⭐ sweet spot
    (260, 930.09, 259.42, 3.585),
    (270, 958.35, 269.58, 3.555),
    (280, 978.23, 279.44, 3.501),
    (290, 996.04, 289.38, 3.442),
    (300, 1010.35, 298.99, 3.379),
    (310, 1021.66, 308.93, 3.307),
    (320, 1035.39, 318.94, 3.246),
    (330, 1041.39, 326.48, 3.190),
    (340, 1043.79, 326.36, 3.198),  # boost-state plateau begins
    (350, 1042.35, 326.43, 3.193),
    (360, 1043.79, 326.46, 3.197),
    (370, 1044.66, 326.63, 3.198),  # stock TDP, plateau holds
    (380, 1071.75, 354.79, 3.021),  # plateau ends, draw jumps
    (390, 1096.88, 381.35, 2.876),  # max — 381W draw at 390W cap
]

caps = [d[0] for d in data]
tps = [d[1] for d in data]
draw = [d[2] for d in data]
eff = [d[3] for d in data]

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.size": 12,
    "axes.titlesize": 16,
    "axes.titleweight": "bold",
    "axes.labelsize": 13,
    "figure.facecolor": "white",
    "axes.facecolor": "white",
})

fig, ax1 = plt.subplots(figsize=(11, 6.4), dpi=150)

# Left axis: prefill TPS
color_tps = "#7b3fa0"
ax1.plot(caps, tps, "o-", color=color_tps, linewidth=2.2, markersize=6,
         label="Prefill TPS (compute-bound)", zorder=3)
ax1.set_xlabel("Power cap (W)", fontsize=13)
ax1.set_ylabel("Prefill TPS (~11K-token prompt + max_tokens=10)", fontsize=13)
ax1.set_xlim(185, 395)
ax1.set_ylim(500, 1130)
ax1.grid(True, alpha=0.3, zorder=0)
ax1.tick_params(axis="both", labelsize=11)

# Right axis: efficiency
ax2 = ax1.twinx()
color_eff = "#d62728"
ax2.plot(caps, eff, "^--", color=color_eff, linewidth=1.8, markersize=5,
         alpha=0.9, label="Efficiency (prefill TPS/W)", zorder=2)
ax2.set_ylabel("Efficiency: prefill TPS/W", color=color_eff, fontsize=13)
ax2.tick_params(axis="y", labelcolor=color_eff, labelsize=11)
ax2.set_ylim(2.7, 3.7)

# Sweet spot annotation: 250W
ax1.axvline(250, color="goldenrod", linestyle=":", alpha=0.5, linewidth=1.5)
ax1.annotate(
    "★ 250W cap\n3.617 TPS/W (best efficiency)\n901 prefill TPS\n68% of stock TDP",
    xy=(250, 901.07),
    xytext=(265, 660),
    fontsize=10.5,
    fontweight="bold",
    bbox=dict(boxstyle="round,pad=0.4", facecolor="#fff3cd", edgecolor="goldenrod", linewidth=1.2),
    arrowprops=dict(arrowstyle="->", color="goldenrod", lw=1.5),
    zorder=4,
)

# Boost-state plateau region (340-370W → all 326W draw)
ax1.axvspan(335, 375, alpha=0.10, color="orange", zorder=0)
ax1.text(355, 525, "boost-state plateau\n(340-370W → ~326W draw)",
         fontsize=9.5, ha="center", color="#aa5500", fontstyle="italic")

# Stock TDP marker
ax1.axvline(370, color="#888", linestyle="--", alpha=0.6, linewidth=1.2)
ax1.annotate(
    "stock TDP\n370W (GPU 0)",
    xy=(370, 1090),
    xytext=(372, 1095),
    fontsize=10,
    ha="left",
    color="#555",
    fontstyle="italic",
)

# Compare with decode sweet spot
ax1.text(295, 540, "(compare: decode-single sweet spot at 290W on same rig)",
         fontsize=9, ha="center", color="#666", fontstyle="italic")

# Title
ax1.set_title(
    "RTX 3090 + Qwen3.6-27B + llama.cpp — prefill-heavy power-cap curve",
    pad=14,
)

# Subtitle
fig.text(
    0.5, 0.92,
    "1× 3090 air-cooled, mainline llama.cpp + Q3_K_XL GGUF, adaptive prompt sizing "
    "(11K tokens calibrated at 390W cap)  |  data: @noonghunna",
    ha="center", fontsize=10, color="#666",
    style="italic",
)

# Combined legend
lines1, labels1 = ax1.get_legend_handles_labels()
lines2, labels2 = ax2.get_legend_handles_labels()
ax1.legend(lines1 + lines2, labels1 + labels2,
           loc="lower right", fontsize=11, framealpha=0.95,
           edgecolor="#ccc")

# Footer
fig.text(
    0.99, 0.01,
    "github.com/noonghunna/club-3090",
    ha="right", fontsize=9, color="#888", style="italic",
)

plt.tight_layout(rect=(0, 0.02, 1, 0.92))

out = "/tmp/power_cap_sweep_3090_prefill.png"
plt.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
print(f"Saved: {out}")
