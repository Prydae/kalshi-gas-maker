"""Recover each day's AAA value from how its strike ladder settled.

    python -m sports_terminal.gas.reconstruct --root data_gas

Every market is "above X". So for a day's ladder, the published value V satisfies::

    V >  max(strike : resolved YES)
    V <= min(strike : resolved NO)

which brackets V in a half-open interval one strike step wide. The interval is half-open
rather than open because the boundary is **strictly greater**: a value landing exactly on
a strike resolves that strike NO, so the upper bound is attainable and the lower is not.

That makes a tie -- V exactly equal to a strike -- the single attainable endpoint of the
bracket, and it cannot be told apart from "just below it" by settlements alone. AAA
publishes four decimals and strikes step 0.0050, so of the fifty values in a gap exactly
one is a tie; the prior is about 2% of days, and the cross-check against the live page is
what turns that into a count.
"""

from __future__ import annotations

import argparse
import collections
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .schema import parse_series

STRIKE_STEP = 0.005
_TARGET = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})$")
_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}


def parse_target_date(text: str) -> date | None:
    """``26SEP17`` -> 2026-09-17."""
    match = _TARGET.match(text or "")
    if not match:
        return None
    yy, mon, dd = match.groups()
    month = _MONTHS.get(mon)
    if not month:
        return None
    try:
        return date(2000 + int(yy), month, int(dd))
    except ValueError:
        return None


@dataclass
class DayBracket:
    series: str
    state: str
    frequency: str
    target: date
    lower: float | None
    upper: float | None
    n_yes: int
    n_no: int
    inconsistent: bool

    @property
    def width(self) -> float | None:
        if self.lower is None or self.upper is None:
            return None
        return round(self.upper - self.lower, 6)

    @property
    def pinned(self) -> bool:
        """Bracketed to a single strike step, the tightest settlements can give."""
        w = self.width
        return w is not None and abs(w - STRIKE_STEP) < 1e-9

    @property
    def tie_value(self) -> float | None:
        """The one value in the bracket that would be an exact tie: the upper bound."""
        return self.upper


def brackets(root: Path, series: str | None = None) -> list[DayBracket]:
    """Every day-bracket under ``root``, or only one series' when ``series`` is given.

    The filter narrows the *glob*, not the result: callers that want one series should
    not pay to read and parse the other twenty-two states' ladders. Filtering after the
    fact would give the same answer, which is what ``test_gas_reconstruct`` pins.
    """
    out: list[DayBracket] = []
    pattern = f"{series}/markets.parquet" if series else "*/markets.parquet"
    for path in sorted(Path(root).glob(pattern)):
        rows = pq.read_table(path).to_pylist()
        grouped: dict[tuple, list[dict]] = collections.defaultdict(list)
        for row in rows:
            if row["result"] not in ("yes", "no") or row["strike"] is None:
                continue
            target = parse_target_date(row["target_date"])
            if target is None:
                continue
            grouped[(row["series_ticker"], target)].append(row)
        for (series, target), items in grouped.items():
            parsed = parse_series(series) or ("unknown", "unknown")
            yes = [r["strike"] for r in items if r["result"] == "yes"]
            no = [r["strike"] for r in items if r["result"] == "no"]
            lower = max(yes) if yes else None
            upper = min(no) if no else None
            # A YES above a NO would mean the ladder disagrees with itself.
            bad = lower is not None and upper is not None and lower >= upper
            out.append(DayBracket(series, parsed[1], parsed[0], target,
                                  lower, upper, len(yes), len(no), bad))
    return sorted(out, key=lambda b: (b.state, b.target))


def schema() -> pa.Schema:
    return pa.schema([
        pa.field("series_ticker", pa.string()),
        pa.field("state", pa.string()),
        pa.field("frequency", pa.string()),
        pa.field("target_date", pa.string()),
        pa.field("lower_exclusive", pa.float64()),
        pa.field("upper_inclusive", pa.float64()),
        pa.field("width", pa.float64()),
        pa.field("pinned_to_one_step", pa.bool_()),
        pa.field("tie_value_if_exact", pa.float64()),
        pa.field("n_yes", pa.int32()),
        pa.field("n_no", pa.int32()),
        pa.field("inconsistent", pa.bool_()),
    ])


def rows(items: list[DayBracket]) -> list[dict]:
    return [{
        "series_ticker": b.series, "state": b.state, "frequency": b.frequency,
        "target_date": b.target.isoformat(),
        "lower_exclusive": b.lower, "upper_inclusive": b.upper, "width": b.width,
        "pinned_to_one_step": b.pinned, "tie_value_if_exact": b.tie_value,
        "n_yes": b.n_yes, "n_no": b.n_no, "inconsistent": b.inconsistent,
    } for b in items]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="sports_terminal.gas.reconstruct")
    parser.add_argument("--root", type=Path, default=Path("data_gas"))
    parser.add_argument("--out", type=Path, default=Path("data_gas/daily_values.parquet"))
    args = parser.parse_args(argv)

    items = brackets(args.root)
    daily = [b for b in items if b.frequency == "daily"]
    print(f"day-brackets reconstructed: {len(items)}  (daily {len(daily)}, "
          f"weekly {len(items) - len(daily)})")
    print(f"states covered: {len({b.state for b in items})}")
    dates = [b.target for b in items]
    print(f"date range: {min(dates)} .. {max(dates)}")

    bad = [b for b in items if b.inconsistent]
    print(f"\nladders where a YES sits above a NO: {len(bad)}")
    for b in bad[:5]:
        print(f"   {b.state} {b.target} lower={b.lower} upper={b.upper}")

    pinned = [b for b in items if b.pinned]
    open_top = [b for b in items if b.upper is None]
    open_bottom = [b for b in items if b.lower is None]
    print(f"\npinned to one strike step ({STRIKE_STEP}): {len(pinned)}/{len(items)} "
          f"({len(pinned)/len(items):.1%})")
    print(f"  value above the whole ladder (no NO):  {len(open_top)}")
    print(f"  value below the whole ladder (no YES): {len(open_bottom)}")
    widths = collections.Counter(b.width for b in items if b.width is not None)
    print(f"  bracket widths: {dict(sorted(widths.items())[:6])}")

    table = pa.Table.from_pylist(rows(items), schema=schema())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, args.out, compression="zstd")
    print(f"\nwrote {args.out}  ({args.out.stat().st_size:,} B)")

    print("\nper state (daily series only)")
    print(f"  {'state':<7}{'days':>6}{'pinned':>8}{'open top':>10}{'open bottom':>13}")
    by_state = collections.defaultdict(list)
    for b in daily:
        by_state[b.state].append(b)
    for state in sorted(by_state):
        group = by_state[state]
        print(f"  {state:<7}{len(group):>6}{sum(1 for b in group if b.pinned):>8}"
              f"{sum(1 for b in group if b.upper is None):>10}"
              f"{sum(1 for b in group if b.lower is None):>13}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
