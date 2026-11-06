"""
Wykresy i tabela podsumowująca z wyników run_all_experiments.py.

    python plot_results.py [--results ../results] [--format png|pdf|svg]

Zapisuje wykresy do <results>/plots/ oraz tabelę <results>/plots/summary.csv.
"""
import argparse
import csv
import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))

STRATEGIES = ["LWW", "VectorClockLWW", "ORSet", "Automerge"]
# Stała kolejność kolorów = stała tożsamość strategii na wszystkich wykresach
COLORS = {"LWW": "#2a78d6", "VectorClockLWW": "#eb6834",
          "ORSet": "#1baf7a", "Automerge": "#eda100"}
MARKERS = {"LWW": "o", "VectorClockLWW": "s", "ORSet": "^", "Automerge": "D"}

SURFACE, INK, INK2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, AXIS = "#e1e0d9", "#c3c2b7"

plt.rcParams.update({
    "font.family": ["Segoe UI", "DejaVu Sans", "sans-serif"],
    "font.size": 10,
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "axes.edgecolor": AXIS, "axes.linewidth": 0.8,
    "axes.labelcolor": INK2, "axes.titlecolor": INK,
    "axes.titlesize": 12, "axes.titleweight": "bold", "axes.titlelocation": "left",
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "axes.grid.axis": "y", "grid.color": GRID, "grid.linewidth": 0.6,
    "axes.axisbelow": True,
    "xtick.color": MUTED, "ytick.color": MUTED,
    "xtick.labelcolor": INK2, "ytick.labelcolor": INK2,
    "legend.frameon": False, "legend.labelcolor": INK2,
})


def load(results_dir, sub, name):
    path = os.path.join(results_dir, sub, name)
    if not os.path.exists(path):
        print(f"  pominięto: brak {path}")
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def by(rows, **filt):
    return [r for r in rows if all(r.get(k) == v for k, v in filt.items())]


def fmt(v):
    if v >= 1000:
        return f"{v / 1000:.1f} ms"
    if v >= 10:
        return f"{v:.0f} µs"
    return f"{v:.1f} µs"


def save(fig, out_dir, name, ext):
    path = os.path.join(out_dir, f"{name}.{ext}")
    fig.savefig(path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"  ✓ {os.path.normpath(path)}")


def legend_top(ax, ncol=4):
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=ncol,
              handlelength=1.2, columnspacing=1.4, borderaxespad=0.3)


def grouped_bars(ax, groups, values, fmt_value, ylabel):
    """values[strategy] = lista wartości dla kolejnych grup."""
    n = len(STRATEGIES)
    width = 0.8 / n
    x = np.arange(len(groups))
    top = max((max(v) for v in values.values() if v), default=1) or 1
    for i, s in enumerate(STRATEGIES):
        xs = x - 0.4 + width * (i + 0.5)
        ax.bar(xs, values[s], width, color=COLORS[s], label=s,
               edgecolor=SURFACE, linewidth=1.5)
        for xi, v in zip(xs, values[s]):
            ax.text(xi, v + top * 0.015, fmt_value(v), ha="center", va="bottom",
                    fontsize=7.5, color=INK2)
    ax.set_xticks(x, groups)
    ax.set_ylabel(ylabel)
    ax.set_ylim(0, top * 1.15)
    ax.tick_params(axis="x", length=0)
    legend_top(ax)


def line_chart(ax, xs, series, xlabel, ylabel, logx=False, logy=True):
    """series[strategy] = (mediany, p25, p75). Etykiety bezpośrednio przy końcach linii."""
    ends = []
    for s in STRATEGIES:
        med, lo, hi = series[s]
        ax.fill_between(xs, lo, hi, color=COLORS[s], alpha=0.12, linewidth=0)
        ax.plot(xs, med, color=COLORS[s], linewidth=2, marker=MARKERS[s],
                markersize=6, markeredgecolor=SURFACE, markeredgewidth=1.2, label=s)
        ends.append([med[-1], s])
    if logx:
        ax.set_xscale("log")
    if logy:
        ax.set_yscale("log")
    ax.set_xticks(xs, [str(x) for x in xs])
    ax.minorticks_off()
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)

    # rozsuń etykiety, żeby się nie nakładały (w przestrzeni osi Y)
    tr = (np.log10 if logy else (lambda v: v))
    inv = ((lambda v: 10 ** v) if logy else (lambda v: v))
    lo_, hi_ = (tr(v) for v in ax.get_ylim())
    gap = (hi_ - lo_) * 0.06
    ends.sort(key=lambda e: e[0])
    pos = []
    for v, s in ends:
        p = tr(v)
        if pos and p - pos[-1] < gap:
            p = pos[-1] + gap
        pos.append(p)
    for (v, s), p in zip(ends, pos):
        ax.text(xs[-1] * (1.08 if logx else 1) + (0 if logx else (xs[-1] - xs[0]) * 0.03),
                inv(p), f"{s}  {fmt(v)}", fontsize=8, color=INK2, va="center", ha="left")
    ax.set_xlim(right=xs[-1] * (1.9 if logx else 1) + (0 if logx else (xs[-1] - xs[0]) * 0.45))
    legend_top(ax)


