"""Monte Carlo validation of Orca's core algorithms.

Runs thousands of randomized trials over the ACTUAL Orca modules (not a
re-implementation) and reports error envelopes, detection/false-alarm rates and
confidence bands, so the performance claims can be independently checked. Writes
validation/REPORT.md and charts in validation/figs/.

Run:  python -m validation.montecarlo [--trials N] [--seed S]
Deterministic for a given seed.
"""
from __future__ import annotations

import argparse
import math
import time
from pathlib import Path

import numpy as np

from orca.adapters.skylark_gnss import GnssMonitor, GnssSample
from orca.df import C, aoa_fix, en_to_ll, ll_to_en, tdoa_fix
from orca.intel.classify import Classifier
from orca.intel.synth import CLASSES, make
from orca.mesh import RadioTransport
from orca import tropo

OUT = Path(__file__).parent
FIGS = OUT / "figs"


def pct(a, q):
    return float(np.percentile(a, q))


# ----------------------------------------------------------------- DF accuracy
def df_accuracy(trials, rng):
    """Random emitter + 3-5 nodes in a ~40 km box; sweep bearing/timing noise.
    Reports CEP50/CEP95 (the radius containing 50%/95% of fixes)."""
    aoa_rows, tdoa_rows = [], []
    for sigma_deg in (0.5, 1.0, 2.0, 5.0):
        errs = []
        for _ in range(trials):
            lat0, lon0 = 13.1, -59.6
            nodes = [(lat0 + rng.uniform(-0.15, 0.15), lon0 + rng.uniform(-0.15, 0.15))
                     for _ in range(rng.integers(3, 6))]
            elat, elon = lat0 + rng.uniform(-0.2, 0.2), lon0 + rng.uniform(-0.2, 0.2)
            ex, ey = ll_to_en(elat, elon, lat0, lon0)
            reps = []
            for la, lo in nodes:
                x, y = ll_to_en(la, lo, lat0, lon0)
                true_brg = math.degrees(math.atan2(ex - x, ey - y)) % 360
                reps.append({"lat": la, "lon": lo,
                             "bearing_deg": true_brg + rng.normal(0, sigma_deg)})
            f = aoa_fix(reps)
            if f:
                fx, fy = ll_to_en(f.lat, f.lon, lat0, lon0)
                errs.append(math.hypot(fx - ex, fy - ey))
        aoa_rows.append((sigma_deg, np.array(errs)))

    for sigma_ns in (10, 30, 100, 300):
        errs = []
        for _ in range(trials):
            lat0, lon0 = 13.1, -59.6
            nodes = [(lat0 + rng.uniform(-0.15, 0.15), lon0 + rng.uniform(-0.15, 0.15))
                     for _ in range(rng.integers(4, 7))]
            elat, elon = lat0 + rng.uniform(-0.2, 0.2), lon0 + rng.uniform(-0.2, 0.2)
            ex, ey = ll_to_en(elat, elon, lat0, lon0)
            reps = []
            for la, lo in nodes:
                x, y = ll_to_en(la, lo, lat0, lon0)
                t = math.hypot(x - ex, y - ey) / C + rng.normal(0, sigma_ns * 1e-9)
                reps.append({"lat": la, "lon": lo, "t": t})
            f = tdoa_fix(reps)
            if f:
                fx, fy = ll_to_en(f.lat, f.lon, lat0, lon0)
                errs.append(math.hypot(fx - ex, fy - ey))
        tdoa_rows.append((sigma_ns, np.array(errs)))
    return aoa_rows, tdoa_rows


# ----------------------------------------------- GNSS jamming detector ROC
def gnss_detector(trials, rng):
    """Pd (probability of detection) and Pfa (false-alarm rate) for the GNSS
    jamming detector over randomized clean and jammed runs."""
    def run(jam_drop):
        hits = 0
        for _ in range(trials):
            m = GnssMonitor("N")
            base = rng.uniform(40, 47)
            for i in range(14):                      # learn a baseline on clean sky
                m.feed(GnssSample(time.time() + i, [rng.normal(base, 3) for _ in range(rng.integers(8, 12))],
                                  13.1, -59.6, 9))
            cn0 = [max(0, rng.normal(base - jam_drop, 3)) for _ in range(rng.integers(6, 11))]
            used = sum(1 for c in cn0 if c > 28)
            evs = m.feed(GnssSample(time.time() + 20, cn0, 13.1, -59.6, used))
            hits += any(e.cls == "rf.gnss.jamming" for e in evs)
        return hits / trials
    pfa = run(0.0)                                    # no real drop -> any hit is a false alarm
    pd = {drop: run(drop) for drop in (8, 12, 16, 20)}
    return pd, pfa


