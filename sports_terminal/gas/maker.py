"""Gas maker v2: a resting offer that could actually have rested.

Version 1 priced an order off the previous two minutes' trade median and filled it on any
print through that price. Two things in that were too kind:

*The order was never checked against the book.* An offer priced below the best ask is not
a resting offer at all -- it is a marketable order that would have executed on arrival,
against the bid, at the bid's price. Simulating it as a passive fill credits it with a
spread it never earned. Here an order must be **non-marketable**: strictly above the best
bid at the moment of placement, measured from the candle that closes at that minute.

*Any print through counted as a fill.* A resting YES offer is lifted by a taker who
**buys** YES. A print through our price with the taker on the NO side means the trade
happened on the other side of the book and our offer was not what filled. Here a fill
needs ``taker_side == "yes"`` as well as a price strictly through ours.

Queue position is still not modelled and still cannot be from public prints. This
overstates fills, and the overstatement is still not quantified.
"""

from __future__ import annotations

import bisect
import collections
import math
import random
from dataclasses import dataclass
from pathlib import Path

import pyarrow.dataset as pds
import pyarrow.parquet as pq

from ..core.config import FeeSettings
from ..core.fees import fee_per_contract
from .dataset import ET, STEP, load_brackets, previous_value
from .reconstruct import parse_target_date

REFERENCE_WINDOW_SEC = 120
PLACEMENT_INTERVAL_SEC = 60
ORDER_LIFE_SEC = 120
MIN_PRICE, MAX_PRICE = 0.01, 0.99
TICK = 0.01


def round_to_tick(price: float, side: str | None = None) -> float:
    """Snap to Kalshi's one-cent grid, breaking ties away from the market.

    A median of two adjacent cents lands on a half-cent, so the tie-break is not
    hypothetical. Rounding a bid down and an ask up makes the order marginally harder to
    fill in both cases, which is the direction this whole module errs in. With no side
    given it is a plain nearest-cent round.

    Moved here unchanged on 2026-10-26 from ``backfill/resting.py`` (Galileo v1), which
    went with the cross-venue scanner. This backtest was its only remaining caller.
    """
    ticks = price / TICK
    if side == "bid":
        return round(math.floor(ticks + 1e-9) * TICK, 2)
    if side == "ask":
        return round(math.ceil(ticks - 1e-9) * TICK, 2)
    return round(round(ticks) * TICK, 2)

PLACEMENTS = ("join_ask", "improve_1c", "ref_plus_d")


@dataclass(frozen=True)
class Fill:
    """One resting YES offer that a YES-buying taker lifted."""

    ticker: str
    state: str
    target: str
    strike: float
    placement: str
    distance_cents: int
    ts_sec: int
    price: float
    size: float
    yes_won: bool
    best_bid: float
    best_ask: float
    hours_to_close: float
    steps: int
    trade_size: float
    levels_in_second: int
    """How many distinct price levels the taker cleared in that same second. More than
    one means we were part of a sweep rather than a single lift."""

    @property
    def hour_et(self) -> int:
        from datetime import datetime

        return datetime.fromtimestamp(self.ts_sec, tz=ET).hour

    @property
    def at_or_above_ask(self) -> bool:
        return self.price >= self.best_ask

    def gross(self) -> float:
        """Per contract, before fees. We sold YES at ``price``."""
        return self.price - (1.0 if self.yes_won else 0.0)

    def exposure(self) -> float:
        """Worst-case loss per contract if the market settles against us."""
        return 1.0 - self.price

    def pnl(self, settings: FeeSettings, *, maker_is_free: bool) -> float:
        fee = 0.0
        if not maker_is_free:
            fee = fee_per_contract("kalshi", self.price, max(int(self.size), 1), True,
                                   settings=settings)
        return (self.gross() - fee) * self.size


