"""Settle the paper fills and print the daily ledger and the kill-rule comparison.

    python -m sports_terminal.gas.ledger --root data_gas/paper

Read-only, like everything else in this package: it reads the paper maker's own JSONL and
asks the public market endpoint how each strike resolved. It places nothing.

Every number is given at three fee readings, because Kalshi does not expose a maker
multiplier for this series (the fee schedule is in core/fees.py):

* maker 0      -- nothing charged on a resting fill
* maker 0.0175 -- the published maker formula at the series' stated multiplier of 1
* taker 0.07   -- the full taker rate, which is what the pre-registered kill rule uses

The kill rule is evaluated on the harshest of the three on purpose.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import random
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..backfill.client import KalshiHistoryClient
from ..core.config import FeeSettings
from ..core.fees import kalshi_fee
from . import tape
from .paper import ET, SERIES

BACKTEST_EV_CENTS = 10.10
"""Gas maker v2, final 3 hours, US national, non-marketable, at the taker rate."""

KILL_AFTER_DAYS = 15
KILL_FLOOR_CENTS = BACKTEST_EV_CENTS / 2.0

EXCLUDED_DATES = frozenset({"2026-09-16", "2026-10-18"})
"""Trial runs, excluded from the kill rule by a dated amendment to the pre-registration.

2026-09-16 was the laptop shakedown: it started at 19:14 instead of 18:00, was restarted
four times for code fixes, and spent part of the evening storing a top-of-book summary
instead of the raw ladder. Its fills are still reported -- they are just not evidence.

2026-10-18 is a disk-full incident on the recording machine. A side logger filled the
disk; from some point that evening every writing service failed, and at about 02:00
ET the gas paper maker entered a 1,638-restart loop. Its tape for that evening has holes
of unknown extent, so the night cannot be counted -- and crucially it cannot be *detected*
either, because a process that cannot write its tape cannot write a gap marker to it. The
exclusion is explicit for exactly that reason: the automatic coverage checks fail in the
same breath as the thing they are meant to catch.

