"""Galileo on gas: calibration, EV by hour, ladder consistency.

Every interval resamples **whole dates** -- all states on a date move together, because
they share an AAA publication cycle and the same crude complex. Resampling state-days
would treat 17 correlated observations as 17 independent ones and shrink every interval
by roughly the square root of that.

Two details that decide whether the EV columns mean anything:

*Untradeable sides are dropped, not averaged in.* About 9% of market-minutes have no ask
(``yes_ask`` = 1.00) and 5% have no bid (so NO costs 1.00). Those are not expensive
prices, they are the absence of a price. Each side is scored only over the minutes where
that side could actually be bought, and the count of those minutes is reported next to it.

*EV is computed per minute and then averaged*, never from an average price. Kalshi's fee
is quadratic in price and rounded up per order, so the fee on a mean price is not the mean
of the fees.
"""

from __future__ import annotations

import collections
import random
from dataclasses import dataclass

from ..core.config import FeeSettings
from ..core.fees import fee_per_contract
from .dataset import Sample

ORDER_SIZE = 10
"""Every EV here is for a 10-lot, with Kalshi's per-order round-up applied to the order
and then divided back down to a per-contract number."""


def fee_table(settings: FeeSettings) -> list[float]:
    """Per-contract taker fee at each whole cent. Prices are cents, so 101 entries cover
    every price that can occur, and the fee stops being a hot loop."""
    return [fee_per_contract("kalshi", c / 100.0, ORDER_SIZE, True, settings=settings)
            for c in range(101)]


def _fee(table: list[float], price: float) -> float:
    return table[min(100, max(0, int(round(price * 100))))]


def taker_fee(price: float, settings: FeeSettings) -> float:
    """Per-contract taker fee at one price. For one-off use; loops want ``fee_table``."""
    return fee_per_contract("kalshi", price, ORDER_SIZE, True, settings=settings)


@dataclass
class DayAgg:
    """One date's contribution to one bucket. Bootstrapping resamples these whole."""

    n: int = 0
    wins: int = 0
    yes_n: int = 0
    yes_price: float = 0.0
    yes_ev: float = 0.0
    no_n: int = 0
    no_price: float = 0.0
    no_ev: float = 0.0


Bucket = dict[str, DayAgg]


def add_sample(bucket: Bucket, s: Sample, fees: list[float]) -> None:
    day = bucket.get(s.target)
    if day is None:
        day = bucket[s.target] = DayAgg()
    day.n += 1
    won = s.yes_won
    day.wins += won
    if s.yes_ask < 1.0:
        price = s.yes_ask
        day.yes_n += 1
        day.yes_price += price
        day.yes_ev += float(won) - price - _fee(fees, price)
    no_ask = s.no_ask
    if no_ask < 1.0:
        day.no_n += 1
        day.no_price += no_ask
        day.no_ev += float(not won) - no_ask - _fee(fees, no_ask)


def summarize(bucket: Bucket) -> dict:
    n = sum(d.n for d in bucket.values())
    wins = sum(d.wins for d in bucket.values())
    yes_n = sum(d.yes_n for d in bucket.values())
    no_n = sum(d.no_n for d in bucket.values())
    nan = float("nan")
    return {
        "samples": n,
        "dates": len(bucket),
        "yes_rate": wins / n if n else nan,
        "yes_n": yes_n,
        "no_n": no_n,
        "avg_yes_ask": sum(d.yes_price for d in bucket.values()) / yes_n if yes_n else nan,
        "avg_no_ask": sum(d.no_price for d in bucket.values()) / no_n if no_n else nan,
        "ev_yes": sum(d.yes_ev for d in bucket.values()) / yes_n if yes_n else nan,
        "ev_no": sum(d.no_ev for d in bucket.values()) / no_n if no_n else nan,
    }


def bootstrap(bucket: Bucket, *, draws: int = 400, seed: int = 11) -> dict:
    """95% CI of EV per side, resampling whole dates with replacement."""
    keys = list(bucket)
    nan = (float("nan"), float("nan"))
    if len(keys) < 3:
        return {"ci_yes": nan, "ci_no": nan}
    days = [bucket[k] for k in keys]
    rng = random.Random(seed)
    out: dict[str, list[float]] = {"yes": [], "no": []}
    size = len(days)
    for _ in range(draws):
        picks = [days[rng.randrange(size)] for _ in range(size)]
        yn = sum(d.yes_n for d in picks)
        nn = sum(d.no_n for d in picks)
        if yn:
            out["yes"].append(sum(d.yes_ev for d in picks) / yn)
        if nn:
            out["no"].append(sum(d.no_ev for d in picks) / nn)
    result = {}
    for side in ("yes", "no"):
        values = out[side]
        if len(values) < draws * 0.5:
            result[f"ci_{side}"] = nan
            continue
        values.sort()
        result[f"ci_{side}"] = (values[int(0.025 * len(values))],
                                values[min(len(values) - 1, int(0.975 * len(values)))])
    return result