def load_books(root: Path) -> dict[str, list[tuple[int, float, float]]]:
    """ticker -> [(minute close ts, best bid, best ask)], sorted."""
    out: dict[str, list[tuple[int, float, float]]] = collections.defaultdict(list)
    for path in sorted(root.glob("*/candles")):
        table = pds.dataset(path, format="parquet", partitioning="hive").to_table(
            columns=["ticker", "end_period_ts", "yes_bid_close", "yes_ask_close"])
        for ticker, ts, bid, ask in zip(
            table.column("ticker").to_pylist(), table.column("end_period_ts").to_pylist(),
            table.column("yes_bid_close").to_pylist(),
            table.column("yes_ask_close").to_pylist(),
        ):
            if ts is None or bid is None or ask is None:
                continue
            # 0/0 minutes are kept so the rejection accounting can name them as
            # "no ask" rather than losing them silently at load time.
            out[ticker].append((int(ts), float(bid), float(ask)))
    for rows in out.values():
        rows.sort()
    return dict(out)


def load_trades(root: Path) -> dict[str, list[tuple[int, float, float, str]]]:
    """ticker -> [(ts, yes price, count, taker side)], sorted."""
    out: dict[str, list[tuple[int, float, float, str]]] = collections.defaultdict(list)
    for path in sorted(root.glob("*/trades")):
        table = pds.dataset(path, format="parquet", partitioning="hive").to_table(
            columns=["ticker", "created_ts_ms", "yes_price", "count", "taker_side"])
        for ticker, ms, price, count, side in zip(
            table.column("ticker").to_pylist(), table.column("created_ts_ms").to_pylist(),
            table.column("yes_price").to_pylist(), table.column("count").to_pylist(),
            table.column("taker_side").to_pylist(),
        ):
            if ms is None or price is None:
                continue
            out[ticker].append((int(ms) // 1000, float(price), float(count or 0),
                                str(side or "")))
    for rows in out.values():
        rows.sort()
    return dict(out)


def sweep_levels(trades: list[tuple[int, float, float, str]]) -> dict[tuple[int, str], int]:
    """(second, taker side) -> number of distinct price levels printed in that second."""
    grouped: dict[tuple[int, str], set[float]] = collections.defaultdict(set)
    for ts, price, _, side in trades:
        grouped[(ts, side)].add(price)
    return {key: len(values) for key, values in grouped.items()}


