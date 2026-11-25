"""Reproduce the paper-tape numbers from the public sample in data/sample/.

    python scripts/reproduce_headline.py

What this checks
----------------
* The P&L method in sports_terminal/gas/ledger.py (the same ``pnl`` and ``bootstrap_dates``
  the research used), applied to the sample slice: five evenings of size-1 paper fills
  (2026-09-21 to 2026-09-25) and the settled US strikes for their target days.
* The per-evening results must equal the figures from the full 12-evening run, which were
  computed from the complete tape. Those five values are pinned below. If the method or the
  sample changes, this script fails.

What it does not check
----------------------
The headline (+16.94c over 12 evenings, 3,270 fills) needs all twelve evenings. The complete
tape is not published, so the script reproduces the method and five nights of it, not the
headline itself. The sample's own aggregate is printed for reference only.

Output is in cents per contract (taker fee, the rule used for every number in the study).
No dollar amounts, no account data.
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sports_terminal.core.config import FeeSettings  # noqa: E402
from sports_terminal.gas import ledger  # noqa: E402

DATA = ROOT / "data" / "sample"
TAKER = FeeSettings()  # 0.07 taker rate, multiplier 1 (see core/config.py stub)

# Full-run values (12 evenings, complete tape), cents per contract, taker fee.
PINNED_NIGHTLY_CENTS = {
    "2026-09-21": 41.68,
    "2026-09-22": 27.66,
    "2026-09-23": -16.47,
    "2026-09-24": -10.21,
    "2026-09-25": 8.36,
}
PINNED_TOLERANCE = 0.005  # the pinned values are rounded to two decimals


def load_results(path: Path) -> dict[str, str]:
    with open(path, encoding="utf-8") as fh:
        return {row["ticker"]: row["result"] for row in csv.DictReader(fh)}


def main() -> int:
    results = load_results(DATA / "markets.csv")
    fills = [f for f in ledger.read_fills(DATA / "paper")
             if f.size_cap == 1 and f.ticker in results and results[f.ticker] in ("yes", "no")]
    print(f"settled size-1 fills in sample: {len(fills):,}")

    by_night: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    by_hour: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0])
    wins = 0
    total_p = total_c = 0.0
    for f in fills:
        p = ledger.pnl(f, results[f.ticker], TAKER.kalshi_taker_rate, TAKER.kalshi_taker_multiplier)
        by_night[f.date_et][0] += p
        by_night[f.date_et][1] += f.size
        by_hour[f.hour_et][0] += p
        by_hour[f.hour_et][1] += f.size
        wins += p > 0
        total_p += p
        total_c += f.size

    print("\nevening        fills   contracts   cents/contract")
    nightly = {}
    for night in sorted(by_night):
        p, c = by_night[night]
        nightly[night] = 100 * p / c
        n = sum(1 for f in fills if f.date_et == night)
        print(f"{night}   {n:6d}   {c:9.1f}   {nightly[night]:+10.2f}")

    print("\nhour (ET)  fills   cents/contract")
    for hour in sorted(by_hour):
        p, c = by_hour[hour]
        n = sum(1 for f in fills if f.hour_et == hour)
        print(f"   {hour:02d}     {n:6d}   {100 * p / c:+10.2f}")

    lo, hi = ledger.bootstrap_dates(fills, results, TAKER.kalshi_taker_rate,
                                    TAKER.kalshi_taker_multiplier, draws=2000, seed=17)
    print(f"\nsample aggregate (5 evenings, reference only): "
          f"{100 * total_p / total_c:+.2f}c per contract, "
          f"95% CI [{lo:+.2f}, {hi:+.2f}] (date bootstrap), "
          f"fill win rate {100 * wins / len(fills):.1f}%, "
          f"positive evenings {sum(1 for v in nightly.values() if v > 0)} of {len(nightly)}")

    failures = []
    for night, expected in PINNED_NIGHTLY_CENTS.items():
        got = nightly.get(night)
        if got is None or abs(got - expected) > PINNED_TOLERANCE:
            failures.append(f"{night}: got {got}, pinned {expected}")
    if failures:
        print("\nPINNED CHECK FAILED:\n  " + "\n  ".join(failures))
        return 1
    print("\nPINNED CHECK PASSED: all five evenings match the full-run figures to 0.01c.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
