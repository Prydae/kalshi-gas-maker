"""STUB / REIMPLEMENTATION of the one helper reconstruct.py needs from the private
schema module. The private module also defines Parquet schemas for the backfill, which
are not published. Only the series-ticker parser is kept.

``KXAAAGASD`` -> ("daily", "US"); ``KXAAAGASDTX`` -> ("daily", "TX");
``KXAAAGASW`` -> ("weekly", "US").
"""
from __future__ import annotations

import re

NATIONAL = "US"
SERIES_RE = re.compile(r"^KXAAAGAS(?P<freq>[DW])(?P<state>[A-Z]{2})?$")


def parse_series(series_ticker: str) -> tuple[str, str] | None:
    match = SERIES_RE.match(series_ticker or "")
    if not match:
        return None
    freq = "daily" if match.group("freq") == "D" else "weekly"
    return freq, match.group("state") or NATIONAL