def markets_with_outcome(root: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in sorted(root.glob("*/markets.parquet")):
        for row in pq.read_table(path).to_pylist():
            if (row["result"] in ("yes", "no") and row["strike"] is not None
                    and row["frequency"] == "daily"):
                out[row["ticker"]] = row
    return out


def simulate(
    root: Path, *, distances_cents=(2, 5, 10), size_cap: int = 10,
) -> tuple[list[Fill], dict[str, int]]:
    """Every fill, for every placement policy. Also returns rejection counts."""
    markets = markets_with_outcome(root)
    books = load_books(root)
    trades_by = load_trades(root)
    brackets = load_brackets(root)
    prev_cache: dict[tuple[str, str], float | None] = {}

    fills: list[Fill] = []
    counts: collections.Counter = collections.Counter()

    for ticker, market in markets.items():
        book = books.get(ticker)
        trades = trades_by.get(ticker)
        if not book or not trades or len(trades) < 3:
            continue
        iso = parse_target_date(market["target_date"])
        if iso is None:
            continue
        iso = iso.isoformat()
        key = (market["state"], iso)
        if key not in prev_cache:
            prev_cache[key] = previous_value(brackets, market["state"], iso)
        prev = prev_cache[key]
        if prev is None:
            continue

        close_sec = market["close_ts_ms"] // 1000
        yes_won = market["result"] == "yes"
        levels = sweep_levels(trades)
        book_times = [t for t, _, _ in book]
        trade_times = [t for t, _, _, _ in trades]

        first, last = trade_times[0], min(trade_times[-1], close_sec)
        placements = list(range(first + REFERENCE_WINDOW_SEC, last + 1,
                                PLACEMENT_INTERVAL_SEC))
        for index, now in enumerate(placements):
            # book as of the candle that closes at or before this minute
            spot = bisect.bisect_right(book_times, now) - 1
            if spot < 0 or now - book_times[spot] > PLACEMENT_INTERVAL_SEC:
                counts["no_candle_for_this_minute"] += 1
                continue
            _, best_bid, best_ask = book[spot]
            if best_ask <= 0.0 or best_ask >= 1.0:
                counts["no_ask_skip_the_minute"] += 1
                continue

            lo = bisect.bisect_left(trade_times, now - REFERENCE_WINDOW_SEC)
            hi = bisect.bisect_left(trade_times, now)  # strictly before: no look-ahead
            window = sorted(p for _, p, _, _ in trades[lo:hi])
            if len(window) < 2:
                counts["thin_reference"] += 1
                continue
            reference = window[len(window) // 2] if len(window) % 2 else (
                (window[len(window) // 2 - 1] + window[len(window) // 2]) / 2)

            expiry = min(now + ORDER_LIFE_SEC, close_sec)
            if index + 1 < len(placements):
                expiry = min(expiry, placements[index + 1])
            if expiry <= now:
                continue
            start = bisect.bisect_right(trade_times, now)
            end = bisect.bisect_right(trade_times, expiry)
            if start >= end:
                continue

            for distance in distances_cents:
                candidates = {
                    "join_ask": best_ask,
                    "improve_1c": round(best_ask - 0.01, 2),
                    "ref_plus_d": round_to_tick(reference + distance * 0.01, "ask"),
                }
                for placement, price in candidates.items():
                    if placement != "ref_plus_d" and distance != distances_cents[0]:
                        continue  # book-relative placements do not depend on D
                    if not MIN_PRICE <= price <= MAX_PRICE:
                        counts[f"{placement}:off_grid"] += 1
                        continue
                    if price <= best_bid:
                        counts[f"{placement}:marketable"] += 1
                        continue
                    if placement == "ref_plus_d" and price < best_ask:
                        counts["ref_plus_d:inside_ask"] += 1
                        continue
                    filled = _first_fill(trades, start, end, price, size_cap)
                    if filled is None:
                        counts[f"{placement}:no_fill"] += 1
                        continue
                    ts, size, trade_size, side = filled
                    fills.append(Fill(
                        ticker=ticker, state=market["state"], target=iso,
                        strike=float(market["strike"]), placement=placement,
                        distance_cents=distance if placement == "ref_plus_d" else 0,
                        ts_sec=ts, price=price, size=size, yes_won=yes_won,
                        best_bid=best_bid, best_ask=best_ask,
                        hours_to_close=(close_sec - ts) / 3600.0,
                        steps=int(round((float(market["strike"]) - prev) / STEP)),
                        trade_size=trade_size,
                        levels_in_second=levels.get((ts, side), 1),
                    ))
    return fills, dict(counts)


def _first_fill(trades, start: int, end: int, price: float, size_cap: int):
    """The first print strictly through ``price`` with the taker buying YES."""
    for ts, traded, traded_size, side in trades[start:end]:
        if traded <= price:
            continue
        if side != "yes":
            continue
        size = min(float(size_cap), float(traded_size or 0))
        if size <= 0:
            continue
        return ts, size, float(traded_size or 0), side
    return None


@dataclass(frozen=True)
class AuditedV1Fill:
    """A version-1 fill, re-derived with the two facts version 1 never looked at."""

    target: str
    price: float
    size: float
    yes_won: bool
    distance_cents: int
    best_bid: float
    best_ask: float
    taker_side: str

    @property
    def non_marketable(self) -> bool:
        """Would the order have rested, rather than executing on arrival?"""
        return self.best_ask > 0.0 and self.price > self.best_bid

    @property
    def at_or_above_ask(self) -> bool:
        return self.best_ask > 0.0 and self.price >= self.best_ask

    @property
    def right_taker(self) -> bool:
        return self.taker_side == "yes"


def audit_v1(root: Path, *, distances_cents=(2, 5, 10), size_cap: int = 10
             ) -> list[AuditedV1Fill]:
    """Replay version 1's offer-side rule, recording the book and the taker side.

    Version 1 placed at ``reference + D`` with no reference to the book and filled on any
    print through. Each fill it produced is re-derived here together with the best bid and
    ask at placement and the taker side of the print that filled it, so the two new rules
    can be applied after the fact and counted.
    """
    markets = markets_with_outcome(root)
    books = load_books(root)
    trades_by = load_trades(root)
    out: list[AuditedV1Fill] = []

    for ticker, market in markets.items():
        trades = trades_by.get(ticker)
        book = books.get(ticker)
        if not trades or not book or len(trades) < 3:
            continue
        iso = parse_target_date(market["target_date"])
        if iso is None:
            continue
        iso = iso.isoformat()
        close_sec = market["close_ts_ms"] // 1000
        yes_won = market["result"] == "yes"
        trade_times = [t for t, _, _, _ in trades]
        book_times = [t for t, _, _ in book]

        first, last = trade_times[0], min(trade_times[-1], close_sec)
        placements = list(range(first + REFERENCE_WINDOW_SEC, last + 1,
                                PLACEMENT_INTERVAL_SEC))
        for index, now in enumerate(placements):
            lo = bisect.bisect_left(trade_times, now - REFERENCE_WINDOW_SEC)
            hi = bisect.bisect_left(trade_times, now)
            window = sorted(p for _, p, _, _ in trades[lo:hi])
            if len(window) < 2:
                continue
            reference = window[len(window) // 2] if len(window) % 2 else (
                (window[len(window) // 2 - 1] + window[len(window) // 2]) / 2)
            expiry = min(now + ORDER_LIFE_SEC, close_sec)
            if index + 1 < len(placements):
                expiry = min(expiry, placements[index + 1])
            if expiry <= now:
                continue
            start = bisect.bisect_right(trade_times, now)
            end = bisect.bisect_right(trade_times, expiry)
            if start >= end:
                continue

            spot = bisect.bisect_right(book_times, now) - 1
            if spot < 0 or now - book_times[spot] > PLACEMENT_INTERVAL_SEC:
                best_bid = best_ask = 0.0
            else:
                _, best_bid, best_ask = book[spot]

            for distance in distances_cents:
                price = round_to_tick(reference + distance * 0.01, "ask")
                if not MIN_PRICE <= price <= MAX_PRICE:
                    continue
                for ts, traded, traded_size, side in trades[start:end]:
                    if traded <= price:
                        continue  # version 1's rule: any print strictly through
                    size = min(float(size_cap), float(traded_size or 0))
                    if size <= 0:
                        continue
                    out.append(AuditedV1Fill(
                        target=iso, price=price, size=size, yes_won=yes_won,
                        distance_cents=distance, best_bid=best_bid, best_ask=best_ask,
                        taker_side=side))
                    break
    return out


def ev(fills: list[Fill], settings: FeeSettings, *, maker_is_free: bool) -> float:
    contracts = sum(f.size for f in fills)
    if not contracts:
        return float("nan")
    return sum(f.pnl(settings, maker_is_free=maker_is_free) for f in fills) / contracts


def bootstrap_by_date(
    fills: list[Fill], settings: FeeSettings, *, maker_is_free: bool,
    draws: int = 400, seed: int = 5,
) -> tuple[float, float]:
    """95% CI of EV per contract, resampling whole target dates."""
    by_date: dict[str, list[float]] = collections.defaultdict(lambda: [0.0, 0.0])
    for f in fills:
        cell = by_date[f.target]
        cell[0] += f.pnl(settings, maker_is_free=maker_is_free)
        cell[1] += f.size
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
            out.append(profit / contracts)
    if len(out) < draws * 0.5:
        return (float("nan"), float("nan"))
    out.sort()
    return out[int(0.025 * len(out))], out[min(len(out) - 1, int(0.975 * len(out)))]