def stats(rows, key="merge_duration_us"):
    v = np.array([r[key] for r in rows], dtype=float)
    return np.median(v), np.percentile(v, 25), np.percentile(v, 75)


def series_over(rows, param, values):
    out = {}
    for s in STRATEGIES:
        st = [stats(by(rows, strategy=s, **{param: x})) for x in values]
        out[s] = tuple(np.array(c) for c in zip(*st))
    return out


def mean_over(rows, param, values, key):
    return {s: [float(np.mean([r[key] for r in by(rows, strategy=s, **{param: x})]))
                for x in values] for s in STRATEGIES}


# ===== Wykresy per scenariusz =====

def plot_s1(rows, out, ext, summary):
    if not rows:
        return
    variants = ["concurrent", "causal_skew"]
    labels = ["Edycje współbieżne", "Edycja przyczynowa\n(zegar B spóźniony o 5 s)"]
    vals = {s: [100 * np.mean([r["operations_lost"] for r in by(rows, strategy=s, variant=v)])
                for v in variants] for s in STRATEGIES}
    fig, ax = plt.subplots(figsize=(7, 4))
    grouped_bars(ax, labels, vals, lambda v: f"{v:.0f}%", "Powtórzenia z utraconą edycją [%]")
    ax.set_title("S1: Konflikt edycji — utrata danych", pad=28)
    save(fig, out, "s1_utrata", ext)
    for v in variants:
        for s in STRATEGIES:
            add_summary(summary, "S1", f"variant={v}", s, by(rows, strategy=s, variant=v))


def plot_s2(rows, out, ext, summary):
    if not rows:
        return
    fig, ax = plt.subplots(figsize=(7, 4))
    vals = [100 * np.mean([r["edit_wins"] for r in by(rows, strategy=s)]) for s in STRATEGIES]
    ax.bar(STRATEGIES, vals, 0.6, color=[COLORS[s] for s in STRATEGIES],
           edgecolor=SURFACE, linewidth=1.5)
    for i, v in enumerate(vals):
        ax.text(i, v + 1.5, f"{v:.0f}%", ha="center", va="bottom", fontsize=9, color=INK2)
    ax.set_ylim(0, 115)
    ax.set_ylabel("Edycja zachowana [% powtórzeń]")
    ax.tick_params(axis="x", length=0)
    ax.set_title("S2: Współbieżne usunięcie i edycja — kto wygrywa", pad=10)
    save(fig, out, "s2_edit_wins", ext)
    for s in STRATEGIES:
        add_summary(summary, "S2", "", s, by(rows, strategy=s))


def plot_scaling(rows, out, ext, summary, scen, param, xlabel, title, fname):
    if not rows:
        return
    xs = sorted({r[param] for r in rows})
    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    line_chart(ax, xs, series_over(rows, param, xs), xlabel,
               "Czas scalania (mediana, skala log)", logx=(param == "n_operations"))
    ax.set_title(f"{title} — czas scalania", pad=28)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: fmt(v)))
    save(fig, out, f"{fname}_czas", ext)

    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    grouped_bars(ax, [str(x) for x in xs], mean_over(rows, param, xs, "operations_lost"),
                 lambda v: f"{v:.0f}", "Utracone operacje (średnio)")
    ax.set_xlabel(xlabel)
    ax.set_title(f"{title} — utracone operacje", pad=28)
    save(fig, out, f"{fname}_utrata", ext)
    for x in xs:
        for s in STRATEGIES:
            add_summary(summary, scen, f"{param}={x}", s, by(rows, strategy=s, **{param: x}))


