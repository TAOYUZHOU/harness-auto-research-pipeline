#!/usr/bin/env python3
"""Generate a synthetic TLC-like regression dataset for pipeline demo."""

import csv
import random
import math
from pathlib import Path

random.seed(42)

DEMO_DIR = Path(__file__).parent
N_TRAIN, N_VAL, N_TEST = 500, 100, 100


def synth_rf(h, ea, dcm, meoh, et2o, mw, logp):
    """Fake Rf = f(solvent polarity, molecular properties)."""
    polarity = 0.0 * h + 0.3 * ea + 0.2 * dcm + 0.8 * meoh + 0.1 * et2o
    rf = 0.1 + 0.6 * (1 - math.exp(-0.5 * polarity)) + 0.05 * logp / 5.0
    rf += random.gauss(0, 0.03)
    return max(0.0, min(1.0, rf))


def make_row():
    solvents = [random.random() for _ in range(5)]
    total = sum(solvents) or 1.0
    solvents = [s / total for s in solvents]
    h, ea, dcm, meoh, et2o = solvents
    mw = random.uniform(100, 600)
    logp = random.uniform(-2, 7)
    rf = synth_rf(h, ea, dcm, meoh, et2o, mw, logp)
    return {
        "H": round(h, 4), "EA": round(ea, 4), "DCM": round(dcm, 4),
        "MeOH": round(meoh, 4), "Et2O": round(et2o, 4),
        "MW": round(mw, 2), "LogP": round(logp, 3),
        "Rf": round(rf, 4),
    }


def write_csv(path: Path, n: int):
    rows = [make_row() for _ in range(n)]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"  wrote {n} rows -> {path}")


if __name__ == "__main__":
    write_csv(DEMO_DIR / "train.csv", N_TRAIN)
    write_csv(DEMO_DIR / "valid.csv", N_VAL)
    write_csv(DEMO_DIR / "test.csv", N_TEST)
    print("Demo data generated.")
