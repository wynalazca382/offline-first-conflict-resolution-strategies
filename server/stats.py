"""
Testy statystyczne do rozdziału 5 (uruchamiać z katalogu server/ po run_all_experiments.py):
    python stats.py [--results ../results] [--out ../results/stats]
Wymaga: numpy, scipy  (pip install scipy)

Liczy:
- statystyki opisowe czasu merge (mediana, Q1, Q3, IQR) dla każdej grupy,
- test Shapiro-Wilka normalności w każdej grupie,
- test Manna-Whitneya U dla każdej pary strategii (dwustronny) z poprawką Bonferroniego
  (6 par na scenariusz/wariant) i wielkością efektu r = |z| / sqrt(n1 + n2),
- dopasowanie modeli skalowania (liniowy, kwadratowy, potęgowy, logarytmiczny)
  do mediany czasu jako funkcji N (S3) i K (S4): R^2 i MSE.
"""
import argparse, csv, itertools, json, math, os
import numpy as np
from scipy import stats

STRATS = ["LWW", "VectorClockLWW", "ORSet", "Automerge"]

def load(d, sub, name):
    p = os.path.join(d, sub, name)
    with open(p, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]

def groups(rows, key=None):
    g = {}
    for r in rows:
        if r.get("sync_success", 1) == 0:
            continue
        k = (r.get(key) if key else None, r["strategy"])
        g.setdefault(k, []).append(r["merge_duration_us"])
    return g

def describe(x):
    q1, med, q3 = np.percentile(x, [25, 50, 75])
    return med, q1, q3, q3 - q1

def mw(a, b):
    res = stats.mannwhitneyu(a, b, alternative="two-sided", method="auto")
    n1, n2 = len(a), len(b)
    mu = n1 * n2 / 2
    sigma = math.sqrt(n1 * n2 * (n1 + n2 + 1) / 12)
    z = (res.statistic - mu) / sigma if sigma else 0.0
    return res.statistic, res.pvalue, abs(z) / math.sqrt(n1 + n2)

def fit_models(xs, ys):
    xs, ys = np.asarray(xs, float), np.asarray(ys, float)
    out = {}
    def score(pred):
        ss_res = float(np.sum((ys - pred) ** 2)); ss_tot = float(np.sum((ys - ys.mean()) ** 2))
        return 1 - ss_res / ss_tot if ss_tot else float("nan"), ss_res / len(ys)
    c = np.polyfit(xs, ys, 1); out["liniowy"] = (*score(np.polyval(c, xs)), c.tolist())
    c = np.polyfit(xs, ys, 2); out["kwadratowy"] = (*score(np.polyval(c, xs)), c.tolist())
    c = np.polyfit(np.log(xs), np.log(ys), 1); out["potęgowy"] = (*score(np.exp(c[1]) * xs ** c[0]), [math.exp(c[1]), c[0]])
    c = np.polyfit(np.log(xs), ys, 1); out["logarytmiczny"] = (*score(np.polyval(c, np.log(xs))), c.tolist())
    return out

def main():
    ap = argparse.ArgumentParser()
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--results", default=os.path.join(here, "..", "results"))
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    out = a.out or os.path.join(a.results, "stats")
    os.makedirs(out, exist_ok=True)

    sets = [
        ("S1", load(a.results, "S1", "s1_results.jsonl"), "variant"),
        ("S2", load(a.results, "S2", "s2_results.jsonl"), None),
        ("S3", load(a.results, "S3", "s3_results.jsonl"), "n_operations"),
        ("S4", load(a.results, "S4", "s4_results.jsonl"), "k_devices"),
        ("S5", load(a.results, "S5", "s5_results.jsonl"), "profile"),
    ]
    desc_rows, mw_rows, fit_rows = [], [], []
    for sc, rows, key in sets:
        g = groups(rows, key)
        params = sorted({k[0] for k in g}, key=lambda v: (str(type(v)), v))
        for p in params:
            for s in STRATS:
                x = g.get((p, s))
                if not x:
                    continue
                med, q1, q3, iqr = describe(x)
                sw_p = stats.shapiro(x).pvalue if len(x) >= 3 and np.ptp(x) > 0 else float("nan")
                desc_rows.append([sc, p, s, len(x), med, q1, q3, iqr, sw_p])
            pairs = list(itertools.combinations(STRATS, 2))
            for s1, s2 in pairs:
                x1, x2 = g.get((p, s1)), g.get((p, s2))
                if not x1 or not x2:
                    continue
                U, pv, r = mw(x1, x2)
                mw_rows.append([sc, p, s1, s2, U, pv, min(1.0, pv * len(pairs)), r])
        if sc in ("S3", "S4"):
            for s in STRATS:
                xs = [p for p in params if (p, s) in g]
                ys = [np.median(g[(p, s)]) for p in xs]
                for model, (r2, mse, coef) in fit_models(xs, ys).items():
                    fit_rows.append([sc, s, model, r2, mse, json.dumps(coef)])

    def w(name, head, rows):
        with open(os.path.join(out, name), "w", newline="", encoding="utf-8") as f:
            cw = csv.writer(f); cw.writerow(head); cw.writerows(rows)
        print("  ✓", os.path.normpath(os.path.join(out, name)), f"({len(rows)} wierszy)")

    w("opisowe.csv", ["scenariusz", "parametr", "strategia", "n", "mediana_us", "q1", "q3", "iqr", "shapiro_p"], desc_rows)
    w("mann_whitney.csv", ["scenariusz", "parametr", "strategia_1", "strategia_2", "U", "p", "p_bonferroni", "r"], mw_rows)
    w("skalowanie.csv", ["scenariusz", "strategia", "model", "R2", "MSE", "wspolczynniki"], fit_rows)

    print("\nS1 concurrent — Mann-Whitney U (czas merge):")
    for r in mw_rows:
        if r[0] == "S1" and r[1] == "concurrent":
            print(f"  {r[2]:>15} vs {r[3]:<15} U={r[4]:7.1f}  p={r[5]:.2e}  p_bonf={r[6]:.2e}  r={r[7]:.2f}")

if __name__ == "__main__":
    main()