Adding a date here after seeing its result would be cheating, so any addition must be
registered in the pre-registration record with the reason and the date it was registered. Both of these
are."""


@dataclass(frozen=True)
class PaperFill:
    ticker: str
    size_cap: int
    price: float
    size: float
    hour_et: int
    date_et: str
    queue_at_placement: float
    seconds_resting: int


def read_fills(root: Path) -> list[PaperFill]:
    """Every paper fill, including the hour-files rotation has already compressed.

    ``tape.read_rows`` is what makes that true. Globbing ``*.jsonl`` here is what made
    the ledger report every fill at ET hour 23: the rest of the evening had been gzipped
    and was silently absent from both the histogram and the settled set.

    ``hour_et`` is recomputed from ``trade_ts_ms`` rather than trusted from the row, so
    the report is correct for rows written by an older build.
    """
    out: list[PaperFill] = []
    for row in tape.read_rows(root, "paper_fill"):
        fill = fill_of(row)
        if fill is not None:
            out.append(fill)
    return out


def fill_of(row: dict) -> PaperFill | None:
    """One tape row as a PaperFill, or None if it is not a usable fill.

    Split out of :func:`read_fills` so the dashboard, which reads the tape incrementally,
    converts rows with this exact code rather than a copy of it.
    """
    try:
        stamp = datetime.fromtimestamp(row["trade_ts_ms"] / 1000, tz=ET)
        return PaperFill(
            ticker=row["ticker"], size_cap=int(row["size_cap"]),
            price=float(row["price"]), size=float(row["size"]),
            hour_et=stamp.hour, date_et=stamp.date().isoformat(),
            queue_at_placement=float(row.get("queue_at_placement") or 0.0),
            seconds_resting=int(row.get("seconds_resting") or 0),
        )
    except (KeyError, TypeError, ValueError, OSError):
        return None


async def fetch_results(tickers: set[str]) -> dict[str, str]:
    """ticker -> 'yes' / 'no' / '' for anything not settled yet."""
    client = KalshiHistoryClient()
    out: dict[str, str] = {}
    try:
        cursor = None
        for _ in range(40):
            params = {"series_ticker": SERIES, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            payload = await client.get("/markets", params)
            for market in payload.get("markets") or []:
                if market["ticker"] in tickers:
                    out[market["ticker"]] = str(market.get("result") or "")
            cursor = payload.get("cursor")
            if not cursor or len(out) >= len(tickers):
                break
    finally:
        await client.aclose()
    return out


def fee_of(price: float, contracts: float, rate: float, multiplier: float) -> float:
    if multiplier <= 0 or contracts <= 0:
        return 0.0
    settings = FeeSettings(kalshi_taker_rate=rate, kalshi_taker_multiplier=multiplier)
    return kalshi_fee(price, contracts, True, settings=settings)


READINGS = (("maker 0", 0.0, 0.0), ("maker 0.0175", 0.0175, 1.0),
            ("taker 0.07", 0.07, 1.0))


def pnl(fill: PaperFill, result: str, rate: float, multiplier: float) -> float:
    """We sold YES at ``price``; a NO result pays the whole price."""
    outcome = 1.0 if result == "yes" else 0.0
    gross = (fill.price - outcome) * fill.size
    return gross - fee_of(fill.price, fill.size, rate, multiplier)


def bootstrap_dates(rows, results, rate, multiplier, *, draws=400, seed=17):
    by_date: dict[str, list[float]] = collections.defaultdict(lambda: [0.0, 0.0])
    for fill in rows:
        cell = by_date[fill.date_et]
        cell[0] += pnl(fill, results[fill.ticker], rate, multiplier)
        cell[1] += fill.size
    keys = list(by_date)
    if len(keys) < 3:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    out = []
    for _ in range(draws):
        profit = contracts = 0.0
        for _ in range(len(keys)):
            p, c = by_date[keys[rng.randrange(len(keys))]]
            profit += p
            contracts += c
        if contracts:
            out.append(profit / contracts * 100)
    out.sort()
    return out[int(0.025 * len(out))], out[min(len(out) - 1, int(0.975 * len(out)))]


def report(root: Path, fills: list[PaperFill], results: dict[str, str]) -> None:
    settled = [f for f in fills if results.get(f.ticker) in ("yes", "no")]
    pending = [f for f in fills if f not in settled]
    print(f"paper fills recorded: {len(fills):,}   settled: {len(settled):,}   "
          f"awaiting settlement: {len(pending):,}")
    if not fills:
        print("\nNo fills yet. The queue model is strict, so an offer only fills after the")
        print("size resting ahead of it is consumed by YES-buying prints.")
        return

    print("\n--- daily ledger ---")
    for size_cap in sorted({f.size_cap for f in fills}):
        print(f"\nsize cap {size_cap}")
        print(f"{'date':>12}{'fills':>7}{'contracts':>11}{'open exposure':>15}"
              f"{'settled P&L':>13}{'EV/contract (taker)':>21}")
        by_date = collections.defaultdict(list)
        for f in fills:
            if f.size_cap == size_cap:
                by_date[f.date_et].append(f)
        for date in sorted(by_date):
            rows = by_date[date]
            done = [f for f in rows if results.get(f.ticker) in ("yes", "no")]
            contracts = sum(f.size for f in rows)
            exposure = sum((1.0 - f.price) * f.size for f in rows)
            profit = sum(pnl(f, results[f.ticker], 0.07, 1.0) for f in done)
            settled_contracts = sum(f.size for f in done)
            ev = (profit / settled_contracts * 100) if settled_contracts else float("nan")
            print(f"{date:>12}{len(rows):>7}{contracts:>11,.0f}{exposure:>15,.2f}"
                  f"{profit:>13,.2f}{ev:>21.2f}")

        hours = collections.Counter()
        for f in fills:
            if f.size_cap == size_cap:
                hours[f.hour_et] += f.size
        # sizes are floats because Kalshi reports fractional contract counts
        print("  fills by hour (ET), contracts: "
              f"{ {h: round(v, 2) for h, v in sorted(hours.items())} }")

    print("\n--- kill-rule comparison (pre-registered 2026-09-16) ---")
    rule_rows = [f for f in settled
                 if f.size_cap == 10 and f.date_et not in EXCLUDED_DATES]
    days = sorted({f.date_et for f in fills
                   if f.size_cap == 10 and f.date_et not in EXCLUDED_DATES})
    skipped = sorted({f.date_et for f in fills} & EXCLUDED_DATES)
    if skipped:
        print(f"excluded as trial runs (pre-registered): {', '.join(skipped)}")
    print(f"benchmark: backtest +{BACKTEST_EV_CENTS:.2f}c per contract at the taker rate")
    print(f"stop after {KILL_AFTER_DAYS} trading days if paper EV (taker) is <= 0c "
          f"or < +{KILL_FLOOR_CENTS:.2f}c")
    print(f"trading days with fills so far: {len(days)} of {KILL_AFTER_DAYS}")
    if not rule_rows:
        print("no settled size-10 fills yet, so the rule has nothing to evaluate")
        return

    contracts = sum(f.size for f in rule_rows)
    print(f"\n{'fee reading':>16}{'contracts':>11}{'P&L':>11}{'EV/contract':>14}"
          f"{'date-bootstrap 95% CI':>26}{'vs backtest':>13}")
    for label, rate, multiplier in READINGS:
        profit = sum(pnl(f, results[f.ticker], rate, multiplier) for f in rule_rows)
        ev = profit / contracts * 100
        low, high = bootstrap_dates(rule_rows, results, rate, multiplier)
        band = "        -        " if low != low else f"[{low:+7.2f},{high:+7.2f}]"
        print(f"{label:>16}{contracts:>11,.0f}{profit:>11,.2f}{ev:>14.2f}"
              f"{band:>26}{ev - BACKTEST_EV_CENTS:>+13.2f}")

    per_day = collections.defaultdict(lambda: [0.0, 0.0])
    for f in rule_rows:
        cell = per_day[f.date_et]
        cell[0] += pnl(f, results[f.ticker], 0.07, 1.0)
        cell[1] += f.size
    day_ev = {d: p / c * 100 for d, (p, c) in per_day.items() if c}
    positive = sum(1 for v in day_ev.values() if v > 0)
    print(f"\npositive days: {positive} of {len(day_ev)}  "
          f"(the rule also requires at least 8 of 15)")
    if len(day_ev) > 1:
        best = max(day_ev, key=day_ev.get)
        rest = [f for f in rule_rows if f.date_et != best]
        if rest:
            profit = sum(pnl(f, results[f.ticker], 0.07, 1.0) for f in rest)
            ev = profit / sum(f.size for f in rest) * 100
            print(f"dropping the best day ({best}): EV {ev:+.2f}c  "
                  f"(the rule also requires this to stay positive)")
    _ = statistics


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="sports_terminal.gas.ledger")
    parser.add_argument("--root", type=Path, default=Path("data_gas/paper"))
    parser.add_argument("--offline", action="store_true",
                        help="skip the settlement lookup and report fills only")
    parser.add_argument("--write-report", type=Path, default=None,
                        help="also write the report to DIR/<today>.txt")
    args = parser.parse_args(argv)

    fills = read_fills(args.root)
    tickers = {f.ticker for f in fills}
    results: dict[str, str] = {}
    if tickers and not args.offline:
        results = asyncio.run(fetch_results(tickers))

    if args.write_report is None:
        report(args.root, fills, results)
        return 0

    # Capture rather than re-run: the report must be identical on stdout and on disk.
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        report(args.root, fills, results)
    text = buffer.getvalue()
    print(text, end="")
    args.write_report.mkdir(parents=True, exist_ok=True)
    today = datetime.now(tz=ET).date().isoformat()
    target = args.write_report / f"{today}.txt"
    header = f"gas paper ledger, written {datetime.now(tz=ET):%Y-%m-%d %H:%M:%S %Z}\n"
    target.write_text(header + text, encoding="utf-8")
    print(f"\nwrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
