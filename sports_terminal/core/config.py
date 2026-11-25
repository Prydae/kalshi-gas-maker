"""STUB. The private repo's core/config.py also reads environment overrides and project
settings; none of that is published here. Only the published Kalshi fee defaults are kept,
with the same field names and default values that core/fees.py and the gas modules use.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeeSettings:
    """Kalshi's published fee schedule (defaults as used for every number in the case study)."""

    kalshi_taker_rate: float = 0.07
    kalshi_maker_rate: float = 0.0175
    kalshi_taker_multiplier: float = 1.0
    kalshi_maker_multiplier: float = 0.0
