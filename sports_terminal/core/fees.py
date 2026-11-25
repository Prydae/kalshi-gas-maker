"""Kalshi trading fees. The decision fee for every study is the **taker** rate.

Rates are passed in through FeeSettings (the published schedule as of 2026-08-19; the
private repo also allowed environment overrides, which are not part of this showcase).
Fees are returned **per contract** in the same 0..1 units as prices (dollars on a 1
contract), so edges stay comparable across sizes.

Kalshi is the only venue this project trades. ``fee_per_contract`` keeps its venue
argument; every caller passes the literal ``"kalshi"``, and any other value returns 0.0.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from typing import Any

from .config import FeeSettings

FEE_SCHEDULE_VERSION = "2026-08-19"
"""Date the defaults in :class:`FeeSettings` were last checked against the published
schedule. Kalshi changes these without much notice, so a tape is only as good as the
version it was recorded under -- a day's manifest stamps this string so an analysis can be
told that its fee assumptions predate a change.

Re-verify against https://kalshi.com/docs/fee-schedule and bump this string. Do not treat
backtest P&L as real until you have.
"""


def describe_schedule(settings: FeeSettings) -> dict[str, Any]:
    """The fee model in force, as data, for a manifest or a report header."""
    return {
        "version": FEE_SCHEDULE_VERSION,
        "kalshi_taker": "ceil_cents(M * rate * C * P * (1-P))",
        "kalshi_maker": "ceil_cents(M * rate * C * P * (1-P)), M defaults to 0",
        "values": {
            "kalshi_taker_rate": settings.kalshi_taker_rate,
            "kalshi_maker_rate": settings.kalshi_maker_rate,
            "kalshi_taker_multiplier": settings.kalshi_taker_multiplier,
            "kalshi_maker_multiplier": settings.kalshi_maker_multiplier,
        },
    }


def _ceil_cents(amount: float) -> float:
    """Round toward +inf to the next cent.

    The ``1e-12`` slack stops a value that is already an exact cent (1.75) from
    being pushed to the next one by binary float noise.
    """
    return math.ceil(amount * 100.0 - 1e-12) / 100.0


def kalshi_fee(
    price: float,
    contracts: float,
    is_taker: bool,
    *,
    settings: FeeSettings,
    multiplier: float | None = None,
) -> float:
    """Kalshi's published formula: ``round_up(M × rate × C × P × (1-P))`` to the next cent.

    ``M`` is the series fee multiplier. Sports series we have seen do not set one, so
    the taker default is 1 and the maker default is 0 (makers pay nothing unless a
    series lists a maker multiplier). A ``None`` multiplier uses those defaults.
    """
    if contracts <= 0 or not 0.0 < price < 1.0:
        return 0.0
    if is_taker:
        rate = settings.kalshi_taker_rate
        m = settings.kalshi_taker_multiplier if multiplier is None else multiplier
        raw = m * rate * contracts * price * (1.0 - price)
        return _ceil_cents(raw)
    rate = settings.kalshi_maker_rate
    m = settings.kalshi_maker_multiplier if multiplier is None else multiplier
    if m == 0:
        return 0.0
    raw = m * rate * contracts * price * (1.0 - price)
    return _ceil_cents(raw)


def fee_per_contract(
    venue: str,
    price: float,
    contracts: float,
    is_taker: bool,
    *,
    settings: FeeSettings,
    multiplier: float | None = None,
) -> float:
    """Fee in dollars for one contract of an order of ``contracts``.

    Kalshi rounds the *order* to the next cent, so the per-contract figure depends
    on size. ``contracts <= 0`` is treated as a single-contract enquiry.
    """
    size = contracts if contracts > 0 else 1.0
    if venue != "kalshi":
        return 0.0
    total = kalshi_fee(price, size, is_taker, settings=settings, multiplier=multiplier)
    return total / size


@dataclass(frozen=True)
class FeeSchedule:
    """Bound settings, so callers do not thread ``FeeSettings`` through every site."""

    settings: FeeSettings

    def fee(
        self,
        venue: str,
        price: float,
        contracts: float,
        is_taker: bool,
        *,
        multiplier: float | None = None,
    ) -> float:
        return fee_per_contract(
            venue, price, contracts, is_taker, settings=self.settings, multiplier=multiplier
        )
