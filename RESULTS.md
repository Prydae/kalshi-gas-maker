# Results

All P&L is in **cents per contract**, after the taker fee (`round_up(0.07 × C × P × (1 − P))`
per order, Kalshi's published formula). No dollar amounts, no account data, no balances.

## Headline

| stage | sample | result | 95% interval |
|---|---|---|---|
| Backtest, pre-registered benchmark | 14,213 simulated fills, 67 days | **+10.10c** | [+4.18, +16.07] |
| Forward paper test | 3,270 simulated fills, 12 evenings | **+16.94c** | [+4.75, +27.85] |

- **Paper win rate:** 72.1% of fills were profitable.
- **Paper consistency:** 8 of 12 evenings were positive.
- **Interval method:** the paper interval is a date-level bootstrap, resampling whole evenings
  rather than fills. The backtest interval is the same bootstrap run on the stored simulation.
  The benchmark was registered with the interval [+4.28, +16.15]; a fresh bootstrap gives
  the interval shown here.
- **Caveat:** paper fills are simulated against recorded order books. They are not real
  execution, so a simulated result can overstate what a live order would earn.

## By hour of fill (Eastern time), paper, 12 evenings

| hour | fills | cents per contract |
|---|---|---|
| 18:00 | 301 | +13.04 |
| 19:00 | 337 | +15.01 |
| 20:00 | 450 | +16.75 |
| 21:00 | 507 | +14.81 |
| 22:00 | 706 | +15.28 |
| 23:00 | 969 | +21.25 |

Every hour is positive. The 23:00 hour, nearest the market's close, earns the most.

## Ideas tested

| status | count | meaning |
|---|---|---|
| Killed or failed a pre-registered gate | **38** | 9 killed under the project's kill rule, 29 failed a gate |
| Untested | 27 | no usable data, or the test was miscalibrated |
| Dropped by decision | 16 | stopped before a test |
| Traded live | 3 | two gas entries and one diesel entry (the diesel engine was paused on 2026-10-25) |
| Open | 1 | needs a decision |
| **Total logged** | **85** | every idea the project has recorded |

Of 85 ideas, 38 were killed or failed a gate. The gas edge is the one that cleared every gate.

## Reproducing the figures

`python scripts/reproduce_headline.py` runs the same P&L method on the five-evening sample in
`data/sample/`. It checks each evening against the full-run figure. It does not reproduce the
12-evening headline, because the complete tape is not published. See `data/sample/README.md`.

## Charts

- `charts/01_cumulative_pnl_paper_size1.png`: cumulative paper P&L, cents per contract, 12 evenings.
- `charts/03_strategies_tested_vs_killed.png`: the ideas-tested breakdown.
