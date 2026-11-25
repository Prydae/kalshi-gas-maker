# Public sample

Five evenings of the gas paper-maker's size-1 fills (2026-09-21 to 2026-09-25, Eastern
time) and the settled US strikes for the five days they trade into (2026-09-22 to 09-26).

- `paper/paper_fill/*.jsonl`: one row per simulated fill. Fields: market ticker, size cap,
  price, size, trade price and time, hour of fill (ET), queue position at placement, seconds
  resting, and a schema version. No account, key, or balance fields.
- `markets.csv`: the settled outcome of each US national strike (`yes` / `no`) for those days,
  from Kalshi's public market data.

Nothing here is a live fill. The fills are what the paper-maker would have done against the
recorded book, and the outcomes are Kalshi's public settlements. No P&L is stored: the
reproduction script computes it, in cents per contract.