def plot_s5(rows, out, ext, summary):
    if not rows:
        return
    profiles = list(dict.fromkeys(r["profile"] for r in rows))
    # sukces i czas synchronizacji nie zależą od strategii — jeden wiersz na powtórzenie
    base = by(rows, strategy=STRATEGIES[0])
    succ = [100 * np.mean([r["sync_success"] for r in by(base, profile=p)]) for p in profiles]
    dur = [np.median([r["sync_duration_ms"] for r in by(base, profile=p) if r["sync_success"]] or [0])
           for p in profiles]
    labels = [p.replace("_", " ", 1).replace("_", " ") for p in profiles]

    fig, axes = plt.subplots(1, 2, figsize=(9, 3.8))
    for ax, vals, ylabel, f, title in (
        (axes[0], succ, "Udane synchronizacje [%]", lambda v: f"{v:.0f}%", "Skuteczność"),
        (axes[1], dur, "Czas synchronizacji, mediana [ms]", lambda v: f"{v:.0f} ms", "Czas"),
    ):
        ax.bar(labels, vals, 0.55, color=COLORS["LWW"], edgecolor=SURFACE, linewidth=1.5)
        top = max(vals) or 1
        for i, v in enumerate(vals):
            ax.text(i, v + top * 0.02, f(v), ha="center", va="bottom", fontsize=9, color=INK2)
        ax.set_ylim(0, top * 1.18)
        ax.set_ylabel(ylabel)
        ax.set_title(title, pad=8, fontsize=11)
        ax.tick_params(axis="x", length=0, labelsize=8.5)
    fig.suptitle("S5: Synchronizacja przez niestabilną sieć (niezależne od strategii)",
                 x=0.01, ha="left", fontsize=12, fontweight="bold", color=INK)
    fig.tight_layout()
    save(fig, out, "s5_siec", ext)
    for p in profiles:
        for s in STRATEGIES:
            add_summary(summary, "S5", f"profile={p}", s,
                        [r for r in by(rows, strategy=s, profile=p) if r["sync_success"]],
                        all_rows=by(rows, strategy=s, profile=p))


# ===== Tabela =====

def add_summary(summary, scen, group, strategy, rows, all_rows=None):
    if not rows and not all_rows:
        return
    d = np.array([r["merge_duration_us"] for r in rows], dtype=float) if rows else np.array([np.nan])
    row = {
        "scenario": scen, "group": group, "strategy": strategy,
        "n": len(all_rows if all_rows is not None else rows),
        "merge_us_median": round(float(np.median(d)), 3),
        "merge_us_p25": round(float(np.percentile(d, 25)), 3),
        "merge_us_p75": round(float(np.percentile(d, 75)), 3),
        "conflicts_mean": round(float(np.mean([r["conflicts_detected"] for r in rows])), 2) if rows else "",
        "lost_mean": round(float(np.mean([r["operations_lost"] for r in rows])), 2) if rows else "",
        "edit_wins_pct": "", "sync_success_pct": "",
    }
    if rows and "edit_wins" in rows[0]:
        row["edit_wins_pct"] = round(100 * float(np.mean([r["edit_wins"] for r in rows])), 1)
    if all_rows is not None:
        row["sync_success_pct"] = round(100 * float(np.mean([r["sync_success"] for r in all_rows])), 1)
    summary.append(row)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=os.path.join(HERE, "..", "results"))
    ap.add_argument("--format", default="png", choices=["png", "pdf", "svg"])
    args = ap.parse_args()
    out = os.path.join(args.results, "plots")
    os.makedirs(out, exist_ok=True)
    print(f"Wykresy → {os.path.normpath(out)}")

    summary = []
    plot_s1(load(args.results, "S1", "s1_results.jsonl"), out, args.format, summary)
    plot_s2(load(args.results, "S2", "s2_results.jsonl"), out, args.format, summary)
    plot_scaling(load(args.results, "S3", "s3_results.jsonl"), out, args.format, summary,
                 "S3", "n_operations", "Liczba operacji offline na urządzenie (N)",
                 "S3: Skalowalność", "s3")
    plot_scaling(load(args.results, "S4", "s4_results.jsonl"), out, args.format, summary,
                 "S4", "k_devices", "Liczba urządzeń (K)", "S4: Wiele urządzeń", "s4")
    plot_s5(load(args.results, "S5", "s5_results.jsonl"), out, args.format, summary)

    if summary:
        path = os.path.join(out, "summary.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(summary[0]), delimiter=";")
            w.writeheader()
            w.writerows(summary)
        print(f"  ✓ {os.path.normpath(path)}")
