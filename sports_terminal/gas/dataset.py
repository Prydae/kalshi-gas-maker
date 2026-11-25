"""Shared sample set for the gas analyses: one row per market-minute.

The organising quantity is **distance**: the strike minus the last AAA value that was
public while the market traded. A market for date D trades from about noon UTC on D-1
until 11:59 PM ET that evening, and the newest AAA number in that window is the one
published for D-1. So ``distance = strike - AAA(D-1)``, in 0.005 steps.

``AAA(D-1)`` comes from the settlement reconstruction, which gives a half-open bracket
rather than a point. The midpoint is used, so every distance carries about +/-0.0025 of
slack -- half a bucket. That is stated rather than hidden: it blurs adjacent buckets and
is the reason a bucket boundary should not be read as exact.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pyarrow.dataset as ds
import pyarrow.parquet as pq

STEP = 0.005
ET = timezone(timedelta(hours=-4))
"""Eastern Daylight Time. Every date in this backfill (2026-07-11 .. 09-16) is inside
EDT, so a fixed offset is exact here; extending past the November change needs a real
timezone database."""


@dataclass(frozen=True)
class Sample:
    ticker: str
    state: str
    target: str
    strike: float
    ts: int
    yes_bid: float
    yes_ask: float
    yes_won: bool
    prev_value: float
    close_ts: int
    volume: int = 0
    """Contracts traded in this minute. Kalshi's candlesticks carry no book depth, so
    this is the only size evidence available -- it says what did trade, not what could."""

    @property
    def distance(self) -> float:
        return self.strike - self.prev_value

    @property
    def steps(self) -> int:
        return int(round(self.distance / STEP))

    @property
    def hours_to_close(self) -> float:
        return (self.close_ts - self.ts) / 3600.0

    @property
    def hour_et(self) -> int:
        return datetime.fromtimestamp(self.ts, tz=ET).hour

    @property
    def no_ask(self) -> float:
        """Cost of buying NO: sell YES into the bid."""
        return 1.0 - self.yes_bid


def load_brackets(root: Path) -> dict[tuple[str, str], dict]:
    path = root / "daily_values.parquet"
    if not path.is_file():
        return {}
    return {(r["state"], r["target_date"]): r
            for r in pq.read_table(path).to_pylist() if r["frequency"] == "daily"}


def previous_value(brackets: dict, state: str, target: str) -> float | None:
    """Midpoint of the prior day's reconstructed bracket, or None."""
    try:
        day = date.fromisoformat(target) - timedelta(days=1)
    except ValueError:
        return None
    row = brackets.get((state, day.isoformat()))
    if not row:
        return None
    lo, up = row["lower_exclusive"], row["upper_inclusive"]
    if lo is None or up is None:
        return None
    return (lo + up) / 2.0


def load_markets(root: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in sorted(root.glob("*/markets.parquet")):
        for row in pq.read_table(path).to_pylist():
            if row["result"] in ("yes", "no") and row["strike"] is not None:
                out[row["ticker"]] = row
    return out


def build(root: Path, *, frequency: str = "daily") -> list[Sample]:
    brackets = load_brackets(root)
    markets = {t: m for t, m in load_markets(root).items() if m["frequency"] == frequency}
    prev_cache: dict[tuple[str, str], float | None] = {}

    out: list[Sample] = []
    for path in sorted(root.glob("*/candles")):
        series_dir = path.parent.name
        table = ds.dataset(path, format="parquet", partitioning="hive").to_table(
            columns=["ticker", "end_period_ts", "yes_bid_close", "yes_ask_close", "volume"])
        for ticker, ts, bid, ask, volume in zip(
            table.column("ticker").to_pylist(), table.column("end_period_ts").to_pylist(),
            table.column("yes_bid_close").to_pylist(), table.column("yes_ask_close").to_pylist(),
            table.column("volume").to_pylist(),
        ):
            market = markets.get(ticker)
            if market is None or ts is None or bid is None or ask is None:
                continue
            if bid == 0.0 and ask == 0.0:
                continue  # a closed market still emits candles; 0/0 is not a quote
            open_ms, close_ms = market["open_ts_ms"], market["close_ts_ms"]
            if not open_ms or not close_ms:
                continue
            ts = int(ts)
            if not (open_ms // 1000 <= ts <= close_ms // 1000):
                continue
            state, target = market["state"], market["target_date"]
            iso = _iso_target(target)
            if iso is None:
                continue
            key = (state, iso)
            if key not in prev_cache:
                prev_cache[key] = previous_value(brackets, state, iso)
            prev = prev_cache[key]
            if prev is None:
                continue
            out.append(Sample(
                ticker=ticker, state=state, target=iso, strike=float(market["strike"]),
                ts=ts, yes_bid=float(bid), yes_ask=float(ask),
                yes_won=market["result"] == "yes", prev_value=prev,
                close_ts=close_ms // 1000, volume=int(volume or 0),
            ))
        del table, series_dir
    return out


def _iso_target(text: str) -> str | None:
    from .reconstruct import parse_target_date

    day = parse_target_date(text)
    return day.isoformat() if day else None


def by_date(samples: list[Sample]) -> dict[str, list[Sample]]:
    out: dict[str, list[Sample]] = collections.defaultdict(list)
    for s in samples:
        out[s.target].append(s)
    return out
