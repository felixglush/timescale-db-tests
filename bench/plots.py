#!/usr/bin/env python3
"""Charts for EXPERIMENT_LOG.md from results/*.csv -> results/figures/*.png.

  python3 bench/plots.py [results_dir]     (needs matplotlib)
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

RES = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "results"
FIG = RES / "figures"

# Fixed categorical order (validated: adjacent CVD dE >= 9.1, normal-vision dE >= 19.6).
# Three slots are < 3:1 against the surface, so every bar carries a value label.
VARIANTS = ["plain", "plain_clustered", "hyper", "cmp_7d", "cmp_30d", "cmp_90d"]
COLORS = dict(zip(VARIANTS, ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]))
MARKERS = dict(zip(VARIANTS, ["o", "s", "^", "D", "v", "P"]))
QUERIES = {"Q1_client_month": "Q1 · 1 client, 1 month",
           "Q2_client_range": "Q2 · 1 client, 1–12 months",
           "Q3_clients_month": "Q3 · 10 clients, 1 month",
           "Q4_clients_range": "Q4 · 10 clients, 1–12 months"}
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
    "text.color": INK, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
    "axes.axisbelow": True, "axes.spines.top": False, "axes.spines.right": False,
    "font.size": 10, "axes.titlesize": 11, "axes.titleweight": "bold", "legend.frameon": False,
})


def read(name):
    with open(RES / name) as f:
        return list(csv.DictReader(f))


def fmt(v):
    return f"{v:,.0f}" if v >= 100 else f"{v:.1f}" if v >= 1 else f"{v:.2f}"


def bars(ax, values, title, unit):
    xs = range(len(VARIANTS))
    ax.bar(xs, values, width=0.78, color=[COLORS[v] for v in VARIANTS],
           edgecolor=SURFACE, linewidth=2)  # 2px surface gap between neighbours
    for x, val in zip(xs, values):
        ax.annotate(fmt(val), (x, val), xytext=(0, 3), textcoords="offset points",
                    ha="center", va="bottom", fontsize=8.5, color=INK2)
    ax.set_xticks(list(xs), VARIANTS, rotation=30, ha="right")
    ax.set_title(title, loc="left")
    ax.set_ylabel(unit)
    ax.grid(axis="x", visible=False)
    ax.set_ylim(0, max(values) * 1.15)


def save(fig, name):
    FIG.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIG / name, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {FIG / name}")


def main():
    sizes, lat = read("sizes.csv"), read("latency_summary.csv")
    scales = sorted({int(r["clients"]) for r in sizes})
    top = scales[-1]
    S = {(int(r["clients"]), r["variant"]): r for r in sizes}
    L = {(int(r["clients"]), r["variant"], r["query"], r["mode"]): r for r in lat}

    # 1. Storage at the largest scale, stacked heap / toast / indexes.
    fig, ax = plt.subplots(figsize=(7.5, 4))
    xs = range(len(VARIANTS))
    parts = [("table_bytes", "heap", 1.0), ("toast_bytes", "toast", 0.6), ("index_bytes", "indexes", 0.3)]
    bottom = [0.0] * len(VARIANTS)
    for col, label, alpha in parts:
        vals = [int(S[(top, v)][col]) / 2**20 for v in VARIANTS]
        ax.bar(xs, vals, bottom=bottom, width=0.78, color=[COLORS[v] for v in VARIANTS],
               alpha=alpha, edgecolor=SURFACE, linewidth=2, label=label)
        bottom = [b + x for b, x in zip(bottom, vals)]
    plain = int(S[(top, "plain")]["total_bytes"])
    for x, v, tot in zip(xs, VARIANTS, bottom):
        ratio = plain / int(S[(top, v)]["total_bytes"])
        ax.annotate(f"{tot:,.0f} MB\n{ratio:.1f}x", (x, tot), xytext=(0, 3), textcoords="offset points",
                    ha="center", va="bottom", fontsize=8.5, color=INK2)
    ax.set_xticks(list(xs), VARIANTS, rotation=30, ha="right")
    ax.set_ylim(0, max(bottom) * 1.2)
    ax.set_ylabel("MB")
    ax.grid(axis="x", visible=False)
    ax.set_title(f"Storage at {top:,} clients ({int(read('build.csv')[-1]['rows']):,} rows): "
                 "heap (solid) / toast (mid) / indexes (light)", loc="left")
    save(fig, "storage.png")

    # 2+3. Server p50 per query at the largest scale, warm and cold.
    for mode, title in [("cold", "Cold cache"), ("warm", "Warm cache")]:
        fig, axes = plt.subplots(1, 4, figsize=(15, 3.8))
        for ax, (q, qt) in zip(axes, QUERIES.items()):
            bars(ax, [float(L[(top, v, q, mode)]["exec_p50_ms"]) for v in VARIANTS], qt, "ms (p50)")
        fig.suptitle(f"{title}: server execution time at {top:,} clients", x=0.01, ha="left",
                     fontweight="bold")
        fig.tight_layout()
        save(fig, f"latency_{mode}.png")

    # 4. Scaling with client count: cold p50 and cold blocks read, Q2 and Q4.
    fig, axes = plt.subplots(1, 4, figsize=(15, 3.8))
    panels = [("Q2_client_range", "exec_p50_ms", "cold p50 (ms)"),
              ("Q4_clients_range", "exec_p50_ms", "cold p50 (ms)"),
              ("Q2_client_range", "avg_shared_read", "8 KB blocks read"),
              ("Q4_clients_range", "avg_shared_read", "8 KB blocks read")]
    for ax, (q, col, unit) in zip(axes, panels):
        for v in VARIANTS:
            ys = [max(float(L[(c, v, q, "cold")][col]), 0.5) for c in scales]
            ax.plot(scales, ys, color=COLORS[v], marker=MARKERS[v], markersize=6, linewidth=2,
                    markeredgecolor=SURFACE, markeredgewidth=1.5, label=v)
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xticks(scales, [f"{c:,}" for c in scales])
        ax.minorticks_off()
        ax.set_xlabel("clients")
        ax.set_ylabel(unit)
        ax.set_title(f"{QUERIES[q]}: {unit.split(' (')[0]}", loc="left")
    axes[0].legend(loc="upper left", fontsize=8.5)
    fig.suptitle("Cold cache as client count grows (log scales)", x=0.01, ha="left", fontweight="bold")
    fig.tight_layout()
    save(fig, "scaling_cold.png")


if __name__ == "__main__":
    main()