def _finish(bucket: Bucket, draws: int) -> dict:
    entry = summarize(bucket)
    entry.update(bootstrap(bucket, draws=draws))
    return entry


HOUR_BINS = ((0, 3), (3, 8), (8, 16), (16, 48))


def calibration(
    samples: list[Sample], settings: FeeSettings, *,
    step_range: tuple[int, int] = (-8, 8), hour_bins=HOUR_BINS,
    min_samples: int = 200, min_dates: int = 8, draws: int = 400,
    state: str | None = None,
) -> list[dict]:
    """Distance (in 0.005 steps) x hours-to-close. Pooled across states by default."""
    fees = fee_table(settings)
    cells: dict[tuple, Bucket] = collections.defaultdict(dict)
    for s in samples:
        if state is not None and s.state != state:
            continue
        steps = s.steps
        if not step_range[0] <= steps <= step_range[1]:
            continue
        hours = s.hours_to_close
        for window in hour_bins:
            if window[0] <= hours < window[1]:
                add_sample(cells[(steps, window)], s, fees)
                break
    out = []
    for (steps, window), bucket in sorted(cells.items()):
        n = sum(d.n for d in bucket.values())
        if n < min_samples or len(bucket) < min_dates:
            continue
        entry = {"steps": steps, "window": window}
        entry.update(_finish(bucket, draws))
        out.append(entry)
    return out


def ev_by_hour(
    samples: list[Sample], settings: FeeSettings, *, draws: int = 400,
    min_samples: int = 200, state: str | None = None,
) -> list[dict]:
    fees = fee_table(settings)
    cells: dict[int, Bucket] = collections.defaultdict(dict)
    for s in samples:
        if state is not None and s.state != state:
            continue
        add_sample(cells[s.hour_et], s, fees)
    out = []
    for hour in sorted(cells):
        bucket = cells[hour]
        if sum(d.n for d in bucket.values()) < min_samples:
            continue
        entry = {"hour_et": hour}
        entry.update(_finish(bucket, draws))
        out.append(entry)
    return out


@dataclass(frozen=True)
class LadderBreak:
    state: str
    target: str
    ts: int
    low_strike: float
    high_strike: float
    low_ask: float
    high_bid: float
    volume: int = 0
    """Contracts traded in that minute across the two legs. The book's depth is not in
    the candle feed, so this is a floor on what was tradeable, not the resting size."""

    @property
    def gross(self) -> float:
        """Credit per pair: sell the higher strike's YES, buy the lower strike's."""
        return self.high_bid - self.low_ask

    def net(self, fees: list[float]) -> float:
        return self.gross - _fee(fees, self.low_ask) - _fee(fees, self.high_bid)


def ladder_breaks(samples: list[Sample], settings: FeeSettings) -> list[LadderBreak]:
    """Minutes where a higher strike bids above a lower strike's ask.

    "Above 4.50" can never be worth more than "above 4.45" -- the first implies the
    second. So a higher strike bidding above a lower strike's ask is a free credit on a
    position that settles at zero or better whatever the price does. Only adjacent-or-not
    pairs that clear both taker fees are kept.
    """
    fees = fee_table(settings)
    grouped: dict[tuple, list[Sample]] = collections.defaultdict(list)
    for s in samples:
        grouped[(s.state, s.target, s.ts)].append(s)
    out: list[LadderBreak] = []
    for (state, target, ts), rows in grouped.items():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda s: s.strike)
        for i, low in enumerate(rows):
            if low.yes_ask >= 1.0:
                continue
            for high in rows[i + 1:]:
                if high.yes_bid > low.yes_ask:
                    item = LadderBreak(state, target, ts, low.strike, high.strike,
                                       low.yes_ask, high.yes_bid,
                                       volume=low.volume + high.volume)
                    if item.net(fees) > 0:
                        out.append(item)
    return out