# ----------------------------------------------- modulation classifier vs SNR
def classifier_robustness(clf, trials, rng):
    rate = 2e6
    rows = []
    for extra_noise in (0.0, 0.05, 0.1, 0.2, 0.4):
        ok = n = 0
        for c in CLASSES:
            for _ in range(trials):
                iq = make(c, 4096, rate, rng)
                iq = iq + (rng.standard_normal(len(iq)) + 1j * rng.standard_normal(len(iq))) * extra_noise
                ok += clf.classify(iq.astype(np.complex64), rate)["top"] == c
                n += 1
        rows.append((extra_noise, ok / n))
    # confusion at the nominal level
    cm = np.zeros((len(CLASSES), len(CLASSES)), int)
    for ci, c in enumerate(CLASSES):
        for _ in range(trials):
            pred = clf.classify(make(c, 4096, rate, rng), rate)["top"]
            cm[ci][CLASSES.index(pred)] += 1
    return rows, cm


# ----------------------------------------------- troposcatter availability
def tropo_availability(trials, rng):
    rows = []
    cases = [("Barbados-St Lucia 2 m FT8", 144.2e6, 150, 100, 13, "ft8"),
             ("Barbados-St Vincent 70 cm FT8", 432.2e6, 190, 75, 16, "ft8"),
             ("Barbados-Grenada 2 m SSB", 144.2e6, 340, 100, 15, "ssb"),
             ("Long path 2 m SSB 500 km", 144.2e6, 500, 100, 15, "ssb")]
    for name, f, d, pw, g, mode in cases:
        b = tropo.link_budget(f, d, pw, g, g, 40, 40, mode)
        thr = tropo.MODES[mode]["min_snr"]
        avails = []
        for _ in range(max(50, trials // 20)):
            s = np.array([tropo.sim_fading_snr(b.snr_db, i * 2.0, rng) for i in range(120)])
            avails.append(float(np.mean(s >= thr)) * 100)
        rows.append((name, b.verdict, round(b.snr_db, 1), np.array(avails)))
    return rows


# ----------------------------------------------- mesh store-and-forward value
def mesh_delivery(trials, rng):
    """Delivery fraction over a fading link, with vs without store-and-forward.
    Without it, a message sent while the path is faded is lost; with it, the
    message is held and delivered at the next opening."""
    class W:
        def write(self, d): pass

    def sim(use_queue):
        delivered = total = 0
        for _ in range(max(50, trials // 10)):
            rt = RadioTransport(min_quality=0.15, queue_max=100000)
            rt._writer = W()
            q = 0.5
            for _ in range(200):
                q = min(1.0, max(0.0, q + rng.normal(0, 0.25)))      # fading quality
                rt.link_quality = q
                if use_queue:
                    rt.flush()
                rt.send(b"m")
                total += 1
                if not use_queue:
                    rt._queue.clear()           # no store-and-forward: faded sends are lost
            if use_queue:
                rt.link_quality = 1.0
                rt.flush()                      # a final opening clears the backlog
            delivered += rt.sent
        return delivered / max(1, total)
    return sim(False), sim(True)


# ----------------------------------------------------------------- report
def bar(ax, labels, vals, title, ylabel, color="#0a6c86"):
    ax.bar(range(len(labels)), vals, color=color)
    ax.set_xticks(range(len(labels))); ax.set_xticklabels(labels, rotation=0, fontsize=8)
    ax.set_title(title, fontsize=10); ax.set_ylabel(ylabel, fontsize=8)
    ax.grid(axis="y", alpha=0.3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    rng = np.random.default_rng(a.seed)
    FIGS.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    print(f"Monte Carlo validation: {a.trials} trials/case, seed {a.seed}")

    aoa, tdoa = df_accuracy(a.trials, rng)
    print("  DF done")
    pd, pfa = gnss_detector(max(300, a.trials // 4), rng)
    print("  GNSS detector done")
    clf = Classifier.load_or_train(OUT / "modrec.json")
    crows, cm = classifier_robustness(clf, max(40, a.trials // 20), rng)
    print("  classifier done")
    trows = tropo_availability(a.trials, rng)
    print("  troposcatter done")
    md_no, md_yes = mesh_delivery(a.trials, rng)
    print("  mesh done")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 3, figsize=(13, 3.4))
    bar(axes[0], [f"{s}°" for s, _ in aoa], [pct(e, 95) for _, e in aoa],
        "DF (AoA) 95% error vs bearing noise", "metres")
    bar(axes[1], [f"{s} ns" for s, _ in tdoa], [pct(e, 95) for _, e in tdoa],
        "DF (TDOA) 95% error vs timing noise", "metres")
    bar(axes[2], [f"+{n}" for n, _ in crows], [r * 100 for _, r in crows],
        "Classifier accuracy vs added noise", "%", color="#1a7442")
    fig.tight_layout(); fig.savefig(FIGS / "summary.png", dpi=110); plt.close(fig)

    def band(e):
        return f"{np.median(e):.0f} m median, {pct(e,95):.0f} m @95%"

    L = []
    L.append("# Orca — Monte Carlo validation report\n")
    L.append(f"Generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())} · "
             f"{a.trials} trials per case · seed {a.seed} · {time.time()-t0:.0f}s\n")
    L.append("All trials run over the actual Orca modules (`orca.df`, `orca.tropo`, "
             "`orca.intel`, `orca.adapters`, `orca.mesh`). Figures in `figs/`.\n")
    L.append("![summary](figs/summary.png)\n")

    L.append("## Direction finding — error envelope\n")
    L.append("Random emitter and 3–6 nodes in a ~40 km area; Gaussian measurement noise.\n")
    L.append("| Method | Noise | Median | CEP95 (95% within) |")
    L.append("|---|---|---|---|")
    for s, e in aoa:
        L.append(f"| AoA | {s}° bearing | {np.median(e):.0f} m | {pct(e,95):.0f} m |")
    for s, e in tdoa:
        L.append(f"| TDOA | {s} ns timing | {np.median(e):.0f} m | {pct(e,95):.0f} m |")
    L.append("")

    L.append("## GNSS jamming detector — detection vs false alarm\n")
    L.append(f"False-alarm rate on clean sky: **{pfa*100:.1f}%**.\n")
    L.append("| C/N0 drop | Probability of detection |")
    L.append("|---|---|")
    for drop, p in pd.items():
        L.append(f"| {drop} dB | {p*100:.0f}% |")
    L.append("")

    L.append("## Modulation classifier — robustness to noise\n")
    L.append("| Added noise σ | Accuracy |")
    L.append("|---|---|")
    for nz, r in crows:
        L.append(f"| +{nz} | {r*100:.0f}% |")
    L.append("\nConfusion matrix (rows = truth) at the nominal level:\n")
    L.append("| truth \\ pred | " + " | ".join(CLASSES) + " |")
    L.append("|" + "---|" * (len(CLASSES) + 1))
    for i, c in enumerate(CLASSES):
        row = cm[i] / max(1, cm[i].sum())
        L.append(f"| {c} | " + " | ".join(f"{row[j]*100:.0f}" for j in range(len(CLASSES))) + " |")
    L.append("")

    L.append("## Troposcatter — availability (Monte Carlo fading)\n")
    L.append("| Path | Predicted | Median SNR | Availability (median, 10th pct) |")
    L.append("|---|---|---|---|")
    for name, verdict, snr, av in trows:
        L.append(f"| {name} | {verdict} | {snr} dB | {np.median(av):.0f}%, {pct(av,10):.0f}% |")
    L.append("")

    L.append("## Mesh — value of store-and-forward on a fading link\n")
    L.append(f"Delivery fraction over a randomly fading link:\n")
    L.append(f"- Without store-and-forward: **{md_no*100:.0f}%**")
    L.append(f"- With store-and-forward: **{md_yes*100:.0f}%**\n")

    L.append("## Method and limits\n")
    L.append("- Monte Carlo over the real implementation, fixed seed, reproducible.\n"
             "- Noise and fading are modelled; this is algorithm validation, not a "
             "hardware-in-the-loop or field trial. Absolute numbers will shift on real "
             "receivers and real propagation.\n"
             "- The troposcatter link budget is a calibrated amateur estimate (OZ1RH), "
             "not an ITU-R P.617 certification.\n")

    (OUT / "REPORT.md").write_text("\n".join(L))
    print(f"Wrote {OUT/'REPORT.md'} and {FIGS/'summary.png'} in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
